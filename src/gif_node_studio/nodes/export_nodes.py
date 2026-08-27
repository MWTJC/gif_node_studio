"""输出/导出节点：ico 合成、GIF 合成/优化、PNG 序列导出（EXPORT_KIND 分派）。

输出类节点**自身定义元数据展示**：覆写 ``StudioNode.describe_output``——
execute 把可读摘要（文件大小等）附到 MultiOutput.metadata，直接透出，
不再逐端口展开/深度探测；PNG 导出返回文件元组，不覆写走默认行为。
"""

from __future__ import annotations
import qtawesome as qta
from typing import Any

from ..core.domain import MediaKind, MediaManifest, MultiOutput
from ..core.options import (
    FFMPEG_DITHER,
    FFMPEG_STATS_MODE,
    GIFSICLE_COLORMAP,
    GIFSICLE_COLOR_METHOD,
    GIFSICLE_DITHER,
    GIFSICLE_OPTIMIZE,
)
from ..media.backend import _remove_path
from ..media.media_info import format_bytes
from .definitions import (
    BoolParam,
    ChoiceParam,
    FloatParam,
    IntParam,
    NodeCategory,
    NodeDefinition,
    PaletteFileParam,
    PanelSpec,
    PortDefinition,
    PortType,
)
from .icon_resource import category_icon
from .node_base import StudioNode
from .parameter_panel import ParameterPanel
from .sequence_nodes import SequenceNode

class IconComposeNode(SequenceNode):
    """ico合成：把若干单帧序列合成为多尺寸图标序列（ICO 分级）。

    处理：backend.icon_compose + backend.write_ico + MediaManifest 清单输出
    参数：auto_grade（BoolParam 勾选框）
    组件：帧滑条 + 导出按钮（PanelSpec.scrub_frames, export_enabled）

    - 至少一个输入序列；每个输入序列必须为单帧（长度 ≠ 1 报错）；
    - 「自动分级」勾选（默认）：取分辨率最高的输入，按常见 icon 分辨率
      阶梯逐级缩小（16/24/32/48/64/128/256/512/1024，最小 16×16）；
    - 不勾选：按端口顺序原样输出各输入的单帧（手动提供各尺寸）。
    """

    NODE_NAME = "ico合成"

    # 输出类节点框架契约（ui.py 按类属性派生导出按钮分派与固定缓存保留）：
    EXPORT_KIND = "ico"                 # 导出类型（ui.export_node 按此分派）
    CACHE_FILENAME = "preview.ico"      # 运行写入本节点工作区的固定缓存 .ico 文件
    ico_center = 0.0
    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "ico_compose", self.NODE_NAME, NodeCategory.OUTPUT,
                # icon=category_icon(NodeCategory.OUTPUT),  # mdi6.checkbox-blank
                icon=qta.icon(  # 手动规定icon
                    "fa6s.square-full", "mdi6.checkbox-blank", "mdi.alpha-i", "mdi.alpha-c", "mdi.alpha-o",
                    options=[
                        {"color": f"{NodeCategory.OUTPUT.color}"},
                        {"color": "white", "scale_factor": 0.8},
                        {'offset': (self.ico_center - 0.12, 0), "scale_factor": 0.5, "color": f"{NodeCategory.OUTPUT.color}", },
                        {'offset': (self.ico_center, 0), "scale_factor": 0.5, "color": f"{NodeCategory.OUTPUT.color}", },
                        {'offset': (self.ico_center + 0.14, 0), "scale_factor": 0.5, "color": f"{NodeCategory.OUTPUT.color}", },
                    ],
                ),
                inputs=tuple(
                    PortDefinition(f"{res}", PortType.SEQUENCE, show_name=True)
                    for res in [512, 256, 128, 64, 48, 32, 24, 16]
                ),
                outputs=(
                    PortDefinition("序列图片", PortType.SEQUENCE),
                    PortDefinition("格式化清单", PortType.MANIFEST),
                ),
                params=(BoolParam("auto_grade", "自动分级", default=True),),
                panel=PanelSpec(scrub_frames=True, export_enabled=True),
            ),
            help=(
                "输入若干图片序列（每个序列必须为单帧）\n"
                "合成为多尺寸图标序列；\n"
                "自动分级：取分辨率最高的输入，按常见 icon 分辨率逐级缩小\n"
                "输出图片序列；\n"
                "另输出格式化清单（各分辨率帧），供 ico分辨率查看 等分析节点使用"
            ),
        )

    @classmethod
    def describe_output(cls, output):
        """输出类节点元数据契约：仅关键信息（节点自身定义展示）。

        execute 已把可读摘要（文件大小等）附到 MultiOutput.metadata，直接
        透出，不再逐端口展开/深度探测（大 GIF 的 probe_gif 九字段、序列
        帧数/尺寸对导出节点是噪音）；其余输出形状回落默认行为。
        """
        if isinstance(output, MultiOutput):
            return dict(output.metadata or {})
        return super().describe_output(output)

    @classmethod
    def execute(cls, inputs, params, backend):
        # 多输入节点：未连接的端口在执行计划中占位为 None，后端自行过滤。
        artifact = backend.icon_compose(inputs, auto_grade=bool(params["auto_grade"]))
        # 固定缓存 .ico（导出按钮把该文件复制到用户选择的路径）。
        ico_path = backend.write_ico(artifact, backend.workspace / cls.CACHE_FILENAME)
        # 清单输出端口：把各分辨率帧作为清单源（ico分辨率查看 按源逐张 1:1 拼贴）。
        manifest = MediaManifest(MediaKind.STATIC_SEQUENCE, tuple(artifact.frames))
        # 输出类节点元数据契约：仅关键信息（可读文件大小）——由本节点的
        # describe_output 覆写直接透出，不再逐端口展开。
        return MultiOutput(
            {"序列图片": artifact, "格式化清单": manifest},
            metadata={"文件大小": format_bytes(ico_path.stat().st_size)},
        )




