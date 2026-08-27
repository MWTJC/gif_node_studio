"""分析节点：调色板查看/1:1 查看/GIF 分析/ico 分析（输出图片产物）。"""

from __future__ import annotations

from typing import Any
from PIL import Image
import qtawesome as qta
from ..core.domain import AnalysisResult, CropSpec, MediaKind, MediaManifest, SequenceArtifact
from .definitions import (
    BoolParam,
    ChoiceParam,
    NodeCategory,
    NodeDefinition,
    PanelSpec,
    PortDefinition,
    PortType,
)
from .icon_resource import category_icon
from .node_base import StudioNode
from .parameter_panel import ParameterPanel

class PaletteViewNode(StudioNode):
    """gif调色板查看：分析类节点，可接清单或序列，无输出。

    处理：backend.analysis_palette + backend.palette_swatch → AnalysisResult
    参数：无
    组件：1:1 预览（PanelSpec.preview_1to1）
    """

    NODE_NAME = "gif调色板查看"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "palette_view", self.NODE_NAME, NodeCategory.ANALYSIS,
                icon=category_icon(NodeCategory.ANALYSIS, "mdi6.checkerboard"),
                inputs=(
                    PortDefinition("格式化清单", PortType.MANIFEST),
                    PortDefinition("序列图片", PortType.SEQUENCE),
                ),
                panel=PanelSpec(preview_1to1=True),
            ),
            help=(
                "输入图片序列/格式化清单\n"
                "查看图片序列及 GIF 的调色板，可能不准确，不支持视频；"
                "色板图为固定 16×16 色块（缺色格透明），预览框按原始像素 1:1 显示；"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        manifest = next((value for value in inputs if isinstance(value, MediaManifest)), None)
        sequence = next((value for value in inputs if isinstance(value, SequenceArtifact)), None)
        colors, has_transparency = backend.analysis_palette(manifest, sequence)
        swatch = backend.palette_swatch(colors, has_transparency)
        return AnalysisResult(
            swatch, {"颜色数": len(colors), "含透明": "是" if has_transparency else "否"}
        )




class ResolutionViewNode(StudioNode):
    """图片1:1分辨率查看：分析类节点，预览框按 1:1 原始像素显示；序列可滑条逐帧查看。

    处理：backend.analysis_first_frame / 源文件直接引用（GIF QMovie 播放、序列滑条）
    参数：transparent_bg（BoolParam 勾选框，纯显示）
    组件：帧滑条 + 1:1 预览 + 透明背景显示
          （PanelSpec.scrub_frames, preview_1to1, preview_bg_param）
    """

    NODE_NAME = "图片1:1分辨率查看"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "res1to1_view", self.NODE_NAME, NodeCategory.ANALYSIS,
                icon=category_icon(NodeCategory.ANALYSIS, "mdi.eye-check"),
                inputs=(
                    PortDefinition("格式化清单", PortType.MANIFEST),
                    PortDefinition("序列图片", PortType.SEQUENCE),
                ),
                params=(BoolParam("transparent_bg", "透明背景", default=True),),
                panel=PanelSpec(scrub_frames=True, preview_1to1=True, preview_bg_param="transparent_bg"),
            ),
            help=(
                "输入图片序列/格式化清单\n"
                "完全按素材原始像素尺寸 1:1 显示，不缩放、不裁剪。\n"
                "便于观察序列的透明通道。"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        manifest = next((value for value in inputs if isinstance(value, MediaManifest)), None)
        sequence = next((value for value in inputs if isinstance(value, SequenceArtifact)), None)
        if manifest is not None and manifest.kind is MediaKind.ANIMATED_IMAGE:
            # GIF：直接返回源 GIF，预览按 1:1 播放动画（QMovie），无滑条。
            path = manifest.sources[0]
            frames: tuple[str, ...] = ()
        elif sequence is not None and sequence.frames:
            # 序列：滑条逐帧 1:1 查看全部帧。
            path = sequence.frames[0]
            frames = tuple(sequence.frames)
        elif manifest is not None and manifest.kind is MediaKind.STATIC_SEQUENCE:
            # 静态图片序列清单：仅当未被裁剪/截取/缩放修改时，源文件本身即
            # 原始帧，可滑条逐张 1:1 查看；已被修改（预览≠原始帧）时只显示
            # 预览首帧，避免滑条内容与预览不一致。
            untouched = (
                manifest.crop == CropSpec()
                and manifest.start is None
                and manifest.end is None
                and manifest.scale_percent == 100
            )
            if untouched and manifest.sources:
                path = manifest.sources[0]
                frames = tuple(manifest.sources)
            else:
                path = backend.analysis_first_frame(manifest, sequence)
                frames = ()
        else:
            # 视频清单等：仅取代表性首帧，无滑条。
            path = backend.analysis_first_frame(manifest, sequence)
            frames = ()
        with Image.open(path) as image:
            size = f"{image.width} × {image.height}"
        return AnalysisResult(path, {"分辨率": size}, frames=frames)




class GifAnalysisNode(StudioNode):
    """gif优化分析：分析类节点，特殊解码——按文件实际存储结构逐帧查看
    透明优化/帧优化的实际情况（与普通播放器的 coalesce 合成结果不同）。

    处理：backend.analysis_gif_frames(mode=stored/coalesced) → AnalysisResult
    参数：view_mode（ChoiceParam 下拉）、transparent_bg（BoolParam 勾选框，纯显示）
    组件：帧滑条 + 1:1 预览 + 透明背景显示
          （PanelSpec.scrub_frames, preview_1to1, preview_bg_param）
    """

    NODE_NAME = "gif优化分析"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "gif_analysis_view", self.NODE_NAME, NodeCategory.ANALYSIS,
                # icon=category_icon(NodeCategory.ANALYSIS),
                icon=qta.icon(  # 手动规定icon
                    "fa6s.square-full", "ph.magnifying-glass", "fa6s.circle", "msc.sparkle",
                    options=[
                        {"color": f"{NodeCategory.ANALYSIS.color}"},
                        {"color": "white", "scale_factor": 0.8},
                        {'offset': (-0.12, -0.12), "scale_factor": 0.4, "color": f"{NodeCategory.ANALYSIS.color}", },
                        {'offset': (-0.12, -0.12), "scale_factor": 0.5, "color": "white", },
                    ],
                ),

                inputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                params=(
                    ChoiceParam("view_mode", "解码方式", default="存储帧", choices=("存储帧", "合成帧")),
                    BoolParam("transparent_bg", "透明背景", default=False),
                ),
                panel=PanelSpec(scrub_frames=True, preview_1to1=True, preview_bg_param="transparent_bg"),
            ),
            help=(
                "输入格式化清单（GIF 文件）\n"
                "特殊解码：按文件实际存储结构逐帧 1:1 查看，\n"
                "直观呈现 gif 在帧优化（局部帧仅存变化区域）与\n"
                "透明优化（帧间未变化像素置透明）方面的实际情况；\n"
                "解码方式：存储帧=按文件实际存储（默认），合成帧=coalesce 完整帧供对照；\n"
                "透明背景：勾选后预览框改用绿幕/品红或棋盘格底纹，便于观察透明像素。"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        manifest = cls.require_input(inputs)
        if not isinstance(manifest, MediaManifest):
            raise ValueError("gif优化分析：输入必须是格式化清单")
        mode = "coalesced" if params.get("view_mode") == "合成帧" else "stored"
        path, frames, metadata = backend.analysis_gif_frames(manifest, mode=mode)
        return AnalysisResult(path, metadata, frames=frames)




class IcoAnalysisNode(StudioNode):
    """ico分辨率查看：分析类节点，把清单携带的各分辨率帧合成为 1:1 拼贴图，
    在预览窗口显示此 ico 包含的所有分辨率的内容。

    处理：backend.analysis_ico_montage → AnalysisResult
    参数：transparent_bg（BoolParam 勾选框，纯显示）
    组件：1:1 预览 + 透明背景显示（PanelSpec.preview_1to1, preview_bg_param）
    """

    NODE_NAME = "ico分辨率查看"
    str_ico_center = -0.12
    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "ico_analysis_view", self.NODE_NAME, NodeCategory.ANALYSIS,
                # icon=category_icon(NodeCategory.ANALYSIS),
                icon=qta.icon(  # 手动规定icon
                    "fa6s.square-full", "ph.magnifying-glass", "fa6s.circle", "mdi.alpha-i", "mdi.alpha-c", "mdi.alpha-o",
                    options=[
                        {"color": f"{NodeCategory.ANALYSIS.color}"},
                        {"color": "white", "scale_factor": 0.8},
                        {'offset': (-0.13, -0.12), "scale_factor": 0.5, "color": f"{NodeCategory.ANALYSIS.color}", },
                        {'offset': (self.str_ico_center-0.12, -0.12), "scale_factor": 0.5, "color": "white", },
                        {'offset': (self.str_ico_center, -0.12), "scale_factor": 0.5, "color": "white", },
                        {'offset': (self.str_ico_center+0.14, -0.12), "scale_factor": 0.5, "color": "white", },
                    ],
                ),
                inputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                params=(
                    BoolParam("transparent_bg", "透明背景", default=True),
                ),
                panel=PanelSpec(preview_1to1=True, preview_bg_param="transparent_bg"),
            ),
            help=(
                "输入格式化清单（ico 合成输出的各分辨率帧）\n"
                "在预览窗口 1:1 显示此 ico 包含的所有分辨率的内容（拼贴图，不缩放），\n"
                "透明区域透出棋盘格底纹；元数据列出各分辨率。"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        manifest = cls.require_input(inputs)
        if not isinstance(manifest, MediaManifest):
            raise ValueError("ico分辨率查看：输入必须是格式化清单")
        path, metadata = backend.analysis_ico_montage(manifest)
        return AnalysisResult(path, metadata)
