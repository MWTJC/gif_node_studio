"""离屏渲染当前节点图基线（每分类一个代表节点 + 连线），输出 PNG。"""
import os, sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath("src"))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtCore import QRectF
from NodeGraphQt import NodeGraph

app = QApplication.instance() or QApplication([])

from gif_node_studio.nodes import registry
from gif_node_studio.nodes.definitions import NodeCategory
from gif_node_studio.ui.theme import apply_graph_theme
REPRESENTATIVES = {
    NodeCategory.INPUT: "视频输入",
    NodeCategory.PREFORMAT: "时间截取",
    NodeCategory.FORMAT: "格式化",
    NodeCategory.SEQUENCE: "序列截取",
    NodeCategory.PROCESS: "画面裁剪",
    NodeCategory.MOTION: "平移滚动",
    NodeCategory.CHANNEL: "RGBA通道分离",
    NodeCategory.OUTPUT: "GIF 合成",
    NodeCategory.ANALYSIS: "gif优化分析",
    NodeCategory.BACKDROP: None,
}

g = NodeGraph()
apply_graph_theme(g)
f = g.node_factory
f.clear_registered_nodes()
for node_class in registry.NODE_CLASSES:
    f.register_node(node_class)
# 背景框：与 MainWindow 一致用项目版（EditableBackdropNode，type_ 指回内建）
from gif_node_studio.nodes.backdrop import EditableBackdropNode, backdrop_definition
g.register_node(EditableBackdropNode, alias="Backdrop")

# 背景模式 = 默认网格线（决策 #90 默认）
from NodeGraphQt.constants import ViewerEnum
g.set_grid_mode(ViewerEnum.GRID_DISPLAY_LINES.value)

created = []
pos_x = 0.0
pos_y = 0.0
by_name = {d.title: d.node_class if hasattr(d, "node_class") else registry.node_class_by_kind(d.kind) for d in registry.node_definitions()}
for cat, name in REPRESENTATIVES.items():
    if name is None:
        continue
    node = g.create_node(
        by_name[name].type_,
        name=name,
        pos=[pos_x, pos_y],
    )
    created.append(node)
    pos_y += 260
    if len(created) % 3 == 0:
        pos_y = 0.0
        pos_x += 340

# 相邻节点连一条线（展示管线颜色；方案 C：管线色应跟随输出端口色）
for i in range(len(created) - 1):
    try:
        out_ports = created[i].output_ports()
        in_ports = created[i + 1].input_ports()
        if out_ports and in_ports:
            created[i].output_ports()[0].connect_to(created[i + 1].input_ports()[0])
    except Exception:
        pass

# 背景框覆盖左上第一列节点（验证背景框配色收编）
backdrop = g.create_node(EditableBackdropNode.type_, name="背景框", pos=[-20, -30])
backdrop.set_property("width", 620, push_undo=False)
backdrop.set_property("height", 780, push_undo=False)
backdrop.set_property("pos", [-20.0, -30.0], push_undo=False)

g.fit_to_selection()

img = QImage(1280, 800, QImage.Format_ARGB32_Premultiplied)
img.fill(QColor("#14161A"))
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
p.setRenderHint(QPainter.TextAntialiasing)
scene = g.scene()
# 渲染整个场景（等比缩小进画布）
view = g.viewer()
view.resize(1280, 800)
scene_rect = scene.itemsBoundingRect().adjusted(-80, -80, 80, 80)
scale = min(1280 / scene_rect.width(), 800 / scene_rect.height(), 2.0)
w, h = int(scene_rect.width() * scale), int(scene_rect.height() * scale)
x0 = (1280 - w) // 2
y0 = (800 - h) // 2
scene.render(p, QRectF(x0, y0, w, h), scene_rect)
p.end()
out = "docs/research/color_theme_after.png"
img.save(out)
print("saved", out, img.width(), img.height())

# 缩小渲染（zoom < -0.5 触发网格换挡：50px 细网格消失、400px 次级网格显形）——
# 验证网格修复后次级网格仍是亮色（不再比背景更深）。
g.viewer().set_zoom(-1.0)
img2 = QImage(1280, 800, QImage.Format_ARGB32_Premultiplied)
img2.fill(QColor("#14161A"))
p2 = QPainter(img2)
p2.setRenderHint(QPainter.Antialiasing)
scene.render(p2, QRectF(0, 0, 1280, 800), scene_rect)
p2.end()
out2 = "docs/research/color_theme_after_zoomed.png"
img2.save(out2)
print("saved", out2, img2.width(), img2.height())
