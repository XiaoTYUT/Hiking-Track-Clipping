# -*- coding: utf-8 -*-
"""裁剪内核：区间模型 + 操作历史（撤销/重做）。

核心思想：原始点永远不动，裁剪状态 = 一组保留的原始下标区间（闭区间）。
- 保留 A→B：现有区间与 [A,B] 求交集
- 删除 A→B：现有区间减去 [A,B]（可能一段变两段）
撤销/重做即历史栈回退。
"""
from __future__ import annotations


def _clip(ranges: list[tuple[int, int]], a: int, b: int) -> list[tuple[int, int]]:
    """所有区间与 [a,b] 求交集。"""
    out = []
    for s, e in ranges:
        ns, ne = max(s, a), min(e, b)
        if ns <= ne:
            out.append((ns, ne))
    return out


def _subtract(ranges: list[tuple[int, int]], a: int, b: int) -> list[tuple[int, int]]:
    """所有区间减去 [a,b]，可能一段拆成两段。"""
    out = []
    for s, e in ranges:
        if e < a or s > b:
            out.append((s, e))
            continue
        if s < a:
            out.append((s, a - 1))
        if e > b:
            out.append((b + 1, e))
    return out


class CropState:
    """裁剪状态机（含撤销/重做）。"""

    def __init__(self, n: int):
        self.n = n
        self.ranges: list[tuple[int, int]] = [(0, n - 1)] if n > 0 else []
        self._undo: list[list[tuple[int, int]]] = []
        self._redo: list[list[tuple[int, int]]] = []
        self._dirty = False  # 载入后是否发生过裁剪

    # ---------- 查询 ----------
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def is_full(self) -> bool:
        return self.ranges == [(0, self.n - 1)]

    def indices(self) -> list[int]:
        """展开为保留下标列表。"""
        return [i for s, e in self.ranges for i in range(s, e + 1)]

    # ---------- 操作 ----------
    def keep(self, a: int, b: int) -> None:
        """只保留 [a,b]，其余全删。"""
        a, b = sorted((max(0, a), min(self.n - 1, b)))
        self._push()
        self.ranges = _clip(self.ranges, a, b)

    def remove(self, a: int, b: int) -> None:
        """删除 [a,b]，保留其余。"""
        a, b = sorted((max(0, a), min(self.n - 1, b)))
        self._push()
        self.ranges = _subtract(self.ranges, a, b)

    def undo(self) -> None:
        if not self._undo:
            return
        self._redo.append(self.ranges)
        self.ranges = self._undo.pop()

    def redo(self) -> None:
        if not self._redo:
            return
        self._undo.append(self.ranges)
        self.ranges = self._redo.pop()

    # ---------- 内部 ----------
    def _push(self) -> None:
        self._undo.append(self.ranges)
        if len(self._undo) > 200:
            self._undo.pop(0)
        self._redo.clear()
        self._dirty = True