class GifExportNode(SequenceNode):
    """GIF 合成。

    处理：backend.export_gif → MultiOutput(序列图片/格式化清单)
    参数：fps（FloatParam 滑条+数值框）、size_percent（IntParam 滑条+数值框）、
          transparent_preview（BoolParam 勾选框，纯显示）
    组件：导出按钮 + 1:1 预览 + 透明背景显示
          （PanelSpec.export_enabled, preview_1to1, preview_bg_param）
    """

    NODE_NAME = "GIF 合成"

    # 导出终端节点的框架契约（ui.py 按类属性派生导出按钮分派与固定缓存保留，
    # 不再平行维护 kind 字符串集合/缓存文件名常量）：
    EXPORT_KIND = "gif"                 # 导出类型（ui.export_node 按此分派）
    CACHE_FILENAME = "preview.gif"      # 运行写入本节点工作区的固定缓存文件

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "gif_export", self.NODE_NAME, NodeCategory.OUTPUT,
                icon=category_icon(NodeCategory.OUTPUT, "mdi6.file-gif-box"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(
                    PortDefinition("序列图片", PortType.SEQUENCE),
                    PortDefinition("格式化清单", PortType.MANIFEST),
                ),
                params=(
                    FloatParam("fps", "帧速", default=12.0, minimum=0.1, maximum=100),
                    IntParam("size_percent", "尺寸 %", default=100, minimum=1, maximum=100),
                    # 纯显示选项（与「图片1:1分辨率查看」的「透明背景」一致）：
                    # 勾选后预览框改用绿幕/品红色，便于观察透明通道，不触发运行。
                    BoolParam("transparent_preview", "透明预览", default=False),
                ),
                panel=PanelSpec(export_enabled=True, preview_1to1=True, preview_bg_param="transparent_preview"),
            ),
            help=(
                "输入图片序列\n"
                "合并图片序列为 gif 文件（按输入**原样合成**："
                "不做帧优化/透明优化，帧间未变化区域全幅存储；"
                "颜色数/仿色由上游「颜色量化」节点控制）；\n"
                "可导出到本地文件；\n"
                "另输出格式化清单（生成的 gif），供 gif优化分析 节点查看存储情况"
            ),
        )

    @classmethod
    def describe_output(cls, output):
        """输出类节点元数据契约：仅关键信息（节点自身定义展示）。

        execute 已把可读摘要（文件大小等）附到 MultiOutput.metadata，直接
        透出，不再逐端口展开/深度探测（大 GIF 的 probe_gif 九字段、序列
        帧数/尺寸对导出节点是噪音）；其余输出形状回落默认行为。
        """
        if isinstance(output, MultiOutput):
            return dict(output.metadata or {})
        return super().describe_output(output)

    @classmethod
    def execute(cls, inputs, params, backend):
        # 无路径参数：始终写入本节点工作区的固定缓存文件 preview.gif，
        # 导出按钮把该文件复制到用户选择的路径。
        artifact = cls.sequence(inputs)
        path = backend.export_gif(
            artifact, backend.workspace / cls.CACHE_FILENAME,
            fps=float(params["fps"]),
            width_percent=int(params["size_percent"]),
        )
        # 清单输出端口：携带生成的 gif 文件路径（gif优化分析 按文件结构解码）。
        manifest = MediaManifest(MediaKind.ANIMATED_IMAGE, (str(path),))
        # 输出类节点元数据契约：仅关键信息（可读文件大小）——由本节点的
        # describe_output 覆写直接透出。
        return MultiOutput(
            {"序列图片": artifact, "格式化清单": manifest},
            metadata={"文件大小": format_bytes(path.stat().st_size)},
        )




