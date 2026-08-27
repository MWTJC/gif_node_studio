"""序列剃刀可视化：帧缩略图胶片条 + 可拖动的红色剃刀线（PR 剃刀工具式切割）。

``RazorStripWidget`` 是「序列剃刀」节点的鼠标交互组件（胶片条画布，决策 #107）：

- 输入序列全部帧以缩略图横向铺成胶片条（等比缩放到固定行高，长序列自动
  跨帧采样显示——采样只影响**显示**，切割位置始终按帧边界精确映射）；
- 红色剃刀线（顶部刀柄）标记当前切割边界；鼠标在条上按下/拖动即移动剃刀，
  位置吸附到最近帧边界（0 基切片下标 1..N-1）；
- 坐标映射为纯函数（``x_to_cut`` / ``cut_to_x``），便于无 Qt 事件测试；
- 信号：拖拽期间实时发 ``cut_changed(int)``（切割边界下标）；手势开始/结束
  发 ``gesture_begin`` / ``gesture_end``，供上层把整个拖拽折叠为一条撤销记录
  （与 ``CropOverlayWidget`` 同约定）；帧集合变化发 ``frames_changed``。

``RazorStripPanel`` 是接管面板（决策 #109）：容器内嵌胶片条 + 切割处两侧帧
实时预览（段A末帧/段B首帧）+ 切割位置只读，作为 ``RazorCutParam`` 的复合
控件直接在节点内声明使用。

绘制坐标约定：逻辑空间 = (0..N) × (0..THUMB_HEIGHT)（每个帧占 1 个逻辑单位
宽）；绘制/命中时按 ``scale = min(1, 条宽 / (N·cell_w))`` 均匀缩放（长序列
压缩、短序列保持自然大小居中），剃刀线/手柄/刻度在**条坐标**（像素）绘制，
保证 2px 线宽在压缩时依旧清晰。
"""

from __future__ import annotations

import math

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.color_tokens import DARK
from .preview_widgets import CheckerPreviewLabel

