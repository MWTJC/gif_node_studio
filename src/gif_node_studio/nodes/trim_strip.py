"""序列截取可视化：帧缩略图胶片条 + 可拖动的起止双手柄（区间选择式截取）。

``TrimStripWidget`` 是「序列截取」节点的鼠标交互组件（胶片条画布，决策 #115，
与「序列剃刀」的 ``RazorStripWidget`` 同构）：

- 输入序列全部帧以缩略图横向铺成胶片条（等比缩放到固定行高，长序列自动
  跨帧采样显示——采样只影响**显示**，区间始终按帧边界精确映射）；
- 蓝色**起始手柄**（左边界）与橙色**结束手柄**（右边界）标记截取区间
  ``[start, end)``（0 基切片下标，输出 = frames[start:end]）；
- 区间内部高亮、区间外部压暗——直观呈现「只保留中间这段」；
- 鼠标按下/拖动移动手柄，位置吸附到最近帧边界；按在条上空白处时移动
  **较近**的那个手柄（时间轴惯例）；拖动钳制 ``start < end`` 恒成立
  （两端都可到 0/N，与剃刀的 1..N-1 不同：截取允许从头/到尾）；
- 坐标映射为纯函数（``x_to_index`` / ``index_to_x``），便于无 Qt 事件测试；
- 信号：拖拽期间实时发 ``range_changed(int, int)``（起/止边界下标）；手势
  开始/结束发 ``gesture_begin`` / ``gesture_end``，供上层把整个拖拽折叠为
  一条撤销记录（与 ``RazorStripWidget`` / ``CropOverlayWidget`` 同约定）；
  帧集合变化发 ``frames_changed``。

``TrimStripPanel`` 是接管面板（决策 #109/#115）：容器内嵌胶片条 + 区间
起止两侧帧实时预览（起始帧 / 结束帧）+ 区间只读，作为 ``TrimRangeParam``
的复合控件直接在节点内声明使用。

绘制坐标约定：逻辑空间 = (0..N) × (0..THUMB_HEIGHT)（每个帧占 1 个逻辑单位
宽）；绘制/命中时按 ``scale = min(1, 条宽 / (N·cell_w))`` 均匀缩放（长序列
压缩、短序列保持自然大小居中），手柄线/三角/刻度在**条坐标**（像素）绘制，
保证 1px 线宽在压缩时依旧清晰。
"""

from __future__ import annotations

import math

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.color_tokens import DARK, rgba
from .preview_widgets import CheckerPreviewLabel

