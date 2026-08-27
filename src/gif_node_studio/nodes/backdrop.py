"""背景框（分组框）：在 NodeGraphQt 内建 BackdropNode 之上增加「标题就地重命名」。

NodeGraphQt 内建背景框的标题只能弹窗重命名：``BackdropNodeItem.mouseDoubleClickEvent``
恒发 ``node_double_clicked`` 信号（早期版本由应用弹 ``QInputDialog`` 处理）。
而普通节点（``NodeItem``）支持双击标题无弹窗就地编辑（``NodeTextItem``）。
本模块提供与 ``NodeItem`` 同机制的项目版背景框，弹窗路径已整体删除：

- ``InlineEditableBackdropNodeItem``：给 ``BackdropNodeItem`` 增加标题 ``NodeTextItem``，
  双击标题栏（顶部 26px）进入就地编辑（回车/失焦提交、Esc 取消，与普通节点一致）；
  标题栏之外双击仍按库行为发射 ``node_double_clicked``，但应用不再连接该信号
  （弹窗路径已删除），右键菜单「重命名背景框」也改为调用 ``begin_title_edit()``
  触发就地编辑。
- ``EditableBackdropNode``：项目版背景框节点，视图即上述图形项。其 ``type_``
  （= ``__identifier__.__name__``）指回内建的 ``nodeGraphQt.nodes.BackdropNode``，
  注册进工厂时替换内建注册——旧存档按 ``type_`` 反序列化时同样获得就地重命名能力。
  节点列表图标与普通节点同机制：``backdrop_definition().icon``（决策 #111 单一
  图标源）由节点库按钮 ``LibraryButton`` 读取显示；画布上背景框不再绘制图标。
  本节点新增两处画布特性（属性面板 Backdrop 标签页可调，存档可持久化）：
  - ``title_bar_height``（QSPIN_BOX，14–120，默认 26）——标题栏高度，绘制/标题
    对齐/双击判定都按它；
  - 大字体标题背景纹理——把标题（``name``）以低透明度大字号绘制在框内正中，
    作为背景水印（替代裸背景的视觉留白，框越大字体越大）。
- ``backdrop_definition()`` / ``BACKDROP_HELP``：背景框的定义与说明，供上层
  （节点库按钮悬停显示、create_backdrop 用标题作节点名）导入使用——背景框的
  全部定义收归本模块，不再散落在 ui.py。定义**惰性构造**（图标需 QApplication，
  见模块内注释，不能在导入期构建）。

绘制说明：库的 ``paint()`` 用 ``painter.drawText`` 直接绘制标题，标题文本项与它
无法共存（否则双重渲染）；因此本项目版重写 ``paint()`` 为库实现的副本、去掉标题
绘制那一行，标题改由 ``NodeTextItem`` 承载（与 ``NodeItem`` 的标题行为完全一致）。
"""

from __future__ import annotations

from NodeGraphQt.constants import NodeEnum, NodePropWidgetEnum
from NodeGraphQt.nodes.backdrop_node import BackdropNode
from NodeGraphQt.qgraphics.node_abstract import AbstractNodeItem
from NodeGraphQt.qgraphics.node_backdrop import BackdropNodeItem
from NodeGraphQt.qgraphics.node_text_item import NodeTextItem
from PySide6 import QtCore, QtGui, QtWidgets

from ..core.color_tokens import DARK, OKLCH_ANCHORS, mix, parse_hex
from .definitions import NodeCategory, NodeDefinition
from .icon_resource import category_icon

# 背景框定义与说明：由「节点库按钮」与 create_backdrop 共享（按钮悬停显示说明、
# 创建时用其标题作节点名），故作为本模块的公共 API；节点类型字符串在
# create_backdrop 内由 EditableBackdropNode.type_ 派生，不在这里维护。
#
# 背景框定义**不在模块导入期构造**（backdrop_definition 惰性构建）：其图标是
# qtawesome 叠加图标（category_icon → qta.icon），而 qtawesome 在无 QApplication
# 时字体/字形表加载失败且单例永久污染（charmap 为空，之后任何 icon 调用都抛
# “Invalid font prefix”）。节点库/创建路径都在 QApplication 建立后运行，故由
# 调用方在运行期取定义（首次调用构造并缓存）。
BACKDROP_HELP = (
    "在画布上放置一个可缩放、可命名的背景框（分组框），用于把若干节点框在一起便于阅读。"
    "创建时若已有节点处于选中状态，背景框尺寸自动覆盖选中节点范围；"
    "双击背景框标题栏可就地重命名标题；右下角拖拽手柄可调整大小，双击该手柄自动贴合内部节点。"
)

