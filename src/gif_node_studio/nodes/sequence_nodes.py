"""序列处理节点：序列族基类 SequenceNode + 序列处理分类节点。"""

from __future__ import annotations

from typing import Any

from ..core.domain import MultiOutput, SequenceArtifact
from ..core.options import FREEZE_END, LENGTH_ALIGN_METHOD, RESAMPLE, SCALE_STRATEGY, STATIC_HOLD_REFERENCE
from .definitions import (
    ChoiceParam,
    IntParam,
    NodeCategory,
    NodeDefinition,
    PanelSpec,
    PortDefinition,
    PortType,
    RazorCutParam,
    TrimRangeParam,
)
from .icon_resource import category_icon
from .node_base import StudioNode
from .parameter_panel import ParameterPanel
from .razor_strip import make_razor_panel
from .trim_strip import make_trim_panel

class SequenceNode(StudioNode):
    """序列族基类：只拥有共享的输入校验。"""

    @classmethod
    def sequence(cls, inputs: list[Any]) -> SequenceArtifact:
        return cls.require_input(inputs)




class PingPongNode(SequenceNode):
    """序列倒带。

    处理：backend.rewind_sequence
    参数：无
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "序列倒带"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "ping_pong", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE, "ri.arrow-left-right-line"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "把序列倒序输出（倒带，只输出反转结果，不自动接回原序列）\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.rewind_sequence(cls.sequence(inputs))




class SequenceAddNode(SequenceNode):
    """序列相加：输入主序列A/追加序列B，以 A 分辨率为基准缩放 B 后追加到 A 末尾。

    处理：backend.concat_sequences(a, b, resample, strategy)
    参数：resample/strategy（ChoiceParam 下拉）
    组件：帧滑条（PanelSpec.scrub_frames）

    端口位约定：第一端口 = 主序列 A（不显示端口名，A 的全部帧原样保留在
    输出开头）；第二端口 = 「追加物」（被缩放后追加到 A 末尾的 B）。
    """

    NODE_NAME = "序列相加"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "sequence_add", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE,"ri.file-add-fill"),
                inputs=(
                    PortDefinition("序列A", PortType.SEQUENCE, show_name=False),
                    PortDefinition("追加物", PortType.SEQUENCE, show_name=True),
                ),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 选项唯一源头：options.RESAMPLE / options.SCALE_STRATEGY
                    # （标签/默认值/机器键派生，修改选项只改 options.py 一处）。
                    ChoiceParam("resample", "缩放算法", options=RESAMPLE),
                    ChoiceParam("strategy", "缩放策略", options=SCALE_STRATEGY),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列（主序列）与追加物（要接在后面的序列）\n"
                "以主序列分辨率为基准，把追加物缩放后接到主序列末尾：\n"
                "缩放算法：放大/缩小时的画面重采样方式\n"
                "缩放策略：追加画面不满画布/超出画布时的摆放方式\n"
                "输出图片序列（= 主序列全部帧 + 缩放后的追加物帧）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        if len(inputs) < 2 or inputs[0] is None or inputs[1] is None:
            raise ValueError("序列相加需要连接两个输入序列（主序列在上、追加物在下）")
        # inputs[0] = 主序列 A，inputs[1] = 追加物 B。
        a, b = inputs[0], inputs[1]
        if not isinstance(a, SequenceArtifact) or not isinstance(b, SequenceArtifact):
            raise ValueError("序列相加的两个输入都必须是图片序列")
        return backend.concat_sequences(
            a,
            b,
            # 标签 → 机器键（后端按机器键分支/查值，稳定不随显示名漂移）。
            resample=RESAMPLE.key_of(params["resample"]),
            strategy=SCALE_STRATEGY.key_of(params["strategy"]),
        )




class SequenceOverlayNode(SequenceNode):
    """序列叠加：输入基础序列A/叠加序列B，把 B 层叠到 A 上（平面叠加，逐帧合成）。

    处理：backend.overlay_sequences(a, b, resample, strategy)
    参数：resample/strategy（ChoiceParam 下拉）
    组件：帧滑条（PanelSpec.scrub_frames）

    端口位约定：第一端口 = 基础序列 A（不显示端口名，决定画布/输出长度）；
    第二端口 = 「叠加物」（层叠到 A 上的 B）。
    """

    NODE_NAME = "序列叠加"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "sequence_overlay", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE,"mdi.layers-plus"),
                inputs=(
                    PortDefinition("序列A", PortType.SEQUENCE, show_name=False),
                    PortDefinition("叠加物", PortType.SEQUENCE, show_name=True),
                ),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 与「序列相加」完全一致的缩放选项（单一源头 options.py）。
                    ChoiceParam("resample", "缩放算法", options=RESAMPLE),
                    ChoiceParam("strategy", "缩放策略", options=SCALE_STRATEGY),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列（基础，画布/底层）与叠加物（要叠在上面的序列）\n"
                "把叠加物逐帧层叠到基础序列上（基础序列决定输出长度与画布）：\n"
                "叠加物帧数不足时循环重复，多于输出长度时取前面部分\n"
                "缩放算法：放大/缩小时的画面重采样方式\n"
                "缩放策略：叠加画面不满画布/超出画布时的摆放方式\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        if len(inputs) < 2 or inputs[0] is None or inputs[1] is None:
            raise ValueError("序列叠加需要连接两个输入序列（基础序列在上、叠加物在下）")
        # inputs[0] = 基础序列 A，inputs[1] = 叠加物 B。
        a, b = inputs[0], inputs[1]
        if not isinstance(a, SequenceArtifact) or not isinstance(b, SequenceArtifact):
            raise ValueError("序列叠加的两个输入都必须是图片序列")
        return backend.overlay_sequences(
            a,
            b,
            resample=RESAMPLE.key_of(params["resample"]),
            strategy=SCALE_STRATEGY.key_of(params["strategy"]),
        )



class SequenceTrimNode(SequenceNode):
    """序列截取：输入序列 → 输出序列，胶片条上拖动起止手柄仅输出区间 [start, end)。

    处理：backend.trim_sequence(start, end)
    参数：start/end（IntParam spin 数值框，被 TrimRangeParam 接管不生成常规行）
    组件：区间截取胶片条（TrimStripPanel 双手柄接管，PR 时间轴式）

    从「起始/结束帧两个数值框」升级为「胶片条双手柄拖拽」（决策 #115，与
    序列剃刀同构）：蓝色起始手柄/橙色结束手柄标记区间，区间内部高亮、外部
    压暗，拖拽实时预览区间首尾帧（起始帧 / 结束帧，不依赖运行）；``start``/
    ``end`` 仍为 0 基切片下标、参数名/值域与旧版一致——旧存档读取透明。
    """

    NODE_NAME = "序列截取"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "sequence_trim", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE, "mdi.view-column"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 值仍由常规 IntParam 声明承载（默认值/存档兼容不变）；
                    # 面板中被 TrimRangeParam 接管，不生成数值行（与裁剪的
                    # left/top/right/bottom 同款「纯声明 + 常规参数」模式）。
                    IntParam("start", "起始帧", default=0, minimum=0, maximum=1000000, widget="spin"),
                    IntParam("end", "结束帧", default=1, minimum=5, maximum=1000000, widget="spin"),
                    # 接管型参数（决策 #109/#115）：面板用双手柄胶片条
                    # （TrimStripPanel）接管 start/end 参数行，值由拖拽直接
                    # 读写，不再生成数值滑条。
                    TrimRangeParam(
                        "trim_range", "区间截取",
                        owned=("start", "end"),
                        data_source="sequence_frames",
                        widget_factory=make_trim_panel,
                    ),
                ),
                panel=PanelSpec(),
            ),
            help=(
                "输入图片序列\n"
                "在胶片条上拖动蓝色起始/橙色结束手柄，只保留区间内的帧：\n"
                "区间 [起始帧, 结束帧)（结束帧不包含），两端手柄均可拖到序列边界\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        artifact = cls.sequence(inputs)
        return backend.trim_sequence(
            artifact,
            start=int(params["start"]),
            end=int(params["end"]),
        )



class SequenceRazorNode(SequenceNode):
    """序列剃刀（序列 → 两个序列）：PR 剃刀工具式切割。

    处理：backend.split_sequence(cut) → MultiOutput(段A/段B)
    参数：cut（RazorCutParam 接管型参数）
    组件：胶片条剃刀接管控件（RazorStripPanel，data_source=sequence_frames）

    输入一个序列，在鼠标拖动的红色剃刀线（``RazorStripWidget`` 胶片条）处
    切成两段：段A = frames[:cut]、段B = frames[cut:]（``cut`` 为 0 基切片
    下标，即第 cut 帧与第 cut+1 帧之间）。切割位置由节点参数 ``cut`` 承载
    （面板侧由剃刀条接管，不生成数值滑条行）；执行按 ``MultiOutput`` 端口名
    输出两个序列，供下游分别连线（与 RGBA 通道分离同款多输出端口解析）。
    """

    NODE_NAME = "序列剃刀"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "sequence_razor", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE, "mdi6.razor-double-edge"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(
                    PortDefinition("段A", PortType.SEQUENCE, show_name=True),
                    PortDefinition("段B", PortType.SEQUENCE, show_name=True),
                ),
                params=(
                    # 接管型参数（决策 #109）：面板用胶片条（RazorStripPanel）
                    # 接管 cut 参数行，值由拖拽直接读写，不再生成数值滑条。
                    RazorCutParam(
                        "cut", "切割帧",
                        default=1, minimum=1, maximum=1000000,
                        owned=("cut",),
                        data_source="sequence_frames",
                        widget_factory=make_razor_panel,
                    ),
                ),
            ),
            help=(
                "输入图片序列\n"
                "拖动红色剃刀线，把序列切成两段：\n"
                "切割位置 = 剃刀线所在帧边界，段A 与段B 两端都非空\n"
                "输出两个图片序列（段A、段B）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        artifact = cls.sequence(inputs)
        a, b = backend.split_sequence(artifact, int(params["cut"]))
        return MultiOutput({"段A": a, "段B": b})




class ResolutionAlignNode(SequenceNode):
    """分辨率统一：输入序列A/序列B，以 A 分辨率为基准把 B 缩放到 A 尺寸（供通道合并前对齐）。

    处理：backend.align_resolution(a, b, resample, strategy)
    参数：resample/strategy（ChoiceParam 下拉）
    组件：帧滑条（PanelSpec.scrub_frames）

    端口位约定：第一端口 = 待处理序列 B（不显示端口名，与其他节点单输入直觉
    一致）；第二端口 = 「分辨率源」（决定目标分辨率的 A）。
    """

    NODE_NAME = "分辨率统一"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "resolution_align", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE, "mdi.move-resize-variant"),
                inputs=(
                    PortDefinition("序列B", PortType.SEQUENCE, show_name=False),
                    PortDefinition("分辨率源", PortType.SEQUENCE, show_name=True),
                ),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 与「序列相加」完全一致的缩放选项（单一源头 options.py）。
                    ChoiceParam("resample", "缩放算法", options=RESAMPLE),
                    ChoiceParam("strategy", "缩放策略", options=SCALE_STRATEGY),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列（待处理，被缩放）与分辨率源（决定目标分辨率的序列）\n"
                "以分辨率源序列的尺寸为目标，把待处理序列逐帧缩放对齐：\n"
                "缩放算法：放大/缩小时的画面重采样方式\n"
                "缩放策略：画面不满画布/超出画布时的摆放方式\n"
                "输出处理后的图片序列（帧数不变，尺寸 = 分辨率源的尺寸）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        if len(inputs) < 2 or inputs[0] is None or inputs[1] is None:
            raise ValueError("分辨率统一需要连接两个输入序列（待处理序列在上、分辨率源在下）")
        # inputs[0] = 待处理序列 B，inputs[1] = 分辨率源 A。
        b, a = inputs[0], inputs[1]
        if not isinstance(a, SequenceArtifact) or not isinstance(b, SequenceArtifact):
            raise ValueError("分辨率统一的两个输入都必须是图片序列")
        return backend.align_resolution(
            a,
            b,
            # 标签 → 机器键（与「序列相加」一致）。
            resample=RESAMPLE.key_of(params["resample"]),
            strategy=SCALE_STRATEGY.key_of(params["strategy"]),
        )



class LengthAlignNode(SequenceNode):
    """序列长度统一：输入序列A/序列B，按 A 的长度统一 B 的帧数（延长方式可选）。

    处理：backend.align_length(a, b, method)
    参数：method（ChoiceParam 下拉）
    组件：帧滑条（PanelSpec.scrub_frames）

    端口位约定：第一端口 = 待处理序列 B（不显示端口名）；第二端口 =
    「长度源」（决定目标长度的 A）。
    """

    NODE_NAME = "序列长度统一"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "length_align", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE, "fa6s.diagram-next"),
                inputs=(
                    PortDefinition("序列B", PortType.SEQUENCE, show_name=False),
                    PortDefinition("长度源", PortType.SEQUENCE, show_name=True),
                ),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(ChoiceParam("method", "方式", options=LENGTH_ALIGN_METHOD),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列（待处理，被统一长度）与长度源（决定目标长度的序列）\n"
                "按长度源序列的长度统一待处理序列：短了补帧、长了截断，分辨率不变\n"
                "方式：补帧时的延长策略（不足长度时的重复/采样方式）\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        if len(inputs) < 2 or inputs[0] is None or inputs[1] is None:
            raise ValueError("序列长度统一需要连接两个输入序列（待处理序列在上、长度源在下）")
        # inputs[0] = 待处理序列 B，inputs[1] = 长度源 A。
        b, a = inputs[0], inputs[1]
        if not isinstance(a, SequenceArtifact) or not isinstance(b, SequenceArtifact):
            raise ValueError("序列长度统一的两个输入都必须是图片序列")
        return backend.align_length(
            a,
            b,
            method=LENGTH_ALIGN_METHOD.key_of(params["method"]),
        )




class SamplingNode(SequenceNode):
    """抽帧：按「定义帧速/输出帧速」的比值对序列抽帧（帧率转换）。

    处理：backend.sample_frames(in_fps, out_fps)
    参数：input_fps/output_fps（IntParam spin 数值框）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "抽帧"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "sampling", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE, "mdi.view-split-horizontal"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    IntParam("input_fps", "定义帧速", default=30, minimum=1, maximum=100000, widget="spin"),
                    IntParam("output_fps", "输出帧速", default=12, minimum=1, maximum=100000, widget="spin"),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "按定义帧速与输出帧速的比值抽帧（输出帧速低于定义帧速才抽帧）\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.sample_frames(
            cls.sequence(inputs),
            in_fps=int(params["input_fps"]),
            out_fps=int(params["output_fps"]),
        )