class TrimStripWidget(QtWidgets.QWidget):
    """帧胶片条 + 可拖动的起止双手柄。

    - ``start``/``end`` 为 0 基切片下标：输出 = frames[start:end]，
      合法范围 0 <= start < end <= N（允许从头/到尾截取，与
      ``backend.trim_sequence`` 的数值防呆一致）；显示层按 1 基表述
      （第 start+1..end 帧）；
    - ``set_frames`` 喂入帧路径列表（可为空 = 无预览）；缩略图懒加载且
      上限 ``MAX_THUMBS``（更长序列跨帧采样显示，条上仍按全部帧边界刻度）；
    - ``set_range`` 程序化设置区间（不触发 range_changed，与 CropOverlay 的
      ``set_values`` 同语义：存档恢复等场景不产生手势/参数信号）。
    """

    range_changed = QtCore.Signal(int, int)
    gesture_begin = QtCore.Signal()
    gesture_end = QtCore.Signal()
    frames_changed = QtCore.Signal()

    STRIP_MAX_WIDTH = 420  # 条宽上限（逻辑像素；长序列按比例压缩）
    HANDLE_HEIGHT = 16     # 顶部手柄/标签区高度
    THUMB_HEIGHT = 56      # 缩略图行高（自然尺寸，压缩前）
    MIN_CELL_WIDTH = 8     # 单帧格宽下限（逻辑像素）
    MAX_CELL_WIDTH = 120   # 单帧格宽上限（逻辑像素，宽银幕帧不至于过宽）
    MAX_THUMBS = 16        # 显示用缩略图数量上限（超长序列跨帧采样）
    HANDLE_WIDTH = 1.0     # 手柄线宽（条坐标像素，压缩时依旧清晰）
    EDGE_TOLERANCE = 8.0   # 命中手柄的像素容差（悬停光标切换用）
    BG_COLOR = DARK.bg
    START_COLOR = DARK.trim_start   # 起始手柄（蓝）
    END_COLOR = DARK.trim_end       # 结束手柄（橙）
    SELECT_TINT = rgba(DARK.trim_start, 30)  # 区间内部高亮（由手柄色派生）
    DIM_TINT = (0, 0, 0, 100)                # 区间外部压暗

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.STRIP_MAX_WIDTH, self.HANDLE_HEIGHT + self.THUMB_HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.setToolTip(
            "拖动蓝色起始手柄 / 橙色结束手柄选择截取区间：仅输出区间内的帧"
        )
        self._frames: list[str] = []
        self._thumbs: list[tuple[int, QtGui.QPixmap]] = []  # (起始帧下标, 缩略图)
        self._cell_w = self.MAX_CELL_WIDTH  # 单帧格宽（逻辑，由首帧宽高比派生）
        self._stride = 1                    # 采样间隔（长序列跨帧采样）
        self._start = 0
        self._end = 1
        self._dragging: str | None = None   # None | "start" | "end"

    # ------------------------------------------------------------------
    # 坐标映射（纯函数，便于无 Qt 事件测试）
    # ------------------------------------------------------------------

    @staticmethod
    def x_to_index(x_px: float, width_px: float, count: int) -> int:
        """条内像素 x → 最近帧边界下标（0 基切片下标，钳制到 0..count）。

        ``count`` = 帧数；宽度按 ``(0..count)`` 逻辑空间线性映射，
        取整到最近整数边界（吸附帧边界）。允许 0 与 count（截取可从
        头/到尾，与剃刀的 1..count-1 不同）。
        """
        if count < 1 or width_px <= 0:
            return 0
        logical = x_px / width_px * count
        return max(0, min(count, round(logical)))

    @staticmethod
    def index_to_x(index: int, width_px: float, count: int) -> float:
        """帧边界下标 → 条内像素 x（供绘制/命中测试）。"""
        if count < 1 or width_px <= 0:
            return 0.0
        return index / count * width_px

    @staticmethod
    def _scale_for(cell_w: float, width_px: float, count: int) -> float:
        """条绘制缩放：长序列压缩（<1），短序列保持自然大小（1）。"""
        if count < 1 or width_px <= 0 or cell_w <= 0:
            return 1.0
        return min(1.0, width_px / (count * cell_w))

    # ------------------------------------------------------------------
    # 状态读写
    # ------------------------------------------------------------------

    def set_frames(self, frames: list[str]) -> None:
        """喂入帧路径列表（空列表 = 无预览）。缩略图按采样间隔惰性构建。"""
        self._frames = list(frames or [])
        self._build_thumbs()
        if self._frames:
            total = len(self._frames)
            self._start = max(0, min(total - 1, self._start))
            self._end = max(self._start + 1, min(total, self._end))
        self.update()
        self.frames_changed.emit()

    def set_range(self, start: int, end: int) -> None:
        """程序化设置区间（不触发 range_changed/手势信号）。"""
        if not self._frames:
            self._start = max(0, int(start))
            self._end = max(self._start + 1, int(end))
            self.update()
            return
        total = len(self._frames)
        start = max(0, min(total - 1, int(start)))
        end = max(start + 1, min(total, int(end)))
        if start != self._start or end != self._end:
            self._start, self._end = start, end
            self.update()

    def range(self) -> tuple[int, int]:
        return self._start, self._end

    def frames(self) -> list[str]:
        """当前胶片条帧路径列表（只读副本，供接管面板取区间起止帧）。"""
        return list(self._frames)

    def frame_count(self) -> int:
        return len(self._frames)

    def has_frames(self) -> bool:
        return bool(self._frames)

    def _build_thumbs(self) -> None:
        """按首帧宽高比派生单帧格宽，并对采样帧生成缩略图。

        采样间隔 ``stride = ceil(N / MAX_THUMBS)``：只加载/持有最多
        ``MAX_THUMBS`` 张缩略图（长序列跨帧采样显示），但区间与帧边界
        刻度始终按**全部**帧数精确映射——采样只影响显示分辨率。
        """
        self._thumbs = []
        if not self._frames:
            return
        first = QtGui.QPixmap(self._frames[0])
        if first.isNull():
            self._cell_w = self.MIN_CELL_WIDTH
            return
        aspect = first.width() / max(1, first.height())
        self._cell_w = max(
            self.MIN_CELL_WIDTH,
            min(self.MAX_CELL_WIDTH, round(self.THUMB_HEIGHT * aspect)),
        )
        total = len(self._frames)
        self._stride = max(1, math.ceil(total / self.MAX_THUMBS))
        for index in range(0, total, self._stride):
            pixmap = QtGui.QPixmap(self._frames[index])
            if pixmap.isNull():
                continue
            span = min(self._stride, total - index)
            target_w = max(1, round(span * self._cell_w))
            thumb = pixmap.scaled(
                target_w, self.THUMB_HEIGHT,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
            self._thumbs.append((index, thumb))

    # ------------------------------------------------------------------
    # 鼠标交互
    # ------------------------------------------------------------------

    def _handle_at(self, x: float) -> str | None:
        """条内 x 命中的手柄（'start'/'end'），未命中返回 None。"""
        total = len(self._frames)
        if not total:
            return None
        start_x = self.index_to_x(self._start, self.width(), total)
        end_x = self.index_to_x(self._end, self.width(), total)
        distance_start, distance_end = abs(x - start_x), abs(x - end_x)
        if min(distance_start, distance_end) <= self.EDGE_TOLERANCE:
            return "start" if distance_start <= distance_end else "end"
        return None

    def _apply_drag(self, x: float) -> None:
        """按当前拖拽手柄把 x 吸附到帧边界（钳制 start < end 恒成立）。"""
        total = len(self._frames)
        index = self.x_to_index(x, self.width(), total)
        if self._dragging == "start":
            self._start = max(0, min(self._end - 1, index))
        else:
            self._end = max(self._start + 1, min(total, index))

    def mousePressEvent(self, event) -> None:
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        if not self._frames:
            return
        target = self._handle_at(event.position().x())
        if target is None:
            # 按在空白处：移动**较近**的手柄（时间轴惯例）。
            total = len(self._frames)
            start_x = self.index_to_x(self._start, self.width(), total)
            end_x = self.index_to_x(self._end, self.width(), total)
            x = event.position().x()
            target = "start" if abs(x - start_x) <= abs(x - end_x) else "end"
        self._dragging = target
        self._apply_drag(event.position().x())
        self.gesture_begin.emit()
        self.range_changed.emit(self._start, self._end)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and self._frames:
            previous = (self._start, self._end)
            self._apply_drag(event.position().x())
            if (self._start, self._end) != previous:
                self.range_changed.emit(self._start, self._end)
                self.update()
            event.accept()
            return
        # 悬停：手柄附近显示水平双向箭头
        if self._handle_at(event.position().x()) is not None:
            self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def mouseReleaseEvent(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = None
        self.gesture_end.emit()
        event.accept()

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def _content_rect(self) -> QtCore.QRectF:
        """缩略图行矩形（条坐标）：顶部留出手柄/标签区。"""
        return QtCore.QRectF(
            0.0, self.HANDLE_HEIGHT, self.width(), self.THUMB_HEIGHT,
        )

    def _handle_x(self, index: int, content: QtCore.QRectF, total: int) -> float:
        """手柄线条坐标 x（内容区内；0/count 边缘时收进半像素保持线可见）。"""
        x = self.index_to_x(index, content.width(), total) + content.x()
        return max(0.5, min(self.width() - 0.5, x))

    def paintEvent(self, _event) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor(self.BG_COLOR))
        if not self._frames:
            painter.setPen(QtGui.QColor("#8a9099"))
            painter.drawText(
                self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "无预览"
            )
            return
        total = len(self._frames)
        content = self._content_rect()
        scale = self._scale_for(self._cell_w, content.width(), total)
        cell_px = self._cell_w * scale  # 单帧格宽（条坐标像素）
        # 缩略图（逻辑空间缩放绘制；压缩时下采样，短序列保持自然大小）
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        for start, thumb in self._thumbs:
            span = min(self._stride, total - start)
            x = content.x() + start * cell_px
            w = span * cell_px
            painter.drawPixmap(
                QtCore.QRectF(x, content.y(), w, content.height()),
                thumb,
                QtCore.QRectF(thumb.rect()),
            )
        # 帧边界刻度（压缩到 <3px 时过密，跳过）
        if cell_px >= 3.0:
            painter.setPen(QtGui.QColor(255, 255, 255, 40))
            for boundary in range(1, total):
                x = content.x() + boundary * cell_px
                painter.drawLine(
                    QtCore.QPointF(x, content.y()),
                    QtCore.QPointF(x, content.bottom()),
                )
        # 区间语义：内部高亮、外部压暗（直观呈现「只保留中间这段」）
        start_x = content.x() + self.index_to_x(self._start, content.width(), total)
        end_x = content.x() + self.index_to_x(self._end, content.width(), total)
        painter.fillRect(
            QtCore.QRectF(start_x, content.y(), end_x - start_x, content.height()),
            QtGui.QColor(*self.SELECT_TINT),
        )
        painter.fillRect(
            QtCore.QRectF(content.x(), content.y(), start_x - content.x(), content.height()),
            QtGui.QColor(*self.DIM_TINT),
        )
        painter.fillRect(
            QtCore.QRectF(end_x, content.y(), content.right() - end_x, content.height()),
            QtGui.QColor(*self.DIM_TINT),
        )
        # 段标签（顶部区左侧/右侧）
        painter.setPen(QtGui.QColor(self.START_COLOR))
        painter.drawText(
            QtCore.QRectF(4.0, 0.0, 40.0, self.HANDLE_HEIGHT),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            "起",
        )
        painter.setPen(QtGui.QColor(self.END_COLOR))
        painter.drawText(
            QtCore.QRectF(self.width() - 44.0, 0.0, 40.0, self.HANDLE_HEIGHT),
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
            "止",
        )
        # 起止手柄线 + 顶部三角（条坐标绘制，压缩时线宽依旧清晰；
        # 三角在条边缘时收进 5px 保证完整可见——截取允许 start=0/end=N）
        for index, color in ((self._start, self.START_COLOR), (self._end, self.END_COLOR)):
            x = self._handle_x(index, content, total)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(color))
            half = self.HANDLE_WIDTH / 2.0
            painter.drawRect(
                QtCore.QRectF(
                    x - half, content.y(),
                    self.HANDLE_WIDTH, content.height(),
                )
            )
            # 手柄：三角箭头朝下（与剃刀刀柄同式样）
            tri_x = max(5.0, min(self.width() - 5.0, x))
            handle_h = 8.0
            painter.drawPolygon(
                QtGui.QPolygonF([
                    QtCore.QPointF(tri_x, content.y()),
                    QtCore.QPointF(tri_x - 5.0, content.y() - handle_h),
                    QtCore.QPointF(tri_x + 5.0, content.y() - handle_h),
                ])
            )
        painter.end()


