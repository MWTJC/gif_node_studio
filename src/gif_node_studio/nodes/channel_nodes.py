"""通道处理节点：RGBA 通道分离/合并、A 通道提取/合并。"""

from __future__ import annotations
import qtawesome as qta
from ..core.domain import MultiOutput
from .definitions import (
    NodeCategory,
    NodeDefinition,
    PanelSpec,
    PortDefinition,
    PortType,
)
from .icon_resource import category_icon
from .parameter_panel import ParameterPanel
from .sequence_nodes import SequenceNode

class ChannelSplitNode(SequenceNode):
    """RGBA 通道分离：输入一个序列，输出红/绿/蓝/透明度四个序列（均为灰度图）。

    处理：backend.split_channels → MultiOutput(R/G/B/A)
    参数：无
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "RGBA通道分离"
    ico_center = -0.15
    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "channel_split", self.NODE_NAME, NodeCategory.CHANNEL,
                # icon=category_icon(NodeCategory.CHANNEL),
                icon=qta.icon(  # 手动规定icon
                    "fa6s.square-full", "mdi.call-split","mdi.square", "mdi.square", "mdi.square",
                    rotated=90,
                    options=[
                        {"color": f"{NodeCategory.CHANNEL.color}"},
                        {"color": "white", "scale_factor": 0.8},
                        {'offset': (0.3, self.ico_center+0.15), "color": "#FF0000", "scale_factor": 0.2},
                        {'offset': (0.3, self.ico_center), "color": "#00FF00", "scale_factor": 0.2},
                        {'offset': (0.3, self.ico_center-0.15), "color": "#0000FF", "scale_factor": 0.2},

                    ],
                ),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(
                    PortDefinition("R", PortType.SEQUENCE, show_name=True),
                    PortDefinition("G", PortType.SEQUENCE, show_name=True),
                    PortDefinition("B", PortType.SEQUENCE, show_name=True),
                    PortDefinition("A", PortType.SEQUENCE, show_name=True),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "把 RGBA 四个通道分离为四个独立序列。\n"
                "输出四个图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        red, green, blue, alpha = backend.split_channels(cls.sequence(inputs))
        return MultiOutput({
            "R": red,
            "G": green,
            "B": blue,
            "A": alpha,
        })




class ChannelMergeNode(SequenceNode):
    """RGBA 通道合并：红/绿/蓝/透明度四路序列 → 一个 RGBA 序列（与通道分离互逆）。

    处理：backend.merge_channels(red, green, blue, alpha)
    参数：无
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "RGBA通道合并"
    ico_center = -0.15
    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "channel_merge", self.NODE_NAME, NodeCategory.CHANNEL,
                icon=qta.icon(  # 手动规定icon
                    "fa6s.square-full", "mdi.call-merge", "mdi.square", "mdi.square", "mdi.square",
                    rotated=90,
                    options=[
                        {"color": f"{NodeCategory.CHANNEL.color}"},
                        {"color": "white", "scale_factor": 0.8},
                        {'offset': (0.3, self.ico_center + 0.15), "color": "#FF0000", "scale_factor": 0.2},
                        {'offset': (0.3, self.ico_center), "color": "#00FF00", "scale_factor": 0.2},
                        {'offset': (0.3, self.ico_center - 0.15), "color": "#0000FF", "scale_factor": 0.2},
                    ],
                ),
                inputs=(
                    PortDefinition("R", PortType.SEQUENCE, show_name=True),
                    PortDefinition("G", PortType.SEQUENCE, show_name=True),
                    PortDefinition("B", PortType.SEQUENCE, show_name=True),
                    PortDefinition("A", PortType.SEQUENCE, show_name=True),
                ),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列RGBA\n"
                "透明度通道未连接时按不透明处理（alpha=255）；\n"
                "各通道序列长度必须一致。\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        # 输入按端口定义顺序占位（未连接为 None）：红/绿/蓝/透明度。
        red, green, blue, alpha = (inputs + [None] * 4)[:4]
        return backend.merge_channels(red, green, blue, alpha)




class AlphaSplitNode(SequenceNode):
    """A通道提取：输入 RGBA 序列 → 输出 alpha 通道灰度序列（只物化 1 份缓存）。

    处理：backend.split_alpha
    参数：无
    组件：帧滑条（PanelSpec.scrub_frames）

    与「RGBA通道分离」的透明度通道语义一致，但不额外物化红/绿/蓝三份
    通道缓存——只想提取 alpha 时无需为 RGBA 分离的 4 份缓存付账。
    """

    NODE_NAME = "A通道提取"
    ico_center = -0.15
    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "alpha_split", self.NODE_NAME, NodeCategory.CHANNEL,
                icon=qta.icon(  # 手动规定icon
                    "fa6s.square-full", "mdi.call-split", "mdi.square", "mdi.square-outline",
                    rotated=90,
                    options=[
                        {"color": f"{NodeCategory.CHANNEL.color}"},
                        {"color": "white", "scale_factor": 0.8},
                        {'offset': (0.3, self.ico_center), "color": "white", "scale_factor": 0.2},
                        {'offset': (0.3, self.ico_center - 0.15), "color": "white", "scale_factor": 0.2},
                    ],
                ),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("透明度通道", PortType.SEQUENCE),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "提取 alpha（透明度）通道为灰度序列\n"
                "输出透明度通道序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.split_alpha(cls.sequence(inputs))




class AlphaMergeNode(SequenceNode):
    """A通道合并：RGB 序列 + alpha 灰度序列 → RGBA 序列（与 A通道分离 互逆）。

    处理：backend.merge_alpha(rgb, alpha)
    参数：无
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "A通道合并"
    ico_center = -0.15
    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "alpha_merge", self.NODE_NAME, NodeCategory.CHANNEL,
                icon=qta.icon(  # 手动规定icon
                    "fa6s.square-full", "mdi.call-merge", "mdi.square", "mdi.square-outline",
                    rotated=90,
                    options=[
                        {"color": f"{NodeCategory.CHANNEL.color}"},
                        {"color": "white", "scale_factor": 0.8},
                        {'offset': (0.3, self.ico_center), "color": "white", "scale_factor": 0.2},
                        {'offset': (0.3, self.ico_center-0.15), "color": "white", "scale_factor": 0.2},
                    ],
                ),
                inputs=(
                    PortDefinition("RGB序列", PortType.SEQUENCE, show_name=True),
                    PortDefinition("透明度通道", PortType.SEQUENCE, show_name=True),
                ),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列 RGB序列、透明度通道\n"
                "把 RGB 序列的彩色通道与透明度通道的灰度值合并为 RGBA 序列\n"
                "透明度通道未连接时按不透明处理（alpha=255）；\n"
                "长度/帧尺寸须一致。\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        # 输入按端口定义顺序占位（未连接为 None）：RGB序列/透明度通道。
        rgb, alpha = (inputs + [None] * 2)[:2]
        return backend.merge_alpha(rgb, alpha)
