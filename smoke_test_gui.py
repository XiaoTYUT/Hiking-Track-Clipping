# -*- coding: utf-8 -*-
"""GUI 冒烟测试：不进 mainloop，构建窗口 → 加载 → 缩放 → 模拟操作 → 删除时间段校验 → 导出。"""
import sys
from pathlib import Path

import tkinter as tk

sys.path.insert(0, str(Path(__file__).parent))
from main import CropApp
from core.parser import parse_kml
from core.exporter import export_kml

ROOT = Path(__file__).parent


def shot(app, name):
    app.canvas.draw()
    app.root.update()
    app.fig.savefig(ROOT / name, dpi=100)
    print(f"已保存截图 {name}")


def main():
    root = tk.Tk()
    root.geometry("1280x820+60+40")
    app = CropApp(root, initial_file=str(ROOT / "cat.kml"))
    app.canvas.draw()
    root.update()
    print(f"加载: {app.track.name}  点数={app.track.n}")
    assert app.track.n == 2720
    # 初始无删除：0 个删除段，2 个原始暂停
    init_types = [app.del_tree.item(i)["values"][0]
                  for i in app.del_tree.get_children()]
    assert init_types.count("删除") == 0, f"初始不应有删除段: {init_types}"
    assert init_types.count("原始暂停") == 2, f"初始应显示2个原始暂停: {init_types}"
    shot(app, "shot_1_loaded.png")

    # 保存全景范围（等比例约束在首次绘制后已最终确定）
    full_view = (app.ax_track.get_xlim(), app.ax_track.get_ylim())

    # ---- 模拟滚轮放大（构造一个伪造的 Tk 事件对象） ----
    class FakeWheel:
        def __init__(self, x, y, delta):
            self.x, self.y, self.delta = x, y, delta

    # 在轨迹图中心放大 5 次
    cx, cy = app.ax_track.bbox.x0 + app.ax_track.bbox.width / 2, app.ax_track.bbox.y0 + app.ax_track.bbox.height / 2
    w_before = app.ax_track.get_xlim()[1] - app.ax_track.get_xlim()[0]
    for _ in range(5):
        app._on_wheel(FakeWheel(cx, cy, 120))
    w_after = app.ax_track.get_xlim()[1] - app.ax_track.get_xlim()[0]
    print(f"滚轮放大: 视图宽 {w_before:.0f}m -> {w_after:.0f}m (缩小={w_after < w_before})")
    assert w_after < w_before, "放大后视野应变窄"
    shot(app, "shot_2_zoomed.png")

    # ---- 滚轮缩小 5 次，验证可以缩小回来 ----
    w_zoomed = app.ax_track.get_xlim()[1] - app.ax_track.get_xlim()[0]
    for _ in range(5):
        app._on_wheel(FakeWheel(cx, cy, -120))
    w_out = app.ax_track.get_xlim()[1] - app.ax_track.get_xlim()[0]
    print(f"滚轮缩小: 视图宽 {w_zoomed:.0f}m -> {w_out:.0f}m (扩大={w_out > w_zoomed})")
    assert w_out > w_zoomed, f"缩小后视野应变宽 (实际 {w_zoomed:.0f}->{w_out:.0f})"

    # ---- Home 键复位到全景 ----
    app._reset_track_view()
    w_home = app.ax_track.get_xlim()[1] - app.ax_track.get_xlim()[0]
    print(f"Home 复位: 视图宽 {w_home:.0f}m (全景={w_home:.0f}m)")
    assert abs(w_home - w_before) < 1, f"Home 应复位到全景 (实际 {w_home:.0f}, 全景 {w_before:.0f})"

    # 回到全景
    app.ax_track.set_xlim(*full_view[0])
    app.ax_track.set_ylim(*full_view[1])

    # ---- 手柄选段 [2400, 2600]，只保留选区 ----
    app.a_idx, app.b_idx = 2400, 2600
    app._update_selection(refresh_all=True)
    root.update()
    print(f"A时间={app.sel_vars['t0'].get()} B时间={app.sel_vars['t1'].get()} "
          f"里程={app.sel_vars['dist'].get()}")
    shot(app, "shot_3_selected.png")

    app.on_keep()
    root.update()
    assert app.crop.ranges == [(2400, 2600)], f"保留失败: {app.crop.ranges}"
    # 删除段应为 2（头、尾）；选区横跨3个原始分段，2 个原始暂停仍显示
    types = [app.del_tree.item(i)["values"][0]
             for i in app.del_tree.get_children()]
    print(f"删除列表类型: {types}")
    assert types.count("删除") == 2, f"应有2个删除段: {types}"
    assert types.count("原始暂停") == 2, f"边界点都在，应保留2个原始暂停: {types}"
    n_del, t_del = app.deleted_summary()
    print(f"删除汇总: {n_del} 段, 合计 {t_del:.0f}s")
    assert n_del == 2 and t_del > 0
    shot(app, "shot_4_kept.png")

    # ---- 撤销：回到全程，删除列表清空，原始暂停恢复 ----
    app.on_undo()
    root.update()
    assert app.crop.ranges == [(0, 2719)]
    types = [app.del_tree.item(i)["values"][0]
             for i in app.del_tree.get_children()]
    print(f"撤销后列表类型: {types}")
    assert types.count("删除") == 0
    assert types.count("原始暂停") == 2, f"全程应显示2个原始暂停: {types}"

    # ---- 导出回读 ----
    app.a_idx, app.b_idx = 2400, 2600
    app.on_keep()
    out = str(ROOT / "out_gui_test.kml")
    r = export_kml(app.track, app.crop, out)
    re = parse_kml(out)
    assert re.n == 201, f"导出点数错误: {re.n}"
    print(f"导出 {Path(out).name}: {re.n} 点, 里程 {r['stats'].distance_m/1000:.2f} km")
    shot(app, "shot_5_final.png")

    root.destroy()
    print("GUI 冒烟测试全部通过 ✔")


if __name__ == "__main__":
    main()