_backdrop_definition: "NodeDefinition | None" = None


def backdrop_definition() -> "NodeDefinition":
    """背景框定义（惰性构造+缓存）：图标构建需要 QApplication，首次调用时创建。

    ``NodeDefinition`` 的 ``icon`` 是必填字段且必须是非空 QIcon（决策 #111），
    但 ``qta.icon`` 不能在模块导入期调用——见本模块头注释。调用方保证在
    QApplication 建立后调用（MainWindow 构造/创建背景框时）。
    """
    global _backdrop_definition
    if _backdrop_definition is None:
        _backdrop_definition = NodeDefinition(
            kind="backdrop",
            title="背景框",
            category=NodeCategory.BACKDROP,
            # 节点列表图标与普通节点同机制（决策 #111）：glyph 直接写在定义处，
            # 想换图样改这一行即可（占位字形 fa6s.folder 的显式替代）。
            icon=category_icon(NodeCategory.BACKDROP, "mdi6.vector-square"),
        )
    return _backdrop_definition

# 标题栏高度：与库 paint() 里 top_rect 的高度一致（26.0）。
_TITLE_BAR_HEIGHT = 26.0
# 标题栏内左右留白：与库 paint() 里标题文字的水平边距一致。
_TITLE_H_MARGIN = 5.0
# 标题栏高度可调范围（属性面板 Backdrop 标签页 QSPIN_BOX 的 range）。
TITLE_BAR_HEIGHT_MIN = 14
TITLE_BAR_HEIGHT_MAX = 120
# 大字体标题背景纹理：标题文字颜色保留 RGB、alpha 压到该值（低透明度水印）。
_TEXTURE_ALPHA = 42
# 纹理字号 = 框短边 × 该系数（clamp 到 [28, 260]），框越大字体越大。
_TEXTURE_FONT_RATIO = 0.22
_TEXTURE_FONT_MIN = 28.0
_TEXTURE_FONT_MAX = 260.0


