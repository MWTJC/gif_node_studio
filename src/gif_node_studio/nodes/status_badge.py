"""节点状态角标：画在节点右上角的圆标识（替代参数面板里的状态文字按钮）。

状态固定四种（以稳定性为重：任何节点、任何时刻都有角标，状态只在这四种间流转）：

- ``dirty``              灰底 + 问号（未运行 / 待运行；排队也归入这一态）
- ``running``            黄底 + 扳手
- ``clean``              绿底 + 勾
- ``error``              红底 + 叉

图形用 qtawesome 渲染（与节点图标同一套字体资源），不手绘路径：字形、笔画、
留白都由图标库决定，改图标只改表里的一行名字。

实现要点：
- 作为 ``StudioNodeItem`` 的**子项**（与分类色条同机制），不用改
  ``paint()``——本库版本里 ``paint()`` 中 ``super()`` 之后的绘制不生效。
- ``NoButton``：不吃鼠标事件，节点在该角仍可拖拽；状态细节走 tooltip。
- 只重绘自身 ``boundingRect``；无动画、无定时器（状态变化才重绘一次）。
- 字形 pixmap 按 3 倍超采样缓存，缩放画布时不糊。
"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

# 状态表：唯一来源（面板只保留运行键外观，不再显示状态文字）。
# 值 = (tooltip 文案, 底色, 字形色, qtawesome 图标名)
STATUS_STYLE: dict[str, tuple[str, str, str, str]] = {
    "dirty": ("未运行", "#5C6470", "#FFFFFF", "fa6s.question"),
    "running": ("运行中", "#F0B33C", "#2A1B00", "fa6s.wrench"),
    "clean": ("完成", "#4FC97E", "#06240F", "fa6s.check"),
    "error": ("错误", "#E4574F", "#FFFFFF", "fa6s.xmark"),
}

BADGE_D = 60.0      # 角标直径（逻辑像素，随画布缩放）：与标题图标可见底板 60 同尺寸
BADGE_RING = "#0E1014"  # 角标外描边：悬在画布上的一半需要一圈暗边与背景分开
GLYPH_RATIO = 0.62  # 字形占直径的比例（qtawesome 自带留白，0.62 视觉上最接近图标）
GLYPH_SS = 3        # 字形超采样倍数

_GLYPH_CACHE: dict[tuple[str, str, int], QtGui.QPixmap] = {}


def _glyph_pixmap(name: str, color: str, size: int) -> QtGui.QPixmap:
    """按 (图标名, 颜色, 像素尺寸) 缓存字形 pixmap（qtawesome 渲染开销不低）。"""
    key = (name, color, size)
    pix = _GLYPH_CACHE.get(key)
    if pix is None:
        import qtawesome as qta

        pix = qta.icon(name, color=color).pixmap(size, size)
        _GLYPH_CACHE[key] = pix
    return pix


class StatusBadge(QtWidgets.QGraphicsItem):
    """节点右上角状态圆标识。父项为 ``StudioNodeItem``（坐标与节点体一致）。"""

    def __init__(self, parent: QtWidgets.QGraphicsItem):
        super().__init__(parent)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(True)
        self.setZValue(60)  # 面板 proxy（z=0）与端口之上
        self._state = "dirty"
        self._detail = ""
        self._rect = QtCore.QRectF()
        self.sync_position()
        self.setToolTip(STATUS_STYLE["dirty"][0])

    # --- 几何 ---

    def sync_position(self) -> None:
        """跟随节点体尺寸重算位置（节点变宽/变高后由 ``draw_node`` 调用）。

        圆心压在节点右上角点上：一半落在标题栏右端的空位，一半悬在画布上，
        因此「角标 = 标题图标尺寸」也不会压住参数面板；第一行端口另有
        ``StudioNodeItem.PORT_TOP_PAD`` 整体下移让位。
        """
        parent = self.parentItem()
        body = parent.boundingRect() if parent is not None else QtCore.QRectF()
        self.prepareGeometryChange()
        half = BADGE_D / 2.0
        self._rect = QtCore.QRectF(
            body.right() - half,
            body.top() - half,
            BADGE_D,
            BADGE_D,
        )
        self.update()

    def boundingRect(self) -> QtCore.QRectF:
        return self._rect.adjusted(-1.0, -1.0, 1.0, 1.0)

    # --- 状态 ---

    def state(self) -> str:
        return self._state

    def set_state(self, state: str, detail: str = "") -> None:
        if state not in STATUS_STYLE:
            state = "dirty"
        if (state, detail) == (self._state, self._detail):
            return
        self._state, self._detail = state, detail
        label = STATUS_STYLE[state][0]
        self.setToolTip(f"{label}：{detail}" if detail else label)
        self.update()

    # --- 绘制 ---

    def paint(self, painter, option, widget=None) -> None:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        _label, bg, glyph_color, icon = STATUS_STYLE[self._state]
        r = self._rect
        c = r.center()
        rad = r.width() / 2.0

        ring = QtGui.QPen(QtGui.QColor(BADGE_RING))
        ring.setWidthF(1.2)
        painter.setPen(ring)
        painter.setBrush(QtGui.QColor(bg))
        painter.drawEllipse(c, rad - 0.6, rad - 0.6)

        glyph = BADGE_D * GLYPH_RATIO
        size = int(round(glyph * GLYPH_SS))
        pix = _glyph_pixmap(icon, glyph_color, size)
        if not pix.isNull():
            painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(
                QtCore.QRectF(c.x() - glyph / 2, c.y() - glyph / 2, glyph, glyph),
                pix,
                QtCore.QRectF(pix.rect()),
            )