class GifExportFfmpegNode(SequenceNode):
    """GIF 合成(FFmpeg)：PyAV 进程内 palettegen/paletteuse 管线（见[关键决策 #100]）。

    处理：backend.export_gif_ffmpeg → MultiOutput(序列图片/格式化清单)
    参数：fps/size_percent（数值）、max_colors/bayer_scale（IntParam）、
          stats_mode/dither（ChoiceParam 下拉）、diff_mode/transparent_preview（BoolParam 勾选框）
    组件：导出按钮 + 1:1 预览 + 透明背景显示
          （PanelSpec.export_enabled, preview_1to1, preview_bg_param）

    与「GIF 合成」（wand 原样合成）平行：FFmpeg 业界事实标准管线——
    palettegen 对整段序列生成共享调色板（与 wand 共享调色板哲学一致）→
    paletteuse 映射（含仿色）→ gif 编码器；「帧优化」勾选时编码器直接
    产出局部帧（gifsicle -O2 级，无需 gifsicle 后处理）。
    """

    NODE_NAME = "GIF 合成（FFmpeg）"

    # 导出终端节点的框架契约（与 GIF 合成/GIF 优化节点一致）：
    # ui.py 按 EXPORT_KIND="gif" 分派导出，固定缓存 preview.gif。
    EXPORT_KIND = "gif"
    CACHE_FILENAME = "preview.gif"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "gif_export_ffmpeg", self.NODE_NAME, NodeCategory.OUTPUT,
                icon=category_icon(NodeCategory.OUTPUT, "mdi6.file-gif-box"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(
                    PortDefinition("序列图片", PortType.SEQUENCE),
                    PortDefinition("格式化清单", PortType.MANIFEST),
                ),
                params=(
                    FloatParam("fps", "帧速", default=12.0, minimum=0.1, maximum=100),
                    IntParam("size_percent", "尺寸 %", default=100, minimum=1, maximum=100),
                    IntParam("max_colors", "颜色数", default=256, minimum=3, maximum=256),
                    ChoiceParam("stats_mode", "调色板统计", options=FFMPEG_STATS_MODE),
                    ChoiceParam("dither", "仿色", options=FFMPEG_DITHER),
                    IntParam("bayer_scale", "Bayer 粒度", default=5, minimum=0, maximum=5,
                             enabled_when=("dither", ("Bayer(有序)",))),
                    BoolParam("diff_mode", "帧优化(diff_mode=rectangle)", default=True),
                    # 纯显示选项（与「GIF 合成」节点一致）：勾选后预览框改用
                    # 绿幕/品红色，便于观察透明通道，不触发运行。
                    BoolParam("transparent_preview", "透明预览", default=False),
                ),
                panel=PanelSpec(export_enabled=True, preview_1to1=True, preview_bg_param="transparent_preview"),
            ),
            help=(
                "输入图片序列\n"
                "FFmpeg(PyAV)：\n"
                "palettegen->paletteuse\n"
                "颜色数=palettegen max_colors 3–256\n"
                "仿色=Floyd-Steinberg(默认)/无/none/\n"
                "Atkinson/Sierra2-4A/Bayer(录屏)/Heckbert；\n"
                "Bayer 粒度=0–5（仅 Bayer 生效）；\n"
                "帧优化=diff_mode=rectangle，编码时直接只存变化区域\n"
                "Floyd-Steinberg/Atkinson/Sierra2-4A 为误差扩散仿色\n"
                "录屏冻结工作流建议配合「帧差静止保持」+无仿色；\n"
                "透明输入按 GIF 1-bit 透明语义保留（半透明像素二值化）；\n"
                "可导出到本地文件；\n"
                "另输出格式化清单（生成的 gif），供 gif优化分析 节点查看存储情况"
            ),
        )

    @classmethod
    def describe_output(cls, output):
        """输出类节点元数据契约：仅关键信息（节点自身定义展示）。

        execute 已把可读摘要（文件大小等）附到 MultiOutput.metadata，直接
        透出，不再逐端口展开/深度探测（大 GIF 的 probe_gif 九字段、序列
        帧数/尺寸对导出节点是噪音）；其余输出形状回落默认行为。
        """
        if isinstance(output, MultiOutput):
            return dict(output.metadata or {})
        return super().describe_output(output)

    @classmethod
    def execute(cls, inputs, params, backend):
        artifact = cls.sequence(inputs)
        path = backend.export_gif_ffmpeg(
            artifact, backend.workspace / cls.CACHE_FILENAME,
            fps=float(params["fps"]),
            width_percent=int(params["size_percent"]),
            max_colors=int(params["max_colors"]),
            stats_mode=FFMPEG_STATS_MODE.key_of(params["stats_mode"]),
            dither=FFMPEG_DITHER.key_of(params["dither"]),
            bayer_scale=int(params["bayer_scale"]),
            diff_mode=bool(params["diff_mode"]),
        )
        manifest = MediaManifest(MediaKind.ANIMATED_IMAGE, (str(path),))
        # 输出类节点元数据契约：仅关键信息（可读文件大小）——由本节点的
        # describe_output 覆写直接透出。
        return MultiOutput(
            {"序列图片": artifact, "格式化清单": manifest},
            metadata={"文件大小": format_bytes(path.stat().st_size)},
        )



