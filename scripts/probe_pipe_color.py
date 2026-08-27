"""像素采样验证：端口色 vs 连线色（NodeGraphQt 默认机制）。"""
import os, sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPainter, QColor
from PySide6.QtCore import QRectF, QPointF
from NodeGraphQt import NodeGraph

app = QApplication.instance() or QApplication([])

from gif_node_studio.nodes import registry

g = NodeGraph()
f = g.node_factory
f.clear_registered_nodes()
for node_class in registry.NODE_CLASSES:
    f.register_node(node_class)

by_kind = {d.kind: d.title for d in registry.node_definitions()}
a = g.create_node(
    registry.node_class_by_kind("video_input").type_,
    name="视频输入",
    pos=[0, 0],
)
b = g.create_node(
    registry.node_class_by_kind("format").type_,
    name="格式化",
    pos=[260, 0],
)
out_port = a.output_ports()[0]
in_port = b.input_ports()[0]
out_port.connect_to(in_port)

img = QImage(420, 220, QImage.Format_ARGB32_Premultiplied)
img.fill(QColor("#14161A"))
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
scene = g.scene()
scene.render(p, QRectF(0, 0, 420, 220), QRectF(-40, -60, 360, 220))
p.end()

def sample(x, y, label):
    c = img.pixelColor(x, y)
    print(f"{label}: rgb({c.red()},{c.green()},{c.blue()}) a={c.alpha()}")

# 端口位置：a 的右端口 (260 节点左边界 0 → 端口在 ~x=260?) 由场景坐标估算
sample(208, 110, "a 输出端口(场景x≈208)")
sample(232, 110, "连线中段")
sample(256, 110, "连线中段2")
sample(280, 110, "连线中段3")
sample(300, 110, "b 输入端口附近")
img.save("docs/research/_pipe_color_probe.png")
print("saved probe")
