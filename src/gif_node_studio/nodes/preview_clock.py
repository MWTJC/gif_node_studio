"""统一预览帧钟（决策 #147）：一拍推进全部在播预览，让界面渲染次数 = 限帧值。

为什么需要它（实测）：Qt 只合并**同一轮事件循环内**的 `update()`。各预览如果
各自持有定时器，帧一拍就会错开——8 个预览 30Hz 时实测画布合成 **57.5 次/秒**
（3 个时因定时器同相位恰好合并成 30.5）。改由单一帧钟驱动后，8 个预览也是
**30.5 次/秒**：每次合成都把当拍所有换过的预览一次画完。

口径：帧钟速率 = 在播预览里最小的限帧值（限帧是全局设置，正常都相同）；没有
在播预览时定时器停止。播放器仍按各自的原生帧延迟累积推进（丢帧保时长），
所以「每预览 ≤ 30fps、时间尺度不变」与「界面渲染 30 次/秒」同时成立。

计费：解码/绘制的工作量仍是各预览之和（8 个预览 30Hz = 244 次标签绘制/秒，
这部分不会因统一帧钟减少）；被钉住的是**合成/刷新次数**与定时器抖动。
"""

from __future__ import annotations

from typing import Protocol

from PySide6 import QtCore


class ClockTarget(Protocol):
    """帧钟驱动的预览播放器契约：每拍调用一次，自行决定是否换帧。"""

    def advance(self, period_ms: float) -> None:
        """按拍长推进；内部按原生帧延迟累积，必要时换帧（最多一次重绘）。"""


class PreviewRenderClock(QtCore.QObject):
    """单一 Precise 定时器驱动全部在播预览（进程内单例，见 ``render_clock()``）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._timer = QtCore.QTimer(self)
        self._timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._limits: dict[int, tuple[ClockTarget, int]] = {}   # id(target) -> (target, fps)
        self._rate: int | None = None
        self.tick_count = 0

    # --- 注册/速率 ---

    def register(self, target: ClockTarget, fps: int) -> None:
        """登记一个在播预览（fps 为其限帧值）；速率取所有在播预览的最小值。"""
        self._limits[id(target)] = (target, max(1, int(fps)))
        self._apply_rate()

    def unregister(self, target: ClockTarget) -> None:
        self._limits.pop(id(target), None)
        self._apply_rate()

    def is_registered(self, target: ClockTarget) -> bool:
        return id(target) in self._limits

    def rate(self) -> int | None:
        """当前出拍速率（fps）；无在播预览时为 None。"""
        return self._rate

    def period_ms(self) -> float:
        return 1000.0 / self._rate if self._rate else 0.0

    def _apply_rate(self) -> None:
        rate = min((fps for _target, fps in self._limits.values()), default=None)
        if rate == self._rate:
            if rate and not self._timer.isActive():
                self._timer.start(max(1, round(1000.0 / rate)))
            elif not rate:
                self._timer.stop()
            return
        self._rate = rate
        if not rate:
            self._timer.stop()
            return
        self._timer.start(max(1, round(1000.0 / rate)))

    # --- 出拍 ---

    def _tick(self) -> None:
        self.tick_count += 1
        period = self.period_ms()
        # 快照：advance() 可能在循环内注销自己（播放结束/出错）
        for target, _fps in list(self._limits.values()):
            try:
                target.advance(period)
            except RuntimeError:
                # C++ 对象已销毁（节点删除/窗口关闭）
                self._limits.pop(id(target), None)

    def stop(self) -> None:
        """进程收尾：停表并清空（窗口关闭时调用）。"""
        self._timer.stop()
        self._limits.clear()
        self._rate = None


_CLOCK: PreviewRenderClock | None = None


def render_clock() -> PreviewRenderClock:
    """进程内共享帧钟（首次调用创建，须在 GUI 线程创建）。"""
    global _CLOCK
    if _CLOCK is None:
        _CLOCK = PreviewRenderClock()
    return _CLOCK
