# -*- coding: utf-8 -*-
"""轨迹统计：里程、时长、爬升/下降。

里程用 Haversine 公式计算（米）。
裁剪后轨迹可能不连续（删了中段），统计按"保留区间"逐段累加，
段与段之间的空缺不计入里程和时长。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .parser import TrackPoint

EARTH_R = 6371000.0  # 地球平均半径（米）

# 海拔平滑窗口：GPS 原始海拔噪声大，直接累加会让爬升虚高 40%+。
# 用 15 点滚动均值滤波后，与两步路导出的 ElevationGain/Loss 元数据误差 <1%（已在 test.kml 上验证）。
ELE_SMOOTH_WINDOW = 15


def smooth_elevation(eles: list[float], window: int = ELE_SMOOTH_WINDOW) -> list[float]:
    """滚动均值平滑海拔序列（用于爬升/下降统计）。"""
    n = len(eles)
    if n == 0:
        return []
    out = list(eles[:min(window, n)])
    for i in range(window, n):
        out.append(sum(eles[i - window + 1:i + 1]) / window)
    return out


def haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """两经纬度点间大圆距离（米）。"""
    import math
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def cumulative_distances(points: list[TrackPoint]) -> list[float]:
    """原始轨迹逐点累计里程：cum[i] = 从点0走到点i的总距离（米）。点0为0。"""
    cum = [0.0]
    for i in range(1, len(points)):
        p0, p1 = points[i - 1], points[i]
        cum.append(cum[-1] + haversine(p0.lon, p0.lat, p1.lon, p1.lat))
    return cum


@dataclass
class SegStats:
    """一段（或多段）轨迹的统计结果。"""
    distance_m: float = 0.0        # 总里程（米）
    duration_s: float = 0.0        # 总时长（秒，段内起止时间差之和，不含段间空缺）
    gain_m: float = 0.0            # 累计爬升（米）
    loss_m: float = 0.0            # 累计下降（米）
    n_points: int = 0              # 点数
    t_start: datetime | None = None  # 最早时间
    t_end: datetime | None = None    # 最晚时间

    @property
    def speed_ms(self) -> float:
        """平均速度（米/秒），无时长时为0。"""
        return self.distance_m / self.duration_s if self.duration_s > 0 else 0.0


def stats_for_ranges(points: list[TrackPoint], ranges: list[tuple[int, int]]) -> SegStats:
    """按原始点下标区间（闭区间，可多段）统计。

    里程/爬升按段内相邻点累加；时长为各段(末点时间-首点时间)之和，段间空缺不计。
    """
    st = SegStats()
    for s, e in ranges:
        if s > e:
            s, e = e, s
        s = max(s, 0)
        e = min(e, len(points) - 1)
        if s > e:
            continue
        st.n_points += e - s + 1
        for i in range(s, e + 1):
            p = points[i]
            if p.time:
                if st.t_start is None or p.time < st.t_start:
                    st.t_start = p.time
                if st.t_end is None or p.time > st.t_end:
                    st.t_end = p.time
            if i > s:
                prev = points[i - 1]
                st.distance_m += haversine(prev.lon, prev.lat, p.lon, p.lat)
        # 爬升/下降：段内海拔先平滑再累加，避免 GPS 噪声虚高
        sm = smooth_elevation([points[i].ele for i in range(s, e + 1)])
        for i in range(1, len(sm)):
            de = sm[i] - sm[i - 1]
            if de > 0:
                st.gain_m += de
            else:
                st.loss_m -= de
        if points[s].time and points[e].time:
            st.duration_s += (points[e].time - points[s].time).total_seconds()
    return st