class InlineEditableBackdropNodeItem(BackdropNodeItem):
    """支持标题就地重命名的背景框图形项。

    与 ``NodeItem`` 相同的标题机制：标题由 ``NodeTextItem`` 承载（默认显示、
    双击进入编辑、回车/失焦提交、Esc 取消），``name``/``text_color``/尺寸变化时
    同步文本项。双击命中标题栏（顶部 ``_TITLE_BAR_HEIGHT`` 区域）即进入编辑并
    吞掉事件，不发射 ``node_double_clicked``；标题栏之外双击不再有任何弹窗路径
    （应用层只保留 ``begin_title_edit()`` 这一种重命名入口）。
    """

    def __init__(self, name="backdrop", text="", parent=None):
        super().__init__(name, text, parent)
        # 标题栏高度（节点属性 title_bar_height 同步写入，默认与库 26.0 一致）。
        self._title_bar_height = _TITLE_BAR_HEIGHT
        self._title_item = NodeTextItem(self.name, self)
        self._title_item.setDefaultTextColor(QtGui.QColor(*self.text_color))
        self._align_title_item()

    def begin_title_edit(self):
        """进入标题就地编辑（右键菜单「重命名背景框」调用；等价于双击标题栏）。"""
        self._title_item.set_editable(True)
        self._title_item.setFocus()

    # --- 标题栏高度属性（节点属性 title_bar_height 经 setattr 同步写入） ---

    @property
    def title_bar_height(self) -> float:
        return self._title_bar_height

    @title_bar_height.setter
    def title_bar_height(self, value) -> None:
        """高度变化：更新绘制高度并重排标题、重绘（属性面板/读档/右键菜单共用）。"""
        value = float(int(value))
        if value != self._title_bar_height:
            self._title_bar_height = value
            self._align_title_item()
            self.update()

    # --- 标题文本项同步（与 NodeItem 的 name/text_color 处理一致） ---

    @AbstractNodeItem.name.setter
    def name(self, name=""):
        AbstractNodeItem.name.fset(self, name)
        if self._title_item.toPlainText() != name:
            self._title_item.setPlainText(name)
        # 标题文字高度随文本/宽度变化（换行、字号度量），改名后重新对齐标题栏。
        self._align_title_item()
        self.update()

    @AbstractNodeItem.text_color.setter
    def text_color(self, color=(100, 100, 100, 255)):
        AbstractNodeItem.text_color.fset(self, color)
        self._title_item.setDefaultTextColor(QtGui.QColor(*color))
        self.update()

    @BackdropNodeItem.width.setter
    def width(self, width=0.0):
        BackdropNodeItem.width.fset(self, width)
        self._align_title_item()

    @BackdropNodeItem.height.setter
    def height(self, height=0.0):
        BackdropNodeItem.height.fset(self, height)
        self._align_title_item()

    def _align_title_item(self):
        """标题文本项：水平左对齐于标题栏、垂直居中于标题栏（高度可调）。"""
        bar_h = self._title_bar_height
        self._title_item.setTextWidth(max(self._width - _TITLE_H_MARGIN * 2, 1.0))
        height = self._title_item.boundingRect().height()
        self._title_item.setPos(_TITLE_H_MARGIN, (bar_h - height) / 2.0)

    # --- 交互 ---

    def mouseDoubleClickEvent(self, event):
        """标题栏内双击 → 就地编辑标题；标题栏之外 → 不再弹窗（信号无人处理）。

        与 ``NodeItem.mouseDoubleClickEvent`` 相同机制：命中标题时进入编辑并
        ``event.ignore()`` 返回（不发射 ``node_double_clicked``）。这里按标题栏
        区域（而非仅文本像素）判断，保证双击标题栏空白处同样进入编辑；标题文字
        上的双击由 ``NodeTextItem`` 自身先处理（同样进入编辑），随后事件继续
        传播到本处，行为一致。标题栏之外双击仍按库行为发射
        ``node_double_clicked``，但应用层不再连接该信号（弹窗重命名已删除）。
        """
        if event.button() == QtCore.Qt.LeftButton and event.pos().y() <= self._title_bar_height:
            self._title_item.set_editable(True)
            self._title_item.setFocus()
            event.ignore()
            return
        super().mouseDoubleClickEvent(event)

    def paint(self, painter, option, widget=None):
        """库 ``BackdropNodeItem.paint`` 的副本，改动两处：

        - 标题 ``drawText`` 一行删除——标题改由 ``self._title_item``（NodeTextItem）
          承载，若保留会造成文字双重渲染；
        - 标题栏高度用 ``self._title_bar_height``（属性可调，默认 26.0），
          并在框内正中追加**大字体标题背景纹理**（低透明度水印，字号随框缩放）。
        其余绘制与库实现逐行一致（nodegraphqt 0.6.44）。
        """
        painter.save()
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(QtCore.Qt.NoBrush)

        margin = 1.0
        rect = self.boundingRect()
        rect = QtCore.QRectF(
            rect.left() + margin,
            rect.top() + margin,
            rect.width() - (margin * 2),
            rect.height() - (margin * 2),
        )

        radius = 2.6
        color = (self.color[0], self.color[1], self.color[2], 50)
        painter.setBrush(QtGui.QColor(*color))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(rect, radius, radius)

        bar_h = self._title_bar_height
        top_rect = QtCore.QRectF(rect.x(), rect.y(), rect.width(), bar_h)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(*self.color)))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(top_rect, radius, radius)
        for pos in [top_rect.left(), top_rect.right() - 5.0]:
            painter.drawRect(
                QtCore.QRectF(pos, top_rect.bottom() - 5.0, 5.0, 5.0))

        if self.backdrop_text:
            painter.setPen(QtGui.QColor(*self.text_color))
            txt_rect = QtCore.QRectF(
                top_rect.x() + 5.0, top_rect.height() + 3.0,
                rect.width() - 5.0, rect.height())
            painter.drawText(
                txt_rect,
                QtCore.Qt.AlignLeft | QtCore.Qt.TextWordWrap,
                self.backdrop_text,
            )

        # 大字体标题背景纹理：标题以低透明度大字号绘制在框内正中（背景水印）。
        # 字号随框短边缩放（clamp 28–260），标题为空时跳过。
        if self.name:
            painter.save()
            r, g, b, _ = self.text_color
            painter.setPen(QtGui.QColor(r, g, b, _TEXTURE_ALPHA))
            font = painter.font()
            short = min(rect.width(), max(rect.height() - bar_h, 1.0))
            font.setPointSizeF(
                max(_TEXTURE_FONT_MIN, min(short * _TEXTURE_FONT_RATIO, _TEXTURE_FONT_MAX))
            )
            painter.setFont(font)
            body_rect = QtCore.QRectF(
                rect.x(), rect.y() + bar_h,
                rect.width(), max(rect.height() - bar_h, 1.0),
            )
            painter.drawText(
                body_rect,
                QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap,
                self.name,
            )
            painter.restore()

        if self.selected:
            sel_color = [x for x in NodeEnum.SELECTED_COLOR.value]
            sel_color[-1] = 15
            painter.setBrush(QtGui.QColor(*sel_color))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawRoundedRect(rect, radius, radius)

        # （库实现此处用 painter.drawText 绘制标题；标题改由 _title_item 承载，已删除。）

        border = 0.8
        border_color = self.color
        if self.selected and NodeEnum.SELECTED_BORDER_COLOR.value:
            border = 1.0
            border_color = NodeEnum.SELECTED_BORDER_COLOR.value
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.setPen(QtGui.QPen(QtGui.QColor(*border_color), border))
        painter.drawRoundedRect(rect, radius, radius)

        painter.restore()


