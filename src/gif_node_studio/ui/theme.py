"""全局主题应用：Fusion 样式 + 设置中保存的颜色方案 + 节点图壳色。"""

from __future__ import annotations

from ..core.color_tokens import DARK, parse_hex
from .settings_manager import SettingsManager, apply_color_scheme


def apply_theme(app, settings: SettingsManager | None = None):
    """应用全局主题：Fusion 样式 + 深色颜色方案（固定，决策 #90）。

    主题固定为深色（暂无细化亮色主题的计划），不再读取设置；未传入设置
    管理器时按默认构造（设置文件位置不影响主题）。
    """
    app.setStyle("Fusion")
    settings = settings if settings is not None else SettingsManager()
    apply_color_scheme(app, settings.theme())


def apply_graph_theme(graph) -> None:
    """应用节点图壳色（决策 #117 方案 A）+ 网格换挡修复 + 管线色跟随端口色
    （决策 #118 方案 C）。

    节点体/边框在 StudioNode 构造时设置（node_base.py，读同一 DARK）；
    这里只处理画布级：背景、网格、选中描边常量、场景绘制补丁与管线着色。
    """
    from NodeGraphQt.constants import NodeEnum

    r, g, b = parse_hex(DARK.canvas)
    graph.set_background_color(r, g, b)
    r, g, b = parse_hex(DARK.grid)
    graph.set_grid_color(r, g, b)
    NodeEnum.SELECTED_BORDER_COLOR._value_ = (*parse_hex(DARK.select), 255)
    _patch_scene_grid()
    _hook_port_connected(graph)


def recolor_all_pipes(graph) -> None:
    """存档加载/批量建连后重刷全部管线色（方案 C）：管线色 = 输出端口色。"""
    from NodeGraphQt.qgraphics.pipe import PipeItem

    for item in graph.scene().items():
        if isinstance(item, PipeItem) and item.output_port is not None:
            item.color = tuple(item.output_port.color)
            item.reset()


def _hook_port_connected(graph) -> None:
    """连接建立时给新管线着色（幂等：同一 graph 只连一次，实例属性标记）。"""
    if getattr(graph, "_studio_pipe_hooked", False):
        return
    graph.port_connected.connect(_on_port_connected)
    graph._studio_pipe_hooked = True


def _on_port_connected(input_port, output_port):
    """管线色 = 输出端口色：端口/连线同色，数据语义一致（决策 #118 方案 C）。"""
    color = tuple(output_port.color)
    for pipe in output_port.view.connected_pipes:
        if pipe.input_port is input_port.view:
            pipe.color = color
            pipe.reset()


def _patch_scene_grid() -> None:
    """修复 NodeGraphQt 网格换挡（决策 #117 修正）：次级网格（400px，缩小
    画布时显形）原用背景色 darker(200)——深背景下比背景更深；改为与细网格
    同色（grid 令牌），缩放时网格颜色恒定。NodeScene 在 viewer 内硬编码
    创建（viewer.setScene(NodeScene(self))），无法注入子类，故直接替换
    类方法（幂等：已替换则跳过）。NodeGraphQt 升级后若 drawBackground
    结构变化，此补丁需同步更新。"""
    from NodeGraphQt.widgets.scene import NodeScene

    if getattr(NodeScene, "drawBackground", None) is _studio_draw_background:
        return
    NodeScene.drawBackground = _studio_draw_background


def _studio_draw_background(self, painter, rect):
    """NodeScene.drawBackground 覆写：次级网格用 grid_color（库用背景
    darker(200)，深底上比背景更深——「缩小画布后网格线变深」根因）。"""
    from PySide6 import QtGui, QtWidgets
    from NodeGraphQt.constants import ViewerEnum

    QtWidgets.QGraphicsScene.drawBackground(self, painter, rect)
    painter.save()
    painter.setRenderHint(QtGui.QPainter.Antialiasing, False)
    painter.setBrush(self.backgroundBrush())
    if self._grid_mode is ViewerEnum.GRID_DISPLAY_DOTS.value:
        pen = QtGui.QPen(QtGui.QColor(*self.grid_color), 0.65)
        self._draw_dots(painter, rect, pen, ViewerEnum.GRID_SIZE.value)
    elif self._grid_mode is ViewerEnum.GRID_DISPLAY_LINES.value:
        zoom = self.viewer().get_zoom()
        if zoom > -0.5:
            pen = QtGui.QPen(QtGui.QColor(*self.grid_color), 0.65)
            self._draw_grid(painter, rect, pen, ViewerEnum.GRID_SIZE.value)
        # 次级网格（缩小时显形）：与细网格同色（原库用背景色 darker(200)）。
        pen = QtGui.QPen(QtGui.QColor(*self.grid_color), 0.65)
        self._draw_grid(painter, rect, pen, ViewerEnum.GRID_SIZE.value * 8)
    painter.restore()