class GifOptimizeNode(StudioNode):
    """GIF 优化节点：gifsicle 后处理（GIF 文件级优化，见[关键决策 #78]）。

    处理：backend.optimize_gif → MultiOutput(格式化清单)
    参数：optimize/color_method/dither/colormap（ChoiceParam 下拉）、
          lossy/colors（IntParam 滑条+数值框）、recolor/careful（BoolParam 勾选框）、
          colormap_file（PaletteFileParam 文件选择行，仅 colormap=自定义文件 时启用）
    组件：导出按钮 + 1:1 预览（PanelSpec.export_enabled, preview_1to1）

    输入为 GIF 文件清单（GIF 合成节点的「格式化清单」输出端口），输出
    更新后的清单指向优化后的 GIF——wand 管像素质量（合成），gifsicle 管
    文件体积（-O3 帧优化 / --lossy 有损压缩 / GIF 级再降色 / 固定色板），
    职责互补，不破坏 wand 组装的共享调色板。
    """

    NODE_NAME = "GIF 优化（gifsicle）"

    # 导出终端节点的框架契约：与 GIF 合成节点一致——固定缓存 preview.gif，
    # 导出按钮把优化后的 GIF 复制到用户路径（ui.py 按 EXPORT_KIND 分派）。
    EXPORT_KIND = "gif"
    CACHE_FILENAME = "preview.gif"
    ico_center = 0.25
    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "gif_optimize", self.NODE_NAME, NodeCategory.OUTPUT,
                icon=qta.icon(  # 手动规定icon
                    "fa6s.square-full", "mdi6.file-gif-box", "fa6s.circle", "msc.sparkle-filled",
                    options=[
                        {"color": f"{NodeCategory.OUTPUT.color}"},
                        {"color": "white", "scale_factor": 0.8},
                        {'offset': (self.ico_center, -self.ico_center), "scale_factor": 0.4, "color": f"{NodeCategory.OUTPUT.color}", },
                        {'offset': (self.ico_center, -self.ico_center), "scale_factor": 0.3, "color": "white", },
                    ],
                ),
                inputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                outputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                params=(
                    # 选项唯一源头：options.GIFSICLE_*（机器键 = gifsicle
                    # CLI 选项，后端经 key_of 分支）。
                    ChoiceParam("optimize", "优化级别", options=GIFSICLE_OPTIMIZE),
                    IntParam("lossy", "有损度(0=关)", default=0, minimum=0, maximum=200),
                    # GIF 级再降色总开关：勾选后颜色数/取色方法/仿色/固定色板
                    # 可用（gifsicle 的 --dither/--color-method/--use-colormap
                    # 仅配合 --colors 生效，未勾选时这些参数不参与命令行）。
                    BoolParam("recolor", "GIF 级降色", default=False),
                    IntParam("colors", "颜色数", default=128, minimum=2, maximum=256,
                             enabled_when=("recolor", (True,))),
                    ChoiceParam("color_method", "取色方法", options=GIFSICLE_COLOR_METHOD,
                                enabled_when=("recolor", (True,))),
                    ChoiceParam("dither", "仿色", options=GIFSICLE_DITHER,
                                enabled_when=("recolor", (True,))),
                    ChoiceParam("colormap", "固定色板", options=GIFSICLE_COLORMAP,
                                enabled_when=("recolor", (True,))),
                    PaletteFileParam("colormap_file", "色板文件",
                                     enabled_when=("colormap", ("自定义文件",))),
                    BoolParam("careful", "兼容模式(--careful)", default=False),
                ),
                panel=PanelSpec(export_enabled=True, preview_1to1=True),
            ),
            help=(
                "输入格式化清单\n"
                "用 gifsicle 做 GIF 文件级优化：\n"
                "优化级别=-O1（只存变化区域）/-O2（另做透明优化）/-O3（多策略择优，默认）；\n"
                "有损度=--lossy 0–200（0=关闭；数值越大体积越小、伪影越明显）；\n"
                "GIF 级降色=--colors+--color-method 对 GIF 再降色\n"
                "配合仿色（--dither：Floyd-Steinberg/Atkinson/ro64/o3/o4/o8/有序）与\n"
                "固定色板（--use-colormap：web 216 色/灰度/黑白/自定义文本色板或 GIF 色表）；\n"
                "兼容模式=--careful\n"
                "输出格式化清单（优化后的 GIF），可导出到本地文件；\n"
            ),
        )

    @classmethod
    def describe_output(cls, output):
        """输出类节点元数据契约：仅关键信息（节点自身定义展示）。

        execute 已把可读摘要（优化前/后大小、压缩率）附到
        MultiOutput.metadata，直接透出，不再逐端口展开/深度探测；
        其余输出形状回落默认行为。
        """
        if isinstance(output, MultiOutput):
            return dict(output.metadata or {})
        return super().describe_output(output)

    @classmethod
    def execute(cls, inputs, params, backend):
        manifest = cls.require_input(inputs)
        if not isinstance(manifest, MediaManifest):
            raise ValueError("GIF 优化：输入必须是格式化清单")
        # 无路径参数：始终写入本节点工作区的固定缓存文件 preview.gif，
        # 导出按钮把该文件复制到用户选择的路径。
        path = backend.workspace / cls.CACHE_FILENAME
        optimized, before, after = backend.optimize_gif(
            manifest,
            path,
            # 标签 → 机器键（后端按机器键分支，稳定不随显示名漂移）。
            optimize=GIFSICLE_OPTIMIZE.key_of(params["optimize"]),
            lossy=int(params["lossy"]),
            recolor=bool(params["recolor"]),
            colors=int(params["colors"]),
            color_method=GIFSICLE_COLOR_METHOD.key_of(params["color_method"]),
            dither=GIFSICLE_DITHER.key_of(params["dither"]),
            colormap=GIFSICLE_COLORMAP.key_of(params["colormap"]),
            colormap_file=params.get("colormap_file") or None,
            careful=bool(params["careful"]),
        )
        ratio = f"{after / before * 100:.1f}%" if before > 0 else "—"
        return MultiOutput(
            {"格式化清单": optimized},
            metadata={
                "优化前大小": format_bytes(before),
                "优化后大小": format_bytes(after),
                "压缩率": ratio,
            },
        )