class RazorStripWidget(QtWidgets.QWidget):
    """帧胶片条 + 可拖动剃刀线。

    - ``cut`` 为 0 基切片下标（帧边界）：段A = frames[:cut]，段B = frames[cut:]，
      合法范围 1..N-1（两端都非空）；显示层按「第 cut 帧后」表述（1 基）；
    - ``set_frames`` 喂入帧路径列表（可为空 = 无预览）；缩略图懒加载且
      上限 ``MAX_THUMBS``（更长序列跨帧采样显示，条上仍按全部帧边界刻度）；
    - ``set_cut`` 程序化设置切割边界（不触发 cut_changed，与 CropOverlay 的
      ``set_values`` 同语义：存档恢复等场景不产生手势/参数信号）。
    """

    cut_changed = QtCore.Signal(int)
    gesture_begin = QtCore.Signal()
    gesture_end = QtCore.Signal()
    frames_changed = QtCore.Signal()

    STRIP_MAX_WIDTH = 420  # 条宽上限（逻辑像素；长序列按比例压缩）
    HANDLE_HEIGHT = 16     # 顶部手柄/标签区高度
    THUMB_HEIGHT = 56      # 缩略图行高（自然尺寸，压缩前）
    MIN_CELL_WIDTH = 8     # 单帧格宽下限（逻辑像素）
    MAX_CELL_WIDTH = 120   # 单帧格宽上限（逻辑像素，宽银幕帧不至于过宽）
    MAX_THUMBS = 16        # 显示用缩略图数量上限（超长序列跨帧采样），因数值过大显示出来也被压缩很扁所以直接改小
    RAZOR_WIDTH = 1.0      # 剃刀线宽（条坐标像素，压缩时依旧清晰）
    EDGE_TOLERANCE = 8.0   # 命中剃刀线的像素容差（悬停光标切换用）
    BG_COLOR = DARK.bg
    RAZOR_COLOR = DARK.danger

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.STRIP_MAX_WIDTH, self.HANDLE_HEIGHT + self.THUMB_HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.setToolTip(
            "拖动剃刀线切割序列：左侧为段A（切割点之前），右侧为段B（切割点之后）"
        )
        self._frames: list[str] = []
        self._thumbs: list[tuple[int, QtGui.QPixmap]] = []  # (起始帧下标, 缩略图)
        self._cell_w = self.MAX_CELL_WIDTH  # 单帧格宽（逻辑，由首帧宽高比派生）
        self._stride = 1                    # 采样间隔（长序列跨帧采样）
        self._cut = 1
        self._dragging = False

    # ------------------------------------------------------------------
    # 坐标映射（纯函数，便于无 Qt 事件测试）
    # ------------------------------------------------------------------

    @staticmethod
    def x_to_cut(x_px: float, width_px: float, count: int) -> int:
        """条内像素 x → 最近帧边界下标（0 基切片下标，钳制到 1..count-1）。

        ``count`` = 帧数；宽度按 ``(0..count)`` 逻辑空间线性映射，
        取整到最近整数边界（吸附帧边界），两端各留 1 个边界保证两段非空。
        """
        if count < 2 or width_px <= 0:
            return max(1, count - 1)
        logical = x_px / width_px * count
        return max(1, min(count - 1, round(logical)))

    @staticmethod
    def cut_to_x(cut: int, width_px: float, count: int) -> float:
        """帧边界下标 → 条内像素 x（供绘制/命中测试）。"""
        if count < 1 or width_px <= 0:
            return 0.0
        return cut / count * width_px

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
        if self._frames and not (1 <= self._cut <= len(self._frames) - 1):
            self._cut = max(1, len(self._frames) // 2)
        self.update()
        self.frames_changed.emit()

    def set_cut(self, cut: int) -> None:
        """程序化设置切割边界（不触发 cut_changed/手势信号）。"""
        if not self._frames:
            self._cut = max(1, cut)
            self.update()
            return
        clamped = max(1, min(len(self._frames) - 1, int(cut)))
        if clamped != self._cut:
            self._cut = clamped
            self.update()

    def cut(self) -> int:
        return self._cut

    def frames(self) -> list[str]:
        """当前胶片条帧路径列表（只读副本，供接管面板取切割处帧）。"""
        return list(self._frames)

    def frame_count(self) -> int:
        return len(self._frames)

    def has_frames(self) -> bool:
        return bool(self._frames)

    def _build_thumbs(self) -> None:
        """按首帧宽高比派生单帧格宽，并对采样帧生成缩略图。

        采样间隔 ``stride = ceil(N / MAX_THUMBS)``：只加载/持有最多
        ``MAX_THUMBS`` 张缩略图（长序列跨帧采样显示），但切割位置与帧边界
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

    def mousePressEvent(self, event) -> None:
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        if not self._frames:
            return
        self._cut = self.x_to_cut(event.position().x(), self.width(), len(self._frames))
        self._dragging = True
        self.gesture_begin.emit()
        self.cut_changed.emit(self._cut)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and self._frames:
            new_cut = self.x_to_cut(event.position().x(), self.width(), len(self._frames))
            if new_cut != self._cut:
                self._cut = new_cut
                self.cut_changed.emit(self._cut)
                self.update()
            event.accept()
            return
        # 悬停：剃刀线附近显示水平双向箭头
        razor_x = self.cut_to_x(self._cut, self.width(), max(1, len(self._frames)))
        if self._frames and abs(event.position().x() - razor_x) <= self.EDGE_TOLERANCE:
            self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def mouseReleaseEvent(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
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
        # 段A/段B 底色区分（半透明，仅覆盖缩略图行）
        razor_x = self.cut_to_x(self._cut, content.width(), total) + content.x()
        painter.fillRect(
            QtCore.QRectF(content.x(), content.y(), razor_x - content.x(), content.height()),
            QtGui.QColor(0, 120, 255, 22),
        )
        painter.fillRect(
            QtCore.QRectF(razor_x, content.y(), content.right() - razor_x, content.height()),
            QtGui.QColor(255, 80, 0, 20),
        )
        # 段标签（顶部区左侧/右侧）
        painter.setPen(QtGui.QColor("#6aa8ff"))
        painter.drawText(
            QtCore.QRectF(4.0, 0.0, 40.0, self.HANDLE_HEIGHT),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            "A",
        )
        painter.setPen(QtGui.QColor("#ff9a6a"))
        painter.drawText(
            QtCore.QRectF(self.width() - 44.0, 0.0, 40.0, self.HANDLE_HEIGHT),
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
            "B",
        )
        # 剃刀线 + 顶部刀柄（条坐标绘制，压缩时线宽依旧清晰）
        razor_x = self.cut_to_x(self._cut, content.width(), total) + content.x()
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(self.RAZOR_COLOR))
        half = self.RAZOR_WIDTH / 2.0
        painter.drawRect(
            QtCore.QRectF(
                razor_x - half, content.y(),
                self.RAZOR_WIDTH, content.height(),
            )
        )
        # 刀柄：红色小三角（上尖）—— PR 剃刀式样
        handle_h = 8.0
        painter.drawPolygon(
            QtGui.QPolygonF([  #  改为三角箭头朝下更符直觉
                QtCore.QPointF(razor_x, content.y()),
                QtCore.QPointF(razor_x - 5.0, content.y() - handle_h),
                QtCore.QPointF(razor_x + 5.0, content.y() - handle_h),
            ])
        )
        painter.end()


class RazorStripPanel(QtWidgets.QWidget):
    """序列剃刀接管面板（复合控件，决策 #109）：胶片条 + 切割处两侧帧预览 + 只读。

    作为 ``RazorCutParam`` 的接管控件（``widget_factory=make_razor_panel``）在
    节点 params 中声明即排版，替代旧面板 ``razor_strip=True`` 构造标志。

    接管控件契约：
    - ``changed`` —— 拖拽切割后发出（面板据此发 ``changed(values())``）；
    - ``values()`` / ``set_values(dict)`` —— 按参数名读写（``{"cut": int}``）；
    - ``gesture_begin`` / ``gesture_end`` —— 拖拽手势（上层折叠为一条撤销记录）；
    - ``set_frames`` —— 喂入上游序列帧路径（``data_source="sequence_frames"``）；
    - ``release_content`` —— 清空两侧预览与只读（**不误伤胶片条帧**：帧由
      ``set_frames`` 显式管理，与旧面板 release_preview 同语义）。
    """

    changed = QtCore.Signal()
    gesture_begin = QtCore.Signal()
    gesture_end = QtCore.Signal()

    SIDE_PREVIEW_SIZE = 200  # 两侧预览适配框边长

    def __init__(self, panel=None, param=None):
        super().__init__(None)
        self._panel = panel
        self.strip = RazorStripWidget()  # 胶片条画布（原交互控件）
        self.strip.cut_changed.connect(self._on_cut_changed)
        self.strip.gesture_begin.connect(self.gesture_begin)
        self.strip.gesture_end.connect(self.gesture_end)
        self.side_a, self.preview_a = self._side_preview("段A末帧")
        self.side_b, self.preview_b = self._side_preview("段B首帧")
        sides = QtWidgets.QHBoxLayout()
        sides.setContentsMargins(0, 0, 0, 0)
        sides.addWidget(self.side_a, 1)
        sides.addWidget(self.side_b, 1)
        sides_wrap = QtWidgets.QWidget()
        sides_wrap.setLayout(sides)
        self.readout = QtWidgets.QLabel("切割位置：—")
        self.readout.setWordWrap(True)
        self.readout.setStyleSheet("color:#ff8a8a;")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.strip)
        layout.addWidget(sides_wrap)
        layout.addWidget(self.readout)

    def _side_preview(self, caption: str) -> tuple[QtWidgets.QWidget, CheckerPreviewLabel]:
        """切割处一侧预览：标题 + 固定适配框（边长 ``SIDE_PREVIEW_SIZE``，不随分辨率放大）。

        与一般节点的预览框同语义（``ParameterPanel`` 固定框 + 只缩不放）：
        ``set_1to1(False)`` 关闭 1:1 跟随模式，否则高分帧会把框撑到素材
        像素尺寸 ÷ DPR（旧行为，见「剃刀节点预览框随分辨率变大」）。
        """
        column = QtWidgets.QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        cap = QtWidgets.QLabel(caption)
        cap.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet("color:#b8bdc7;")
        label = CheckerPreviewLabel("—")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        label.set_fit_box(self.SIDE_PREVIEW_SIZE, self.SIDE_PREVIEW_SIZE)
        # 适配模式：固定适配框，内容等比 contain（只缩不放）——与一般
        # 节点固定预览框一致，不随输入序列分辨率放大。
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

    def set_cut(self, cut: int) -> None:
        self.strip.set_cut(cut)
        self._refresh_ui()

    def cut(self) -> int:
        return self.strip.cut()

    def values(self) -> dict:
        return {"cut": self.cut()}

    def set_values(self, values: dict) -> None:
        if "cut" in values:
            self.set_cut(int(values["cut"]))

    def release_content(self) -> None:
        """清空两侧预览与只读（不误伤胶片条帧，帧由 set_frames 显式管理）。"""
        self.preview_a.clear_content()
        self.preview_b.clear_content()
        self.readout.setText("切割位置：—")

    # --- 内部 ---

    def _on_cut_changed(self, _cut: int) -> None:
        self._refresh_ui()
        self.changed.emit()

    def _refresh_ui(self) -> None:
        """刷新两侧帧预览（段A末帧/段B首帧）与切割位置只读（拖拽/程序化共用）。"""
        cut = self.strip.cut()
        total = self.strip.frame_count()
        frames = self.strip.frames()
        if not frames or not (1 <= cut <= total - 1):
            self.preview_a.clear_content()
            self.preview_b.clear_content()
            self.readout.setText("切割位置：—")
            return
        a_pixmap = QtGui.QPixmap(frames[cut - 1])  # 段A末帧（切割线左侧）
        b_pixmap = QtGui.QPixmap(frames[cut])      # 段B首帧（切割线右侧）
        if a_pixmap.isNull() or b_pixmap.isNull():
            self.preview_a.clear_content()
            self.preview_b.clear_content()
            self.readout.setText("切割位置：—")
            return
        self.preview_a.set_content(a_pixmap)
        self.preview_b.set_content(b_pixmap)
        self.readout.setText(
            f"切割位置：第 {cut} 帧后（段A 1..{cut} / 段B {cut + 1}..{total}）"
        )


def make_razor_panel(panel, param):
    """``RazorCutParam`` 的控件工厂（供 ``TakeoverParam.widget_factory`` 引用）。"""
    return RazorStripPanel(panel, param)
