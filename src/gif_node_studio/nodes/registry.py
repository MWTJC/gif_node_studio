"""节点注册表：唯一的有序具体类注册表 NODE_CLASSES + 按 kind 派生的目录/查找函数。

- ``NODE_CLASSES`` —— 唯一注册表（节点清单、节点库按钮、执行链、旧存档
  兼容清洗全部由它派生，不维护平行全局映射）。
- 目录函数按 TestModuleLib.get_all_test() 的方式实例化每个具体类一次读取
  声明（实例化需要 QApplication，见 app 引导顺序）；结果记忆化缓存。

节点类本身按分类分散在兄弟模块（input_nodes/manifest_nodes/sequence_nodes/
process_nodes/motion_nodes/channel_nodes/export_nodes/analysis_nodes），
本模块只负责汇总注册与按 kind 解析。
"""

from __future__ import annotations

from collections.abc import Callable

from .analysis_nodes import GifAnalysisNode, IcoAnalysisNode, PaletteViewNode, ResolutionViewNode
from .channel_nodes import AlphaMergeNode, AlphaSplitNode, ChannelMergeNode, ChannelSplitNode
from .definitions import NodeDefinition
from .export_nodes import (
    ApngExportNode,
    GifExportFfmpegNode,
    GifExportNode,
    GifOptimizeNode,
    IconComposeNode,
    PngExportNode,
    WebpExportNode,
)
from .input_nodes import (
    BlankSequenceNode,
    GifInputNode,
    GradientSequenceNode,
    ImageSequenceInputNode,
    VideoInputNode,
)
from .manifest_nodes import FormatNode, FrameTrimNode, TimeTrimNode
from .motion_nodes import PanScrollNode
from .node_base import StudioNode
from .process_nodes import (
    AspectRatioNode,
    BinarizeNode,
    BrightnessNode,
    ColorBalanceNode,
    ColorQuantizeNode,
    ContrastNode,
    DitherNode,
    FlipNode,
    GrayscaleNode,
    HueNode,
    InvertNode,
    RotateNode,
    SaturationNode,
    SequenceCropNode,
    SuperKeyNode,
)
from .sequence_nodes import (
    FrameFreezeNode,
    LengthAlignNode,
    PingPongNode,
    ResolutionAlignNode,
    SamplingNode,
    SequenceAddNode,
    SequenceOverlayNode,
    SequenceRazorNode,
    SequenceTrimNode,
    StaticHoldNode,
)

# 唯一的有序具体类注册表：目录与按 kind 查找一律由它派生，不再维护平行全局映射。
NODE_CLASSES: tuple[type[StudioNode], ...] = (
    VideoInputNode,
    ImageSequenceInputNode,
    GifInputNode,
    BlankSequenceNode,
    GradientSequenceNode,
    TimeTrimNode,
    FrameTrimNode,
    FormatNode,
    BrightnessNode,
    SaturationNode,
    HueNode,
    ColorBalanceNode,
    BinarizeNode,
    GrayscaleNode,
    ContrastNode,
    InvertNode,
    SuperKeyNode,
    RotateNode,
    AspectRatioNode,
    FlipNode,
    SequenceCropNode,
    PingPongNode,
    SequenceAddNode,
    SequenceOverlayNode,
    SequenceTrimNode,
    SequenceRazorNode,
    ResolutionAlignNode,
    LengthAlignNode,
    IconComposeNode,
    GifExportNode,
    GifExportFfmpegNode,
    GifOptimizeNode,
    DitherNode,
    ColorQuantizeNode,
    SamplingNode,
    StaticHoldNode,
    FrameFreezeNode,
    PanScrollNode,
    ChannelSplitNode,
    ChannelMergeNode,
    AlphaSplitNode,
    AlphaMergeNode,
    PngExportNode,
    WebpExportNode,
    ApngExportNode,
    PaletteViewNode,
    ResolutionViewNode,
    GifAnalysisNode,
    IcoAnalysisNode,
)

# 派生缓存：声明只存在于实例上（GBT43504/TestModule 式 super().__init__ 注入），
# 因此目录函数按 TestModuleLib.get_all_test() 的方式实例化每个具体类一次来读取声明。
# 这是由 NODE_CLASSES 派生的缓存，不是平行维护的注册表。
_definitions_by_class_cache: dict[type[StudioNode], NodeDefinition] | None = None
# 节点帮助文本（实例上的 help）缓存：与定义缓存共用同一份实例化，不重复建节点。
_help_by_kind_cache: dict[str, str] | None = None

# 启动泵动（决策 #103）：缓存构建 = MainWindow 构造的最大热点（48 个节点
# 控件实例化，真实显示 ~0.6s）。pump 每 PUMP_EVERY 个节点泵动一次事件循环，
# 让 splash 的 QMovie 动画在实例化间隙持续推进。仅首次构建时生效（缓存命中
# 后不再调用）；运行时工作线程调用（executors.py 经 node_class_by_kind）不传
# pump，绝不从非主线程 processEvents。
PUMP_EVERY = 1


def _definitions_by_class(
    pump: Callable[[], None] | None = None,
) -> dict[type[StudioNode], NodeDefinition]:
    global _definitions_by_class_cache, _help_by_kind_cache
    if _definitions_by_class_cache is None:
        instances: dict[type[StudioNode], StudioNode] = {}
        for index, node_class in enumerate(NODE_CLASSES):
            instances[node_class] = node_class()
            if pump is not None and index % PUMP_EVERY == 0:
                pump()
        _definitions_by_class_cache = {
            node_class: instance.definition for node_class, instance in instances.items()
        }
        _help_by_kind_cache = {instance.KIND: instance.help for instance in instances.values()}
    return _definitions_by_class_cache


def node_help_by_kind(kind: str) -> str:
    """按稳定 kind 解析节点帮助文本（实例上的 help，记忆化缓存）。

    与 ``_definitions_by_class`` 共用同一份实例化，不额外创建节点。
    """
    global _help_by_kind_cache
    if _help_by_kind_cache is None:
        _definitions_by_class()
    return _help_by_kind_cache[kind]


def node_class_by_kind(kind: str) -> type[StudioNode]:
    """按稳定 kind 解析具体节点 class（替代 NODE_CLASSES_BY_KIND 全局映射）。"""
    for node_class, definition in _definitions_by_class().items():
        if definition.kind == kind:
            return node_class
    raise KeyError(kind)


def node_definitions(pump: Callable[[], None] | None = None) -> tuple[NodeDefinition, ...]:
    """按注册顺序返回全部节点定义（替代 NODE_DEFINITIONS 全局元组）。

    ``pump``：可选事件泵动回调（决策 #103）——仅在**首次**构建定义缓存
    （实例化全部节点类，启动耗时热点）期间每 PUMP_EVERY 个节点调用一次；
    缓存命中后不再调用。启动画面用它推进 QMovie 动画；运行时调用方
    （含工作线程）不传，默认 None = 不泵动。
    """
    return tuple(_definitions_by_class(pump=pump)[node_class] for node_class in NODE_CLASSES)


def definition_by_kind(kind: str) -> NodeDefinition:
    """按稳定 kind 解析节点定义（替代 DEFINITIONS_BY_KIND 全局映射）。"""
    return _definitions_by_class()[node_class_by_kind(kind)]