class PngExportNode(SequenceNode):
    """格式化 PNG 输出。

    处理：backend.export_pngs（写固定缓存 preview_frames/）
    参数：无
    组件：导出按钮（PanelSpec.export_enabled）
    """

    NODE_NAME = "格式化 PNG 输出"

    # 导出终端节点的框架契约（与 GifExportNode 一致，ui.py 据此分派导出）。
    EXPORT_KIND = "png"                  # 导出类型（ui.export_node 按此分派）
    CACHE_FILENAME = "preview_frames"    # 运行写入本节点工作区的固定缓存目录

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "png_export", self.NODE_NAME, NodeCategory.OUTPUT,
                icon=category_icon(NodeCategory.OUTPUT, "mdi6.file-png-box"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                panel=PanelSpec(export_enabled=True),
            ),
            help="输入图片序列\n把上游 RGBA PNG 序列输出到所选文件夹。",
        )

    # 不覆写 describe_output：PNG 导出返回文件元组（非 MultiOutput），
    # 默认行为（default_describe_output 的文件元组分支）给出
    # 「输出文件数/输出总大小」可读摘要——输出类节点契约的唯一例外。

    @classmethod
    def execute(cls, inputs, params, backend):
        # 无路径参数：始终写入本节点工作区的固定缓存目录 preview_frames/，
        # 导出按钮把其中的帧复制到用户选择的目录（默认前缀 sequence_）。
        # 重新运行时先清空该固定缓存，避免残留上一轮的旧帧（导出会一并复制）。
        cache_dir = backend.workspace / cls.CACHE_FILENAME
        _remove_path(cache_dir)
        return backend.export_pngs(cls.sequence(inputs), cache_dir)



