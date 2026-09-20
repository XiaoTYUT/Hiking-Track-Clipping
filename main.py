# -*- coding: utf-8 -*-
"""徒步轨迹裁剪器 —— 桌面 GUI 入口。

用法：
    C:\\Users\\xiao\\miniconda3\\envs\\test_skill\\python.exe main.py [轨迹.kml]

界面：上方轨迹平面图（等比例）+ 下方海拔剖面图，两图联动选段。
A/B 两个手柄：拖动剖面图上的竖线，或直接点击轨迹图/剖面图移动最近的手柄。
"""
from __future__ import annotations

import bisect
import math
import sys
import tkinter as tk
from datetime import timedelta, timezone
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt  # noqa: E402  (只为设置字体)
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Polygon  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

# 中文字体
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

sys.path.insert(0, str(Path(__file__).parent))
from core.cropper import CropState          # noqa: E402
from core.exporter import export_kml        # noqa: E402
from core.parser import parse_kml           # noqa: E402
from core.stats import cumulative_distances, stats_for_ranges  # noqa: E402

C_ORIG = "#b8b8b8"   # 原始轨迹（灰）
C_KEEP = "#1f77b4"   # 当前保留（蓝）
C_SEL = "#ff9a2e"    # 选区（橙）
C_A = "#d62728"      # 手柄A（红）
C_B = "#7b3fbf"      # 手柄B（紫）
C_START = "#2ca02c"  # 当前起点（绿）
C_END = "#d62728"    # 当前终点（红）

HANDLE_HIT_PX = 10   # 手柄命中判定半径（像素）

TZ_CN = timezone(timedelta(hours=8))  # 界面时间一律显示北京时间


def fmt_dur(seconds: float) -> str:
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}"


def fmt_clock(t) -> str:
    """带时区的时间 → 'HH:MM:SS'（北京时间）。"""
    return t.astimezone(TZ_CN).strftime("%H:%M:%S")


