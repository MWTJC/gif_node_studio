"""预览刷新读数（状态栏「渲染 fps」的唯一数据源，决策 #146/#147）。

口径（用户两轮裁定后的终版）：**画布刷新率** —— 与游戏里的 FPS 计数器同义，
即「界面每秒合成多少次」，而不是各预览框绘制次数之和。

为什么改（实测）：Qt 只合并同一轮事件循环内的 `update()`。3 个预览各自 30Hz
定时器时恰好同相位 → 视口 30.5 次/秒；8 个预览错相 → **57.5 次/秒**。改由统一
帧钟（``preview_clock``）一拍推进全部预览后，8 个预览也稳定在 30.5 次/秒——只有
这样「渲染 fps」才等于设定值，才是用户理解的「限帧 30」。

三路计数：
- ``note_viewport_paint``：画布 viewport 的 paintEvent（**读数主口径**）；
- ``note_paint``：预览框（CheckerPreviewLabel）自绘次数（真实绘制工作量）；
- ``note_frame``：播放器换帧次数。

后两者只进 tooltip 作对照（例如「渲染 30 / 预览框绘制 90 / 换帧 90」说明一拍
合并了 3 个预览的换帧）。

实现约定：计数是模块级整数自增（无信号、无对象分配）；``FpsMeter`` 每秒读取
并清零，读数窗口用 ``PreciseTimer``（1s 的 Coarse 定时器实测被量化成 ≈1.09s，
会把读数系统性压低 ≈8%）。
"""

from __future__ import annotations

from PySide6 import QtCore

_viewport_paints = 0
_paints = 0
_frames = 0
_batch_open = False


def note_viewport_paint() -> None:
    """记录一次画布重绘；**同一轮事件循环内的连续重绘合并为一次界面刷新**。

    为什么合并：一次内容变化在 QGraphicsView + QGraphicsProxyWidget 下可能触发
    两次局部重绘（控件 update + 代理项失效），逐次计数会把「30fps 的界面」读成
    60。合并后一次刷新只计一次，与游戏 FPS 计数器的语义一致。
    """
    global _batch_open
    if _batch_open:
        return
    _batch_open = True
    QtCore.QTimer.singleShot(0, _close_batch)


def _close_batch() -> None:
    """结束当前刷新批次（0ms 定时器或 take_counts 触发）。"""
    global _batch_open, _viewport_paints
    if not _batch_open:
        return
    _batch_open = False
    _viewport_paints += 1


def note_paint(count: int = 1) -> None:
    """记录一次预览框（CheckerPreviewLabel）自绘。"""
    global _paints
    _paints += count


def note_frame(count: int = 1) -> None:
    """记录一次预览换帧（播放器每帧回调）。"""
    global _frames
    _frames += count


def take_counts() -> tuple[int, int, int]:
    """取出并清零累计计数，返回 ``(画布刷新, 预览框绘制, 换帧)`` 次数。"""
    global _viewport_paints, _paints, _frames
    _close_batch()  # 结算仍打开的批次，避免末次刷新丢失
    counts = (_viewport_paints, _paints, _frames)
    _viewport_paints = _paints = _frames = 0
    return counts


class FpsMeter(QtCore.QObject):
    """每秒把累计计数换算成每秒次数，发出 ``updated(渲染, 预览框绘制, 换帧)``。"""

    updated = QtCore.Signal(int, int, int)
    INTERVAL_MS = 1000

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

    def start(self) -> None:
        take_counts()  # 丢弃启动前的零星计数，避免首秒虚高
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _poll(self) -> None:
        viewport, paints, frames = take_counts()
        self.updated.emit(viewport, paints, frames)