class EditableBackdropNode(BackdropNode):
    """项目版背景框：标题支持就地重命名（内建 BackdropNode 仅弹窗）。

    与内建类保持同一 ``type_`` 字符串（见模块 docstring 与下方 ``__name__``
    指回），因此注册进工厂时替换内建注册，新建与旧存档反序列化都得到该能力。
    额外声明 ``title_bar_height`` 属性（Backdrop 标签页 QSPIN_BOX）——标题栏
    高度可调且随存档持久化；``set_property`` 同步到视图重排/重绘。
    """

    def __init__(self, qgraphics_views=None):
        super().__init__(qgraphics_views or InlineEditableBackdropNodeItem)
        # 背景框颜色收编（决策 #118 方案 C）：库默认青 (5,129,138) 未纳入配色——
        # 标题栏/边框/框体填充改由 backdrop 锚点（低彩度蓝灰）压向背景的
        # 深色变体派生（低调容器，不抢节点风头）。set_color 走 set_property，
        # 与普通节点一致；背景框 color 同样被会话剥离，新旧存档兼容。
        _r, _g, _b = parse_hex(mix(OKLCH_ANCHORS["backdrop"], DARK.bg, 0.82))
        self.set_color(_r, _g, _b)
        # 标题栏高度可调属性（与 backdrop_text 同 tab，随存档持久化）。
        self.create_property(
            "title_bar_height",
            int(_TITLE_BAR_HEIGHT),
            range=(TITLE_BAR_HEIGHT_MIN, TITLE_BAR_HEIGHT_MAX),
            widget_type=NodePropWidgetEnum.QSPIN_BOX.value,
            widget_tooltip="背景框标题栏高度（像素）",
            tab="Backdrop",
        )
        self.view.title_bar_height = int(_TITLE_BAR_HEIGHT)

    def set_property(self, name, value, push_undo=True):
        """标题栏高度属性同步到视图（重排标题并重绘），其余走库行为。"""
        super().set_property(name, value, push_undo)
        if name == "title_bar_height" and self.view is not None:
            self.view.title_bar_height = int(value)


# NodeGraphQt 以「__identifier__.__name__」作为节点类型字符串，会话存档与
# 反序列化都按它查工厂。把子类的 __name__ 指回内建名，使 type_ 保持
# "nodeGraphQt.nodes.BackdropNode"——旧存档加载时同样创建本项目版背景框。
EditableBackdropNode.__name__ = "BackdropNode"
