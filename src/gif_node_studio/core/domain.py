from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class MediaKind(str, Enum):
    VIDEO = "video"
    STATIC_SEQUENCE = "static_sequence"
    ANIMATED_IMAGE = "animated_image"


@dataclass(frozen=True)
class CropSpec:
    left: float = 0.0
    top: float = 0.0
    right: float = 1.0
    bottom: float = 1.0

    def __post_init__(self) -> None:
        if not (0 <= self.left < self.right <= 1 and 0 <= self.top < self.bottom <= 1):
            raise ValueError("crop bounds must be normalized and increasing")

    def compose(self, inner: "CropSpec") -> "CropSpec":
        """Combine two normalized crop specs into one equivalent spec.

        ``self`` is applied to the original frame first, then ``inner`` crops
        within the resulting region. The result is a single spec that produces
        the same output as applying both in sequence — this keeps chained crop
        nodes consistent between preview (data-level chaining) and decode
        (single spec applied to the original source).
        """
        return CropSpec(
            self.left + inner.left * (self.right - self.left),
            self.top + inner.top * (self.bottom - self.top),
            self.left + inner.right * (self.right - self.left),
            self.top + inner.bottom * (self.bottom - self.top),
        )


def compose_trim(
    *,
    upstream_mode: str,
    upstream_start: float | int | None,
    upstream_end: float | int | None,
    own_mode: str,
    own_start_pct: float,
    own_duration: float | int,
    total_seconds: float | None = None,
    total_frames: int | None = None,
    fps: float | None = None,
) -> tuple[str, float | int | None, float | int | None]:
    """把「当前节点」的截取参数叠加上游已截取窗口，合成新的**源坐标系**绝对窗口。

    与 ``CropSpec.compose`` 同一语义：**后者基于前者的结果进一步截取**——
    当前节点的 ``start%`` 是相对上游窗口长度的百分比，``duration`` 是相对
    该窗口的持续长度（秒 / 帧数），不是取交集、也不是相对整段源。
    后级**最多只能处理前级交给它的窗口**：合成窗口按上游窗口边界钳制
    （``end`` 不超出上游窗口终点），因此「截取1(50%,30帧) → 截取2(0%,50帧)」
    的结果是前级的 30 帧而非按源坐标系外扩的 50 帧。

    - 上游未截取（``start`` 与 ``end`` 均为 ``None``）：以源全长为窗口；
    - 上游 time 窗口：长度 = end - start（开放终点用 ``total_seconds`` 补全）；
    - 上游 frame 窗口：长度 = end - start（开放终点用 ``total_frames`` 补全）；
    - 跨模式（time↔frame）换算需要 ``fps``；
    - 输出 mode 与当前节点一致（``own_mode``），边界已折算到源坐标系。

    返回 ``(mode, start, end)``；窗口为空或缺少探测数据时抛出清晰中文错误。
    """
    untrimmed = upstream_start is None and upstream_end is None
    if untrimmed:
        if own_mode == "time":
            if total_seconds is None:
                raise ValueError("无法确定源总时长，无法按百分比截取")
            start = own_start_pct / 100.0 * total_seconds
            end = start + float(own_duration)
            return "time", start, end
        if total_frames is None:
            raise ValueError("无法确定源总帧数，无法按百分比截取")
        start = round(own_start_pct / 100.0 * total_frames)
        end = start + int(own_duration)
        return "frame", start, end

    if upstream_mode == "time":
        base = float(upstream_start) if upstream_start is not None else 0.0
        length = (
            float(upstream_end) - base
            if upstream_end is not None
            else ((total_seconds - base) if total_seconds is not None else None)
        )
        if length is None:
            raise ValueError("无法确定上游时间窗口长度")
        if length <= 0:
            raise ValueError("上游截取窗口为空，无法继续截取")
        if own_mode == "time":
            start = base + own_start_pct / 100.0 * length
            end = start + float(own_duration)
            # 后级最多只能处理前级交给它的窗口：终点不超出上游窗口。
            end = min(end, base + length)
            return "time", start, end
        if fps is None:
            raise ValueError("无法合成跨模式截取：缺少视频帧率")
        window_first = round(base * fps)
        window_len = round(length * fps)
        start = window_first + round(own_start_pct / 100.0 * window_len)
        end = start + int(own_duration)
        # 后级最多只能处理前级交给它的窗口：终点不超出上游窗口。
        end = min(end, window_first + window_len)
        return "frame", start, end

    # upstream_mode == "frame"
    base = int(upstream_start) if upstream_start is not None else 0
    length = (
        int(upstream_end) - base
        if upstream_end is not None
        else ((total_frames - base) if total_frames is not None else None)
    )
    if length is None:
        raise ValueError("无法确定上游帧窗口长度")
    if length <= 0:
        raise ValueError("上游截取窗口为空，无法继续截取")
    if own_mode == "frame":
        start = base + round(own_start_pct / 100.0 * length)
        end = start + int(own_duration)
        # 后级最多只能处理前级交给它的窗口：终点不超出上游窗口。
        end = min(end, base + length)
        return "frame", start, end
    if fps is None:
        raise ValueError("无法合成跨模式截取：缺少视频帧率")
    window_start_s = base / fps
    window_len_s = length / fps
    start = window_start_s + own_start_pct / 100.0 * window_len_s
    end = start + float(own_duration)
    # 后级最多只能处理前级交给它的窗口：终点不超出上游窗口。
    end = min(end, window_start_s + window_len_s)
    return "time", start, end


