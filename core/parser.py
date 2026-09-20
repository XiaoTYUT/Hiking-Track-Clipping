# -*- coding: utf-8 -*-
"""KML 解析器。

支持两种轨迹格式：
1. 两步路(2bulu)导出的 gx:Track 格式（带时间戳、带每点扩展数据）
2. 普通 LineString KML（无时间戳）

解析结果连同原始 XML 树一起保留，导出时以原始树为模板重写。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

KML_NS = "http://www.opengis.net/kml/2.2"
GX_NS = "http://www.google.com/kml/ext/2.2"

# 注册命名空间前缀，保证导出时 <kml xmlns=...> 和 <gx:xxx> 写法与原文件一致
ET.register_namespace("", KML_NS)
ET.register_namespace("gx", GX_NS)


def q(ns: str, tag: str) -> str:
    """返回 ElementTree 的带命名空间标签写法。"""
    return f"{{{ns}}}{tag}"


@dataclass
class TrackPoint:
    """单个轨迹点。"""
    lon: float
    lat: float
    ele: float
    time: datetime | None = None
    # 两步路 GxTrackExtendedData 中与该点配对的原始 "a,b" 串（原样保留，导出时按点切片回填）
    extra: str = ""


@dataclass
class KmlTrack:
    """一份 KML 的完整解析结果。"""
    points: list[TrackPoint] = field(default_factory=list)
    tree: ET.ElementTree | None = None          # 原始 XML 树（导出模板，永不修改）
    name: str = ""                              # 轨迹名称（Document/name）
    is_gx_track: bool = False                   # 是否为两步路 gx:Track 格式
    source_path: str = ""
    # 每个原始 gx:Track 对应的合并点下标闭区间（同一次徒步暂停/继续会产生多个 gx:Track）
    track_spans: list[tuple[int, int]] = field(default_factory=list)
    # 与 track_spans 对齐：各段 GxTrackExtendedData 原文是否以分号结尾
    extras_trailing_seps: list[bool] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.points)


def _parse_when(text: str) -> datetime | None:
    """解析 KML 时间戳，如 2026-05-10T02:06:11Z。"""
    text = text.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_gx_coord(text: str) -> TrackPoint | None:
    """解析 gx:coord 文本：'lon lat [ele]'。"""
    parts = text.split()
    if len(parts) < 2:
        return None
    lon = float(parts[0])
    lat = float(parts[1])
    ele = float(parts[2]) if len(parts) >= 3 else 0.0
    return TrackPoint(lon=lon, lat=lat, ele=ele)


def _parse_linestring_coord(text: str) -> TrackPoint | None:
    """解析 LineString coordinates 的一行：'lon,lat[,ele]'。"""
    parts = text.strip().split(",")
    if len(parts) < 2:
        return None
    try:
        lon = float(parts[0])
        lat = float(parts[1])
        ele = float(parts[2]) if len(parts) >= 3 else 0.0
    except ValueError:
        return None
    return TrackPoint(lon=lon, lat=lat, ele=ele)


def find_track_elem(root: ET.Element) -> ET.Element | None:
    """在树中定位第一个 gx:Track 元素。"""
    return root.find(f".//{q(GX_NS, 'Track')}")


def find_all_track_elems(root: ET.Element) -> list[ET.Element]:
    """定位全部 gx:Track 元素（暂停/继续的徒步会有多段）。"""
    return root.findall(f".//{q(GX_NS, 'Track')}")


def find_placemark_by_id(root: ET.Element, pid: str) -> ET.Element | None:
    """按 id 查找 Placemark（两步路的起点/终点标注用）。"""
    for pm in root.iter(q(KML_NS, "Placemark")):
        if pm.get("id") == pid:
            return pm
    return None


def find_document(root: ET.Element) -> ET.Element | None:
    return root.find(f".//{q(KML_NS, 'Document')}")


def find_document_extended(root: ET.Element) -> ET.Element | None:
    """Document 直接子级的 ExtendedData（存放总里程等元数据）。"""
    doc = find_document(root)
    if doc is None:
        return None
    return doc.find(q(KML_NS, "ExtendedData"))


def _parse_one_gx_track(gx_track: ET.Element) -> tuple[list[TrackPoint], bool]:
    """解析单个 gx:Track 元素 → (点列表, 扩展数据原文是否以分号结尾)。

    gx:Track 内元素排列为：ExtendedData → 全部 gx:coord → 全部 when。
    """
    coords = gx_track.findall(q(GX_NS, "coord"))
    whens = gx_track.findall(q(KML_NS, "when"))

    extras: list[str] = []
    trailing_sep = False
    ext = gx_track.find(q(KML_NS, "ExtendedData"))
    if ext is not None:
        for data in ext.findall(q(KML_NS, "Data")):
            if data.get("name") == "GxTrackExtendedData":
                val = data.find(q(KML_NS, "value"))
                if val is not None and val.text:
                    raw = val.text.strip()
                    trailing_sep = raw.endswith(";")
                    extras = [p for p in raw.split(";") if p.strip()]
                break

    pts: list[TrackPoint] = []
    for i, coord in enumerate(coords):
        pt = _parse_gx_coord(coord.text or "")
        if pt is None:
            continue
        if i < len(whens):
            pt.time = _parse_when(whens[i].text or "")
        if i < len(extras):
            pt.extra = extras[i]
        pts.append(pt)
    return pts, trailing_sep


def parse_kml(path: str) -> KmlTrack:
    """解析 KML 文件，返回 KmlTrack。"""
    tree = ET.parse(path)
    root = tree.getroot()
    track = KmlTrack(tree=tree, source_path=str(path))

    # 名称：Document/name，否则用文件名
    doc = find_document(root)
    if doc is not None:
        name_elem = doc.find(q(KML_NS, "name"))
        if name_elem is not None and name_elem.text:
            track.name = name_elem.text.strip()
    if not track.name:
        track.name = Path(path).stem

    gx_tracks = find_all_track_elems(root)
    if gx_tracks:
        track.is_gx_track = True
        for gx_elem in gx_tracks:
            seg_pts, trailing = _parse_one_gx_track(gx_elem)
            s0 = len(track.points)
            track.points.extend(seg_pts)
            track.track_spans.append((s0, len(track.points) - 1))
            track.extras_trailing_seps.append(trailing)
    else:
        # 普通 LineString KML（只取第一条轨迹）
        for ls in root.iter(q(KML_NS, "LineString")):
            coords_elem = ls.find(q(KML_NS, "coordinates"))
            if coords_elem is None or not coords_elem.text:
                continue
            for line in coords_elem.text.split():
                pt = _parse_linestring_coord(line)
                if pt is not None:
                    track.points.append(pt)
            break
        if track.points:
            track.track_spans = [(0, len(track.points) - 1)]

    return track
