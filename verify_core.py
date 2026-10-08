# -*- coding: utf-8 -*-
"""内核全链路验证：用 cat.kml 跑 解析 → 统计 → 裁剪 → 导出 → 回读校验。

cat.kml 特点：同一次徒步暂停/继续产生了 3 个 gx:Track（2478+95+147=2720 点）。
注意：cat.kml 含真实徒步数据，未上传至仓库。请将本地 cat.kml 放到脚本同目录后运行。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

KML = Path(__file__).parent / "cat.kml"
OUT_KEEP = Path(__file__).parent / "out_verify_keep.kml"
OUT_REMOVE = Path(__file__).parent / "out_verify_remove.kml"
OUT_DROP_SEG = Path(__file__).parent / "out_verify_drop_seg.kml"

# cat.kml 的两步路元数据基准
DIST_META, GAIN_META, LOSS_META = 13166.67, 915.29, 907.36
SPANS_META = [(0, 2477), (2478, 2572), (2573, 2719)]


def main():
    if not KML.exists():
        print(f"[SKIP] 未找到 {KML.name}（含真实徒步数据，未上传仓库）。")
        print("       请将本地 cat.kml 放到脚本同目录后重试。")
        return 0
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("[PASS] " if cond else "[FAIL] ") + msg)
        if not cond:
            ok = False

    from core.parser import (
        KML_NS,
        parse_kml,
        find_all_track_elems,
        find_document_extended,
        find_placemark_by_id,
        q,
    )
    from core.stats import cumulative_distances, stats_for_ranges
    from core.cropper import CropState
    from core.exporter import export_kml

    track = parse_kml(str(KML))
    print(f"轨迹: {track.name}  点数: {track.n}  gx:Track: {track.is_gx_track}")
    check(track.n == 2720, f"点数 2720 (实际 {track.n})")
    check(track.track_spans == SPANS_META, f"原始3段区间正确: {track.track_spans}")
    check(all(p.time for p in track.points), "所有点都有时间戳")
    times = [p.time for p in track.points]
    check(times == sorted(times), "时间戳单调递增")
    check(len(find_all_track_elems(track.tree.getroot())) == 3, "树中含 3 个 gx:Track 元素")
    extras = [p.extra for p in track.points if p.extra]
    print(f"带扩展数据的点: {len(extras)} / {track.n}")

    # ---- 统计验证：对照两步路元数据 ----
    full = stats_for_ranges(track.points, [(0, track.n - 1)])
    print(f"我方里程: {full.distance_m:.2f}m  元数据: {DIST_META}m")
    print(f"我方爬升: {full.gain_m:.2f}m  元数据: {GAIN_META}m   "
          f"下降: {full.loss_m:.2f}m  元数据: {LOSS_META}m")
    print(f"我方时长: {full.duration_s:.0f}s = {full.duration_s/3600:.2f}h")
    check(abs(full.distance_m - DIST_META) < DIST_META * 0.02, "里程与元数据误差 < 2%")
    check(abs(full.gain_m - GAIN_META) < GAIN_META * 0.03, "滤波后爬升与元数据误差 < 3%")
    check(abs(full.loss_m - LOSS_META) < LOSS_META * 0.03, "滤波后下降与元数据误差 < 3%")

    # ---- 扩展数据语义：与分段速度的相关性（历史结论：≈速度语义） ----
    cum = cumulative_distances(track.points)
    seg_d = [0.0] + [cum[i] - cum[i - 1] for i in range(1, track.n)]
    seg_v = [0.0] + [
        seg_d[i] / max(0.1, (times[i] - times[i - 1]).total_seconds())
        for i in range(1, track.n)
    ]

    def _f(s):
        try:
            return float(s.split(",")[0])
        except Exception:
            return 0.0

    ex = [_f(p.extra) for p in track.points]
    import statistics
    corr_v = statistics.correlation(ex[1:], seg_v[1:])
    # extras 语义在不同轨迹上不稳定（test.kml 0.64 / cat.kml 0.25），
    # 软件将其视为不透明数据原样切片，因此这里只做信息展示，不作为正确性判据
    print(f"extras 与分段速度相关: {corr_v:.3f} (仅信息展示，不参与判定)")

    # ---- 裁剪1：保留跨3段的 [2400, 2600]（201点） ----
    state = CropState(track.n)
    state.keep(2400, 2600)
    st = stats_for_ranges(track.points, state.ranges)
    print(f"保留[2400,2600]: 点数={st.n_points} 里程={st.distance_m:.1f}m 时长={st.duration_s:.0f}s")
    check(st.n_points == 201, "保留段点数 201")
    check(st.distance_m < full.distance_m, "保留段里程小于全程")

    export_kml(track, state, str(OUT_KEEP))
    re = parse_kml(str(OUT_KEEP))
    check(re.n == 201, f"导出回读点数 201 (实际 {re.n})")
    check(re.track_spans == [(0, 77), (78, 172), (173, 200)],
          f"导出仍为3段且各段点数正确: {re.track_spans}")
    p0, p1 = track.points[2400], track.points[2600]
    check(abs(re.points[0].lon - p0.lon) < 1e-9, "导出首点=原点[2400]")
    check(abs(re.points[-1].lon - p1.lon) < 1e-9, "导出末点=原点[2600]")
    check(re.points[0].time == p0.time and re.points[-1].time == p1.time, "首末点时间戳正确")
    got_extra = [p.extra for p in re.points if p.extra]
    check(len(got_extra) == 201, f"扩展数据同步切片 (实际 {len(got_extra)})")
    check(got_extra[0] == track.points[2400].extra, "扩展数据按点对应")
    check(len(find_all_track_elems(re.tree.getroot())) == 3, "导出文件仍含 3 个 gx:Track")

    # 起终点标注
    root = re.tree.getroot()
    sp_coord = (find_placemark_by_id(root, "startPoint")
                .find(f"{q(KML_NS, 'Point')}/{q(KML_NS, 'coordinates')}").text)
    check(sp_coord.startswith(f"{p0.lon:.6f},{p0.lat:.6f}"), f"起点标注已重建: {sp_coord}")
    ep_coord = (find_placemark_by_id(root, "endPoint")
                .find(f"{q(KML_NS, 'Point')}/{q(KML_NS, 'coordinates')}").text)
    check(ep_coord.startswith(f"{p1.lon:.6f},{p1.lat:.6f}"), "终点标注已重建")

    # 元数据
    ext = find_document_extended(root)
    meta = {}
    for d in ext.findall(q(KML_NS, "Data")):
        val = d.find(q(KML_NS, "value"))
        meta[d.get("name")] = (val.text or "") if val is not None else ""
    print(f"导出元数据 Distance={meta.get('Distance')} Step={meta.get('Step')} "
          f"Calorie={meta.get('Calorie')}")
    check(abs(float(meta["Distance"]) - st.distance_m) < 0.01, "元数据 Distance 已重算")
    check(int(meta["BeginTime"]) == round(p0.time.timestamp() * 1000), "BeginTime 已重算")
    check(int(meta["Step"]) < 20820, "步数按裁剪比例缩小")

    # ---- 裁剪2：删中段 [1200, 1500]（全在track0内，拆两段） ----
    state2 = CropState(track.n)
    state2.remove(1200, 1500)
    st2 = stats_for_ranges(track.points, state2.ranges)
    print(f"删除[1200,1500]: 区间={state2.ranges} 点数={st2.n_points}")
    check(state2.ranges == [(0, 1199), (1501, 2719)], "删除中段后区间正确")
    check(st2.n_points == track.n - 301, "删除段点数正确")
    export_kml(track, state2, str(OUT_REMOVE))
    re2 = parse_kml(str(OUT_REMOVE))
    check(re2.n == track.n - 301, "中段删除导出回读正确")
    check(len(find_all_track_elems(re2.tree.getroot())) == 3, "删中段不影响原始3个 gx:Track")

    # ---- 裁剪3：只保留 track0 内的段 → track1/track2 的 Placemark 应整体移除 ----
    state3 = CropState(track.n)
    state3.keep(100, 1000)
    export_kml(track, state3, str(OUT_DROP_SEG))
    re3 = parse_kml(str(OUT_DROP_SEG))
    check(re3.n == 901, f"整段移除场景点数 901 (实际 {re3.n})")
    check(re3.track_spans == [(0, 900)], "导出只剩 1 个段")
    check(len(find_all_track_elems(re3.tree.getroot())) == 1,
          "被裁空的 track1/track2 所在 Placemark 已整体移除")

    # ---- 撤销/重做 ----
    state2.undo()
    check(state2.ranges == [(0, track.n - 1)], "撤销恢复全程")
    state2.redo()
    check(state2.ranges == [(0, 1199), (1501, 2719)], "重做恢复两段")
    state2.remove(0, 100)  # 继续裁剪
    check(state2.ranges == [(101, 1199), (1501, 2719)], f"叠加裁剪正确: {state2.ranges}")
    state2.undo()
    state2.undo()
    check(state2.ranges == [(0, track.n - 1)], "连续撤销回全程")

    print()
    print("全部通过 ✔" if ok else "存在失败项 ✘")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
