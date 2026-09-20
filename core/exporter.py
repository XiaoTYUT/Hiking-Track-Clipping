# -*- coding: utf-8 -*-
"""KML 导出器：以原始 XML 树为模板，按裁剪状态重写并另存新文件。

导出时自动完成三件事（手工裁剪最容易出错的地方）：
1. gx:coord 与 when 严格成对切片
2. 起点/终点标注按新轨迹首末点重建（坐标、海拔、时间描述）
3. Document 元数据重算：总里程/爬升/下降/起止时间/均速，步数与卡路里按里程等比缩放
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from pathlib import Path

from .cropper import CropState
from .parser import (
    KML_NS,
    GX_NS,
    KmlTrack,
    find_all_track_elems,
    find_document_extended,
    find_placemark_by_id,
    q,
)
from .stats import stats_for_ranges

TZ_CN = timezone(timedelta(hours=8))  # 两步路描述文本用北京时间


def _fmt_coord(v: float) -> str:
    return f"{v:.6f}"


def _set_data(ext: ET.Element, name: str, value: str) -> None:
    """更新 Document/ExtendedData 中名为 name 的 Data 值（不存在则跳过）。"""
    for data in ext.findall(q(KML_NS, "Data")):
        if data.get("name") == name:
            val = data.find(q(KML_NS, "value"))
            if val is not None:
                val.text = value
            return


def _point_description(lon: float, lat: float, ele: float, time_text: str) -> str:
    return (
        f"<div>经度：{lon:.6f}</div>"
        f"<div>纬度：{lat:.6f}</div>"
        f"<div>海拔：{ele:.6f}</div>"
        f"<div>时间：{time_text}</div>"
    )


def _update_endpoint_placemark(root: ET.Element, pid: str, point, tz=TZ_CN) -> None:
    """重建起点/终点标注点。"""
    pm = find_placemark_by_id(root, pid)
    if pm is None or point is None:
        return
    coords = pm.find(f"{q(KML_NS, 'Point')}/{q(KML_NS, 'coordinates')}")
    if coords is not None:
        coords.text = f"{_fmt_coord(point.lon)},{_fmt_coord(point.lat)},{_fmt_coord(point.ele)}"
    desc = pm.find(q(KML_NS, "description"))
    if desc is not None and point.time is not None:
        local = point.time.astimezone(tz)
        desc.text = _point_description(point.lon, point.lat, point.ele, local.strftime("%Y-%m-%d %H:%M:%S"))


def _parent_of(root: ET.Element, target: ET.Element) -> ET.Element | None:
    """在树中查找 target 元素的直接父节点。"""
    for el in root.iter():
        if target in list(el):
            return el
    return None


def export_kml(track: KmlTrack, state: CropState, out_path: str) -> dict:
    """按裁剪状态导出新 KML，返回 {'stats': SegStats, 'n_points': int}。"""
    if not state.ranges:
        raise ValueError("没有可保留的轨迹点")

    kept = state.indices()
    st = stats_for_ranges(track.points, state.ranges)

    tree = copy.deepcopy(track.tree)
    root = tree.getroot()

    gx_elems = find_all_track_elems(root)
    if gx_elems and track.is_gx_track:
        # --- 逐个原始 gx:Track 在各自点区间内切片 ---
        dead_placemarks = []
        for seg_i, gx_track in enumerate(gx_elems):
            s0, e0 = track.track_spans[seg_i]
            seg_kept = [i for i in kept if s0 <= i <= e0]

            coords = gx_track.findall(q(GX_NS, "coord"))
            whens = gx_track.findall(q(KML_NS, "when"))
            # 局部下标必须与 coord 元素下标一一对应（两步路导出的 coord 均非空）
            if len(coords) != e0 - s0 + 1:
                raise ValueError(
                    f"第 {seg_i+1} 段轨迹点数与坐标元素数不一致，无法安全切片")

            if not seg_kept:
                # 整段被裁空 → 标记移除其所在 Placemark
                pm = _parent_of(root, gx_track)
                if pm is not None:
                    dead_placemarks.append(pm)
                continue

            local = [i - s0 for i in seg_kept]
            for el in coords + whens:
                gx_track.remove(el)
            for li in local:
                gx_track.append(coords[li])
            for li in local:
                gx_track.append(whens[li])

            # 该段扩展数据按点切片
            ext = gx_track.find(q(KML_NS, "ExtendedData"))
            if ext is not None:
                for data in ext.findall(q(KML_NS, "Data")):
                    if data.get("name") == "GxTrackExtendedData":
                        val = data.find(q(KML_NS, "value"))
                        if val is not None:
                            joined = ";".join(track.points[i].extra for i in seg_kept
                                              if track.points[i].extra)
                            if track.extras_trailing_seps[seg_i]:
                                joined += ";"
                            val.text = joined
                        break

        # 移除被裁空的整段 Placemark
        for pm in dead_placemarks:
            parent = _parent_of(root, pm)
            if parent is not None:
                parent.remove(pm)
    else:
        # --- 普通 LineString：重写坐标串 ---
        ls_coords = root.find(f".//{q(KML_NS, 'LineString')}/{q(KML_NS, 'coordinates')}")
        if ls_coords is not None:
            ls_coords.text = "\n".join(
                f"{track.points[i].lon:.8f},{track.points[i].lat:.8f},{track.points[i].ele:.3f}"
                for i in kept
            ) + "\n"

    # --- 起点/终点标注重建（全局保留首末点） ---
    first, last = track.points[kept[0]], track.points[kept[-1]]
    _update_endpoint_placemark(root, "startPoint", first)
    _update_endpoint_placemark(root, "endPoint", last)

    # --- Document 元数据重算 ---
    doc_ext = find_document_extended(root)
    if doc_ext is not None and st.t_start is not None:
        ratio = st.distance_m / stats_for_ranges(track.points, [(0, track.n - 1)]).distance_m if track.n > 1 else 1.0
        _set_data(doc_ext, "Distance", str(st.distance_m))
        _set_data(doc_ext, "Mileage", str(st.distance_m))
        _set_data(doc_ext, "ElevationGain", str(st.gain_m))
        _set_data(doc_ext, "ElevationLoss", str(st.loss_m))
        _set_data(doc_ext, "BeginTime", str(round(st.t_start.timestamp() * 1000)))
        _set_data(doc_ext, "EndTime", str(round(st.t_end.timestamp() * 1000)))
        if st.duration_s > 0:
            _set_data(doc_ext, "SportAvgSpeed", str(st.speed_ms))
        # 步数/卡路里按里程等比缩放（近似值）
        for name in ("Step", "Calorie"):
            for data in doc_ext.findall(q(KML_NS, "Data")):
                if data.get("name") == name:
                    val = data.find(q(KML_NS, "value"))
                    if val is not None and val.text:
                        try:
                            old = float(val.text)
                            new = old * ratio
                            val.text = str(int(round(new))) if name == "Step" else str(round(new, 6))
                        except ValueError:
                            pass
                    break

    # --- 写文件 ---
    ET.indent(tree, space="\t")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    tree.write(out_path, encoding="UTF-8", xml_declaration=True)

    return {"stats": st, "n_points": len(kept)}