class TrimStripPanel(QtWidgets.QWidget):
    """序列截取接管面板（复合控件，决策 #109/#115）：胶片条 + 区间起止帧预览 + 只读。

    作为 ``TrimRangeParam`` 的接管控件（``widget_factory=make_trim_panel``）在
    节点 params 中声明即排版。

    接管控件契约：
    - ``changed`` —— 拖拽区间后发出（面板据此发 ``changed(values())``）；
    - ``values()`` / ``set_values(dict)`` —— 按参数名读写（``{"start": int, "end": int}``）；
    - ``gesture_begin`` / ``gesture_end`` —— 拖拽手势（上层折叠为一条撤销记录）；
    - ``set_frames`` —— 喂入上游序列帧路径（``data_source="sequence_frames"``）；
    - ``release_content`` —— 清空两侧预览与只读（**不误伤胶片条帧**：帧由
      ``set_frames`` 显式管理）。
    """

    changed = QtCore.Signal()
    gesture_begin = QtCore.Signal()
    gesture_end = QtCore.Signal()

    SIDE_PREVIEW_SIZE = 200  # 两侧预览适配框边长

    def __init__(self, panel=None, param=None):
        super().__init__(None)
        self._panel = panel
        self.strip = TrimStripWidget()  # 胶片条画布（原交互控件）
        self.strip.range_changed.connect(self._on_range_changed)
        self.strip.gesture_begin.connect(self.gesture_begin)
        self.strip.gesture_end.connect(self.gesture_end)
        self.side_a, self.preview_a = self._side_preview("起始帧")
        self.side_b, self.preview_b = self._side_preview("结束帧")
        sides = QtWidgets.QHBoxLayout()
        sides.setContentsMargins(0, 0, 0, 0)
        sides.addWidget(self.side_a, 1)
        sides.addWidget(self.side_b, 1)
        sides_wrap = QtWidgets.QWidget()
        sides_wrap.setLayout(sides)
        self.readout = QtWidgets.QLabel("截取范围：—")
        self.readout.setWordWrap(True)
        self.readout.setStyleSheet("color:#8ab4ff;")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.strip)
        layout.addWidget(sides_wrap)
        layout.addWidget(self.readout)

    def _side_preview(self, caption: str) -> tuple[QtWidgets.QWidget, CheckerPreviewLabel]:
        """区间一侧预览：标题 + 固定适配框（边长 ``SIDE_PREVIEW_SIZE``，不随分辨率放大）。

        与剃刀节点同语义（``ParameterPanel`` 固定框 + 只缩不放）：
        ``set_1to1(False)`` 关闭 1:1 跟随模式，否则高分帧会把框撑到素材
        像素尺寸 ÷ DPR。
        """
        column = QtWidgets.QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        cap = QtWidgets.QLabel(caption)
        cap.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet("color:#b8bdc7;")
        label = CheckerPreviewLabel("—")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        label.set_fit_box(self.SIDE_PREVIEW_SIZE, self.SIDE_PREVIEW_SIZE)
        label.set_1to1(False)
        column.addWidget(cap)
        column.addWidget(label, 1)
        wrap = QtWidgets.QWidget()
        wrap.setLayout(column)
        return wrap, label

    # --- 接管契约 ---

    def set_frames(self, frames: list[str]) -> None:
        """喂入上游序列帧路径（空列表 = 无预览）；随后刷新两侧预览与只读。"""
        self.strip.set_frames(list(frames or []))
        self._refresh_ui()

    def set_range(self, start: int, end: int) -> None:
        self.strip.set_range(start, end)
        self._refresh_ui()

    def range(self) -> tuple[int, int]:
        return self.strip.range()

    def values(self) -> dict:
        start, end = self.strip.range()
        return {"start": start, "end": end}

    def set_values(self, values: dict) -> None:
        if "start" in values or "end" in values:
            start, end = self.strip.range()
            if "start" in values:
                start = int(values["start"])
            if "end" in values:
                end = int(values["end"])
            self.set_range(start, end)

    def release_content(self) -> None:
        """清空两侧预览与只读（不误伤胶片条帧，帧由 set_frames 显式管理）。"""
        self.preview_a.clear_content()
        self.preview_b.clear_content()
        self.readout.setText("截取范围：—")

    # --- 内部 ---

    def _on_range_changed(self, _start: int, _end: int) -> None:
        self._refresh_ui()
        self.changed.emit()

    def _refresh_ui(self) -> None:
        """刷新两侧帧预览（起始帧/结束帧）与区间只读（拖拽/程序化共用）。"""
        start, end = self.strip.range()
        total = self.strip.frame_count()
        frames = self.strip.frames()
        if not frames or not (0 <= start < end <= total):
            self.preview_a.clear_content()
            self.preview_b.clear_content()
            self.readout.setText("截取范围：—")
            return
        start_pixmap = QtGui.QPixmap(frames[start])        # 区间首帧
        end_pixmap = QtGui.QPixmap(frames[end - 1])        # 区间末帧
        if start_pixmap.isNull() or end_pixmap.isNull():
            self.preview_a.clear_content()
            self.preview_b.clear_content()
            self.readout.setText("截取范围：—")
            return
        self.preview_a.set_content(start_pixmap)
        self.preview_b.set_content(end_pixmap)
        self.readout.setText(
            f"截取范围：第 {start + 1}..{end} 帧（共 {end - start} 帧）"
        )


def make_trim_panel(panel, param):
    """``TrimRangeParam`` 的控件工厂（供 ``TakeoverParam.widget_factory`` 引用）。"""
    return TrimStripPanel(panel, param)