class StaticHoldNode(SequenceNode):
    """帧差静止保持（序列 → 序列）：消除录屏/视频编码在静止区域的时域噪声。

    处理：backend.static_hold_sequence(threshold, reference, neighbors)
    参数：threshold/neighbors（IntParam 滑条+数值框）、reference（ChoiceParam 下拉）
    组件：帧滑条（PanelSpec.scrub_frames）

    针对「电脑录屏 → GIF」场景（见关键决策 #96）：录屏编码器（H.264/HEVC）
    在静止 UI 区域留下逐帧 ±1~3 级的量化噪声，颜色量化的扩散仿色会把它
    放大成「烂噪」图案、浪费共享调色板条目并造成帧间闪烁，且使 gifsicle
    帧优化（逐像素精确比较）失效——静止区域每帧像素都不同，无法合并为
    未变化区域。本节点逐帧与参考帧逐像素比较，差值 ≤ 阈值判定为「静止」的
    像素沿用参考帧的精确值（携带保持后的版本，噪声不回流），运动区域保留
    当前帧；静止区域因此在时间轴上像素精确一致——量化取同一色板项
    （无闪烁）、gifsicle -O2/-O3 可把静止区域合并为未变化区域（体积下降）。
    """

    NODE_NAME = "帧差静止保持"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "static_hold", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE, "mdi.pulse"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    IntParam("threshold", "静止阈值", default=3, minimum=0, maximum=255),
                    # 选项唯一源头：options.STATIC_HOLD_REFERENCE
                    ChoiceParam("reference", "参考帧", options=STATIC_HOLD_REFERENCE),
                    IntParam("neighbors", "邻域判定", default=4, minimum=0, maximum=8),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "消除录屏/编码在静止区域留下的时域噪声（录屏 → GIF 前处理）：\n"
                "静止阈值：静止判定允许的逐通道最大差值（0–255，录屏噪声通常 1–3）\n"
                "参考帧：与哪一帧比较来判定静止\n"
                "邻域判定：周围也需静止的像素数（0–8，越大越谨慎，默认 4）\n"
                "输出图片序列（Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.static_hold_sequence(
            cls.sequence(inputs),
            threshold=int(params["threshold"]),
            reference=STATIC_HOLD_REFERENCE.key_of(params["reference"]),
            neighbors=int(params["neighbors"]),
        )