class WebpExportNode(SequenceNode):
    """WebP 动画导出：Pillow 内建动画 WebP 写入（零新依赖，见[关键决策 #101]）。

    处理：backend.export_webp → MultiOutput(序列图片)
    参数：fps/size_percent/quality（IntParam/FloatParam 数值）、
          lossless/transparent_preview（BoolParam 勾选框）
    组件：导出按钮 + 1:1 预览 + 透明背景显示
          （PanelSpec.export_enabled, preview_1to1, preview_bg_param）

    RGBA 逐帧无损保留 alpha；WebP 动画体积通常远小于 GIF。输出为
    「序列图片」透传端口（供继续处理/预览），固定缓存 preview.webp。
    """

    NODE_NAME = "WebP 动画导出"

    # 导出终端节点的框架契约（与其它输出节点一致）：
    # ui.py 按 EXPORT_KIND="webp" 分派导出，固定缓存 preview.webp。
    EXPORT_KIND = "webp"
    CACHE_FILENAME = "preview.webp"
    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "webp_export", self.NODE_NAME, NodeCategory.OUTPUT,
                icon=category_icon(NodeCategory.OUTPUT),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    FloatParam("fps", "帧速", default=12.0, minimum=0.1, maximum=100),
                    IntParam("size_percent", "尺寸 %", default=100, minimum=1, maximum=100),
                    IntParam("quality", "质量", default=80, minimum=0, maximum=100,
                             enabled_when=("lossless", (False,))),
                    BoolParam("lossless", "无损编码", default=False),
                    # 纯显示选项（与「GIF 合成」节点一致）：预览框绿幕/品红背景。
                    BoolParam("transparent_preview", "透明预览", default=False),
                ),
                panel=PanelSpec(export_enabled=True, preview_1to1=True, preview_bg_param="transparent_preview"),
            ),
            help=(
                "输入图片序列\n"
                "导出 WebP 动画（Pillow 内建编码，零新依赖）：\n"
                "质量=0–100（无损编码勾选时忽略，改用无损模式）；\n"
                "无损编码=WebP 无损压缩（体积更大、画质无损）；\n"
                "序列含透明时**自动使用无损编码**（Pillow WebP 动画有损路径\n"
                "丢失 alpha，见 Pillow #8101 同类缺陷）；\n"
                "帧速=帧/秒（导出动画时长基准）；尺寸%=输出宽高比例；\n"
                "无限循环播放。\n"
                "运行写缓存 preview.webp，「导出…」弹框保存（默认 output.webp）；\n"
                "输出图片序列（原样透传）"
            ),
        )

    @classmethod
    def describe_output(cls, output):
        """输出类节点元数据契约：仅关键信息（节点自身定义展示）。

        execute 已把可读摘要（文件大小/编码模式）附到 MultiOutput.metadata，
        直接透出，不再逐端口展开/深度探测；其余输出形状回落默认行为。
        """
        if isinstance(output, MultiOutput):
            return dict(output.metadata or {})
        return super().describe_output(output)

    @classmethod
    def execute(cls, inputs, params, backend):
        artifact = cls.sequence(inputs)
        cache = backend.workspace / cls.CACHE_FILENAME
        backend.export_webp(
            artifact, cache,
            fps=float(params["fps"]),
            quality=int(params["quality"]),
            lossless=bool(params["lossless"]),
            width_percent=int(params["size_percent"]),
        )
        # 输出「序列图片」透传端口（预览取首帧；WebP/APNG 无对应分析节点，
        # 不输出格式化清单端口）。输出类节点元数据契约：仅关键信息（可读
        # 文件大小）。序列含透明时后端强制无损（Pillow 有损路径丢 alpha），
        # 元数据如实报告编码模式。
        metadata: dict[str, Any] = {"文件大小": format_bytes(cache.stat().st_size)}
        if artifact.has_alpha and not bool(params["lossless"]):
            metadata["编码"] = "无损（序列含透明）"
        return MultiOutput({"序列图片": artifact}, metadata=metadata)