@dataclass(frozen=True)
class MediaManifest:
    kind: MediaKind
    sources: tuple[str, ...]
    start: float | int | None = None
    end: float | int | None = None
    crop: CropSpec = CropSpec()
    scale_percent: int = 100
    range_mode: str = "time"
    preview: str | None = None

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError("at least one source is required")
        if not 1 <= self.scale_percent <= 100:
            raise ValueError("scale_percent must be between 1 and 100")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaManifest":
        values = dict(data)
        values["kind"] = MediaKind(values["kind"])
        values["sources"] = tuple(values["sources"])
        values["crop"] = CropSpec(**values.get("crop", {}))
        return cls(**values)


@dataclass(frozen=True)
class SequenceArtifact:
    """格式化解码产物（图片序列）。

    不再携带帧率/帧速信息（用户需求）：需要帧率工作的节点（GIF 合成、抽帧）
    均由用户把帧速作为参数输入。
    """

    frames: tuple[str, ...]
    width: int
    height: int
    has_alpha: bool
    cache_dir: str

    def validate_files(self) -> None:
        for frame in self.frames:
            if not Path(frame).is_file():
                raise FileNotFoundError(frame)


@dataclass(frozen=True)
class AnalysisResult:
    """分析类节点输出：预览图路径 + 附加元数据。

    描述与预览的统一出口：``describe_output``（默认行为）合并文件信息与
    ``metadata``，``preview_path_for_node`` 取 ``path`` 显示。
    ``frames`` 供「图片1:1分辨率查看」等节点携带可逐帧滑条查看的帧路径
    （分析节点自身不做全量物化）。
    """

    path: str | Path
    metadata: dict[str, Any] = field(default_factory=dict)
    frames: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MultiOutput:
    """多输出端口节点的输出容器：``ports = {端口名: 值}``。

    端口名与 ``NodeDefinition.outputs`` 中的端口名一致；执行计划按上游
    输出端口名取出对应值喂给下游（见 ``ui._execution_plan`` / ``_execute_step``）。
    ``preview_path_for_node`` 对多输出节点取首个可显示通道；``describe_output``
    按端口名逐通道摘要。

    ``metadata`` 为可选的节点自定义摘要（如 GIF 优化节点的优化前/后大小）；
    元数据展示由**节点自身定义**（``StudioNode.describe_output`` 继承实现）：
    覆写了展示的节点（如导出终端节点）仅显示该摘要（不再逐端口展开），
    其余多输出节点在端口摘要之后合并显示。
    """

    ports: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
