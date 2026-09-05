"""动效处理节点：平移滚动（无缝循环平移，跑马灯式）。

分类「动效处理」（NodeCategory.MOTION）目前只有本节点；后续动效类节点
（如缩放/淡入淡出/位移关键帧等）同样放本模块。
"""

from __future__ import annotations

from ..core.options import PAN_DIRECTION, PAN_INTERPOLATION, SPEED_CURVE
from .definitions import (
    ChoiceParam,
    IntParam,
    NodeCategory,
    NodeDefinition,
    PanelSpec,
    PortDefinition,
    PortType,
)
from .icon_resource import category_icon
from .parameter_panel import ParameterPanel
from .sequence_nodes import SequenceNode

class PanScrollNode(SequenceNode):
    """平移滚动：画面向所选方向无缝循环平移（跑马灯式）。

    处理：backend.pan_sequence(direction, duration, curve, interpolation)
    参数：direction/curve/interpolation（ChoiceParam 下拉）、duration（IntParam spin 数值框）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "平移滚动"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "pan_scroll", self.NODE_NAME, NodeCategory.MOTION,
                icon=category_icon(NodeCategory.MOTION, "mdi.arrow-all"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    ChoiceParam("direction", "平移方向", options=PAN_DIRECTION),
                    IntParam("duration", "持续帧数", default=30, minimum=1, maximum=1000000, widget="spin"),
                    ChoiceParam("curve", "速度曲线", options=SPEED_CURVE),
                    ChoiceParam("interpolation", "插值方式", options=PAN_INTERPOLATION),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "让画面向所选方向无缝循环平移（跑马灯式，出画部分从对侧绕回）：\n"
                "平移方向：画面移动的方向\n"
                "持续帧数：输出序列的帧数（素材不足时循环补足）\n"
                "速度曲线：整段位移的速度变化规律\n"
                "插值方式：位移取样的平滑程度（像素画建议锐利档）\n"
                "输出图片序列（帧数 = 持续帧数）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.pan_sequence(
            cls.sequence(inputs),
            # 标签 → 机器键（后端按机器键分支/查值，稳定不随显示名漂移）。
            direction=PAN_DIRECTION.key_of(params["direction"]),
            duration=int(params["duration"]),
            curve=SPEED_CURVE.key_of(params["curve"]),
            interpolation=PAN_INTERPOLATION.key_of(params["interpolation"]),
        )