class ApngExportNode(SequenceNode):
    """APNG 动画导出：Pillow 内建 APNG 写入（零新依赖，见[关键决策 #101]）。

    处理：backend.export_apng → MultiOutput(序列图片)
    参数：fps（FloatParam 滑条+数值框）、size_percent（IntParam 滑条+数值框）、
          transparent_preview（BoolParam 勾选框，纯显示）
    组件：导出按钮 + 1:1 预览 + 透明背景显示
          （PanelSpec.export_enabled, preview_1to1, preview_bg_param）

    无损格式（PNG 帧 + acTL 动画块），alpha 全保留。输出为「序列图片」
    透传端口，固定缓存 preview.apng。
    """

    NODE_NAME = "APNG 导出"

    # 导出终端节点的框架契约：ui.py 按 EXPORT_KIND="apng" 分派导出。
    EXPORT_KIND = "apng"
    CACHE_FILENAME = "preview.apng"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "apng_export", self.NODE_NAME, NodeCategory.OUTPUT,
                icon=category_icon(NodeCategory.OUTPUT),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    FloatParam("fps", "帧速", default=12.0, minimum=0.1, maximum=100),
                    IntParam("size_percent", "尺寸 %", default=100, minimum=1, maximum=100),
                    # 纯显示选项（与「WebP 动画导出」节点一致）：预览框绿幕/品红背景。
                    BoolParam("transparent_preview", "透明预览", default=False),
                ),
                panel=PanelSpec(export_enabled=True, preview_1to1=True, preview_bg_param="transparent_preview"),
            ),
            help=(
                "输入图片序列\n"
                "导出 APNG 动画（Pillow）：\n"
                "APNG 为无损格式（PNG 帧 + acTL 动画块），alpha 全保留；\n"
                "帧速=帧/秒；尺寸%=输出宽高比例；无限循环播放。\n"
                "输出图片序列（原样透传）"
            ),
        )

    @classmethod
    def describe_output(cls, output):
        """输出类节点元数据契约：仅关键信息（节点自身定义展示）。

        execute 已把可读摘要（文件大小）附到 MultiOutput.metadata，直接
        透出，不再逐端口展开/深度探测；其余输出形状回落默认行为。
        """
        if isinstance(output, MultiOutput):
            return dict(output.metadata or {})
        return super().describe_output(output)

    @classmethod
    def execute(cls, inputs, params, backend):
        artifact = cls.sequence(inputs)
        cache = backend.workspace / cls.CACHE_FILENAME
        backend.export_apng(
            artifact, cache,
            fps=float(params["fps"]),
            width_percent=int(params["size_percent"]),
        )
        # 输出「序列图片」透传端口（预览取首帧；WebP/APNG 无对应分析节点，
        # 不输出格式化清单端口）。输出类节点元数据契约：仅关键信息（可读文件大小）。
        return MultiOutput(
            {"序列图片": artifact},
            metadata={"文件大小": format_bytes(cache.stat().st_size)},
        )
