"""输入类节点：视频/图片序列/GIF 文件输入 + 空白序列 + 渐变序列。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..core.domain import MediaKind, MediaManifest
from ..media.backend import MediaBackend
from ..media.sequence import discover_numbered_sequence
from .definitions import (
    ColorParam,
    FloatParam,
    GifFileParam,
    ImageFileParam,
    IntParam,
    NodeCategory,
    NodeDefinition,
    PanelSpec,
    PortDefinition,
    PortType,
    VideoFileParam,
)
from .icon_resource import category_icon
from .node_base import StudioNode
from .parameter_panel import ParameterPanel

class VideoInputNode(StudioNode):
    """视频输入。

    处理：MediaManifest(MediaKind.VIDEO, (path,)) 构造 + backend.extract_first_frame（首帧预览）
    参数：path（VideoFileParam 文件选择行，默认空）
    组件：无增量（默认预览框）
    """

    NODE_NAME = "视频输入"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "video_input", self.NODE_NAME, NodeCategory.INPUT,
                icon=category_icon(NodeCategory.INPUT, "mdi.file-video"),
                outputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                params=(VideoFileParam("path", "视频文件", default=""),),
            ),
            help="选择单个视频文件，\n输出格式化清单。",
        )

    @classmethod
    def execute(cls, inputs: list[Any], params: dict[str, Any], backend: MediaBackend) -> MediaManifest:
        path = params.get("path", "")
        if not path:
            raise ValueError("请选择输入文件")
        manifest = MediaManifest(MediaKind.VIDEO, (str(Path(path)),))
        # 默认截取第一帧作为后续节点（格式化解码前）的预览图。
        return replace(manifest, preview=backend.extract_first_frame(manifest))




class ImageSequenceInputNode(StudioNode):
    """静态图片序列输入。

    处理：discover_numbered_sequence 源发现（回退单张）→ MediaManifest(STATIC_SEQUENCE)
          + backend.extract_first_frame（首帧预览）
    参数：path（ImageFileParam 文件选择行，默认空）
    组件：无增量（默认预览框）
    """

    NODE_NAME = "静态图片序列输入"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "image_sequence_input", self.NODE_NAME, NodeCategory.INPUT,
                icon=category_icon(NodeCategory.INPUT, "mdi6.image-multiple"),
                outputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                params=(ImageFileParam("path", "序列首图", default=""),),
            ),
            help="选择诸如“abc_0001.png”图片序列的首张并自动识别，\n也可选择单张图片作为单帧序列输入；\n输出格式化清单。",
        )

    @classmethod
    def execute(cls, inputs: list[Any], params: dict[str, Any], backend: MediaBackend) -> MediaManifest:
        path = params.get("path", "")
        if not path:
            raise ValueError("请选择输入文件")
        try:
            sources = tuple(str(item) for item in discover_numbered_sequence(path))
        except ValueError:
            # 允许单张非序列图片输入：文件名不以数字序号结尾时，
            # 把所选图片本身作为单帧序列（单帧同样走格式化/下游节点）。
            sources = (str(Path(path)),)
        manifest = MediaManifest(MediaKind.STATIC_SEQUENCE, sources)
        # 默认截取第一帧作为后续节点（格式化解码前）的预览图。
        return replace(manifest, preview=backend.extract_first_frame(manifest))




class GifInputNode(StudioNode):
    """gif输入。

    处理：MediaManifest(MediaKind.ANIMATED_IMAGE, (path,)) 构造 + backend.extract_first_frame（首帧预览）
    参数：path（GifFileParam 文件选择行，默认空）
    组件：无增量（默认预览框）
    """

    NODE_NAME = "gif输入"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "gif_input", self.NODE_NAME, NodeCategory.INPUT,
                icon=category_icon(NodeCategory.INPUT, "ri.file-gif-fill"),
                outputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                params=(GifFileParam("path", "GIF 文件", default=""),),
            ),
            help="导入单个 GIF 文件，\n输出格式化清单",
        )

    @classmethod
    def execute(cls, inputs: list[Any], params: dict[str, Any], backend: MediaBackend) -> MediaManifest:
        path = params.get("path", "")
        if not path:
            raise ValueError("请选择输入文件")
        manifest = MediaManifest(MediaKind.ANIMATED_IMAGE, (str(Path(path)),))
        # 默认截取第一帧作为后续节点（格式化解码前）的预览图。
        return replace(manifest, preview=backend.extract_first_frame(manifest))




class BlankSequenceNode(StudioNode):
    """空白序列：无输入 → 输出不透明纯白序列（用户定义分辨率与帧数）。

    处理：backend.blank_sequence(width, height, frames, color)
    参数：img_width/img_height/frames（IntParam spin 数值框）、key_color（ColorParam 色块按钮）
    组件：帧滑条（PanelSpec.scrub_frames）

    供「序列相加」「分辨率统一」等多输入节点作对齐基准/背景；配合
    「分辨率统一」的最近邻缩放可实现像素画风倍数放大（见决策 #69）。
    """

    NODE_NAME = "空白序列"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "blank_sequence", self.NODE_NAME, NodeCategory.INPUT,
                icon=category_icon(NodeCategory.INPUT, "mdi6.checkbox-multiple-blank"),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 注意：参数名不能叫 width/height——NodeGraphQt 模型保留
                    # 这两个属性名（model.py 默认属性表），会抛 NodePropertyError。
                    IntParam("img_width", "宽度", default=256, minimum=1, maximum=10000, widget="spin"),
                    IntParam("img_height", "高度", default=256, minimum=1, maximum=10000, widget="spin"),
                    IntParam("frames", "帧数", default=1, minimum=1, maximum=100000, widget="spin"),
                    ColorParam("key_color", "背景色", default="#ffffff"),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "无输入\n"
                "生成纯白不透明图片序列，可定义分辨率（宽×高）与帧数（均为整数）；\n"
                "供「序列相加」「分辨率统一」等多输入节点作对齐基准/背景。\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.blank_sequence(
            width=int(params["img_width"]),
            height=int(params["img_height"]),
            frames=int(params["frames"]),
            color=params["key_color"],
        )



class GradientSequenceNode(StudioNode):
    """渐变序列：无输入 → 输出线性渐变 RGB 序列（用户定义分辨率、两端颜色、角度与帧数）。

    处理：backend.gradient_sequence(width, height, frames, start_color, end_color, angle)
    参数：img_width/img_height/frames（IntParam spin 数值框）、
          start_color/end_color（ColorParam 色块按钮）、angle（FloatParam 滑条+数值框）
    组件：帧滑条（PanelSpec.scrub_frames）

    与「空白序列」同属生成型输入节点，可作「序列相加」「分辨率统一」等多输入
    节点的对齐基准/背景，也可直接作为渐变蒙版素材。
    """

    NODE_NAME = "渐变色序列"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "gradient_sequence", self.NODE_NAME, NodeCategory.INPUT,
                icon=category_icon(NodeCategory.INPUT, "mdi.gradient"),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 同「空白序列」：参数名不能叫 width/height（NodeGraphQt 模型保留）。
                    IntParam("img_width", "宽度", default=256, minimum=1, maximum=10000, widget="spin"),
                    IntParam("img_height", "高度", default=256, minimum=1, maximum=10000, widget="spin"),
                    IntParam("frames", "帧数", default=1, minimum=1, maximum=100000, widget="spin"),
                    ColorParam("start_color", "起点颜色", default="#000000"),
                    ColorParam("end_color", "终点颜色", default="#ffffff"),
                    FloatParam("angle", "渐变角度", default=0, minimum=-180, maximum=180, widget="slider"),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "无输入\n"
                "生成线性渐变图片序列\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.gradient_sequence(
            width=int(params["img_width"]),
            height=int(params["img_height"]),
            frames=int(params["frames"]),
            start_color=params["start_color"],
            end_color=params["end_color"],
            angle=float(params["angle"]),
        )