class CropApp:
    def __init__(self, root: tk.Tk, initial_file: str | None = None):
        self.root = root
        root.title("徒步轨迹裁剪器")
        root.geometry("1280x820")
        root.minsize(960, 600)

        # ---------- 数据状态 ----------
        self.track = None          # KmlTrack
        self.crop: CropState | None = None
        self.cum: list[float] = []  # 原始累计里程
        self.xs: list[float] = []   # 轨迹图投影坐标（米）
        self.ys: list[float] = []
        self.a_idx = 0
        self.b_idx = 0
        self._drag: str | None = None
        self._ele_lim: tuple[float, float] = (0.0, 1.0)

        self._build_ui()
        self._bind_events()

        if initial_file:
            self.load_file(initial_file)
        else:
            self._set_status("请打开一个 KML 轨迹文件")

    # ================= 界面搭建 =================
    def _build_ui(self):
        # 顶部工具栏
        bar = ttk.Frame(self.root)
        bar.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)
        self.btn_open = ttk.Button(bar, text="打开 KML…", command=self.on_open)
        self.btn_open.pack(side=tk.LEFT)
        self.btn_undo = ttk.Button(bar, text="撤销", command=self.on_undo, state=tk.DISABLED, width=8)
        self.btn_undo.pack(side=tk.LEFT, padx=(16, 0))
        self.btn_redo = ttk.Button(bar, text="重做", command=self.on_redo, state=tk.DISABLED, width=8)
        self.btn_redo.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_export = ttk.Button(bar, text="导出 KML…", command=self.on_export, state=tk.DISABLED)
        self.btn_export.pack(side=tk.RIGHT)

        # 中部：左图右面板
        mid = ttk.Frame(self.root)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # --- matplotlib 图（上：轨迹平面图 / 下：海拔剖面） ---
        self.fig = Figure(figsize=(8, 6), dpi=100)
        gs = self.fig.add_gridspec(2, 1, height_ratios=[3, 2], hspace=0.16)
        self.ax_track = self.fig.add_subplot(gs[0])
        self.ax_prof = self.fig.add_subplot(gs[1])
        self.ax_track.axis("off")
        self.ax_track.set_title("轨迹平面图：滚轮缩放（以光标为中心），工具栏可框选放大/平移，点击或拖圆点手柄选段", fontsize=10)
        self.ax_prof.set_title("海拔剖面：滚轮缩放精细选段，拖动 A/B 竖线", fontsize=10)
        self.ax_prof.set_xlabel("累计里程 (km)")
        self.ax_prof.set_ylabel("海拔 (m)")
        self.ax_prof.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v/1000:g}"))

        self.canvas = FigureCanvasTkAgg(self.fig, master=mid)
        self.canvas.get_tk_widget().pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.toolbar = NavigationToolbar2Tk(self.canvas, mid)
        self.toolbar.update()
        self.canvas.get_tk_widget().pack(side=tk.LEFT, fill=tk.BOTH, expand=True)  # toolbar 会 pack_bottom

        # --- 右侧面板 ---
        side = ttk.Frame(mid, width=260)
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 8), pady=4)
        side.pack_propagate(False)

        # 选区统计
        f_sel = ttk.LabelFrame(side, text="选区统计（A → B）")
        f_sel.pack(fill=tk.X, pady=(0, 8))
        self.sel_vars = {}
        for label, key in [("A时间", "t0"), ("B时间", "t1"),
                           ("里程", "dist"), ("时长", "dur"), ("爬升", "gain"), ("点数", "pts")]:
            row = ttk.Frame(f_sel)
            row.pack(fill=tk.X, padx=8, pady=1)
            ttk.Label(row, text=label, width=6).pack(side=tk.LEFT)
            var = tk.StringVar(value="—")
            ttk.Label(row, textvariable=var).pack(side=tk.RIGHT)
            self.sel_vars[key] = var

        # 裁剪操作
        f_act = ttk.LabelFrame(side, text="裁剪操作")
        f_act.pack(fill=tk.X, pady=(0, 8))
        self.btn_keep = ttk.Button(f_act, text="只保留 A→B 选区", command=self.on_keep, state=tk.DISABLED)
        self.btn_keep.pack(fill=tk.X, padx=8, pady=(6, 2))
        self.btn_remove = ttk.Button(f_act, text="删除 A→B 选区", command=self.on_remove, state=tk.DISABLED)
        self.btn_remove.pack(fill=tk.X, padx=8, pady=2)
        self.btn_reset = ttk.Button(f_act, text="手柄复位到保留段两端", command=self.on_reset_handles, state=tk.DISABLED)
        self.btn_reset.pack(fill=tk.X, padx=8, pady=(2, 6))

        # 当前轨迹统计
        f_stat = ttk.LabelFrame(side, text="当前轨迹统计（保留部分）")
        f_stat.pack(fill=tk.X, pady=(0, 8))
        self.stat_vars = {}
        for label, key in [("里程", "dist"), ("时长", "dur"), ("爬升", "gain"), ("下降", "loss"), ("点数", "pts")]:
            row = ttk.Frame(f_stat)
            row.pack(fill=tk.X, padx=8, pady=1)
            ttk.Label(row, text=label, width=6).pack(side=tk.LEFT)
            var = tk.StringVar(value="—")
            ttk.Label(row, textvariable=var).pack(side=tk.RIGHT)
            self.stat_vars[key] = var

        # 已删除时间段
        f_del = ttk.LabelFrame(side, text="已删除时间段（北京时间）")
        f_del.pack(fill=tk.X, pady=(0, 8))
        cols = ("type", "start", "end", "dur")
        self.del_tree = ttk.Treeview(f_del, columns=cols, show="headings", height=7)
        for c, txt, w in (("type", "类型", 52), ("start", "开始", 68),
                          ("end", "结束", 68), ("dur", "时长", 54)):
            self.del_tree.heading(c, text=txt)
            self.del_tree.column(c, width=w, anchor="center", stretch=False)
        self.del_tree.pack(side=tk.LEFT, fill=tk.X, padx=(8, 0), pady=6)
        sb = ttk.Scrollbar(f_del, orient="vertical", command=self.del_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y, pady=6, padx=(0, 4))
        self.del_tree.configure(yscrollcommand=sb.set)

        # 文件信息
        f_info = ttk.LabelFrame(side, text="文件")
        f_info.pack(fill=tk.X)
        self.info_var = tk.StringVar(value="未打开文件")
        ttk.Label(f_info, textvariable=self.info_var, wraplength=230, justify=tk.LEFT).pack(
            fill=tk.X, padx=8, pady=6, anchor="w")

        # 底部状态栏
        self.status_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor="w").pack(
            side=tk.BOTTOM, fill=tk.X)

        # 快捷键
        self.root.bind("<Control-z>", lambda e: self.on_undo())
        self.root.bind("<Control-y>", lambda e: self.on_redo())

    def _bind_events(self):
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        # 鼠标滚轮：以光标为中心缩放，用于精细化裁剪
        self.canvas.get_tk_widget().bind("<MouseWheel>", self._on_wheel)

    # ================= 数据加载 =================
    def load_file(self, path: str):
        try:
            track = parse_kml(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("打开失败", f"无法解析 KML 文件：\n{e}")
            return
        if track.n < 2:
            messagebox.showerror("打开失败", "轨迹点数不足（至少需要 2 个点）")
            return
        self.track = track
        self.crop = CropState(track.n)
        self.cum = cumulative_distances(track.points)
        # 等面积投影到米（保证平面图形状不失真）
        lat0 = sum(p.lat for p in track.points) / track.n
        coslat = math.cos(math.radians(lat0))
        self.xs = [(p.lon - track.points[0].lon) * 111320 * coslat for p in track.points]
        self.ys = [(p.lat - track.points[0].lat) * 111320 for p in track.points]
        self.a_idx, self.b_idx = 0, track.n - 1
        self.info_var.set(f"{Path(path).name}\n{track.name}\n共 {track.n} 个轨迹点")
        self._rebuild()
        self._set_status(f"已打开 {Path(path).name}：{track.n} 点，拖动手柄选择要保留的路段")

    def on_open(self):
        path = filedialog.askopenfilename(
            title="打开 KML 轨迹",
            filetypes=[("KML 文件", "*.kml"), ("所有文件", "*.*")],
            parent=self.root)
        if path:
            self.load_file(path)

    # ================= 绘制 =================
    def _rebuild(self):
        """打开/裁剪/撤销后整体重绘。"""
        ax_t, ax_p = self.ax_track, self.ax_prof
        ax_t.clear()
        ax_p.clear()
        ax_t.axis("off")
        ax_t.set_title("轨迹平面图：滚轮缩放（以光标为中心），工具栏可框选放大/平移，点击或拖圆点手柄选段", fontsize=10)
        ax_p.set_xlabel("累计里程 (km)")
        ax_p.set_ylabel("海拔 (m)")
        ax_p.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v/1000:g}"))

        pts = self.track.points
        cum, xs, ys = self.cum, self.xs, self.ys
        eles = [p.ele for p in pts]

        # 原始全程（灰）
        ax_t.plot(xs, ys, color=C_ORIG, lw=1.2, zorder=1)
        ax_p.plot(cum, eles, color=C_ORIG, lw=1.0, zorder=1)

        # 当前保留段（蓝）
        for s, e in self.crop.ranges:
            ax_t.plot(xs[s:e + 1], ys[s:e + 1], color=C_KEEP, lw=1.8, zorder=2)
            ax_p.plot(cum[s:e + 1], eles[s:e + 1], color=C_KEEP, lw=1.8, zorder=2)

        # 当前起点/终点标记
        s0 = self.crop.ranges[0][0]
        e0 = self.crop.ranges[-1][1]
        ax_t.plot([xs[s0]], [ys[s0]], marker="^", ms=11, color=C_START, mec="white", zorder=5)
        ax_t.plot([xs[e0]], [ys[e0]], marker="v", ms=11, color=C_END, mec="white", zorder=5)
        ax_t.annotate("起点", (xs[s0], ys[s0]), textcoords="offset points", xytext=(6, 6),
                      fontsize=9, color=C_START)
        ax_t.annotate("终点", (xs[e0], ys[e0]), textcoords="offset points", xytext=(6, -12),
                      fontsize=9, color=C_END)

        # 剖面图纵轴范围（固定，便于手柄竖线定位）
        lo, hi = min(eles), max(eles)
        pad = max((hi - lo) * 0.1, 5)
        self._ele_lim = (lo - pad, hi + pad)
        ax_p.set_ylim(*self._ele_lim)
        ax_p.set_xlim(0, cum[-1] * 1.01)

        # 选区（橙）+ 手柄 —— 引用保存在实例上，拖动时原地更新
        self.sel_t, = ax_t.plot([], [], color=C_SEL, lw=3.5, alpha=0.9, zorder=3)
        self.sel_p, = ax_p.plot([], [], color=C_SEL, lw=3.0, alpha=0.9, zorder=3)
        ylo, yhi = self._ele_lim
        self.span_p = Polygon([[0, ylo], [0, yhi], [0, yhi], [0, ylo]], closed=True,
                              color=C_SEL, alpha=0.13, zorder=0)
        ax_p.add_patch(self.span_p)
        self.hline_a, = ax_p.plot([0, 0], [ylo, yhi], color=C_A, lw=1.8, zorder=6)
        self.hline_b, = ax_p.plot([0, 0], [ylo, yhi], color=C_B, lw=1.8, zorder=6)
        self.mk_a, = ax_t.plot([], [], marker="o", ms=12, mfc="white", mec=C_A, mew=2.5, zorder=6)
        self.mk_b, = ax_t.plot([], [], marker="o", ms=12, mfc="white", mec=C_B, mew=2.5, zorder=6)
        self.txt_a = ax_t.annotate("A", (0, 0), fontsize=10, fontweight="bold", color=C_A, zorder=7)
        self.txt_b = ax_t.annotate("B", (0, 0), fontsize=10, fontweight="bold", color=C_B, zorder=7)
        self.lbl_a = ax_p.annotate("A", (0, 0), fontsize=9, fontweight="bold", color=C_A,
                                   xytext=(4, 4), textcoords="offset points", zorder=7)
        self.lbl_b = ax_p.annotate("B", (0, 0), fontsize=9, fontweight="bold", color=C_B,
                                   xytext=(4, 4), textcoords="offset points", zorder=7)

        # 轨迹图等比例
        ax_t.set_aspect("equal", adjustable="datalim")

        self._update_selection(refresh_all=True)
        self._refresh_stats()
        self._refresh_deleted()
        self._refresh_buttons()
        self.canvas.draw_idle()

    def _update_selection(self, refresh_all: bool = False):
        """拖动手柄时更新选区艺术对象与选区统计。"""
        if self.track is None:
            return
        a, b = sorted((self.a_idx, self.b_idx))
        xs, ys, cum = self.xs, self.ys, self.cum
        eles = [p.ele for p in self.track.points]
        self.sel_t.set_data(xs[a:b + 1], ys[a:b + 1])
        self.sel_p.set_data(cum[a:b + 1], eles[a:b + 1])
        self.span_p.set_visible(True)
        self.span_p.set_xy([[cum[a], self._ele_lim[0]],
                            [cum[a], self._ele_lim[1]],
                            [cum[b], self._ele_lim[1]],
                            [cum[b], self._ele_lim[0]]])
        ylo, yhi = self._ele_lim
        self.hline_a.set_data([cum[self.a_idx]] * 2, [ylo, yhi])
        self.hline_b.set_data([cum[self.b_idx]] * 2, [ylo, yhi])
        self.mk_a.set_data([xs[self.a_idx]], [ys[self.a_idx]])
        self.mk_b.set_data([xs[self.b_idx]], [ys[self.b_idx]])
        for txt, idx in ((self.txt_a, self.a_idx), (self.txt_b, self.b_idx)):
            txt.set_position((xs[idx] + 8, ys[idx] + 8))
        self.lbl_a.set_position((cum[self.a_idx], yhi - (yhi - ylo) * 0.04))
        self.lbl_b.set_position((cum[self.b_idx], yhi - (yhi - ylo) * 0.04))

        # 选区统计
        st = stats_for_ranges(self.track.points, [(a, b)])
        pa, pb = self.track.points[self.a_idx], self.track.points[self.b_idx]
        self.sel_vars["t0"].set(fmt_clock(pa.time) if pa.time else "—")
        self.sel_vars["t1"].set(fmt_clock(pb.time) if pb.time else "—")
        self.sel_vars["dist"].set(f"{st.distance_m/1000:.2f} km")
        self.sel_vars["dur"].set(fmt_dur(st.duration_s) if st.duration_s else "—")
        self.sel_vars["gain"].set(f"{st.gain_m:.0f} m" if st.gain_m else "—")
        self.sel_vars["pts"].set(f"{st.n_points} / {self.track.n}")
        if refresh_all:
            self._refresh_buttons()
        self.canvas.draw_idle()

    def _refresh_stats(self):
        st = stats_for_ranges(self.track.points, self.crop.ranges)
        self.stat_vars["dist"].set(f"{st.distance_m/1000:.2f} km")
        self.stat_vars["dur"].set(fmt_dur(st.duration_s) if st.duration_s else "—")
        self.stat_vars["gain"].set(f"+{st.gain_m:.0f} m")
        self.stat_vars["loss"].set(f"-{st.loss_m:.0f} m")
        self.stat_vars["pts"].set(f"{st.n_points} / {self.track.n}")

    # ================= 已删除时间段 =================
    def deleted_rows(self) -> list[tuple[object, tuple]]:
        """计算当前删除情况，返回 [(排序用起始时间, (类型,开始,结束,时长)), ...]。

        包含两类：
        - 删除：被裁掉的点区间，时间取该区间首末点
        - 原始暂停：原始多段 gx:Track 之间的时间空隙（仅当边界点都还在时列出）
        """
        pts = self.track.points
        kept_set = set(self.crop.indices())
        rows: list[tuple[object, tuple]] = []

        def add_removed(i: int, j: int):
            if pts[i].time is None:
                return
            rows.append((pts[i].time, (
                "删除",
                fmt_clock(pts[i].time),
                fmt_clock(pts[j].time),
                fmt_dur((pts[j].time - pts[i].time).total_seconds()),
            )))

        # 保留下标区间相对于原始全程 [0,n-1] 的补集
        cur = 0
        for s, e in sorted(self.crop.ranges):
            if cur < s:
                add_removed(cur, s - 1)
            cur = max(cur, e + 1)
        if cur < self.track.n:
            add_removed(cur, self.track.n - 1)

        # 原始暂停空隙：track_spans 相邻边界 b 与 b+1
        for _, b in self.track.track_spans[:-1]:
            if b in kept_set and b + 1 in kept_set and pts[b].time:
                gap = (pts[b + 1].time - pts[b].time).total_seconds()
                rows.append((pts[b].time, (
                    "原始暂停",
                    fmt_clock(pts[b].time),
                    fmt_clock(pts[b + 1].time),
                    fmt_dur(gap),
                )))

        rows.sort(key=lambda r: r[0])
        return rows

    def _refresh_deleted(self):
        for item in self.del_tree.get_children():
            self.del_tree.delete(item)
        for _, values in self.deleted_rows():
            self.del_tree.insert("", "end", values=values)

    def deleted_summary(self) -> tuple[int, float]:
        """返回 (删除段数, 删除总秒数)，不含原始暂停。"""
        count = total = 0
        for _, values in self.deleted_rows():
            if values[0] == "删除":
                count += 1
                # 'H:MM:SS' 反解析
                h, m, s = (int(x) for x in values[3].split(":"))
                total += h * 3600 + m * 60 + s
        return count, total

    def _refresh_buttons(self):
        has = self.track is not None
        sel_ok = has and abs(self.b_idx - self.a_idx) >= 1
        self.btn_undo.config(state=tk.NORMAL if has and self.crop.can_undo else tk.DISABLED)
        self.btn_redo.config(state=tk.NORMAL if has and self.crop.can_redo else tk.DISABLED)
        self.btn_export.config(state=tk.NORMAL if has and self.crop.ranges else tk.DISABLED)
        self.btn_keep.config(state=tk.NORMAL if sel_ok else tk.DISABLED)
        self.btn_remove.config(state=tk.NORMAL if sel_ok and len(self.crop.ranges) else tk.DISABLED)
        self.btn_reset.config(state=tk.NORMAL if has else tk.DISABLED)

    def _set_status(self, msg: str):
        self.status_var.set(msg)

    # ================= 交互：手柄拖动 =================
    def _handle_at(self, event):
        """判断事件是否命中某个手柄（任一图内），返回 'a'/'b'/None。"""
        if self.track is None:
            return None
        for name, idx in (("a", self.a_idx), ("b", self.b_idx)):
            if self._in_axes(event):
                dx_t = abs(event.x - self.ax_track.transData.transform((self.xs[idx], self.ys[idx]))[0])
                dy_t = abs(event.y - self.ax_track.transData.transform((self.xs[idx], self.ys[idx]))[1])
                if math.hypot(dx_t, dy_t) <= HANDLE_HIT_PX * 1.5:
                    return name
                x_p = self.ax_prof.transData.transform((self.cum[idx], 0))[0]
                if abs(event.x - x_p) <= HANDLE_HIT_PX:
                    return name
        return None

    def _in_axes(self, event):
        return event.inaxes in (self.ax_track, self.ax_prof)

    def _nearest_index(self, event) -> int | None:
        """把事件位置映射到最近的轨迹点下标。放大后只在可见范围内找点。"""
        n = self.track.n
        if event.inaxes is self.ax_prof and event.xdata is not None:
            i = bisect.bisect_left(self.cum, event.xdata)
            i = max(1, min(n - 1, i))
            return i - 1 if abs(self.cum[i - 1] - event.xdata) <= abs(self.cum[i] - event.xdata) else i
        if event.inaxes is self.ax_track and event.xdata is not None and event.ydata is not None:
            xmin, xmax = self.ax_track.get_xlim()
            ymin, ymax = self.ax_track.get_ylim()
            best, best_d = None, float("inf")
            for i, (x, y) in enumerate(zip(self.xs, self.ys)):
                # 先粗筛可见范围，放大到局部时既快又不会被视野外的点"抢走"手柄
                if xmin <= x <= xmax and ymin <= y <= ymax:
                    d = (x - event.xdata) ** 2 + (y - event.ydata) ** 2
                    if d < best_d:
                        best, best_d = i, d
            if best is not None:
                return best
            # 视野内没有点（例如在空白处点击）→ 全量扫描兜底
            for i, (x, y) in enumerate(zip(self.xs, self.ys)):
                d = (x - event.xdata) ** 2 + (y - event.ydata) ** 2
                if d < best_d:
                    best, best_d = i, d
            return best
        return None

    def _on_press(self, event):
        if not self._in_axes(event) or event.button != 1:
            return
        hit = self._handle_at(event)
        if hit:
            self._drag = hit
            self.canvas.get_tk_widget().config(cursor="sb_h_double_arrow")
            return
        # 空白处点击：把最近的手柄移到点击处
        idx = self._nearest_index(event)
        if idx is None:
            return
        idx = max(0, min(self.track.n - 1, idx))
        if abs(idx - self.a_idx) <= abs(idx - self.b_idx):
            self.a_idx = idx
        else:
            self.b_idx = idx
        self._update_selection(refresh_all=True)

    def _on_motion(self, event):
        if self._drag is None or not self._in_axes(event) or self.track is None:
            return
        idx = self._nearest_index(event)
        if idx is None:
            return
        idx = max(0, min(self.track.n - 1, idx))
        if self._drag == "a":
            self.a_idx = idx
        else:
            self.b_idx = idx
        self._update_selection()

    def _on_release(self, event):
        if self._drag is not None:
            self._drag = None
            self.canvas.get_tk_widget().config(cursor="")
            self._refresh_buttons()

    def _on_wheel(self, event):
        """滚轮缩放：以光标位置为中心，光标在哪个子图就缩放哪个子图。"""
        if self.track is None:
            return
        factor = 1.25 ** (event.delta / 120)  # 向上滚 → factor>1 放大
        for ax in (self.ax_track, self.ax_prof):
            if not ax.bbox.contains(event.x, event.y):
                continue
            x, y = ax.transData.inverted().transform((event.x, event.y))
            xmin, xmax = ax.get_xlim()
            ymin, ymax = ax.get_ylim()
            ax.set_xlim(x - (x - xmin) / factor, x + (xmax - x) / factor)
            ax.set_ylim(y - (y - ymin) / factor, y + (ymax - y) / factor)
            self.canvas.draw_idle()
            break

    # ================= 操作 =================
    def on_keep(self):
        a, b = sorted((self.a_idx, self.b_idx))
        if a == b:
            return
        self.crop.keep(a, b)
        n_del, t_del = self.deleted_summary()
        self._after_crop(
            f"已裁剪：保留 {fmt_clock(self.track.points[a].time)}→"
            f"{fmt_clock(self.track.points[b].time)}；共删除 {n_del} 段、合计 {fmt_dur(t_del)}（Ctrl+Z 撤销）")

    def on_remove(self):
        a, b = sorted((self.a_idx, self.b_idx))
        if a == b:
            return
        self.crop.remove(a, b)
        n_del, t_del = self.deleted_summary()
        self._after_crop(
            f"已删除选区 {fmt_clock(self.track.points[a].time)}→"
            f"{fmt_clock(self.track.points[b].time)}；累计删除 {n_del} 段、合计 {fmt_dur(t_del)}（可撤销）")

    def _after_crop(self, msg: str):
        if not self.crop.ranges:
            messagebox.showwarning("提示", "没有可保留的轨迹点了，已自动撤销")
            self.crop.undo()
        self._rebuild()
        self._set_status(msg)

    def on_undo(self):
        if self.crop and self.crop.can_undo:
            self.crop.undo()
            self._rebuild()
            self._set_status("已撤销上一步裁剪")

    def on_redo(self):
        if self.crop and self.crop.can_redo:
            self.crop.redo()
            self._rebuild()
            self._set_status("已重做")

    def on_reset_handles(self):
        self.a_idx = self.crop.ranges[0][0]
        self.b_idx = self.crop.ranges[-1][1]
        self._update_selection(refresh_all=True)
        self._set_status("手柄已复位到当前保留段两端")

    def on_export(self):
        stem = Path(self.track.source_path).stem
        out = filedialog.asksaveasfilename(
            title="导出裁剪后的 KML",
            defaultextension=".kml",
            initialfile=f"{stem}_crop.kml",
            filetypes=[("KML 文件", "*.kml")],
            parent=self.root)
        if not out:
            return
        try:
            r = export_kml(self.track, self.crop, out)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("导出失败", str(e))
            return
        st = r["stats"]
        self._set_status(
            f"已导出：{out}（{r['n_points']} 点，"
            f"{st.distance_m/1000:.2f} km，时长 {fmt_dur(st.duration_s)}）")
        messagebox.showinfo(
            "导出成功",
            f"文件：{Path(out).name}\n"
            f"点数：{r['n_points']} / {self.track.n}\n"
            f"里程：{st.distance_m/1000:.2f} km\n"
            f"时长：{fmt_dur(st.duration_s)}\n"
            f"爬升：+{st.gain_m:.0f} m / 下降：-{st.loss_m:.0f} m\n\n"
            f"起点/终点标注与统计数据已同步重算，可导入两步路或 Google Earth。")


def main():
    root = tk.Tk()
    # 测试阶段：未指定文件时默认加载脚本同目录下的 cat.kml
    if len(sys.argv) > 1:
        initial = sys.argv[1]
    else:
        default_kml = Path(__file__).parent / "cat.kml"
        initial = str(default_kml) if default_kml.exists() else None
    CropApp(root, initial_file=initial)
    root.mainloop()


if __name__ == "__main__":
    main()
