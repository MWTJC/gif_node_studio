"""领域无关节点基座：StudioNode（声明/执行/UI 工厂）、StudioNodeItem、EmbeddedPanelWidget。

具体节点类见 input_nodes / manifest_nodes / sequence_nodes / process_nodes /
channel_nodes / export_nodes / analysis_nodes；注册表见 registry。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from NodeGraphQt import BaseNode, NodeBaseWidget
from NodeGraphQt.qgraphics.node_base import NodeItem
from PySide6 import QtCore, QtGui, QtWidgets

from ..core.color_tokens import DARK, PORT_ANCHORS, parse_hex
from ..media.backend import MediaBackend
from ..media.media_info import default_describe_output
from .definitions import NodeDefinition, PortType
from .icon_resource import category_color
from .parameter_panel import ParameterPanel

# ---------------------------------------------------------------------------
# 标题栏外观开关（开发对比用，节点创建时读取，改动后重启生效）
# ---------------------------------------------------------------------------
# 分类色条（节点左缘强调竖条）总开关：False = 完全隐藏竖条效果，标题/图标
# 的左偏移随之紧凑（_TITLE_LEFT 与 _align_icon_horizontal 同源判断）。
CATEGORY_COLOR_BAR_ENABLED = True

# 节点体圆角半径 / 内边距：与 NodeGraphQt ``NodeItem._paint_horizontal`` 的
# ``radius=4.0`` / ``margin=1.0`` 保持一致——色条路径按同一圆角裁剪，
# 保证「竖条圆角 = 节点体圆角」。
_NODE_RADIUS = 4.0
_NODE_MARGIN = 1.5

class EmbeddedPanelWidget(NodeBaseWidget):
    def __init__(self, panel: ParameterPanel, parent=None):
        super().__init__(parent, name="node_parameters", label="")
        self.panel = panel
        self.set_custom_widget(panel)

    def sync_geometry(self) -> None:
        """Propagate a changing QWidget size through the proxy to NodeGraphQt."""
        self.panel.updateGeometry()
        self.panel.adjustSize()
        container = self.widget()
        if container is not None:
            container.layout().invalidate()
            container.layout().activate()
            container.adjustSize()
            self.resize(container.sizeHint())
        if self.node is not None:
            self.node.view.prepareGeometryChange()
            self.node.view.draw_node()

    def get_value(self):
        return self.panel.values()

    def set_value(self, value):
        self.panel.set_values(value or {})




class StudioNodeItem(NodeItem):
    """NodeGraphQt 图形项：修正两处 0.6.44 默认布局问题。

    - 节点宽度由内容（端口/内嵌 widget）决定：标题只有在比整体内容更宽时
      才撑大节点，而不是把标题宽度无条件累加进总宽（否则标题每多一个字，
      节点就等宽变宽一点，尽管标题两侧还有大量空白）；
    - 内嵌 widget 水平方向恒居中，即使节点只有输入或只有输出
      （NodeGraphQt 默认把 widget 贴到无端口的一侧，造成视觉偏移）；
    - 内嵌组件的尺寸/对齐计算不受视野缩放（proxy 模式）隐藏的影响：
      NodeGraphQt 缩小视图时会隐藏内嵌组件，但那只是显示层降级，组件的
      真实几何（boundingRect）依然存在；若尺寸计算按 isVisible() 跳过它们，
      运行节点/参数变更/预览刷新在缩小时触发 draw_node() 会把节点板缩成
      「标题+端口」的极小尺寸（160×60），放大视图后组件重新显示、节点板
      却无法恢复（本 bug 根因）。

    通过 ``StudioNode.__init__`` 的 ``qgraphics_item=`` 注入，属于
    NodeGraphQt 官方的继承扩展点，不需要 monkeypatch。
    """

    # 标题栏：分类色条 + 图标 + 标题的几何常量（图标从色条右侧开始，标题再右移让位）。
    # TITLE_SCALE：标题栏整体尺寸倍率（图标 + 字体 + 间距统一缩放，用户反馈
    # 原尺寸偏小；1.0 = 原尺寸 18px 图标）。节点创建时读取，改动后重启生效。
    TITLE_SCALE = 1.2
    COLOR_BAR_W = 5
    ICON_PAD = round(5 * TITLE_SCALE)    # 色条与图标间距
    ICON_W = round(58 * TITLE_SCALE)     # 图标尺寸（与 NodeEnum.ICON_SIZE 同源，按倍率缩放）
    TEXT_GAP = round(6 * TITLE_SCALE)    # 图标与标题间距
    # 标题左偏移：色条关闭时省去色条占位（与 _align_icon_horizontal 同源判断）。
    _TITLE_LEFT = (COLOR_BAR_W + ICON_PAD if CATEGORY_COLOR_BAR_ENABLED else ICON_PAD) + ICON_W + TEXT_GAP

    def post_init(self, viewer=None, pos=None):
        """库在节点加入场景时会把内置图标重置为默认（实测 _properties['icon']
        被清空）；且在本库版本里 ``paint()`` 中 ``super()`` 之后的绘制不生效。
        故分类图标与色条都改为**子项**（与端口/标题同机制，可靠渲染）：
        - 图标：把分类图标 pixmap 设到内置 ``_icon_item``（子项）；
        - 色条：``_sync_color_bar`` 创建 QGraphicsPathItem（分类主色），路径
          = 节点体圆角矩形 ∩ 左缘竖条（圆角跟随节点体）。

        标题字体按 ``TITLE_SCALE`` 在布局计算前缩放：尺寸/对齐（
        ``_calc_size_horizontal`` / ``_align_icon_horizontal`` /
        ``_align_label_horizontal``）都基于缩放后的实际尺寸。
        """
        _icon = getattr(self, "_node_icon", None)
        if _icon is not None and not _icon.isNull():
            self._icon_item.setPixmap(
                _icon.scaledToHeight(self.ICON_W, QtCore.Qt.SmoothTransformation)
            )
        font = self._text_item.font()
        font.setPointSizeF(max(1.0, font.pointSizeF() * self.TITLE_SCALE))
        self._text_item.setFont(font)
        super().post_init(viewer, pos)

    def draw_node(self):
        """节点重排后同步色条路径（尺寸变化/圆角跟随节点体）。"""
        super().draw_node()
        self._sync_color_bar()

    def _sync_color_bar(self) -> None:
        """创建/更新分类色条子项：路径 = 节点体圆角矩形 ∩ 左缘竖条。

        - 圆角跟随：节点体用 radius=4/margin=1 圆角矩形（NodeGraphQt
          ``_paint_horizontal``），竖条路径取与它的交集，顶端/底端圆角与
          节点体一致，消除「节点圆角、竖条直角」的割裂感（原实现为直角
          QGraphicsRectItem，且只在 post_init 定高一次，节点变高后残留）；
        - 尺寸跟随：每次 ``draw_node`` 重算路径，节点变高/变矮时竖条
          同步伸缩；
        - 总开关：``CATEGORY_COLOR_BAR_ENABLED=False`` 时不创建/隐藏。
        """
        if not CATEGORY_COLOR_BAR_ENABLED:
            bar = getattr(self, "_color_bar_item", None)
            if bar is not None:
                bar.setVisible(False)
            return
        color = getattr(self, "_category_color", None)
        if color is None:
            return
        bar = getattr(self, "_color_bar_item", None)
        if bar is None:
            bar = QtWidgets.QGraphicsPathItem(QtGui.QPainterPath(), self)
            bar.setBrush(QtGui.QBrush(QtGui.QColor(*color)))
            bar.setPen(QtCore.Qt.NoPen)
            bar.setZValue(-1)  # 节点体之上、端口/标题之下
            self._color_bar_item = bar
        rect = self.boundingRect()
        body = QtGui.QPainterPath()
        body.addRoundedRect(
            QtCore.QRectF(
                rect.left() + _NODE_MARGIN,
                rect.top() + _NODE_MARGIN,
                rect.width() - 2 * _NODE_MARGIN,
                rect.height() - 2 * _NODE_MARGIN,
            ),
            _NODE_RADIUS,
            _NODE_RADIUS,
        )
        strip = QtGui.QPainterPath()
        # 竖条外框 = margin + COLOR_BAR_W：与节点体求交后，落在节点体上的
        # 可见宽度恰为 COLOR_BAR_W（左侧 margin 宽被圆角矩形外边裁掉）。
        strip.addRect(
            QtCore.QRectF(
                rect.left(), rect.top(),
                _NODE_MARGIN + self.COLOR_BAR_W, rect.height(),
            )
        )
        bar.setPath(body.intersected(strip))
        bar.setVisible(True)

    def _align_icon_horizontal(self, h_offset, v_offset):
        """图标水平定位到分类色条右侧（库默认 left+2 会压住自绘色条，右移让位）。"""
        icon_rect = self._icon_item.boundingRect()
        text_rect = self._text_item.boundingRect()
        x = self.boundingRect().left() + (self.COLOR_BAR_W if CATEGORY_COLOR_BAR_ENABLED else 0) + self.ICON_PAD
        y = text_rect.center().y() - (icon_rect.height() / 2)
        self._icon_item.setPos(x + h_offset, y + v_offset)

    def _align_label_horizontal(self, h_offset, v_offset):
        """标题左对齐到图标右侧（库默认居中会与/远离图标；左对齐更像标题栏表头）。"""
        rect = self.boundingRect()
        x = rect.left() + self._TITLE_LEFT
        self._text_item.setPos(x + h_offset, rect.y() + v_offset)

    def _widget_laid_out(self, widget) -> bool:
        """内嵌组件是否参与布局（尺寸计算/对齐）。

        非 proxy 模式维持原语义（显式隐藏的组件不参与布局）；
        proxy 模式下组件是被视野缩放隐藏的，仍必须按真实内容参与布局。
        """
        return self._proxy_mode or widget.isVisible()

    def _port_text_laid_out(self, port, text) -> bool:
        """端口标签是否参与宽度计算。

        非 proxy 模式维持原语义（不可见的标签不计）；
        proxy 模式下标签是被视野缩放隐藏的（draw_node 里还会按
        ``display_name`` 重新显示），宽度计算须按完整内容计。
        """
        return text.isVisible() or (self._proxy_mode and port.display_name)

    def _calc_size_horizontal(self):
        # width, height from node name text.
        text_w = self._text_item.boundingRect().width()
        text_h = self._text_item.boundingRect().height()

        # width, height from node ports.
        port_width = 0.0
        p_input_text_width = 0.0
        p_output_text_width = 0.0
        p_input_height = 0.0
        p_output_height = 0.0
        for port, text in self._input_items.items():
            if not port.isVisible():
                continue
            if not port_width:
                port_width = port.boundingRect().width()
            t_width = text.boundingRect().width()
            if self._port_text_laid_out(port, text) and t_width > p_input_text_width:
                p_input_text_width = text.boundingRect().width()
            p_input_height += port.boundingRect().height()
        for port, text in self._output_items.items():
            if not port.isVisible():
                continue
            if not port_width:
                port_width = port.boundingRect().width()
            t_width = text.boundingRect().width()
            if self._port_text_laid_out(port, text) and t_width > p_output_text_width:
                p_output_text_width = text.boundingRect().width()
            p_output_height += port.boundingRect().height()

        port_text_width = p_input_text_width + p_output_text_width

        # width, height from node embedded widgets.
        widget_width = 0.0
        widget_height = 0.0
        for widget in self._widgets.values():
            if not self._widget_laid_out(widget):
                continue
            w_width = widget.boundingRect().width()
            w_height = widget.boundingRect().height()
            if w_width > widget_width:
                widget_width = w_width
            widget_height += w_height

        side_padding = 0.0
        if all([widget_width, p_input_text_width, p_output_text_width]):
            port_text_width = max([p_input_text_width, p_output_text_width])
            port_text_width *= 2
        elif widget_width:
            side_padding = 10

        # 修复点：标题只在与“端口标签 + widget”整体比较时更宽才撑大节点。
        # 原实现是 port_width + max(标题, 端口标签) + padding + widget_width，
        # 标题宽度被无条件累加，导致节点宽随标题长度 1:1 增长。
        # 图标集成后：标题左移 offset（色条+图标+间距），标题宽度与“内容”
        # 比较时需加上该偏移，避免标题在极窄节点里溢出右缘。
        content_width = port_text_width + widget_width
        width = port_width + max([self._TITLE_LEFT + text_w, content_width]) + side_padding
        height = max([text_h, p_input_height, p_output_height, widget_height])
        if widget_height:
            height += 4.0
        height *= 1.05
        return width, height

    def _align_widgets_horizontal(self, v_offset):
        if not self._widgets:
            return
        rect = self.boundingRect()
        y = rect.y() + v_offset
        for widget in self._widgets.values():
            if not self._widget_laid_out(widget):
                continue
            widget_rect = widget.boundingRect()
            # 修复点：恒居中。原实现是 仅输出→贴左、仅输入→贴右、
            # 两侧都有→居中，导致单侧端口节点的 widget 明显偏移。
            x = rect.center().x() - (widget_rect.width() / 2)
            widget.widget().setTitleAlign("center")
            widget.setPos(x, y)
            y += widget_rect.height()




class StudioNode(BaseNode, ABC):
    """Base for one cohesive node type: declaration, UI factory and algorithm.

    Mirrors the ``TestModule``: every concrete subclass declares its
    whole definition inline and passes it to ``super().__init__(
    definition=..., help=...)`` — the parameterless-construction equivalent of
    ``super().__init__(test_type=..., name=..., ...)``. This base stores the
    declaration on ``self`` and provides the shared NodeGraphQt machinery.
    """

    __identifier__ = "anime_gif"
    NODE_NAME = "媒体节点"
    # 端口数据语义色（决策 #118 方案 C）：收编自 color_tokens.PORT_ANCHORS
    # （暖=素材流红橙、冷=序列流蓝），不再硬编码；管线色连接时取同源值。
    PORT_COLORS: ClassVar[dict[PortType, tuple[int, int, int]]] = {
        PortType.MANIFEST: parse_hex(PORT_ANCHORS["manifest"]),
        PortType.SEQUENCE: parse_hex(PORT_ANCHORS["sequence"]),
    }

    def __init__(self, definition: NodeDefinition, *, help: str = ""):
        super().__init__(qgraphics_item=StudioNodeItem)
        self.definition = definition
        self.help = help
        # NodeGraphQt registers and titles nodes from the class-level NODE_NAME;
        # shadow it with the concrete title so newly created nodes are named
        # after their declaration instead of the base default.
        self.NODE_NAME = definition.title
        for port in self.definition.inputs:
            self.add_input(port.name, display_name=port.show_name, color=self.PORT_COLORS[port.type])
        for port in self.definition.outputs:
            self.add_output(port.name, display_name=port.show_name, color=self.PORT_COLORS[port.type])
        # 方案 A 节点壳色（决策 #117）：节点体/边框统一读 DARK，与画布形成
        # 亮度阶梯；存档已剥离 color/border_color（session.py），新旧存档
        # 兼容（加载时 model 保持本处设置值，不被旧存档覆盖）。
        _r, _g, _b = parse_hex(DARK.node)
        self.set_color(_r, _g, _b)
        self.view.border_color = (*parse_hex(DARK.node_border), 255)
        self.params = self.definition.default_params()
        for name, value in self.params.items():
            self.create_property(name, value)
        self.panel = self.create_panel()
        self.embedded = EmbeddedPanelWidget(self.panel)
        self.add_custom_widget(self.embedded)
        self.embedded.setParentItem(self.view)
        self._sync_pending = False
        self.panel.geometry_changed.connect(self._schedule_sync_geometry)
        self.embedded.sync_geometry()
        self.output_data = None
        self.dirty = True
        self.revision = 0
        self.preview_output = None
        self.output_metadata: dict[str, Any] = {}
        self.last_elapsed_seconds: float | None = None
        self.set_status("dirty")
        # 标题栏：分类色条颜色 + 分类图标。
        # 库的 icon 属性在场景重加节点（post_init/draw_node）时会被重置为默认
        # 图标，故不依赖它；图标 QIcon 来自节点单一定义（definition.icon，
        # 决策 #111），本构造期预渲染 2x pixmap 写入 _node_icon，待节点加入
        # 场景时由 StudioNodeItem.post_init 设到内置 _icon_item（子项）渲染。
        self.view._category_color = category_color(self.definition.category)
        self.view._node_icon = self.definition.icon.pixmap(
            QtCore.QSize(self.view.ICON_W * 2, self.view.ICON_W * 2)
        )

    @property
    def KIND(self) -> str:
        return self.definition.kind

    def _schedule_sync_geometry(self) -> None:
        """合并几何变更：同一事件循环批次内多次 geometry_changed 只触发一次
        sync_geometry。

        GIF 播放逐帧 emit geometry_changed（尺寸恒定但信号每帧都发），若每帧
        都 singleShot 一次，事件循环处理不过来时会堆积大量待执行的重排回调；
        合并后同一批次的多次变更只重排一次，消除布局风暴。
        """
        if self._sync_pending:
            return
        self._sync_pending = True
        QtCore.QTimer.singleShot(0, self._flush_sync_geometry)

    def _flush_sync_geometry(self) -> None:
        self._sync_pending = False
        self.embedded.sync_geometry()

    def create_panel(self) -> ParameterPanel:
        """构造参数面板：完全由节点声明驱动（决策 #109），无需覆写。

        - ``definition.panel``（``PanelSpec``）—— 显示/装饰特征（scrub 滑条、
          1:1 预览、透明背景、导出按钮）；
        - ``definition.params`` 里的 ``TakeoverParam`` —— 接管型复合控件
          （序列剃刀 / 可视化裁剪）由面板按声明统一构造。
        """
        return ParameterPanel(self.definition)

    @classmethod
    def describe_output(cls, output: Any) -> dict[str, Any]:
        """节点输出元数据展示：**默认行为**——按输出值类型给出通用摘要。

        不同节点的元数据显示定义由节点自身通过继承实现：无特殊需求时
        无需覆写（默认行为接管，委托 ``media_info.default_describe_output``）；
        需要自定义展示的节点覆写本方法（如导出终端节点仅显示 execute 附的
        关键信息，见 ``export_nodes.py``），可用 ``super().describe_output``
        回落默认行为。执行与元数据探测均在工作线程进行且只持有**节点类**
        （``worker`` 传 ``step[1]``），故本方法为类方法，不能依赖实例状态。
        """
        return default_describe_output(output)

    @classmethod
    @abstractmethod
    def execute(cls, inputs: list[Any], params: dict[str, Any], backend: MediaBackend) -> Any:
        raise NotImplementedError

    @staticmethod
    def require_input(inputs: list[Any]) -> Any:
        if not inputs or inputs[0] is None:
            raise ValueError("节点无输入")
        return inputs[0]

    def set_status(self, state: str, detail: str = "") -> None:
        self.panel.set_status(state, detail)
