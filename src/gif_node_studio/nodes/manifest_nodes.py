"""预格式化/格式化节点：清单族（时间截取/帧截取/裁剪/格式化）。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..core.domain import MediaKind, MediaManifest, compose_trim
from ..media.media_info import source_frame_count, video_duration_seconds
from .definitions import (
    ChoiceParam,
    FloatParam,
    IntParam,
    NodeCategory,
    NodeDefinition,
    PanelSpec,
    PortDefinition,
    PortType,
)
from .icon_resource import category_icon
from .node_base import StudioNode
from .parameter_panel import ParameterPanel

class ManifestNode(StudioNode):
    """清单族基类：只拥有共享的输入校验。"""

    @classmethod
    def manifest(cls, inputs: list[Any]) -> MediaManifest:
        return cls.require_input(inputs)




class TimeTrimNode(ManifestNode):
    """时间截取（清单 → 清单，仅视频）。

    处理：compose_trim 串行窗口合成 + backend.extract_start_frame（截取起点预览）
    参数：start（FloatParam 滑条+数值框，0..100 %）、duration（FloatParam spin 数值框）
    组件：无增量（默认预览框）
    """

    NODE_NAME = "时间截取"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "time_trim", self.NODE_NAME, NodeCategory.PREFORMAT,
                icon=category_icon(NodeCategory.PREFORMAT, "mdi6.timeline-clock"),
                inputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                outputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                params=(
                    FloatParam("start", "开始 %", default=0.0, minimum=0, maximum=100),
                    FloatParam("duration", "持续秒数", default=5.0, minimum=0.1, maximum=3600, widget="spin"),
                ),
            ),
            help="输入格式化清单（仅视频）\n滑条定义起点百分比，数值框指定持续秒数；\n输出格式化清单。",
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        manifest = cls.manifest(inputs)
        if manifest.kind is not MediaKind.VIDEO:
            raise ValueError("时间截取仅支持视频输入，当前输入为不支持的类型")
        duration = video_duration_seconds(manifest.sources[0])
        if not duration:
            raise ValueError("无法获取视频总时长，无法按百分比截取")
        total_frames = source_frame_count(manifest)
        fps = (total_frames / duration) if total_frames else None
        # 串行合成：start% 相对上游窗口（未截取时相对源全长），duration 为持续秒数；
        # 与 CropSpec.compose 同语义——后者基于前者的结果进一步截取，而非取交集。
        mode, start, end = compose_trim(
            upstream_mode=manifest.range_mode,
            upstream_start=manifest.start,
            upstream_end=manifest.end,
            own_mode="time",
            own_start_pct=float(params["start"]),
            own_duration=float(params["duration"]),
            total_seconds=duration,
            total_frames=total_frames,
            fps=fps,
        )
        trimmed = replace(manifest, range_mode=mode, start=start, end=end)
        # 运行后（未接格式化时）在预览框显示截取起点帧。
        preview = backend.extract_start_frame(trimmed) if backend is not None else None
        if preview is not None:
            trimmed = replace(trimmed, preview=preview)
        return trimmed




class FrameTrimNode(ManifestNode):
    """帧位截取（清单 → 清单）。

    处理：compose_trim 串行窗口合成 + backend.extract_start_frame（截取起点预览）
    参数：start（FloatParam 滑条+数值框，0..100 %）、duration（IntParam spin 数值框）
    组件：无增量（默认预览框）
    """

    NODE_NAME = "帧位截取"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "frame_trim", self.NODE_NAME, NodeCategory.PREFORMAT,
                icon=category_icon(NodeCategory.PREFORMAT, "mdi6.timeline"),
                inputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                outputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                params=(
                    FloatParam("start", "开始 %", default=0.0, minimum=0, maximum=100),
                    IntParam("duration", "持续帧数", default=30, minimum=1, maximum=100000, widget="spin"),
                ),
            ),
            help="输入格式化清单\n滑条定义起点百分比，数值框指定持续帧数（整数；）\n输出格式化清单。",
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        manifest = cls.manifest(inputs)
        total = source_frame_count(manifest)
        if not total:
            raise ValueError("无法确定源总帧数，无法按百分比截取")
        fps = None
        duration = None
        if manifest.kind is MediaKind.VIDEO:
            duration = video_duration_seconds(manifest.sources[0])
            fps = (total / duration) if duration else None
        # 串行合成：start% 相对上游窗口（未截取时相对源全长），duration 为持续帧数；
        # 上游为时间窗口（视频）时按 fps 换算为帧窗口，进一步截取。
        mode, start, end = compose_trim(
            upstream_mode=manifest.range_mode,
            upstream_start=manifest.start,
            upstream_end=manifest.end,
            own_mode="frame",
            own_start_pct=float(params["start"]),
            own_duration=int(params["duration"]),
            total_seconds=duration,
            total_frames=total,
            fps=fps,
        )
        trimmed = replace(manifest, range_mode=mode, start=start, end=end)
        # 运行后（未接格式化时）在预览框显示截取起点帧。
        preview = backend.extract_start_frame(trimmed) if backend is not None else None
        if preview is not None:
            trimmed = replace(trimmed, preview=preview)
        return trimmed




class FormatNode(ManifestNode):
    """Name intentionally kept stable because NodeGraphQt serializes it.

    处理：backend.format_manifest（清单 → 序列，解码）
    参数：scale_percent（IntParam 滑条+数值框，1..100 %）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "格式化"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "format", self.NODE_NAME, NodeCategory.FORMAT,
                icon=category_icon(NodeCategory.FORMAT, "mdi6.format-list-group"),  # mdi.buffer
                inputs=(PortDefinition("格式化清单", PortType.MANIFEST),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(IntParam("scale_percent", "解码分辨率 %", default=100, minimum=1, maximum=100),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入格式化清单\n解码节点，将清单格式化为图片序列；\n输出图片序列。"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        manifest = replace(
            cls.manifest(inputs),
            scale_percent=int(params["scale_percent"]),
        )
        return backend.format_manifest(manifest)