class FrameFreezeNode(SequenceNode):
    """帧冻结（序列 → 序列）：把首帧/末帧的静态内容定格延长若干帧。

    处理：backend.freeze_sequence(end, count)
    参数：end（ChoiceParam 下拉）、count（IntParam spin 数值框）
    组件：帧滑条（PanelSpec.scrub_frames）

    - 「冻结位置」：第一帧 = 在序列**开头**插入 K 份首帧副本（首帧定格在
      开头）；最后一帧 = 在序列**末尾**追加 K 份末帧副本（末帧定格在结尾）；
    - 「冻结延长帧数」：插入/追加的静态帧数（0 = 原样输出，不冻结）；
    - 冻结帧为边界帧的逐像素副本（纯静态内容，无插值/过渡），常用于
      GIF 片头定格/结尾停留。
    """

    NODE_NAME = "帧冻结"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "frame_freeze", self.NODE_NAME, NodeCategory.SEQUENCE,
                icon=category_icon(NodeCategory.SEQUENCE, "mdi.camera-burst"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 选项唯一源头：options.FREEZE_END（冻结位置：第一帧/最后一帧）。
                    ChoiceParam("end", "冻结位置", options=FREEZE_END),
                    IntParam("count", "冻结延长帧数", default=1, minimum=0, maximum=100000, widget="spin"),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "把首帧或末帧定格延长若干帧（GIF 片头定格/结尾停留）：\n"
                "冻结位置：定格在序列开头（第一帧）还是结尾（最后一帧）\n"
                "冻结延长帧数：延长插入的静态帧数（0 = 不冻结）\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.freeze_sequence(
            cls.sequence(inputs),
            end=FREEZE_END.key_of(params["end"]),
            count=int(params["count"]),
        )
