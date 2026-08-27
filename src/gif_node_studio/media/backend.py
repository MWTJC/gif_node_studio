"""媒体后端：MediaBackend 单类。

2026-08 代码整理（见关键决策 #99）：撤销 #82 的 mixin 拆分，与 MainWindow 同源
问题（决策 #98）——mixin 内跨文件引用无法被 IDE 静态分析定位。现收敛为单类：
七个职责区段（格式化/颜色/序列/导出/量化/分析/缓存）以区段注释分组；无状态
辅助提升为模块级纯函数。

- ``palettes.py`` —— 调色板/阈值图辅助；
- ``image_utils.py`` —— wand/PIL 图像底层辅助。

本文件 = MediaBackend 完整类（实例状态 + 全部行为）。
"""

from __future__ import annotations

import io
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import uuid4

import av
import numpy as np
from PIL import Image, ImageColor, ImageEnhance

from ..core.domain import CropSpec, MediaKind, MediaManifest, SequenceArtifact
from ..core.options import PAN_DIRECTION, PAN_INTERPOLATION, RESAMPLE, SCALE_STRATEGY, SPEED_CURVE
from .gifsicle import (
    CREATE_NO_WINDOW_FLAG,
    GIFSICLE_TIMEOUT_S,
    build_gifsicle_args,
    configure_gifsicle,
    require_gifsicle,
)
from .ffmpeg_gif import encode_gif_frames
from .image_utils import (
    DEFAULT_SEQUENCE_FPS,
    ICON_SIZES,
    PNG_CACHE_COMPRESS_LEVEL,
    PNG_EXPORT_WORKERS,
    _bmp_dib_bytes,
    _sample_source_indices,
    _save_wand_png,
    _wand_quantize_all,
    _wand_rgba_bytes,
)
from .imagemagick import ImageMagickRuntime, configure_imagemagick, require_wand
from .media_info import (
    frame_optimization_ratio,
    gif_native_fps,
    gif_palette_entries,
    gif_playback_info,
)
from .palettes import _palette_png_blob, _websafe_map_blob, system_palette_blob

ProgressReporter = Callable[[float | None, str], None]

# ---------------------------------------------------------------------------
# 模块级纯函数（无窗口状态；原模块级函数 + 由 mixin 提升，决策 #99）
# ---------------------------------------------------------------------------


def _cubic_bezier_ease_out(p: float, p1x: float, p1y: float, p2x: float, p2y: float) -> float:
    """求值 cubic-bezier(0,0 → (p1x,p1y) → (p2x,p2y) → 1,1) 在 x=p 处的 y 值。

    Win10/Fluent 减速曲线 ``cubic-bezier(0.1, 0.9, 0.2, 1.0)`` 的求值器：
    牛顿迭代解 ``x(t) = p``（曲线的 x 分量单调递增）后取 ``y(t)``。
    起步快（t=0 处切线斜率 3·0.9）、平滑减速至停（t=1 处切线斜率 0）。
    """
    def point(t: float) -> tuple[float, float]:
        u = 1.0 - t
        x = 3.0 * u * u * t * p1x + 3.0 * u * t * t * p2x + t * t * t
        y = 3.0 * u * u * t * p1y + 3.0 * u * t * t * p2y + t * t * t
        return x, y

    t = p  # 初始猜测：曲线接近线性，一次迭代即可达到足够精度
    for _ in range(12):
        x, _y = point(t)
        error = x - p
        if abs(error) < 1e-9:
            break
        # dx/dt = 3(1−t)²·p1x + 6(1−t)t·p2x + 3t²（x 分量切线的解析导数）
        dx_dt = 3.0 * (1.0 - t) * (1.0 - t) * p1x + 6.0 * (1.0 - t) * t * p2x + 3.0 * t * t
        if dx_dt < 1e-9:
            break
        t -= error / dx_dt
        t = min(1.0, max(0.0, t))
    return point(t)[1]




def _wrap_shift_array(array: np.ndarray, shift: float, axis: int, mode: str) -> np.ndarray:
    """沿指定轴做无缝循环位移：``out[i] = in[(i − shift) mod n]``。

    - 位移取模归一化到 ``[0, n)``，负位移（向左/向上）同样成立；
    - ``mode == "nearest"``：位移四舍五入取整后纯 ``np.roll``（零插值，像素锐利）；
    - ``mode == "bilinear"``：整数部分 ``np.roll`` + 小数部分线性插值
      （``out[i] = (1−f)·in[i−k] + f·in[i−k−1]``，模 n 天然无缝，
      平移扫过的边缘不存在缝隙/接缝）。
    """
    n = array.shape[axis]
    shift_mod = shift % n
    if mode == "nearest":
        integer = int(math.floor(shift_mod + 0.5))
        return np.roll(array, integer, axis=axis)
    integer = int(math.floor(shift_mod))
    fraction = shift_mod - integer
    shifted = np.roll(array, integer, axis=axis)
    if fraction < 1e-9:
        return shifted
    shifted = shifted.astype(np.float32)
    blended = (1.0 - fraction) * shifted + fraction * np.roll(shifted, 1, axis=axis)
    return np.clip(blended, 0, 255).astype(np.uint8)


# 速度曲线：机器键 → 进度 p∈[0,1] 到位移比例 curve(p)∈[0,1]（单调不减）。
_SPEED_CURVES: dict[str, Callable[[float], float]] = {
    "linear": lambda p: p,
    "accelerate": lambda p: p * p,
    "decelerate": lambda p: 1.0 - (1.0 - p) ** 2,
    "win10_decelerate": lambda p: _cubic_bezier_ease_out(p, 0.1, 0.9, 0.2, 1.0),
}




def _sequence_within_color_budget(frames, budget: int = 256) -> bool:
    """整条序列的**跨帧颜色并集**是否 ≤ budget（GIF 全局色表上限 256）。

    - 逐帧 PIL ``getcolors(budget)``：单帧颜色数 > budget 返回 None → False；
    - 并集用 Python set 累积（每帧 ≤ budget 项，内存有界），超过 budget 即
      短路返回 False；
    - RGBA 计数（含 Alpha）：对 GIF 1-bit 透明语义偏保守，安全；
    - 供 ``_assemble_gif`` 判定「已 ≤256 色 → 跳过二次量化」（决策 #97）。
    """
    seen: set[tuple[int, int, int, int]] = set()
    for path in frames:
        with Image.open(path) as image:
            colors = image.convert("RGBA").getcolors(budget)
            if colors is None:
                return False
            for _count, color in colors:
                seen.add(color)
                if len(seen) > budget:
                    return False
    return True






def _remove_path(path: Path, attempts: int = 5) -> None:
    """Remove a cache path, tolerating short Windows handle-release races."""
    for attempt in range(attempts):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))







def _in_range(manifest: MediaManifest, index: int, seconds: float) -> bool:
    """范围过滤：清单携带的合成窗口（time 按秒、frame 按帧号）单份生效。"""
    if manifest.range_mode == "time":
        if manifest.start is not None and seconds + 1e-9 < float(manifest.start):
            return False
        if manifest.end is not None and seconds >= float(manifest.end):
            return False
    else:
        if manifest.start is not None and index < int(manifest.start):
            return False
        if manifest.end is not None and index >= int(manifest.end):
            return False
    return True







def _frame_index(frame: av.VideoFrame, stream: av.VideoStream, fps: float) -> int | None:
    """由 PTS 推算全局帧号；PTS 缺失时返回 None（调用方回退为计数）。"""
    pts = frame.pts
    if pts is None or stream.time_base is None:
        return None
    start_pts = getattr(stream, "start_time", None) or 0
    index = round((pts - start_pts) * float(stream.time_base) * fps)
    return max(0, index)





def _frame_to_image(frame: av.VideoFrame, manifest: MediaManifest) -> Image.Image:
    """单帧处理：swscale 一步完成缩放+RGBA 转换（先缩放后裁剪，省内存）。"""
    scale = manifest.scale_percent / 100
    sw = max(1, round(frame.width * scale))
    sh = max(1, round(frame.height * scale))
    rgba = frame.reformat(width=sw, height=sh, format="rgba").to_image()
    c = manifest.crop
    w, h = rgba.size
    box = (round(c.left * w), round(c.top * h), round(c.right * w), round(c.bottom * h))
    return rgba.crop(box)





def _crop_scale(image: Image.Image, manifest: MediaManifest) -> Image.Image:
    w, h = image.size
    c = manifest.crop
    box = (round(c.left * w), round(c.top * h), round(c.right * w), round(c.bottom * h))
    image = image.crop(box)
    if manifest.scale_percent != 100:
        scale = manifest.scale_percent / 100
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return image

# ------------------------------------------------------------------
# 序列逐帧 PNG 导出（并行）：PNG 编码是 CPU 密集操作，编码期间 GIL 释放
# （Pillow zlib 段 / wand 经 ctypes 调 C 库），多线程可真实利用多核。
# 实测（30 帧 1920×1080 RGBA）：wand 默认编码串行 ≈6.2s → 并行 ≈1.6s；
# PIL 串行 ≈1.4s → 并行 ≈0.4s。克隆本身几乎免费（MagickCloneImage
# 30 帧 ≈ 2ms），瓶颈在编码器。
# ------------------------------------------------------------------







def _scale_to_canvas(
    image: Image.Image,
    width: int,
    height: int,
    strategy: str,
    resampler: Image.Resampling,
) -> Image.Image:
    """把单帧缩放到 ``(width, height)`` 画布（见 ``concat_sequences`` 的策略语义）。

    - ``none``（不缩放）：保持原尺寸——单轴超出画布时该轴居中裁剪到
      画布大小（``box`` 按原图坐标），未超出轴保持原尺寸；裁剪/原图
      居中摆在透明画布上，未覆盖区域为全透明黑。
    """
    src_w, src_h = image.size
    if strategy == "stretch":
        return image.resize((width, height), resampler)
    if strategy == "fill":
        # cover：保纵横比放大到铺满画布，溢出部分居中裁剪。
        scale = max(width / src_w, height / src_h)
        new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
        scaled = image.resize((new_w, new_h), resampler)
        left, top = (new_w - width) // 2, (new_h - height) // 2
        return scaled.crop((left, top, left + width, top + height))
    if strategy == "none":
        # 不缩放：单轴超出画布 → 该轴居中裁剪到画布大小；未超出 → 保持原尺寸。
        box_left = max(0, (src_w - width) // 2)
        box_top = max(0, (src_h - height) // 2)
        box_w, box_h = min(src_w, width), min(src_h, height)
        if src_w > width or src_h > height:
            image = image.crop((box_left, box_top, box_left + box_w, box_top + box_h))
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        # 注意：不能用 canvas.paste(image, (x, y), image)——paste 的 mask 语义
        # 是按 mask 透明度**混合**两图（半透明像素会被叠淡，颜色与 alpha 同时
        # 衰减），alpha_composite 才是「src over dst」的正确合成。
        canvas.alpha_composite(image, dest=((width - box_w) // 2, (height - box_h) // 2))
        return canvas
    # fit：contain——保纵横比缩小到完整容纳，未填满区域为全透明黑。
    scale = min(width / src_w, height / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    scaled = image.resize((new_w, new_h), resampler)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    canvas.alpha_composite(scaled, dest=((width - new_w) // 2, (height - new_h) // 2))
    return canvas


def _frame_optimization_label(durations_ms: list[int], frame_count: int) -> str:
    """帧优化占比展示文本：相对等时长全最短帧序列节省的帧数比例。

    以最短帧时间为基准（``media_info.frame_optimization_ratio``）：等时长
    全最短帧序列帧数 = 总时长 ÷ 最短帧时间，占比 = 1 − 实际帧数 ÷ 基准帧数。
    无法计算（帧时间缺失或含 0、无帧）时显示原因，不隐藏该统计。
    """
    ratio_info = frame_optimization_ratio(durations_ms, frame_count)
    if ratio_info is None:
        return "—（帧时间缺失或含 0，无法以最短帧为基准）"
    ratio, baseline, actual = ratio_info
    baseline_text = f"{baseline:.0f}" if float(baseline).is_integer() else f"{baseline:.1f}"
    return f"节省 {ratio * 100:.1f}%（等时长全最短帧 {baseline_text} 帧 → 实际 {actual} 帧）"





class MediaBackend:
    """节点执行后端：解码、变换、编码、探测、缓存管理（方法见各区段）。"""

    def __init__(
        self,
        workspace: str | Path,
        root_workspace: str | Path | None = None,
        imagemagick: ImageMagickRuntime | None = None,
        progress_callback: ProgressReporter | None = None,
    ):
        self.workspace = Path(workspace)
        self.root_workspace = Path(root_workspace) if root_workspace is not None else self.workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.imagemagick = imagemagick or configure_imagemagick()
        self.progress_callback = progress_callback

    def for_node(self, node_id: str, progress_callback: ProgressReporter | None = None) -> "MediaBackend":
        safe_id = "".join(character if character.isalnum() or character in "-_" else "_" for character in node_id)
        return MediaBackend(
            self.root_workspace / "nodes" / safe_id,
            self.root_workspace,
            self.imagemagick,
            progress_callback if progress_callback is not None else self.progress_callback,
        )

    def _progress(self, fraction: float | None, label: str) -> None:
        if self.progress_callback:
            self.progress_callback(fraction, label)

    def _job_dir(self, prefix: str) -> Path:
        path = self.workspace / f"{prefix}_{uuid4().hex[:10]}"
        path.mkdir(parents=True)
        return path

    # ===== 区段 1：解码/格式化/预览提取（原 MediaBackendFormatMixin） =====

    def extract_first_frame(self, manifest: MediaManifest) -> str:
        """Return a representative first-frame image path for the manifest.

        Static sequences already contain images, so their first source is used
        directly; video and animated images get one frame materialized as PNG
        into this backend's workspace.
        """
        if manifest.kind is MediaKind.STATIC_SEQUENCE:
            return manifest.sources[0]
        output = self._job_dir("preview")
        target = output / "frame_000000.png"
        if manifest.kind is MediaKind.ANIMATED_IMAGE:
            with Image.open(manifest.sources[0]) as image:
                image.seek(0)
                image.convert("RGBA").save(target, "PNG")
            return str(target)
        with av.open(manifest.sources[0]) as container:
            stream = container.streams.video[0]
            for frame in container.decode(stream):
                frame.to_image().convert("RGBA").save(target, "PNG")
                return str(target)
        raise ValueError("无法提取视频首帧")


    # ------------------------------------------------------------------
    # 截取起点帧预览（时间/帧位截取节点运行后，未格式化时显示）
    # ------------------------------------------------------------------


    def extract_start_frame(self, manifest: MediaManifest) -> str | None:
        """物化截取起点的帧作为预览图；尽力而为，失败返回 None 不中断运行。

        - time 模式：视频 PyAV seek 到起点秒解码；
        - frame 模式：静态序列直接引用源文件，GIF 用 PIL 定位帧号，
          视频按帧号换算秒后 seek 解码。
        """
        try:
            if manifest.range_mode == "time":
                if manifest.kind is not MediaKind.VIDEO:
                    return None
                seconds = float(manifest.start) if manifest.start is not None else 0.0
                return self._extract_video_frame(manifest.sources[0], seconds)
            index = int(manifest.start) if manifest.start is not None else 0
            if manifest.kind is MediaKind.STATIC_SEQUENCE:
                if not manifest.sources:
                    return None
                return manifest.sources[min(index, len(manifest.sources) - 1)]
            if manifest.kind is MediaKind.ANIMATED_IMAGE:
                with Image.open(manifest.sources[0]) as image:
                    image.seek(index)
                    target = self._job_dir("preview") / "frame.png"
                    image.convert("RGBA").save(target, "PNG")
                    return str(target)
            with av.open(manifest.sources[0]) as container:
                stream = container.streams.video[0]
                fps = float(stream.average_rate or stream.base_rate or 1.0)
                return self._extract_video_frame(container, index / fps)
        except Exception:
            return None


    def _extract_video_frame(self, container_or_path, seconds: float) -> str | None:
        """解码视频在 seconds 时刻的帧并物化为 PNG；失败返回 None。"""
        try:
            if isinstance(container_or_path, (str, Path)):
                with av.open(container_or_path) as container:
                    return self._extract_video_frame(container, seconds)
            container = container_or_path
            stream = container.streams.video[0]
            if seconds > 0 and stream.time_base is not None:
                offset = int(seconds * stream.time_base.denominator / stream.time_base.numerator)
                container.seek(offset, stream=stream, backward=True)
            for frame in container.decode(stream):
                current = float(frame.time) if frame.time is not None else 0.0
                if current < seconds - 1e-9:
                    continue  # backward seek 落在关键帧，跳过窗口前的帧
                target = self._job_dir("preview") / "frame.png"
                frame.to_image().convert("RGBA").save(target, "PNG")
                return str(target)
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------
    # 格式化（三种源统一流式：逐帧处理、立即写盘，内存峰值 ≈ 单帧）
    # ------------------------------------------------------------------


    def format_manifest(self, manifest: MediaManifest) -> SequenceArtifact:
        """物化源窗口内全部帧为图片序列。

        序列产物**不携带帧率/帧速信息**（用户需求）：帧率只作为格式化解码
        内部的窗口换算依据（视频流帧率 / GIF 帧时间换算 / 静态序列默认 12），
        不写入产物；需要帧率的节点（GIF 合成、抽帧）由用户把帧速作为参数输入。
        """
        output = self._job_dir("format")
        if manifest.kind is MediaKind.STATIC_SEQUENCE:
            return self._format_static_sequence(manifest, output)
        if manifest.kind is MediaKind.ANIMATED_IMAGE:
            return self._format_animated_image(manifest, output)
        return self._format_video(manifest, output)

    def _format_static_sequence(self, manifest: MediaManifest, output: Path) -> SequenceArtifact:
        total = len(manifest.sources)

        def process(index: int) -> Image.Image | None:
            path = manifest.sources[index]
            seconds = index / DEFAULT_SEQUENCE_FPS
            if not _in_range(manifest, index, seconds):
                return None
            with Image.open(path) as image:
                return _crop_scale(image.convert("RGBA"), manifest)

        written = self._parallel_pil_export(total, output, "处理静态序列", process)
        if not written:
            raise ValueError("selected range produced no frames")
        # 尺寸以首帧为准（产物帧尺寸一致）。
        with Image.open(written[0]) as image:
            width, height = image.size
        return SequenceArtifact(written, width, height, True, str(output))


    def _format_animated_image(self, manifest: MediaManifest, output: Path) -> SequenceArtifact:
        """GIF 输入：Wand 合并部分帧（coalesce）后逐帧导出，再走裁剪/缩放。

        注意：这里不再安装 MagickSetImageProgressMonitor 进度监视器——实测该
        监视器与 wand/ImageMagick 组合会产生随机性原生内存崩溃（access
        violation / illegal instruction，进程内偶发），导致解包失败；进度改为
        逐帧显式上报（``self._progress``），coalesce 阶段显示不确定进度。

        帧率仅用于窗口换算（帧号 → 秒，见 ``gif_native_fps``），不写入产物。
        """
        require_wand(self.imagemagick, "GIF 解码")
        from wand.image import Image as WandImage

        fps = gif_native_fps(manifest.sources[0])
        written: list[str] = []
        width = height = 0
        # 解码循环只做「原始 RGBA 字节直取 + 裁剪/缩放」，不再走
        # 「wand PNG 编码 → PIL 解码」的双重编码（实测主循环 40ms/帧 → 1.6ms/帧）；
        # 裁剪/缩放仍在 PIL 完成（与其它格式化路径同语义），PNG 编码交给
        # 有界线程池并行（内存上界 ≈ PNG_EXPORT_WORKERS×2 帧），解码流式不整段缓冲。
        save_sem = threading.BoundedSemaphore(PNG_EXPORT_WORKERS * 2)
        futures: list[Future] = []
        with WandImage(filename=manifest.sources[0]) as gif:
            gif.coalesce()
            total = len(gif.sequence)
            with ThreadPoolExecutor(max_workers=PNG_EXPORT_WORKERS) as pool:
                for index in range(total):
                    seconds = index / fps
                    if not _in_range(manifest, index, seconds):
                        continue
                    frame = gif.sequence[index]
                    # 注意：不要强制 alpha_channel=True —— 实测会把整个帧的 alpha 清零
                    # （无 disposal 的 GIF 全部帧透明、disposal=2 的 GIF 首帧透明），
                    # 导致解包出的帧变成“看不见”的透明图。coalesce 后的帧已自带
                    # 正确的 alpha 掩码；_wand_rgba_bytes 只读像素，不做任何 alpha 操作。
                    raw, frame_width, frame_height = _wand_rgba_bytes(frame)
                    image = Image.frombytes("RGBA", (frame_width, frame_height), raw)
                    image = _crop_scale(image, manifest)
                    width, height = image.size
                    target = output / f"frame_{len(written):06d}.png"
                    save_sem.acquire()
                    futures.append(pool.submit(self._parallel_pil_save_bounded, image, target, save_sem))
                    written.append(str(target))
                    self._progress((index + 1) / total, "解包 GIF")
                # 等待全部保存完成（异常在此重新抛出）。
                self._drain_save_futures(futures)
        if not written:
            raise ValueError("selected range produced no frames")
        return SequenceArtifact(tuple(written), width, height, True, str(output))


    def _format_video(self, manifest: MediaManifest, output: Path) -> SequenceArtifact:
        """视频流式解码：seek 到窗口起点、解码到窗口终点即停、逐帧写盘即弃。

        对比旧实现（整段视频全部解码并常驻内存后过滤），本方法把内存峰值
        从“全部帧 × RGBA”降到“单帧”；同时只解码 [start, end) 窗口内的帧。

        清单携带的截取窗口已是**逐级合成**后的单份绝对窗口（时间截取 + 帧位
        截取等任意链式组合在节点处折算为源坐标系，见 compose_trim）。窗口内
        的帧全部保留（改帧速请接「抽帧」节点，帧速由用户参数输入）。
        帧率仅用于窗口换算（秒↔帧号、进度预估），不写入产物。
        """
        written: list[str] = []
        width = height = 0
        time_mode = manifest.range_mode == "time"
        with av.open(manifest.sources[0]) as container:
            stream = container.streams.video[0]
            fps = float(stream.average_rate or stream.base_rate or DEFAULT_SEQUENCE_FPS)
            duration_s = (
                container.duration / 1_000_000 if getattr(container, "duration", None) else None
            )
            if time_mode:
                window_start = float(manifest.start) if manifest.start is not None else 0.0
                window_end = float(manifest.end) if manifest.end is not None else duration_s
                start_index = 0
                end_index = None
            else:
                start_index = int(manifest.start) if manifest.start is not None else 0
                end_index = int(manifest.end) if manifest.end is not None else None
                window_start = start_index / fps
                window_end = (end_index / fps) if end_index is not None else duration_s
            # 预估窗口帧数，用于进度条；未知时进度为不确定态。
            estimated = 0
            if window_end is not None:
                estimated = max(1, round((window_end - window_start) * fps))
            elif duration_s is not None:
                estimated = max(1, round(duration_s * fps))

            if window_start and window_start > 0 and stream.time_base is not None:
                offset = int(window_start * stream.time_base.denominator / stream.time_base.numerator)
                container.seek(offset, stream=stream, backward=True)

            decoded = 0
            # 保存与解码解耦：解码循环只负责解出 PIL 帧，保存交给有界线程池并行
            # （内存上界 ≈ PNG_EXPORT_WORKERS×2 帧），解码流式不整段缓冲。
            save_sem = threading.BoundedSemaphore(PNG_EXPORT_WORKERS * 2)
            futures: list[Future] = []
            with ThreadPoolExecutor(max_workers=PNG_EXPORT_WORKERS) as pool:
                for frame in container.decode(stream):
                    seconds = float(frame.time) if frame.time is not None else decoded / fps
                    if seconds < window_start - 1e-9:
                        continue  # backward seek 会落在关键帧，先跳过窗口前的帧
                    if window_end is not None and seconds >= window_end:
                        break
                    decoded += 1
                    take = False
                    if time_mode:
                        take = True  # 时间窗内全部帧保留（不再按采样帧率抽样）
                    else:
                        index = _frame_index(frame, stream, fps)
                        if index is None:
                            index = start_index + decoded - 1
                        if start_index <= index and (end_index is None or index < end_index):
                            take = True
                    if take:
                        image = _frame_to_image(frame, manifest)
                        width, height = image.size
                        target = output / f"frame_{len(written):06d}.png"
                        save_sem.acquire()
                        futures.append(pool.submit(self._parallel_pil_save_bounded, image, target, save_sem))
                        written.append(str(target))
                    if decoded % 10 == 0:
                        fraction = min(0.99, decoded / estimated) if estimated else None
                        self._progress(fraction, f"解码视频 {decoded} 帧")
                # 等待全部保存完成（异常在此重新抛出）。
                self._drain_save_futures(futures)
        if not written:
            raise ValueError("selected range produced no frames")
        return SequenceArtifact(tuple(written), width, height, True, str(output))

    def _parallel_pil_export(
        self,
        total: int,
        output: Path,
        label: str,
        process: Callable[[int], Image.Image | None],
        *,
        compress_level: int = PNG_CACHE_COMPRESS_LEVEL,
    ) -> tuple[str, ...]:
        """并行逐帧处理 + 保存 PNG（Pillow 路径）。

        ``process(index)`` 在 worker 线程内打开/处理第 index 帧并返回 PIL Image
        （返回 ``None`` 表示跳过该帧，如超出截取窗口）。worker 先保存到临时名，
        主线程按源顺序整理为 ``frame_{顺序号:06d}.png``（``os.replace`` 同目录
        重命名，原子且廉价），保证命名与旧实现一致。内存峰值 ≈ 并发 worker ×
        单帧（worker 内的图像随保存完成即释放，不整段缓冲）。
        """
        output = Path(output)
        tmp = [output / f".tmp_{index:06d}.png" for index in range(total)]

        def job(index: int):
            image = process(index)
            if image is None:
                return None
            image.save(tmp[index], "PNG", compress_level=compress_level)
            return index

        with ThreadPoolExecutor(max_workers=PNG_EXPORT_WORKERS) as pool:
            finished = set(pool.map(job, range(total)))
        written: list[str] = []
        for index in range(total):
            if index in finished:
                final = output / f"frame_{len(written):06d}.png"
                os.replace(tmp[index], final)
                written.append(str(final))
            self._progress((index + 1) / total, label)
        return tuple(written)


    def _parallel_pil_save_bounded(
        self, image: Image.Image, target: Path, semaphore: threading.BoundedSemaphore
    ) -> None:
        """有界并行保存单帧 PNG（供流式解码路径调用，worker 线程执行）。

        ``semaphore`` 限制在途未保存帧数（≈ 内存上界），保存完成后释放，
        使解码循环可以继续提交下一帧，不整段缓冲。
        """
        try:
            image.save(target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL)
        finally:
            semaphore.release()


    def _drain_save_futures(self, futures: list[Future]) -> None:
        """等待流式解码路径提交的全部保存任务完成；异常在此重新抛出。"""
        for future in futures:
            future.result()

    # ------------------------------------------------------------------
    # 序列变换与导出
    # ------------------------------------------------------------------

    # ===== 区段 2：颜色处理（原 MediaBackendColorMixin） =====

    def adjust_color(
        self, artifact: SequenceArtifact, brightness: float = 0, saturation: float = 0, hue: float = 0
    ) -> SequenceArtifact:
        output = self._job_dir("color")
        total = len(artifact.frames)
        brightness, saturation, hue = float(brightness), float(saturation), float(hue)

        def process(index: int) -> Image.Image:
            image = Image.open(artifact.frames[index]).convert("RGBA")
            alpha = image.getchannel("A")
            rgb = image.convert("RGB")
            rgb = ImageEnhance.Brightness(rgb).enhance(max(0, 1 + brightness / 100))
            rgb = ImageEnhance.Color(rgb).enhance(max(0, 1 + saturation / 100))
            if hue:
                hsv = rgb.convert("HSV")
                h, s, v = hsv.split()
                h = h.point(lambda value: (value + round(hue / 360 * 255)) % 256)
                rgb = Image.merge("HSV", (h, s, v)).convert("RGB")
            rgba = rgb.convert("RGBA")
            rgba.putalpha(alpha)
            return rgba

        paths = self._parallel_pil_export(total, output, "色彩调整", process)
        return SequenceArtifact(paths, artifact.width, artifact.height, True, str(output))


    def binarize_sequence(self, artifact: SequenceArtifact, threshold: int) -> SequenceArtifact:
        """二值化（序列 → 序列）：转灰度后按阈值二值化，输出只有黑/白两色。

        ``threshold`` 0–255：像素值 < 阈值 → 黑 (0,0,0)，≥ 阈值 → 白 (255,255,255)。
        只作用于 RGB，Alpha 原样保留。
        """
        threshold = int(threshold)
        if not 0 <= threshold <= 255:
            raise ValueError(f"二值化阈值必须在 0–255 之间（当前 {threshold}）")
        output = self._job_dir("binarize")
        total = len(artifact.frames)

        def process(index: int) -> Image.Image:
            with Image.open(artifact.frames[index]) as image:
                rgba = image.convert("RGBA")
                alpha = rgba.getchannel("A")
                gray = rgba.convert("L")
                binary = gray.point(lambda value: 255 if value >= threshold else 0)
                return Image.merge("RGBA", (binary, binary, binary, alpha))

        paths = self._parallel_pil_export(total, output, "二值化", process)
        return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))


    def grayscale_sequence(self, artifact: SequenceArtifact) -> SequenceArtifact:
        """灰度化（序列 → 序列）：RGB 转灰度（R==G==B），Alpha 原样保留。"""
        output = self._job_dir("gray")
        total = len(artifact.frames)

        def process(index: int) -> Image.Image:
            with Image.open(artifact.frames[index]) as image:
                rgba = image.convert("RGBA")
                alpha = rgba.getchannel("A")
                gray = rgba.convert("L")
                return Image.merge("RGBA", (gray, gray, gray, alpha))

        paths = self._parallel_pil_export(total, output, "灰度化", process)
        return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))


    def contrast_sequence(self, artifact: SequenceArtifact, amount: float) -> SequenceArtifact:
        """对比度调整（序列 → 序列）：PIL ImageEnhance.Contrast。

        ``amount`` 为百分比增量（-100..100）：0 = 不变，正值增强、负值减弱。
        只作用于 RGB，Alpha 原样保留。
        """
        output = self._job_dir("contrast")
        total = len(artifact.frames)
        factor = max(0, 1 + float(amount) / 100)

        def process(index: int) -> Image.Image:
            with Image.open(artifact.frames[index]) as image:
                rgba = image.convert("RGBA")
                alpha = rgba.getchannel("A")
                rgb = rgba.convert("RGB")
                rgb = ImageEnhance.Contrast(rgb).enhance(factor)
                return Image.merge("RGBA", (*rgb.split(), alpha))

        paths = self._parallel_pil_export(total, output, "对比度调整", process)
        return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))


    def invert_sequence(self, artifact: SequenceArtifact) -> SequenceArtifact:
        """反相（序列 → 序列）：RGB 各通道取反（255 - 原值），Alpha 原样保留。

        与灰度化/对比度同一语义：只作用于 RGB（``PIL.ImageOps.invert``），
        不处理 Alpha 通道（透明像素仍是透明像素，只是颜色反转）。
        """
        from PIL import ImageOps

        output = self._job_dir("invert")
        total = len(artifact.frames)

        def process(index: int) -> Image.Image:
            with Image.open(artifact.frames[index]) as image:
                rgba = image.convert("RGBA")
                alpha = rgba.getchannel("A")
                rgb = rgba.convert("RGB")
                rgb = ImageOps.invert(rgb)
                return Image.merge("RGBA", (*rgb.split(), alpha))

        paths = self._parallel_pil_export(total, output, "反相", process)
        return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))


    def flip_sequence(self, artifact: SequenceArtifact, direction: str) -> SequenceArtifact:
        """画面翻转（序列 → 序列）：水平（左右镜像）或垂直（上下翻转）翻转每一帧。

        - ``direction`` 为机器键：``"horizontal"`` → PIL ``ImageOps.mirror``、
          ``"vertical"`` → PIL ``ImageOps.flip``；
        - 几何变换逐像素搬运，**Alpha 随像素正确翻转**（无需像反相那样拆通道）；
        - 输出尺寸/alpha 标志与原序列一致（翻转不改变画布尺寸）。
        """
        from PIL import ImageOps

        if direction not in ("horizontal", "vertical"):
            raise ValueError(f"未知翻转方向 {direction!r}（可选：horizontal / vertical）")
        output = self._job_dir("flip")
        total = len(artifact.frames)

        def process(index: int) -> Image.Image:
            with Image.open(artifact.frames[index]) as image:
                rgba = image.convert("RGBA")
                return ImageOps.mirror(rgba) if direction == "horizontal" else ImageOps.flip(rgba)

        paths = self._parallel_pil_export(total, output, "画面翻转", process)
        return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))


    def color_key_sequence(
        self,
        artifact: SequenceArtifact,
        key_color: tuple[int, int, int],
        edge_strength: float,
    ) -> SequenceArtifact:
        """超级键（颜色键抠像，序列 → 序列）：按用户选取的背景色把相近像素变为透明。

        - ``key_color``：RGB 三元组（如 (0, 255, 0) 绿幕）；
        - ``edge_strength``：边缘处理强弱（0..100 百分数）：
          * 0% = 硬边二值抠像——与背景色归一化距离 ≤ 0.25 的像素完全透明，
            其余完全保留（PR 颜色键式硬切）；
          * 100% = 大范围柔和过渡——过渡带宽度 0.5（归一化距离），
            距背景色越近越透明（羽化边缘，PR 超级键式）；
          * 中间值线性插值过渡带宽度。

        距离度量 = RGB 欧氏距离归一化（除以 √3·255 → 0..1）。
        输出 RGB 保持原值；``alpha = 原 alpha × 键控系数``（只减不增，
        原本透明的像素保持透明；非键出像素保留原 alpha）。
        numpy 向量化逐帧计算（依赖 numpy 2.x，2026-08 新增）。
        """
        key_r, key_g, key_b = (int(channel) for channel in key_color)
        strength = max(0.0, min(100.0, float(edge_strength)))
        # 键控中心阈值（归一化距离 0..1）：距离 ≤ center 的像素完全键出。
        center = 0.25
        # 过渡带宽度随边缘处理强弱线性扩展（0% → 0，100% → 0.5）。
        band = strength / 100.0 * 0.5
        inner = center - band / 2.0
        outer = center + band / 2.0
        key = np.array((key_r, key_g, key_b), dtype=np.float32)
        output = self._job_dir("key")
        total = len(artifact.frames)

        def process(index: int) -> Image.Image:
            with Image.open(artifact.frames[index]) as image:
                rgba = np.asarray(image.convert("RGBA"))  # uint8 (h, w, 4)
            diff = rgba[..., :3].astype(np.float32) - key
            distance = np.sqrt((diff * diff).sum(axis=2) / 3.0) / 255.0
            if outer > inner:
                # 过渡带：距离 ≤ inner 完全键出（透明）、≥ outer 完全保留，
                # 之间线性过渡（羽化）——距背景色越近 factor 越接近 1。
                factor = np.clip((outer - distance) / (outer - inner), 0.0, 1.0)
            else:
                # 0% 硬边：距离 ≤ 阈值全键出，其余全保留（二值）。
                factor = (distance <= inner).astype(np.float32)
            out = rgba.copy()
            # 只减不增：键控系数 1 = 完全透明、0 = 保留原 alpha。
            out[..., 3] = np.round(rgba[..., 3] * (1.0 - factor)).astype(np.uint8)
            return Image.fromarray(out, "RGBA")

        paths = self._parallel_pil_export(total, output, "超级键", process)
        return SequenceArtifact(paths, artifact.width, artifact.height, True, str(output))

    # ===== 区段 3：序列结构处理（原 MediaBackendSequenceMixin） =====

    def rewind_sequence(self, artifact: SequenceArtifact) -> SequenceArtifact:
        """序列倒带（序列 → 序列）：把序列倒序输出（逆序播放）。

        不再自行追加序列（原「序列往复」行为）：如需往复效果，由用户
        再接「序列相加」节点把倒带结果接到原序列末尾。
        """
        output = self._job_dir("rewind")
        order = list(reversed(artifact.frames))
        paths: list[str] = []
        total = len(order)
        for index, source in enumerate(order):
            target = output / f"frame_{index:06d}.png"
            shutil.copy2(source, target)
            paths.append(str(target))
            self._progress((index + 1) / total, "序列倒带")
        return SequenceArtifact(tuple(paths), artifact.width, artifact.height, artifact.has_alpha, str(output))


    def freeze_sequence(
        self,
        artifact: SequenceArtifact,
        *,
        end: str = "first",
        count: int = 1,
    ) -> SequenceArtifact:
        """帧冻结（序列 → 序列）：把首帧/末帧的静态内容定格延长若干帧。

        - ``end``（机器键）：``first`` = 在序列**开头**插入 ``count`` 份首帧
          副本（首帧定格在开头）；``last`` = 在序列**末尾**追加 ``count`` 份
          末帧副本（末帧定格在结尾）；
        - ``count``（0 起）：插入/追加的静态帧数；0 = 原样输出（不冻结）；
        - 冻结帧为边界帧的**逐像素副本**（``shutil.copy2`` 直接复制文件，
          不重新编码、不做插值/过渡），输出总帧数 = ``len(frames) + count``。
        """
        if end not in ("first", "last"):
            raise ValueError(f"未知冻结位置 {end!r}（可选：first/last）")
        count = int(count)
        if count < 0:
            raise ValueError(f"冻结延长帧数不能为负数（当前 {count}）")
        if not artifact.frames:
            raise ValueError("输入序列为空，无法帧冻结")
        boundary = artifact.frames[0] if end == "first" else artifact.frames[-1]
        if end == "first":
            order = [boundary] * count + list(artifact.frames)
        else:
            order = list(artifact.frames) + [boundary] * count
        output = self._job_dir("freeze")
        paths: list[str] = []
        total = len(order)
        for index, source in enumerate(order):
            target = output / f"frame_{index:06d}.png"
            shutil.copy2(source, target)
            paths.append(str(target))
            self._progress((index + 1) / total, "帧冻结")
        return SequenceArtifact(
            tuple(paths), artifact.width, artifact.height, artifact.has_alpha, str(output),
        )


    def concat_sequences(
        self,
        a: SequenceArtifact,
        b: SequenceArtifact,
        *,
        resample: str = "lanczos",
        strategy: str = "fit",
    ) -> SequenceArtifact:
        """序列相加（序列 → 序列）：以 A 序列分辨率为基准，缩放 B 后追加到 A 末尾。

        - ``resample``（缩放算法，机器键）：nearest / bilinear / bicubic / lanczos
          （选项唯一源头 ``options.RESAMPLE``，此处经 ``RESAMPLE.value_for_key``
          取 PIL ``Image.resize`` 的重采样算法）；
        - ``strategy``（缩放策略，机器键）：
          * ``stretch``（拉伸）：不保纵横比，直接铺满 A 画布；
          * ``fill``（填充）：保纵横比放大到**铺满**画布（cover），溢出部分
            居中裁剪——保证画面填满；
          * ``fit``（适合）：保纵横比缩小到**完整容纳**（contain），居中贴在
            透明画布上，未填满区域为全透明黑 rgba(0,0,0,0)——保证画面无裁剪；
          * ``none``（不缩放）：保持 B 帧原尺寸——单轴超出 A 画布时该轴居中
            裁剪到画布大小，未超出轴保持原尺寸居中摆放，未覆盖区域透明。

        A 的帧原样复制（已处于目标分辨率，像素不变）；B 的每帧按策略缩放后
        追加。**快路径**：B 分辨率与 A 相同时（``B.width == A.width`` 且
        ``B.height == A.height``）各策略的缩放/合成均为恒等变换，直接复制
        文件不重新编码（与 A 帧同路径），提高效率。输出分辨率 = A 的分辨率，
        ``has_alpha`` 恒为 True（适合策略会产生透明边缘，B 自身也可能带透明）。
        """
        if strategy not in SCALE_STRATEGY.key_set:
            raise ValueError(f"未知缩放策略：{strategy}")
        if resample not in RESAMPLE.key_set:
            raise ValueError(f"未知缩放算法：{resample}")
        if not a.frames:
            raise ValueError("序列A为空，无法相加")
        if not b.frames:
            raise ValueError("序列B为空，无法相加")
        output = self._job_dir("concat")
        target_w, target_h = a.width, a.height
        paths: list[str] = []
        # 1) A 序列帧原样复制（分辨率基准，像素不变）。
        for index, source in enumerate(a.frames):
            target = output / f"frame_{index:06d}.png"
            shutil.copy2(source, target)
            paths.append(str(target))
        # 2) B 序列帧追加。分辨率与 A 相同时直接复制（缩放 = 恒等变换，各策略
        #    输出像素一致），跳过打开/转换/缩放/合成；否则按策略缩放。
        same_size = b.width == target_w and b.height == target_h
        for index, source in enumerate(b.frames):
            if same_size:
                target = output / f"frame_{len(a.frames) + index:06d}.png"
                shutil.copy2(source, target)
                paths.append(str(target))
                self._progress((index + 1) / len(b.frames), "序列相加")
                continue
            with Image.open(source) as image:
                rgba = image.convert("RGBA")
            scaled = _scale_to_canvas(rgba, target_w, target_h, strategy, RESAMPLE.value_for_key(resample))
            target = output / f"frame_{len(a.frames) + index:06d}.png"
            scaled.save(target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL)
            paths.append(str(target))
            self._progress((index + 1) / len(b.frames), "序列相加")
        return SequenceArtifact(tuple(paths), target_w, target_h, True, str(output))

    def align_resolution(
        self,
        a: SequenceArtifact,
        b: SequenceArtifact,
        *,
        resample: str = "lanczos",
        strategy: str = "fit",
    ) -> SequenceArtifact:
        """分辨率统一（序列A/序列B → 序列B）：以 A 分辨率为基准，把 B 缩放到 A 的尺寸。

        缩放算法/策略与「序列相加」完全一致（``options.RESAMPLE`` /
        ``options.SCALE_STRATEGY``，语义见 ``concat_sequences``）：stretch 拉伸
        铺满 / fill 填充（cover，居中裁剪）/ fit 适合（contain，未填满区域
        全透明黑）。B 的帧数不变，输出分辨率 = A 的分辨率。供通道合并前
        对齐各通道分辨率使用。
        """
        if strategy not in SCALE_STRATEGY.key_set:
            raise ValueError(f"未知缩放策略：{strategy}")
        if resample not in RESAMPLE.key_set:
            raise ValueError(f"未知缩放算法：{resample}")
        if not a.frames:
            raise ValueError("序列A为空，无法统一分辨率")
        if not b.frames:
            raise ValueError("序列B为空，无法统一分辨率")
        output = self._job_dir("res_align")
        target_w, target_h = a.width, a.height
        resampler = RESAMPLE.value_for_key(resample)
        paths: list[str] = []
        total = len(b.frames)
        for index, source in enumerate(b.frames):
            with Image.open(source) as image:
                rgba = image.convert("RGBA")
            scaled = _scale_to_canvas(rgba, target_w, target_h, strategy, resampler)
            target = output / f"frame_{index:06d}.png"
            scaled.save(target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL)
            paths.append(str(target))
            self._progress((index + 1) / total, "分辨率统一")
        return SequenceArtifact(tuple(paths), target_w, target_h, True, str(output))


    def overlay_sequences(
        self,
        a: SequenceArtifact,
        b: SequenceArtifact,
        *,
        resample: str = "lanczos",
        strategy: str = "fit",
    ) -> SequenceArtifact:
        """序列叠加（序列A/序列B → 序列）：以 A 的画布与长度为基准，把 B 层叠到 A 上。

        - 逐帧合成：输出第 i 帧 = ``alpha_composite(A[i], B[i])``——B 作为
          顶层叠加在 A 上（平面层叠，A/B 各自携带的透明通道参与混合）；
        - 输出长度 = len(A)；B 帧不足时**直接循环重复**（``i % len(B)``，
          用户需求：叠加序列长度不够直接重复）；B 长于 A 时取前 len(A) 帧
          （与「序列长度统一」的截取语义一致）；
        - B 帧相对 A 画布的缩放算法/策略与「序列相加」「分辨率统一」完全
          一致（``options.RESAMPLE`` / ``options.SCALE_STRATEGY``，语义见
          ``concat_sequences``）；``none``（不缩放）时 B 保持原尺寸、单轴
          超出画布居中裁剪、未覆盖区域透明；
        - 输出分辨率 = A 的分辨率，``has_alpha`` 恒为 True。
        """
        if strategy not in SCALE_STRATEGY.key_set:
            raise ValueError(f"未知缩放策略：{strategy}")
        if resample not in RESAMPLE.key_set:
            raise ValueError(f"未知缩放算法：{resample}")
        if not a.frames:
            raise ValueError("序列A为空，无法叠加")
        if not b.frames:
            raise ValueError("序列B为空，无法叠加")
        output = self._job_dir("overlay")
        target_w, target_h = a.width, a.height
        total = len(a.frames)
        resampler = RESAMPLE.value_for_key(resample)

        def process(index: int) -> Image.Image:
            with Image.open(a.frames[index]) as image:
                base = image.convert("RGBA")
            with Image.open(b.frames[index % len(b.frames)]) as image:
                overlay = image.convert("RGBA")
            top = _scale_to_canvas(overlay, target_w, target_h, strategy, resampler)
            return Image.alpha_composite(base, top)

        paths = self._parallel_pil_export(total, output, "序列叠加", process)
        return SequenceArtifact(paths, target_w, target_h, True, str(output))


    def split_channels(self, artifact: SequenceArtifact) -> tuple[SequenceArtifact, SequenceArtifact, SequenceArtifact, SequenceArtifact]:
        """RGBA 通道分离（序列 → 四个序列）。

        每个通道输出为**灰度图**（R==G==B，通道值复制到三通道），Alpha 恒为
        255（不透明）——透明度通道同样以灰度图输出（灰度值 = 原 alpha 值），
        便于直接预览与下游处理（透明像素若保留原 alpha 会在预览中不可见）。
        返回 (红, 绿, 蓝, 透明度) 四个序列产物。
        """
        output = self._job_dir("channel")
        channel_dirs = {name: output / name for name in ("red", "green", "blue", "alpha")}
        for directory in channel_dirs.values():
            directory.mkdir(parents=True, exist_ok=True)
        paths: dict[str, list[str]] = {name: [] for name in channel_dirs}
        total = len(artifact.frames)
        for index, source in enumerate(artifact.frames):
            with Image.open(source) as image:
                rgba = image.convert("RGBA")
            red, green, blue, alpha = rgba.split()
            for name, channel in (("red", red), ("green", green), ("blue", blue), ("alpha", alpha)):
                gray = channel.convert("L")
                target = channel_dirs[name] / f"frame_{index:06d}.png"
                Image.merge("RGB", (gray, gray, gray)).save(
                    target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL
                )
                paths[name].append(str(target))
            self._progress((index + 1) / total, "RGBA 通道分离")
        return tuple(
            SequenceArtifact(tuple(paths[name]), artifact.width, artifact.height, False, str(channel_dirs[name]))
            for name in ("red", "green", "blue", "alpha")
        )


    def merge_channels(
        self,
        red: SequenceArtifact | None,
        green: SequenceArtifact | None,
        blue: SequenceArtifact | None,
        alpha: SequenceArtifact | None,
    ) -> SequenceArtifact:
        """RGBA 通道合并（四路灰度序列 → 一个 RGBA 序列，与 split_channels 互逆）。

        - 每个输入帧取**灰度值**（``convert(\"L\")``）作为对应通道值——通道分离
          节点输出的正是 R==G==B 的灰度图（值即原通道值），因此 split→merge
          往返可像素级还原；普通彩色输入则取其亮度作为该通道值；
        - ``alpha`` 为 ``None``（未连接/占位）时按**不透明**处理（alpha=255）；
        - 红/绿/蓝三个必填通道未连接或为空 → 抛清晰中文错误；
        - 各通道序列**长度不一致** → raise（用户需求）；
        - 各通道帧**尺寸不一致** → raise（``Image.merge`` 要求各通道同尺寸，
          主动校验避免模糊的底层错误）。
        """
        channels = [("红通道", red), ("绿通道", green), ("蓝通道", blue), ("透明度通道", alpha)]
        for name, artifact in channels[:3]:
            if artifact is None:
                raise ValueError(f"RGBA 通道合并：{name}未连接")
            if not artifact.frames:
                raise ValueError(f"RGBA 通道合并：{name}为空序列")
        lengths = {
            name: len(artifact.frames) for name, artifact in channels if artifact is not None
        }
        if len(set(lengths.values())) != 1:
            detail = "、".join(f"{name}={count}" for name, count in lengths.items())
            raise ValueError(f"RGBA 通道合并：各通道序列长度不一致（{detail}）")
        total = lengths["红通道"]
        sizes: dict[str, tuple[int, int]] = {}
        for name, artifact in channels:
            if artifact is None:
                continue
            with Image.open(artifact.frames[0]) as image:
                sizes[name] = image.size
        if len(set(sizes.values())) != 1:
            detail = "、".join(f"{name}={w}×{h}" for name, (w, h) in sizes.items())
            raise ValueError(f"RGBA 通道合并：各通道帧尺寸不一致（{detail}）")
        width, height = sizes["红通道"]
        output = self._job_dir("merge")

        def process(index: int) -> Image.Image:
            images: list[Image.Image] = []
            for name, artifact in channels:
                if artifact is None:
                    continue
                with Image.open(artifact.frames[index]) as image:
                    images.append(image.convert("L"))
            if alpha is None:
                # 透明度通道未连接 → 按不透明处理（alpha=255）。
                images.append(Image.new("L", (width, height), 255))
            return Image.merge("RGBA", images)

        paths = self._parallel_pil_export(total, output, "RGBA 通道合并", process)
        return SequenceArtifact(paths, width, height, True, str(output))


    def split_alpha(self, artifact: SequenceArtifact) -> SequenceArtifact:
        """A通道分离（序列 → 序列）：输出 alpha 通道灰度序列（R==G==B=alpha 值，不透明）。

        与 RGBA 通道分离的「透明度通道」语义一致（灰度值 = 原 alpha），但
        只物化 1 份缓存（不额外生成 RGB 分量）——避免「只想分离 alpha」时
        用 RGBA 分离白白占用红/绿/蓝/透明度 4 个通道的缓存。
        """
        output = self._job_dir("alpha")
        total = len(artifact.frames)

        def process(index: int) -> Image.Image:
            with Image.open(artifact.frames[index]) as image:
                rgba = image.convert("RGBA")
                gray = rgba.getchannel("A").convert("L")
                return Image.merge("RGB", (gray, gray, gray))

        paths = self._parallel_pil_export(total, output, "A通道分离", process)
        return SequenceArtifact(paths, artifact.width, artifact.height, False, str(output))


    def merge_alpha(
        self,
        rgb: SequenceArtifact | None,
        alpha: SequenceArtifact | None,
    ) -> SequenceArtifact:
        """A通道合并（RGB 序列 + alpha 灰度序列 → RGBA 序列，与 split_alpha 互逆）。

        - ``rgb`` 必填：每帧取 RGB 通道（保留颜色，不转灰度）；
        - ``alpha`` 可空：未连接/为空按不透明处理（alpha=255）；非空时取
          灰度值（与 split_alpha 输出一致）；
        - **透明度通道长度自动对齐（用户实测需求，不再报长度不一致）**：
          短于 RGB → 循环复制延长到 RGB 长度；长于 RGB → 保留前 len(RGB)
          帧（帧路径引用，不额外复制文件）；
        - 帧尺寸不一致 → 抛清晰中文错误（与 RGBA 通道合并一致）。
        """
        if rgb is None or not rgb.frames:
            raise ValueError("A通道合并：RGB序列未连接或为空")
        total = len(rgb.frames)
        # 透明度通道长度对齐：短 → 循环延长；长 → 截取。空/未连接 → 不透明。
        alpha_frames: list[str] = []
        if alpha is not None and alpha.frames:
            alpha_frames = [
                alpha.frames[index % len(alpha.frames)]
                for index in range(total)
            ]
        sizes: dict[str, tuple[int, int]] = {}
        with Image.open(rgb.frames[0]) as image:
            sizes["RGB序列"] = image.size
        if alpha is not None:
            with Image.open(alpha.frames[0]) as image:
                sizes["透明度通道"] = image.size
        if len(set(sizes.values())) != 1:
            detail = "、".join(f"{name}={w}×{h}" for name, (w, h) in sizes.items())
            raise ValueError(f"A通道合并：RGB序列与透明度通道帧尺寸不一致（{detail}）")
        width, height = sizes["RGB序列"]
        output = self._job_dir("alpha_merge")

        def process(index: int) -> Image.Image:
            with Image.open(rgb.frames[index]) as image:
                red, green, blue, _alpha = image.convert("RGBA").split()
            if alpha_frames:
                with Image.open(alpha_frames[index]) as image:
                    alpha_band = image.convert("L")
            else:
                alpha_band = Image.new("L", (width, height), 255)
            return Image.merge("RGBA", (red, green, blue, alpha_band))

        paths = self._parallel_pil_export(total, output, "A通道合并", process)
        return SequenceArtifact(paths, width, height, True, str(output))


    def sample_frames(
        self, artifact: SequenceArtifact, in_fps: int, out_fps: int
    ) -> SequenceArtifact:
        """按「定义帧速/输出帧速」的比值对序列抽帧（帧率转换，序列 → 序列）。

        - 间隔 ``interval = in_fps / out_fps``（源帧数 / 输出帧），按**目标帧驱动**
          选取源帧：输出第 k 帧取源 ``round(k * in_fps / out_fps)``，整数/小数比值
          都精确（如 30→12 间隔 2.5：源 0,2,5,8,…）；
        - ``out_fps >= in_fps`` 时不抽帧（不插值放大），保留全部帧；
        - 定义帧速/输出帧速均由用户参数输入（序列产物不携带帧率信息）。
        """
        if in_fps <= 0 or out_fps <= 0:
            raise ValueError("定义帧速与输出帧速必须为正整数")
        total = len(artifact.frames)
        if out_fps >= in_fps:
            selected = list(artifact.frames)
        else:
            count = max(1, round(total * out_fps / in_fps))
            selected = [
                artifact.frames[min(round(index * in_fps / out_fps), total - 1)]
                for index in range(count)
            ]
        output = self._job_dir("sampling")
        paths: list[str] = []
        for index, source in enumerate(selected):
            target = output / f"frame_{index:06d}.png"
            shutil.copy2(source, target)
            paths.append(str(target))
            self._progress((index + 1) / len(selected), "抽帧")
        return SequenceArtifact(
            tuple(paths),
            artifact.width, artifact.height, artifact.has_alpha, str(output),
        )


    def static_hold_sequence(
        self,
        artifact: SequenceArtifact,
        *,
        threshold: int = 3,
        reference: str = "prev",
        neighbors: int = 4,
    ) -> SequenceArtifact:
        """帧差静止保持（序列 → 序列）：消除录屏/视频编码在静止区域的时域噪声。

        针对「电脑录屏 → GIF」场景：录屏编码器（H.264/HEVC）在静止 UI 区域
        留下逐帧 ±1~3 级的量化噪声（+ 色度子采样在文字边缘的欠码模糊）。
        这些噪声在后续「颜色量化」的扩散仿色下会被放大成「烂噪」图案、
        浪费共享调色板条目并造成帧间闪烁，且使「GIF 优化」的 gifsicle 帧
        优化（逐像素精确比较）失效——静止区域每帧像素都不同，无法合并为
        未变化区域（体积与观感双输）。

        本方法逐帧与参考帧逐像素比较：差值 ≤ ``threshold`` 判定为「静止」的
        像素**沿用参考帧的精确像素值**，运动区域保留当前帧。静止区域因此在
        时间轴上像素精确一致——共享调色板量化取同一色板项（无帧间闪烁），
        gifsicle -O2/-O3 可把静止区域合并为未变化区域（体积下降）。

        - ``threshold``（0–255，默认 3）：逐通道最大允许差值（含 Alpha）；
        - ``reference``（机器键）：``prev`` = 参考上一帧的**保持后**结果
          （流式因果，内容变化后自动恢复静止判定，噪声不回流）；``first`` =
          参考首帧的保持后结果（静止背景整体统一到首帧；内容一旦变化该区域
          与首帧恒不同，将永久视为运动，不再被保持）；
        - ``neighbors``（0–8，默认 4）：判定静止还需 ≥N 个 8 邻域像素也
          静止（np.pad 边界外按非静止计），剔除运动边缘的孤立静止像素
          （防拖尾/防渐变区域局部误判）；0 = 不检查邻域。

        Alpha 全程逐像素原样携带（不做任何 alpha 运算）；帧保存走有界线程池
        并行（与流式解码同模式），计算本身是顺序的（每帧依赖前一帧结果），
        内存峰值 ≈ 参考帧 + 在途待保存帧。
        """
        threshold = int(threshold)
        neighbors = int(neighbors)
        if not artifact.frames:
            raise ValueError("输入序列为空，无法帧差静止保持")
        if not 0 <= threshold <= 255:
            raise ValueError(f"静止阈值必须在 0–255（当前 {threshold}）")
        if not 0 <= neighbors <= 8:
            raise ValueError(f"邻域判定必须在 0–8（当前 {neighbors}）")
        if reference not in ("prev", "first"):
            raise ValueError(f"未知参考模式 {reference!r}（可选：prev/first）")
        output = self._job_dir("static_hold")
        total = len(artifact.frames)
        paths: list[str] = []
        held_ref: np.ndarray | None = None   # first 模式：首帧的保持结果
        held_prev: np.ndarray | None = None  # prev 模式：上一帧的保持结果
        save_sem = threading.BoundedSemaphore(PNG_EXPORT_WORKERS * 2)
        futures: list[Future] = []
        with ThreadPoolExecutor(max_workers=PNG_EXPORT_WORKERS) as pool:
            for index, source in enumerate(artifact.frames):
                with Image.open(source) as image:
                    cur = np.asarray(image.convert("RGBA"), dtype=np.uint8)
                ref = held_ref if reference == "first" else held_prev
                if ref is not None:
                    diff = np.abs(cur.astype(np.int16) - ref.astype(np.int16)).max(axis=-1)
                    static = diff <= threshold
                    if neighbors > 0:
                        # 邻域一致性：np.pad 补一圈非静止（边界外不参与计数），
                        # 9 次错位切片求和统计 8 邻域静止数。
                        padded = np.pad(static, 1, mode="constant")
                        height, width = static.shape
                        count = np.zeros_like(static, dtype=np.int8)
                        for dy in (-1, 0, 1):
                            for dx in (-1, 0, 1):
                                if dx == 0 and dy == 0:
                                    continue
                                count += padded[1 + dy:1 + dy + height, 1 + dx:1 + dx + width]
                        static &= count >= neighbors
                    if static.any():
                        # 携带保持后的值（含 Alpha），噪声不回流。
                        cur = np.where(static[..., None], ref, cur)
                if held_ref is None:
                    held_ref = cur  # 首帧无参考：自身即参考
                if reference == "prev":
                    held_prev = cur
                target = output / f"frame_{len(paths):06d}.png"
                image = Image.fromarray(cur, "RGBA")
                save_sem.acquire()
                futures.append(pool.submit(self._parallel_pil_save_bounded, image, target, save_sem))
                paths.append(str(target))
                self._progress((index + 1) / total, "帧差静止保持")
            self._drain_save_futures(futures)
        return SequenceArtifact(
            tuple(paths), artifact.width, artifact.height, artifact.has_alpha, str(output),
        )


    def trim_sequence(
        self, artifact: SequenceArtifact, start: int, end: int
    ) -> SequenceArtifact:
        """序列截取（序列 → 序列）：保留 [start, end) 区间内的帧（半开区间）。

        数值防呆：起始帧/结束帧必须为合法区间，且不得超出序列长度，
        否则抛出清晰中文错误（不在执行时静默钳制，避免用户误以为截取生效）。
        """
        total = len(artifact.frames)
        if start < 0:
            raise ValueError(f"起始帧不能为负数（当前 {start}）")
        if end <= start:
            raise ValueError(
                f"结束帧必须大于起始帧（当前 起始帧={start}，结束帧={end}），请先设置截取范围"
            )
        if start >= total:
            raise ValueError(f"起始帧超出序列长度（共 {total} 帧）")
        if end > total:
            raise ValueError(f"结束帧超出序列长度（共 {total} 帧，最大可填 {total}）")
        output = self._job_dir("trim")
        paths: list[str] = []
        selected = artifact.frames[start:end]
        for index, source in enumerate(selected):
            target = output / f"frame_{index:06d}.png"
            shutil.copy2(source, target)
            paths.append(str(target))
            self._progress((index + 1) / len(selected), "序列截取")
        return SequenceArtifact(
            tuple(paths),
            artifact.width, artifact.height, artifact.has_alpha, str(output),
        )


    def split_sequence(
        self, artifact: SequenceArtifact, cut: int
    ) -> tuple[SequenceArtifact, SequenceArtifact]:
        """序列剃刀（序列 → 两个序列）：在 cut 处把序列切成两段。

        - ``cut`` 为 0 基切片下标（帧边界）：段A = frames[:cut]，段B = frames[cut:]；
        - 合法范围 1..len-1（两端都必须非空，任何一段为空都没有意义）；
        - 数值防呆：越界抛清晰中文错误（不在执行时静默钳制，与 trim_sequence 一致）；
        - 两段复制到独立 job 目录（razor_a_* / razor_b_*），输出尺寸/alpha 与原序列一致。
        """
        total = len(artifact.frames)
        if total < 2:
            raise ValueError(f"序列只有 {total} 帧，无法切割成两段")
        if cut < 1:
            raise ValueError(f"切割帧必须 ≥ 1（当前 {cut}），段A不能为空")
        if cut >= total:
            raise ValueError(f"切割帧超出可切割范围（共 {total} 帧，可在 1..{total - 1} 之间切割）")
        output_a = self._job_dir("razor_a")
        output_b = self._job_dir("razor_b")
        paths_a: list[str] = []
        paths_b: list[str] = []
        done = 0
        for index, source in enumerate(artifact.frames[:cut]):
            target = output_a / f"frame_{index:06d}.png"
            shutil.copy2(source, target)
            paths_a.append(str(target))
            done += 1
            self._progress(done / total, "序列剃刀")
        for index, source in enumerate(artifact.frames[cut:]):
            target = output_b / f"frame_{index:06d}.png"
            shutil.copy2(source, target)
            paths_b.append(str(target))
            done += 1
            self._progress(done / total, "序列剃刀")
        return (
            SequenceArtifact(
                tuple(paths_a), artifact.width, artifact.height, artifact.has_alpha, str(output_a),
            ),
            SequenceArtifact(
                tuple(paths_b), artifact.width, artifact.height, artifact.has_alpha, str(output_b),
            ),
        )


    def align_length(self, a: SequenceArtifact, b: SequenceArtifact, method: str = "loop") -> SequenceArtifact:
        """序列长度统一（序列A/序列B → 序列B）：按 A 的长度统一 B 的帧数。

        - 目标长度 = len(A)；
        - ``method``：B 短于 A 时的延长方式——
          - ``"loop"``（循环复制）：把 B 的帧**循环复制**延长到 A 的长度
            （周期重复，如 B=[f0,f1,f2]、A 长 5 → [f0,f1,f2,f0,f1]）——
            用户实测需求，不再报错；
          - ``"sample"``（均匀采样）：重复帧**均匀分布**到整个序列
            （整数倍时每帧均等复制，如 B=[f0,f1,f2,f3]、A 长 8 →
            [f0,f0,f1,f1,f2,f2,f3,f3]；非整数倍时均匀插入，如 A 长 6 →
            [f0,f1,f1,f2,f3,f3]）；
        - B 长于/等于 A：两种方式均保留前 len(A) 帧（对齐 0 帧截取）；
        - 分辨率不变（输出尺寸 = B 的尺寸）。供通道合并前对齐各通道长度使用。
        """
        if not a.frames:
            raise ValueError("序列A为空，无法统一长度")
        if not b.frames:
            raise ValueError("序列B为空，无法统一长度")
        target = len(a.frames)
        total = len(b.frames)
        if method == "loop":
            # 循环延长：短于 A 时重复帧；长于 A 时取前 target 帧。
            selected = [b.frames[index % total] for index in range(target)]
        elif method == "sample":
            selected = [b.frames[index] for index in _sample_source_indices(total, target)]
        else:
            raise ValueError(f"未知的序列长度统一方式：{method!r}（可选：loop/sample）")
        output = self._job_dir("len_align")
        paths: list[str] = []
        for index, source in enumerate(selected):
            target_path = output / f"frame_{index:06d}.png"
            shutil.copy2(source, target_path)
            paths.append(str(target_path))
            self._progress((index + 1) / target, "序列长度统一")
        return SequenceArtifact(tuple(paths), b.width, b.height, b.has_alpha, str(output))


    def pan_sequence(
        self,
        artifact: SequenceArtifact,
        *,
        direction: str = "right",
        duration: int = 30,
        curve: str = "linear",
        interpolation: str = "bilinear",
    ) -> SequenceArtifact:
        """平移滚动（序列 → 序列）：画面向指定方向无缝循环平移（跑马灯式）。

        - ``direction``（机器键）：up/down/left/right——画面内容向该方向滚动，
          被推出画布一侧的像素从对侧绕回（无缝衔接，不产生黑边/接缝）；
        - ``duration``：输出帧数（持续帧数）。输入序列不足时按周期循环补足
          （第 k 帧取输入 ``k mod L``）；输入不少于 duration 时取前 duration 帧；
        - ``curve``（机器键）：速度曲线——第 k 帧进度 ``p = k / duration``，
          位移 = ``curve(p) × 画面宽（左右）/ 高（上下）``，整段动画恰好走满
          一个画面循环回到起点（首尾无缝衔接成循环）。linear 匀速 / accelerate
          线性加速（p²，加速度恒定）/ decelerate 线性减速（1−(1−p)²，减速度
          恒定）/ win10_decelerate = Win10/Fluent 动效官方减速曲线
          cubic-bezier(0.1, 0.9, 0.2, 1.0)（起步快、减速至停）；
        - ``interpolation``（机器键）：位移为小数时的插值方式——nearest 取整
          （像素锐利）/ bilinear 双线性（平滑，亚像素运动不抖动）。
        """
        if direction not in PAN_DIRECTION.key_set:
            raise ValueError(f"未知平移方向：{direction!r}（可选：{sorted(PAN_DIRECTION.key_set)}）")
        if curve not in SPEED_CURVE.key_set:
            raise ValueError(f"未知速度曲线：{curve!r}（可选：{sorted(SPEED_CURVE.key_set)}）")
        if interpolation not in PAN_INTERPOLATION.key_set:
            raise ValueError(f"未知插值方式：{interpolation!r}（可选：{sorted(PAN_INTERPOLATION.key_set)}）")
        if duration <= 0:
            raise ValueError(f"持续帧数必须为正整数（当前 {duration}）")
        if not artifact.frames:
            raise ValueError("输入序列为空，无法平移")
        output = self._job_dir("pan")
        total = int(duration)
        source_count = len(artifact.frames)
        easing = _SPEED_CURVES[curve]
        # 左右沿宽度（axis=1）、上下沿高度（axis=0）；向左/向上位移取负。
        if direction in ("left", "right"):
            axis, dimension = 1, artifact.width
            sign = 1.0 if direction == "right" else -1.0
        else:
            axis, dimension = 0, artifact.height
            sign = 1.0 if direction == "down" else -1.0

        def process(index: int) -> Image.Image:
            source = artifact.frames[index % source_count]
            with Image.open(source) as image:
                array = np.asarray(image.convert("RGBA"))
            p = index / total
            offset = sign * easing(p) * dimension
            shifted = _wrap_shift_array(array, offset, axis, interpolation)
            return Image.fromarray(shifted, "RGBA")

        paths = self._parallel_pil_export(total, output, "平移滚动", process)
        return SequenceArtifact(
            tuple(paths),
            artifact.width, artifact.height, artifact.has_alpha, str(output),
        )


    def crop_sequence(self, artifact: SequenceArtifact, crop: CropSpec) -> SequenceArtifact:
        """画面裁剪（序列 → 序列）：按归一化裁剪规格裁剪每一帧。

        与旧「清单级裁剪」（``manifest.crop``，仅能在格式化解码前生效）不同，
        本方法直接裁剪已格式化的图片序列，节点可放在处理链任意位置。裁剪框
        与 overlay 交互预览的换算一致（``CropOverlayWidget.crop_rect`` 同款
        取整与最小 1px 防呆），保证「预览框所见 = 产物」；Alpha 通道原样
        保留（裁剪不引入透明）。
        """
        if not artifact.frames:
            raise ValueError("输入序列为空，无法裁剪")
        width, height = artifact.width, artifact.height
        left = max(0, min(width, round(crop.left * width)))
        top = max(0, min(height, round(crop.top * height)))
        right = max(0, min(width, round(crop.right * width)))
        bottom = max(0, min(height, round(crop.bottom * height)))
        if right <= left:
            right = min(width, left + 1)
        if bottom <= top:
            bottom = min(height, top + 1)
        box = (left, top, right, bottom)
        output = self._job_dir("crop")

        def process(index: int) -> Image.Image:
            with Image.open(artifact.frames[index]) as image:
                return image.convert("RGBA").crop(box)

        paths = self._parallel_pil_export(len(artifact.frames), output, "画面裁剪", process)
        return SequenceArtifact(
            paths,
            max(1, right - left),
            max(1, bottom - top),
            artifact.has_alpha,
            str(output),
        )


    def squeeze_aspect_sequence(self, artifact: SequenceArtifact, factor: float) -> SequenceArtifact:
        """纵横比挤压（序列 → 序列）：按因子非等比缩放帧宽，高度不变。

        ``factor``（滑条 0.2..5.0，1.0 = 不变）：输出宽度 = round(原宽 ×
        factor)，输出高度 = 原高——即 新纵横比 = 原纵横比 × factor。
        factor < 1 横向压扁（更窄），factor > 1 横向拉宽（更扁）。
        重采样 BICUBIC（平滑）；Alpha 通道原样保留。
        """
        factor = float(factor)
        if factor <= 0:
            raise ValueError(f"纵横比挤压因子必须为正数（当前 {factor}）")
        output = self._job_dir("aspect")
        total = len(artifact.frames)
        target_w = max(1, round(artifact.width * factor))
        target_h = artifact.height

        def process(index: int) -> Image.Image:
            with Image.open(artifact.frames[index]) as image:
                rgba = image.convert("RGBA")
            if rgba.size != (target_w, target_h):
                rgba = rgba.resize((target_w, target_h), Image.Resampling.BICUBIC)
            return rgba

        paths = self._parallel_pil_export(total, output, "纵横比挤压", process)
        return SequenceArtifact(paths, target_w, target_h, artifact.has_alpha, str(output))


    def blank_sequence(self, width: int, height: int, frames: int, color:str) -> SequenceArtifact:
        """空白序列（无输入 → 序列）：生成纯白不透明 RGBA 序列。

        ``width``/``height`` 分辨率与 ``frames`` 帧数均为正整数；每帧
        rgba(255,255,255,255) 纯白不透明。供「序列相加」「分辨率统一」等
        多输入节点作对齐基准/背景（如配合最近邻缩放实现像素画风倍数放大）。
        """
        width, height, frames = int(width), int(height), int(frames)
        if width <= 0 or height <= 0:
            raise ValueError(f"空白序列分辨率必须为正整数（当前 {width}×{height}）")
        if frames <= 0:
            raise ValueError(f"空白序列帧数必须为正整数（当前 {frames}）")
        output = self._job_dir("blank")

        def process(index: int) -> Image.Image:
            return Image.new("RGB", (width, height), color)

        paths = self._parallel_pil_export(frames, output, "生成空白序列", process)
        return SequenceArtifact(paths, width, height, False, str(output))


    def gradient_sequence(self, width: int, height: int, frames: int,
                          start_color: str, end_color: str, angle: float = 0.0) -> SequenceArtifact:
        """渐变序列（无输入 → 序列）：生成线性渐变的不透明 RGB 序列。

        ``width``/``height`` 分辨率与 ``frames`` 帧数均为正整数；``angle`` 为渐变
        方向（度），0° 从左到右、逆时针为正（90° 自下而上）。每帧内容相同，可作
        「序列相加」「分辨率统一」等多输入节点的对齐基准/背景或渐变蒙版素材。
        """
        width, height, frames = int(width), int(height), int(frames)
        if width <= 0 or height <= 0:
            raise ValueError(f"渐变序列分辨率必须为正整数（当前 {width}×{height}）")
        if frames <= 0:
            raise ValueError(f"渐变序列帧数必须为正整数（当前 {frames}）")

        c0 = np.asarray(ImageColor.getrgb(start_color)[:3], dtype=np.float32)
        c1 = np.asarray(ImageColor.getrgb(end_color)[:3], dtype=np.float32)

        # 屏幕坐标 y 轴向下，取负号让正角度表现为逆时针（数学习惯）。
        rad = math.radians(float(angle))
        dx, dy = math.cos(rad), -math.sin(rad)
        xs = np.arange(width, dtype=np.float32) - (width - 1) / 2.0
        ys = np.arange(height, dtype=np.float32) - (height - 1) / 2.0
        proj = xs[None, :] * dx + ys[:, None] * dy

        span = float(proj.max() - proj.min())
        if span <= 1e-6:  # 1×1 之类的退化尺寸
            ratio = np.zeros((height, width), dtype=np.float32)
        else:
            ratio = (proj - proj.min()) / span

        rgb = c0 + ratio[..., None] * (c1 - c0)
        base = Image.fromarray(np.clip(rgb + 0.5, 0, 255).astype(np.uint8), "RGB")

        output = self._job_dir("gradient")

        def process(index: int) -> Image.Image:
            # 每帧内容一致，复制一份避免多线程共享同一 PIL 对象。
            return base.copy()

        paths = self._parallel_pil_export(frames, output, "生成渐变序列", process)
        return SequenceArtifact(paths, width, height, False, str(output))

    # ===== 区段 4：ico/GIF/PNG 导出（原 MediaBackendExportMixin） =====

    def icon_compose(
        self,
        inputs: list[SequenceArtifact],
        auto_grade: bool = True,
    ) -> SequenceArtifact:
        """ico 合成（多路单帧序列 → 多尺寸图标序列）。

        - 至少一个输入序列；每个输入序列**必须为单帧**（长度 ≠ 1 抛清晰
          中文错误，用户需求）；
        - ``auto_grade``（自动分级）勾选：取分辨率最高的输入，按常见 icon
          分辨率阶梯（16/24/32/48/64/128/256/512/1024）逐级缩小
          （**只缩小**：目标尺寸 ≤ 源最小边；**最小保证 16×16**；源小于
          16×16 时无法分级 → 抛错）。每级按「适合」策略等比缩放并居中贴到
          该尺寸的方形透明画布（图标均为方形，非方形源不拉伸）；
        - 不勾选：按输入端口顺序原样输出各输入的单帧（用户手动提供各尺寸）。

        输出序列每帧 = 一个尺寸等级（升序）；``width/height`` = 最大帧尺寸。
        """
        artifacts = [value for value in inputs if value is not None]
        if not artifacts:
            raise ValueError("ico 合成：至少需要连接一个输入序列")
        for index, value in enumerate(artifacts):
            if not isinstance(value, SequenceArtifact):
                raise ValueError("ico 合成：输入必须是图片序列")
            if len(value.frames) != 1:
                raise ValueError(
                    f"ico 合成：输入序列长度必须为 1（单帧），"
                    f"第 {index + 1} 个输入为 {len(value.frames)} 帧"
                )
        output = self._job_dir("ico")
        if auto_grade:
            source = max(artifacts, key=lambda value: value.width * value.height)
            with Image.open(source.frames[0]) as image:
                rgba = image.convert("RGBA")
            src_w, src_h = rgba.size
            sizes = tuple(size for size in ICON_SIZES if size <= min(src_w, src_h))
            if not sizes:
                raise ValueError(
                    f"ico 合成：源分辨率 {src_w}×{src_h} 小于 16×16，无法自动分级"
                )
            resampler = Image.Resampling.LANCZOS
            frames: list[str] = []
            for index, size in enumerate(sizes):
                scaled = _scale_to_canvas(rgba, size, size, "fit", resampler)
                target = output / f"frame_{index:06d}.png"
                scaled.save(target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL)
                frames.append(str(target))
                self._progress((index + 1) / len(sizes), "ico 合成")
            return SequenceArtifact(
                tuple(frames), sizes[-1], sizes[-1], True, str(output)
            )
        # 手动分级：按输入端口顺序复制各输入的单帧。
        frames = []
        max_w = max_h = 0
        for index, value in enumerate(artifacts):
            target = output / f"frame_{index:06d}.png"
            shutil.copy2(value.frames[0], target)
            frames.append(str(target))
            with Image.open(value.frames[0]) as image:
                max_w = max(max_w, image.width)
                max_h = max(max_h, image.height)
        return SequenceArtifact(
            tuple(frames), max_w, max_h, any(value.has_alpha for value in artifacts), str(output)
        )


    def write_ico(self, artifact: SequenceArtifact, path: str | Path) -> Path:
        """把多尺寸序列帧写入 .ico 容器，返回路径。

        ICO 容器（ICONDIR + ICONDIRENTRY×N + 图像数据）按 Vista 约定：
        尺寸 ≥256 的条目 width/height 字节写 0 且图像数据用 **PNG**
        （DIB 的 1 字节尺寸字段装不下 256+）；小尺寸用经典 **32bpp BMP DIB**
        （BITMAPINFOHEADER + 自底向上 BGRA + 全零 AND 掩码，alpha 由 DIB
        携带）。每帧一个条目，保持序列帧顺序（ico 合成输出即各分辨率升序）。
        """
        import io
        import struct

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        entries: list[tuple[int, int, bytes]] = []
        for source in artifact.frames:
            with Image.open(source) as image:
                rgba = image.convert("RGBA")
            width, height = rgba.size
            if max(width, height) >= 256:
                buffer = io.BytesIO()
                rgba.save(buffer, "PNG")
                data = buffer.getvalue()
            else:
                data = _bmp_dib_bytes(rgba)
            entries.append((width, height, data))
        count = len(entries)
        header_size = 6 + 16 * count
        with open(path, "wb") as file:
            file.write(struct.pack("<HHH", 0, 1, count))
            offset = header_size
            for width, height, data in entries:
                width_byte = 0 if width >= 256 else width
                height_byte = 0 if height >= 256 else height
                file.write(
                    struct.pack(
                        "<BBBBHHII", width_byte, height_byte, 0, 0, 1, 32, len(data), offset
                    )
                )
                offset += len(data)
            for _width, _height, data in entries:
                file.write(data)
        return path


    def export_pngs(self, artifact: SequenceArtifact, directory: str | Path, prefix: str = "frame_") -> tuple[Path, ...]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        written = []
        total = len(artifact.frames)
        for index, source in enumerate(artifact.frames):
            target = directory / f"{prefix}{index:06d}.png"
            shutil.copy2(source, target)
            written.append(target)
            self._progress((index + 1) / total, "导出 PNG")
        return tuple(written)


    def export_gif(
        self,
        artifact: SequenceArtifact,
        path: str | Path,
        fps: float | None = None,
        colors: int = 256,
        dither: str = "FloydSteinberg",
        loop: int = 0,
        width_percent: int = 100,
    ) -> Path:
        """GIF 导出（Wand，无 CLI 子进程）。

        流程：逐帧载入（含缩放）→ MagickQuantizeImages 共享调色板量化
        → 重设帧延迟 → loop → 保存。

        按输入**原样合成**（见[关键决策 #77]）：不做帧优化/透明优化，
        帧间未变化区域全幅存储（旧版 optimize_layers/optimize_transparency
        参数及其内容包围盒裁剪已移除，优化交由后续「GIF 优化」节点/
        gifsicle 承担）。与旧 CLI 命令 `-delay -dispose frames -resize
        -dither -colors -loop` 行为对齐。
        """
        require_wand(self.imagemagick, "GIF 导出")
        import wand.image as wand_image

        # 帧速由调用方（GIF 合成节点的「帧速」参数）输入，序列产物不携带帧率
        # 信息，不再有产物帧率回退。
        if not fps or fps <= 0:
            raise ValueError("导出 GIF 需要帧速参数（请设置 GIF 合成节点的「帧速」）")
        delay = max(1, round(100 / fps))
        target_width = artifact.width
        target_height = artifact.height
        if width_percent != 100:
            target_width = max(1, round(target_width * width_percent / 100))
            target_height = max(1, round(target_height * width_percent / 100))
        dither_method = wand_image.DITHER_METHODS.index(
            {
                "FloydSteinberg": "floyd_steinberg",
                "Riemersma": "riemersma",
                "None": "no",
            }.get(dither, "no")
        )
        return self._assemble_gif(
            artifact, path, delay=delay,
            target_width=target_width, target_height=target_height,
            colors=colors, dither_index=dither_method,
            loop=loop,
        )


    def export_gif_ffmpeg(
        self,
        artifact: SequenceArtifact,
        path: str | Path,
        *,
        fps: float | None = None,
        width_percent: int = 100,
        max_colors: int = 256,
        stats_mode: str = "full",
        dither: str = "floyd_steinberg",
        bayer_scale: int = 5,
        diff_mode: bool = True,
    ) -> Path:
        """GIF 导出（FFmpeg palettegen/paletteuse 管线，PyAV 进程内）。

        对应[关键决策 #100]的「GIF 合成(FFmpeg)」节点后端：业界事实标准
        管线 palettegen（整段序列共享调色板，与 wand MagickQuantizeImages
        哲学一致）→ paletteuse（仿色/帧优化）→ gif 编码器；``diff_mode``
        开启时编码器**直接产出局部帧**（gifsicle -O2 级帧优化，无需后处理）。

        与 ``export_gif``（wand 原样合成）平行：wand 管共享调色板精确控制
        （录屏冻结等需要确定性调色板的场景），FFmpeg 管线管「调色板 + 编码
        时帧优化」一体。参数（机器键）见 ``ffmpeg_gif.encode_gif_frames``。
        """
        return encode_gif_frames(
            artifact.frames,
            path,
            fps=fps,
            width_percent=width_percent,
            max_colors=max_colors,
            stats_mode=stats_mode,
            dither=dither,
            bayer_scale=bayer_scale,
            diff_mode=diff_mode,
            progress=self._progress,
        )


    def export_webp(
        self,
        artifact: SequenceArtifact,
        path: str | Path,
        *,
        fps: float | None = None,
        quality: int = 80,
        lossless: bool = False,
        width_percent: int = 100,
    ) -> Path:
        """WebP 动画导出（Pillow 内建，零新依赖，见[关键决策 #101]）。

        RGBA 逐帧保留 alpha（WebP 动画支持全透明通道）；``lossless`` 勾选时
        用无损编码（体积更大），否则 ``quality`` 0–100 有损编码。

        ⚠️ 透明序列强制无损：实测 Pillow 的 WebP 动画**有损**路径丢失 alpha
        （透明区域写为不透明黑；单帧/无损均正常，Pillow #8101 同类缺陷）。
        含透明通道的序列自动 ``lossless=True``（调用方在元数据中如实报告）。
        """
        if artifact.has_alpha:
            lossless = True
        return self._export_animated_pillow(
            artifact, path, fmt="WEBP", fps=fps, width_percent=width_percent,
            save_kwargs={"quality": quality, "lossless": lossless},
        )


    def export_apng(
        self,
        artifact: SequenceArtifact,
        path: str | Path,
        *,
        fps: float | None = None,
        width_percent: int = 100,
    ) -> Path:
        """APNG 动画导出（Pillow 内建，零新依赖，见[关键决策 #101]）。

        APNG 为无损格式（PNG 帧 + acTL 动画块），alpha 全保留。
        """
        return self._export_animated_pillow(
            artifact, path, fmt="PNG", fps=fps, width_percent=width_percent,
            save_kwargs={},
        )


    def _export_animated_pillow(
        self,
        artifact: SequenceArtifact,
        path: str | Path,
        *,
        fmt: str,
        fps: float | None,
        width_percent: int,
        save_kwargs: dict,
    ) -> Path:
        """Pillow 动画导出共用实现（WebP/APNG）：逐帧 RGBA → save_all。

        帧速由调用方（节点的「帧速」参数）输入（同 GIF 合成语义，序列产物
        不携带帧率）；duration = 1000/fps 毫秒。loop=0（无限循环）。
        """
        if not fps or fps <= 0:
            raise ValueError("导出动画需要帧速参数（请设置节点的「帧速」）")
        duration = max(1, round(1000 / fps))
        target_w = artifact.width
        target_h = artifact.height
        if width_percent != 100:
            target_w = max(1, round(target_w * width_percent / 100))
            target_h = max(1, round(target_h * width_percent / 100))
        frames = [Path(p) for p in artifact.frames]
        if not frames:
            raise ValueError("导出动画：输入序列为空")
        first: Image.Image | None = None
        rest: list[Image.Image] = []
        total = len(frames)
        for index, source in enumerate(frames):
            with Image.open(source) as image:
                rgba = image.convert("RGBA")
                if (rgba.width, rgba.height) != (target_w, target_h):
                    rgba = rgba.resize((target_w, target_h), Image.Resampling.LANCZOS)
                if first is None:
                    first = rgba
                else:
                    rest.append(rgba)
            self._progress((index + 1) / total, "导出动画")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        first.save(
            path,
            format=fmt,
            save_all=True,
            append_images=rest,
            duration=duration,
            loop=0,
            **save_kwargs,
        )
        self._progress(1.0, "导出完成")
        return path


    def optimize_gif(
        self,
        manifest: MediaManifest,
        path: str | Path,
        *,
        optimize: str = "o3",
        lossy: int = 0,
        recolor: bool = False,
        colors: int = 128,
        color_method: str = "diversity",
        dither: str = "floyd-steinberg",
        colormap: str = "none",
        colormap_file: str | None = None,
        careful: bool = False,
    ) -> tuple[MediaManifest, int, int]:
        """gifsicle 后处理：GIF 文件级优化（文件进/文件出，一次有界子进程调用）。

        对应[关键决策 #78]的「GIF 优化」节点后端：输入为 GIF 文件清单
        （GIF 合成节点的「格式化清单」输出），输出为优化后的 GIF 文件——
        wand 管像素质量（合成），gifsicle 管文件体积（-O3 帧优化 /
        --lossy 有损压缩 / GIF 级再降色 / 固定色板），职责互补，不破坏
        wand 组装的共享调色板。

        参数（机器键，见 ``options.GIFSICLE_*`` 与 ``gifsicle.build_gifsicle_args``）：
        ``optimize`` = none/o1/o2/o3；``lossy`` = 0–200（0=不启用有损）；
        ``recolor`` 开启 GIF 级再降色后，``colors``（2–256）/``color_method``
        （diversity/blend-diversity/median-cut）/``dither``（none/…）/
        ``colormap``（none/web/gray/bw/file）与 ``colormap_file`` 生效；
        ``careful`` = --careful 兼容模式。

        返回 ``(新清单, 优化前字节数, 优化后字节数)``；输出先写临时文件
        再原子替换到 ``path``（失败不破坏旧缓存）。
        """
        runtime = configure_gifsicle()
        require_gifsicle(runtime, "GIF 优化")
        if manifest is None or not manifest.sources:
            raise ValueError("GIF 优化：节点无输入")
        source = Path(manifest.sources[0])
        if not source.is_file() or source.suffix.lower() != ".gif":
            raise ValueError(f"GIF 优化：输入清单必须指向 GIF 文件（{source}）")
        palette_path: Path | None = None
        if colormap == "file":
            if not colormap_file:
                raise ValueError("GIF 优化：固定色板选择「自定义文件」但未提供色板文件")
            palette_path = Path(colormap_file)
            if not palette_path.is_file():
                raise ValueError(f"GIF 优化：色板文件不存在：{palette_path}")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.tmp")
        args = build_gifsicle_args(
            source,
            temp,
            optimize=optimize,
            lossy=lossy,
            recolor=recolor,
            colors=colors,
            color_method=color_method,
            dither=dither,
            colormap=colormap,
            colormap_file=str(palette_path) if palette_path is not None else None,
            careful=careful,
        )
        before = source.stat().st_size
        # 单次子进程调用（gifsicle 无进度 API）；期间进度为不确定态，
        # 停止落在下一个检查点（与 wand 长调用的限制一致）。
        self._progress(None, "gifsicle 优化中")
        # stdin=subprocess.DEVNULL：双击启动（无控制台）下标准句柄无效，
        # subprocess._get_handles 处理 stdin 继承抛 WinError 6（Nuitka #3030）；
        # gifsicle 只读输入文件不读 stdin，显式 DEVNULL 绕开（与探测一致）。
        # creationflags：无控制台父进程启动控制台子进程会闪黑框，禁止新窗口。
        try:
            proc = subprocess.run(
                [str(runtime.exe), *args],
                capture_output=True,
                text=True,
                timeout=GIFSICLE_TIMEOUT_S,
                errors="replace",
                stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW_FLAG,
            )
        except subprocess.TimeoutExpired:
            _remove_path(temp)
            raise ValueError(
                f"gifsicle 优化超时（超过 {GIFSICLE_TIMEOUT_S} 秒），"
                "请降低优化级别或减小 GIF"
            ) from None
        if proc.returncode != 0:
            _remove_path(temp)
            detail = (proc.stderr or proc.stdout or "").strip()
            raise ValueError(
                f"gifsicle 优化失败（exit {proc.returncode}）：{detail[:300]}"
            )
        if not temp.is_file() or temp.stat().st_size == 0:
            _remove_path(temp)
            raise ValueError("gifsicle 优化失败：未生成输出文件")
        try:
            os.replace(temp, path)
        except OSError:
            _remove_path(temp)
            raise
        after = path.stat().st_size
        self._progress(1.0, "优化完成")
        return MediaManifest(MediaKind.ANIMATED_IMAGE, (str(path),)), before, after


    def _assemble_gif(
        self,
        artifact: SequenceArtifact,
        path: str | Path,
        *,
        delay: int,
        target_width: int,
        target_height: int,
        colors: int,
        dither_index: int,
        loop: int,
    ) -> Path:
        """GIF 组装公共流程：逐帧载入（含缩放）→ 共享调色板量化（≤256 色时跳过）
        → 重设帧延迟 → loop → 保存。

        按输入**原样合成**（见[关键决策 #77]）：不做帧优化/透明优化，
        帧间未变化区域全幅存储（旧版内容包围盒裁剪及其 1px 背景色边距
        机制已移除，优化交由后续「GIF 优化」节点/gifsicle 承担）。

        量化步骤（见[关键决策 #97]）：整条序列的跨帧颜色并集 ≤256 且未发生
        缩放时**跳过二次量化**——上游「颜色量化」节点已确定调色板/仿色
        （决策 #77 语义），导出不再用 256-FS 重新仿色（IM Floyd-Steinberg
        误差扩散对相同帧也输出不同图案，会破坏「帧差静止保持」的逐帧冻结
        并重新引入帧间闪烁）；>256 色或缩放插值出新颜色的场景保留
        256-FS 量化安全网。
        """
        require_wand(self.imagemagick, "GIF 导出")
        from wand.image import Image as WandImage

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        total = len(artifact.frames)
        resized = False
        with WandImage() as gif:
            for index, frame_path in enumerate(artifact.frames):
                with WandImage(filename=frame_path) as frame:
                    if frame.width != target_width or frame.height != target_height:
                        frame.resize(target_width, target_height)
                        resized = True
                    frame.dispose = "background"
                    gif.sequence.append(frame)
                self._progress((index + 1) / total, "载入帧")

            # 说明：进度不再依赖 MagickSetImageProgressMonitor（与 wand 组合存在
            # 随机性原生崩溃，见 _format_animated_image 注释）；各阶段用显式
            # 进度上报，长操作（量化）期间为不确定进度。
            skip_quantize = not resized and _sequence_within_color_budget(artifact.frames, 256)
            if not skip_quantize:
                self._progress(None, "量化调色板")
                _wand_quantize_all(gif.wand, max(2, min(256, colors)), dither_index)
                self._progress(1.0, "量化调色板")

            for frame in gif.sequence:
                frame.delay = delay
            gif.loop = loop

            gif.save(filename=str(path))
            self._progress(1.0, "导出完成")
        return path

    # ------------------------------------------------------------------
    # 缓存管理
    # ------------------------------------------------------------------

    # ===== 区段 5：颜色量化（原 MediaBackendQuantizeMixin） =====

    def color_reduce_sequence(
        self,
        artifact: SequenceArtifact,
        *,
        algorithm: str = "adaptive",
        colors: int = 256,
        dither: str = "diffusion",
        map_name: str = "o8x8",
        levels: str = "13",
    ) -> SequenceArtifact:
        """PS「存储为 Web 所用格式」式颜色深度控制（序列→序列），输出新的 RGBA PNG 序列。

        ``algorithm``（降低颜色深度算法，机器键；「可选择 Selective」不实现）：
        - ``adaptive``（可感知）：ImageMagick 默认 octree 量化（sRGB 空间）；
        - ``perceptual``（随样性）：Lab 感知色彩空间量化（等价 CLI ``-quantize lab``）；
        - ``restrictive_web``（受限 Web）：逐帧 remap 到 216 色 web-safe 固定色板
          （``colors`` 忽略，固定 216 色）；
        - ``grayscale``（灰度）：先转灰度再量化（``colors`` 生效）；
        - ``bw``（黑白）：转灰度后 remap 到**固定纯黑/纯白色板**（``colors`` 忽略，
          输出只有 (0,0,0) 与 (255,255,255)）；
        - ``windows`` / ``macos``：逐帧 remap 到系统色板 PNG
          （``data/palettes/<name>.png``，固定 256 色，``colors`` 忽略）。

        ``dither``（仿色算法）：``diffusion``（误差扩散）/ ``none``（无仿色）/
        ``pattern``（逐帧有序仿色，配合 ``map_name``/``levels``）/ ``noise``
        （逐帧均匀加噪后无仿色量化）。

        逐帧预处理（灰度转换/有序仿色/加噪/固定色板 remap）在装帧前完成，
        只作用于 RGB 通道，Alpha 原样保留；octree 量化在整条序列上执行
        （共享调色板，避免逐帧独立调色板导致的闪烁/偏色）。
        """
        require_wand(self.imagemagick, "颜色深度")
        from wand.image import Image as WandImage

        map_spec = f"{map_name},{levels}" if levels else map_name
        gray = algorithm in ("grayscale", "bw")
        fixed_palette: WandImage | None = None
        if algorithm in ("restrictive_web", "windows", "macos", "bw"):
            if algorithm == "bw":
                # 黑白：固定纯黑/纯白色板。remap 分配保证输出**只有**
                # (0,0,0) 与 (255,255,255) 两色（octree 量化 2 色会取灰度均值，
                # 输出并非纯黑白）。仿色=扩散时仍做误差扩散分配（PS 1-bit 语义）。
                blob = _palette_png_blob([(0, 0, 0), (255, 255, 255)])
            elif algorithm == "restrictive_web":
                blob = _websafe_map_blob()
            else:
                blob = system_palette_blob(algorithm)
            fixed_palette = WandImage(blob=blob)
        output = self._job_dir("dither")
        paths: list[str] = []
        total = len(artifact.frames)
        # octree 量化路径的 alpha 掩码（与装入序列的帧一一对应，None=无 alpha）：
        # MagickQuantizeImages 对 RGB 内容为灰度（R==G==B）或 gray 色彩空间的
        # 带 alpha 图像会破坏 alpha 通道（实测 alpha 被量化成 0/1 或 0..N 的
        # 乱值，产出全透明），因此量化前先摘出掩码、量化后再回填（与固定色板
        # remap 路径同一套手法，见下）。
        alpha_masks: list = []
        try:
            with WandImage() as sequence:
                # 1) 逐帧预处理（可选灰度/有序仿色/加噪/固定色板 remap），装入序列。
                for index, source in enumerate(artifact.frames):
                    with WandImage(filename=source) as frame:
                        if fixed_palette is None and frame.alpha_channel:
                            alpha_mask = frame.clone()
                            alpha_mask.alpha_channel = "extract"
                            frame.alpha_channel = "off"
                            alpha_masks.append(alpha_mask)
                        else:
                            alpha_masks.append(None)
                        if gray:
                            frame.transform_colorspace("gray")
                        # 灰度帧只有单一通道：仿色/加噪作用于 gray 通道，
                        # 避免对不存在 RGB 通道的操作报错或误处理。
                        channel = "gray" if gray else "rgb"
                        if dither == "pattern":
                            frame.ordered_dither(map_spec, channel=channel)
                        elif dither == "noise":
                            frame.noise("uniform", channel=channel)
                        if fixed_palette is not None:
                            # remap 按 RGBA 全通道做最近色匹配，会把半透明/全透明
                            # 像素吸附为**不透明**的色板色（实测 alpha 被清零为 255）。
                            # 先提取 alpha 掩码、remap 后再回填，保证只作用于 RGB。
                            alpha_mask = frame.clone()
                            alpha_mask.alpha_channel = "extract"
                            frame.remap(
                                fixed_palette,
                                method="floyd_steinberg" if dither == "diffusion" else "no",
                            )
                            frame.composite(alpha_mask, operator="copy_alpha")
                            alpha_mask.close()
                        sequence.sequence.append(frame)
                    self._progress((index + 1) / total, "颜色深度")
                # 2) 调色板策略：固定色板路径（含黑白）remap 已就位；
                #    octree 路径做共享量化。
                self._progress(None, "量化调色板")
                if fixed_palette is None:
                    number_colors = max(2, min(256, int(colors)))
                    colorspace = "lab" if algorithm == "perceptual" else "srgb"
                    dither_index = 3 if dither == "diffusion" else 1  # floyd_steinberg / no
                    _wand_quantize_all(sequence.wand, number_colors, dither_index, colorspace=colorspace)
                    # 量化只作用于 RGB，把预摘出的 alpha 掩码回填到各帧。
                    for frame, alpha_mask in zip(sequence.sequence, alpha_masks):
                        if alpha_mask is not None:
                            frame.composite(alpha_mask, operator="copy_alpha")
                            alpha_mask.close()
                self._progress(1.0, "量化调色板")
                # 3) 逐帧导出 PNG：MagickCloneImage 克隆几乎免费（30 帧 ≈ 2ms），
                #    PNG 编码才是大头（ImageMagick 默认 quality=75 做自适应滤波，
                #    实测比 Pillow 慢 3–4 倍）。克隆出独立帧后交给线程池并行编码
                #    保存（每线程只操作自己的克隆，ctypes 调用期间 GIL 释放），
                #    实测 30 帧 1920×1080：串行 ≈6.2s → 并行 ≈1.6s。
                clones = [sequence.sequence[index].clone() for index in range(len(sequence.sequence))]
                try:
                    targets = [output / f"frame_{index:06d}.png" for index in range(len(clones))]
                    with ThreadPoolExecutor(max_workers=PNG_EXPORT_WORKERS) as pool:
                        futures = {
                            pool.submit(_save_wand_png, clone, target): index
                            for index, (clone, target) in enumerate(zip(clones, targets))
                        }
                        for future in as_completed(futures):
                            future.result()  # 保存异常在此抛出
                            self._progress((futures[future] + 1) / total, "导出序列")
                finally:
                    for clone in clones:
                        clone.close()
                paths = [str(target) for target in targets]
        finally:
            if fixed_palette is not None:
                fixed_palette.close()
        return SequenceArtifact(
            tuple(paths), artifact.width, artifact.height,
            artifact.has_alpha, str(output),
        )


    def color_quantize_sequence(
        self,
        artifact: SequenceArtifact,
        *,
        colorspace: str = "srgb",
        colors: int = 256,
        treedepth: int = 0,
        dither: str = "floyd_steinberg",
        use_ordered: bool = False,
        ordered_map: str = "o8x8",
        levels: str = "",
        posterize_levels: int = 0,
    ) -> SequenceArtifact:
        """IM 原生颜色量化（序列→序列），输出新的 RGBA PNG 序列。

        参数直接映射 ImageMagick 操作符（见[关键决策 #76]，设计上不做 PS 语义
        对齐、不携带旧颜色深度节点的固定色板/取色补丁）：

        - ``colorspace``：``-quantize <space>`` 量化分桶色彩空间（srgb/lab/
          gray/transparent 等，wand ``COLORSPACE_TYPES``）；``gray`` 同时把帧
          转为灰度再量化（等价 ``-colorspace gray -colors N``）；``transparent``
          把 Alpha 纳入量化（GIF 1-bit 透明语义）；
        - ``colors``：``-colors`` 目标颜色数（2–256），整条序列共享调色板
          （``MagickQuantizeImages``，避免逐帧独立调色板闪烁/偏色）；
        - ``treedepth``：``-treedepth`` octree 树深度（0 = 自动）；
        - ``dither``：``-dither`` 量化仿色（no / floyd_steinberg / riemersma）；
        - ``use_ordered``/``ordered_map``/``levels``：``-ordered-dither``
          逐帧有序仿色预处理（map 可选 `,levels` 后缀），误差扩散之外的原生
          仿色方式（与量化仿色互斥，面板置灰）；
        - ``posterize_levels``：``-posterize`` 每通道均匀色阶（0 = 关闭）。

        Alpha 语义：``transparent`` 空间下 Alpha 参与量化（逐帧预摘 Alpha 的
        掩码机制不适用）；其余空间先摘出 Alpha 掩码、量化 RGB 后回填——保持
        原 Alpha 逐像素精确（IM 对灰度内容/gray 空间量化会破坏 alpha，见旧
        节点注释与[决策 #56]）。
        """
        require_wand(self.imagemagick, "颜色量化")
        from wand.image import Image as WandImage

        alpha_in_quantize = colorspace == "transparent"
        gray = colorspace == "gray"
        dither_index = {"no": 1, "riemersma": 2, "floyd_steinberg": 3}.get(dither, 3)
        map_spec = f"{ordered_map},{levels}" if levels else ordered_map
        output = self._job_dir("quantize")
        paths: list[str] = []
        total = len(artifact.frames)
        # 非 transparent 空间的 alpha 掩码（与帧一一对应，None=无 alpha）：
        # 量化前摘出、量化后回填（见方法 docstring 的 Alpha 语义）。
        alpha_masks: list = []
        try:
            with WandImage() as sequence:
                # 1) 逐帧预处理（灰度转换/有序仿色/海报化/摘 alpha），装入序列。
                for index, source in enumerate(artifact.frames):
                    with WandImage(filename=source) as frame:
                        if gray:
                            frame.transform_colorspace("gray")
                        if use_ordered:
                            frame.ordered_dither(
                                map_spec,
                                channel="gray" if gray else "rgb",
                            )
                        if posterize_levels > 0:
                            frame.posterize(posterize_levels, dither="no")
                        if not alpha_in_quantize and frame.alpha_channel:
                            alpha_mask = frame.clone()
                            alpha_mask.alpha_channel = "extract"
                            frame.alpha_channel = "off"
                            alpha_masks.append(alpha_mask)
                        else:
                            alpha_masks.append(None)
                        sequence.sequence.append(frame)
                    self._progress((index + 1) / total, "颜色量化")
                # 2) 共享调色板量化（整条序列一个调色板）。
                self._progress(None, "量化调色板")
                _wand_quantize_all(
                    sequence.wand,
                    max(2, min(256, int(colors))),
                    dither_index,
                    colorspace=colorspace,
                    treedepth=treedepth,
                )
                # 量化只作用于 RGB（transparent 空间已含 alpha，无掩码），
                # 把预摘出的 alpha 掩码回填到各帧。
                for frame, alpha_mask in zip(sequence.sequence, alpha_masks):
                    if alpha_mask is not None:
                        frame.composite(alpha_mask, operator="copy_alpha")
                        alpha_mask.close()
                self._progress(1.0, "量化调色板")
                # 3) 逐帧克隆并行导出 PNG（与颜色深度节点同一套并行编码）。
                clones = [sequence.sequence[index].clone() for index in range(len(sequence.sequence))]
                try:
                    targets = [output / f"frame_{index:06d}.png" for index in range(len(clones))]
                    with ThreadPoolExecutor(max_workers=PNG_EXPORT_WORKERS) as pool:
                        futures = {
                            pool.submit(_save_wand_png, clone, target): index
                            for index, (clone, target) in enumerate(zip(clones, targets))
                        }
                        for future in as_completed(futures):
                            future.result()  # 保存异常在此抛出
                            self._progress((futures[future] + 1) / total, "导出序列")
                finally:
                    for clone in clones:
                        clone.close()
                paths = [str(target) for target in targets]
        finally:
            for alpha_mask in alpha_masks:
                if alpha_mask is not None:
                    try:
                        alpha_mask.close()
                    except Exception:
                        pass
        return SequenceArtifact(
            tuple(paths), artifact.width, artifact.height,
            artifact.has_alpha, str(output),
        )

    # ===== 区段 6：分析（原 MediaBackendAnalysisMixin） =====

    def analysis_first_frame(self, manifest: MediaManifest | None = None, sequence: SequenceArtifact | None = None) -> str:
        """分析节点用：取一张代表性图片（1:1 原始分辨率）的路径。

        序列优先取第 0 帧；清单优先用其预览图（输入节点已物化的首帧），
        无预览时回退 extract_first_frame。
        """
        if sequence is not None and sequence.frames:
            return sequence.frames[0]
        if manifest is not None:
            if manifest.preview and Path(manifest.preview).is_file():
                return manifest.preview
            return self.extract_first_frame(manifest)
        raise ValueError("节点无输入")


    def analysis_palette(self, manifest: MediaManifest | None = None, sequence: SequenceArtifact | None = None) -> tuple[list[tuple[int, int, int]], bool]:
        """统计输入的调色板：全部像素 RGB 去重（按从小到大排序），返回 (颜色列表, 是否含透明)。

        颜色数大于 256 时直接抛出 ``ValueError``（不支持）。GIF 逐帧、静态序列逐图、
        视频经 PyAV 解码逐帧；任一来源颜色数超限即早停报错。
        """
        colors: set[tuple[int, int, int]] = set()
        has_transparency = False

        def collect(image) -> None:
            nonlocal has_transparency
            rgba = image.convert("RGBA")
            if rgba.getchannel("A").getextrema()[0] < 255:
                has_transparency = True
            counts = rgba.convert("RGB").getcolors(256)
            if counts is None:
                raise ValueError("调色板颜色数大于 256，不支持")
            colors.update(tuple(color) for _count, color in counts)
            if len(colors) > 256:
                raise ValueError("调色板颜色数大于 256，不支持")

        if sequence is not None:
            total = len(sequence.frames)
            for index, frame_path in enumerate(sequence.frames):
                with Image.open(frame_path) as image:
                    collect(image)
                self._progress((index + 1) / total, "统计调色板")
        elif manifest is not None and manifest.kind is MediaKind.ANIMATED_IMAGE:
            # GIF：读颜色表条目（与 probe_gif 的「颜色板颜色数」同源的结构解析，
            # 条目数天然一致；GIF 色表 ≤256，无需超限检查）。
            colors, transparent_index = gif_palette_entries(manifest.sources[0])
            return list(colors), transparent_index is not None
        elif manifest is not None and manifest.kind is MediaKind.VIDEO:
            raise ValueError("不支持视频输入，仅支持图片序列及 GIF")
        elif manifest is not None:
            total = len(manifest.sources)
            for index, source in enumerate(manifest.sources):
                with Image.open(source) as image:
                    collect(image)
                self._progress((index + 1) / total, "统计调色板")
        else:
            raise ValueError("节点无输入")
        return sorted(colors), has_transparency


    def palette_swatch(self, colors: list[tuple[int, int, int]], has_transparency: bool) -> str:
        """把调色板绘制为固定 16×16 色块图（RGBA，缺色格透明）。

        固定 16 列 × 16 行对齐（256 格），颜色按行优先铺入；颜色不足
        256 时其余色块为**透明**（alpha=0，预览框 1:1 显示时透出底纹）。
        含透明时在颜色之后附一个棋盘格色块（仅在有空位时；256 色全满时
        省略，透明信息由元数据「含透明」承担）。
        """
        from PIL import ImageDraw

        cell, gap = 12, 1
        columns = rows = 16
        capacity = columns * rows
        entries: list[tuple[int, int, int] | None] = list(colors)
        if has_transparency and len(entries) < capacity:
            entries.append(None)  # None = 透明（棋盘格）
        width = columns * cell + (columns - 1) * gap
        height = rows * cell + (rows - 1) * gap
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for index, color in enumerate(entries):
            x = (index % columns) * (cell + gap)
            y = (index // columns) * (cell + gap)
            if color is None:
                # 透明色块：棋盘格
                for yy in range(cell):
                    for xx in range(cell):
                        draw.point(
                            (x + xx, y + yy),
                            fill=(150, 150, 158, 255) if ((xx // 5) + (yy // 5)) % 2 else (40, 40, 44, 255),
                        )
            else:
                draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(*color, 255))
        output = self._job_dir("palette")
        target = output / "palette.png"
        image.save(target, "PNG")
        return str(target)


    def analysis_gif_frames(
        self,
        manifest: MediaManifest | None = None,
        mode: str = "stored",
    ) -> tuple[str, tuple[str, ...], dict[str, Any]]:
        """GIF 优化分析：特殊解码——按文件实际存储结构逐帧解出 PNG（1:1 供预览滑条）。

        ``mode``：
        - ``stored``（存储帧）：按文件**实际存储**解码——帧优化（局部帧只存
          变化区域）与透明优化（帧间未变化像素置透明）的真实形态原样可见：
          局部帧按偏移贴回全画布透明底（未存储区域=透明），因此完整画布上
          每一帧只显示其实际内容，与普通播放器的 coalesce 合成结果不同；
        - ``coalesced``（合成帧）：coalesce 后的完整合成帧（与普通播放器
          一致），用于与存储帧对照（合成后帧优化/透明优化的痕迹消失）。

        透明像素统计按**帧自身存储数据**（GCE 透明索引实际生效的像素）计算，
        不含存储帧模式贴画布产生的合成透明区。返回 ``(首帧路径, 全部帧路径,
        元数据)``；元数据含画布/帧数/循环/解码方式、帧优化统计（局部帧数，
        **按文件结构计算**——两种解码方式都如实反映文件的帧优化情况，合成帧
        模式下 coalesce 抹掉的只是显示层）、帧优化占比（以最短帧时间为基准，
        相对等时长全最短帧序列节省的帧数比例）、透明优化统计（含透明像素帧数、
        占比）——不含逐帧明细（按需求收敛为关键统计，逐帧信息由滑条逐帧查看
        承担）。
        """
        if manifest is None or not manifest.sources:
            raise ValueError("gif优化分析：节点无输入")
        path = Path(manifest.sources[0])
        if not path.is_file() or path.suffix.lower() != ".gif":
            raise ValueError(f"gif优化分析：输入清单必须指向 GIF 文件（{path}）")
        info = gif_playback_info(str(path))
        if info is None:
            raise ValueError(f"gif优化分析：无法解析 GIF 文件结构：{path}")
        canvas_w, canvas_h = info["width"], info["height"]
        frame_count = info["frame_count"]
        loop = info["loop"]
        output = self._job_dir("gif_analysis")
        frames: list[str] = []
        # 帧优化统计按文件结构（图像描述符声明的存储区域）计算：合成帧模式下
        # coalesce 会抹掉局部帧的显示痕迹，但文件的优化情况仍应如实报告。
        partial_count = sum(
            1
            for (_left, _top, width, height) in (info.get("regions") or [])
            if (width, height) != (canvas_w, canvas_h)
        )
        transp_frames = 0
        transp_pixels = 0
        total_pixels = 0
        try:
            # Wand 路径：逐帧直取 RGBA 字节（_wand_rgba_bytes），存储帧模式
            # 按 frame.page 偏移贴回全画布透明底；合成帧模式先 coalesce。
            require_wand(self.imagemagick, "gif优化分析")
            from wand.image import Image as WandImage

            with WandImage(filename=str(path)) as img:
                if mode == "coalesced":
                    img.coalesce()
                sequence = list(img.sequence)
                for index, frame in enumerate(sequence):
                    raw, width, height = _wand_rgba_bytes(frame)
                    left, top = (
                        (frame.page[2], frame.page[3])
                        if mode == "stored"
                        else (0, 0)
                    )
                    array = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
                    alpha = array[..., 3]
                    transparent = int((alpha < 255).sum())
                    area = width * height
                    if transparent:
                        transp_frames += 1
                        transp_pixels += transparent
                    total_pixels += area
                    rgba = Image.frombuffer(
                        "RGBA", (width, height), raw, "raw", "RGBA", 0, 1
                    ).copy()
                    if mode == "stored":
                        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
                        canvas.paste(rgba, (left, top))
                        rgba = canvas
                    target = output / f"frame_{index:06d}.png"
                    rgba.save(target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL)
                    frames.append(str(target))
                    self._progress((index + 1) / frame_count, "gif优化分析")
        except ValueError:
            # 仅 ImageMagick 缺失等显式错误向上抛（coalesce 合成帧必须 wand）。
            if mode == "coalesced":
                raise
            # 回退 PIL：seek 逐帧，存储帧取 tile 区域贴回透明画布。
            # （wand 路径若中途失败，先复位统计再从头解码。）
            frames = []
            transp_frames = 0
            transp_pixels = 0
            total_pixels = 0
            try:
                with Image.open(str(path)) as image:
                    count = getattr(image, "n_frames", 1)
                    for index in range(count):
                        image.seek(index)
                        rgba = image.convert("RGBA")
                        if image.tile:
                            left, top, right, bottom = image.tile[0][1]
                        else:
                            left, top = 0, 0
                            right, bottom = rgba.width, rgba.height
                        region = rgba.crop((left, top, right, bottom))
                        alpha = region.convert("RGBA").getchannel("A")
                        transparent = int((np.asarray(alpha) < 255).sum())
                        area = region.width * region.height
                        if transparent:
                            transp_frames += 1
                            transp_pixels += transparent
                        total_pixels += area
                        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
                        canvas.paste(region, (left, top))
                        target = output / f"frame_{index:06d}.png"
                        canvas.save(target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL)
                        frames.append(str(target))
                        self._progress((index + 1) / count, "gif优化分析")
            except Exception as exc:
                raise ValueError(f"gif优化分析：GIF 解码失败：{exc}") from exc
        if not frames:
            raise ValueError("gif优化分析：未能解出任何帧")
        metadata = {
            "画布尺寸": f"{canvas_w} × {canvas_h}",
            "帧数": frame_count,
            "循环次数": loop if loop is not None else "未指定",
            "解码方式": "存储帧（按文件实际存储）"
            if mode == "stored"
            else "合成帧（coalesce）",
            "帧优化": (
                f"{partial_count}/{frame_count} 帧为局部帧（仅存变化区域）"
                if partial_count
                else "无局部帧（每帧全幅存储）"
            ),
            "帧优化占比": _frame_optimization_label(
                info.get("durations_ms") or [], frame_count
            ),
            "透明优化": (
                f"{transp_frames}/{frame_count} 帧含透明像素"
                f"（透明像素占比 {transp_pixels / total_pixels * 100:.2f}%）"
                if transp_frames
                else "无透明像素（未做透明优化）"
            ),
        }
        return frames[0], tuple(frames), metadata


    def analysis_ico_montage(self, manifest: MediaManifest | None = None) -> tuple[str, dict[str, Any]]:
        """ico 分辨率查看：把清单携带的各分辨率帧合成为 1:1 拼贴图（所有分辨率同屏可见）。

        每帧按**原始像素尺寸** 1:1 绘制（不缩放、不裁剪），按尺寸从大到小
        打包成行（每行总宽 ≤ 最大尺寸 + 间距），图标下方标注尺寸；透明区域
        透出棋盘格底纹。返回 ``(拼贴图路径, 元数据)``。
        """
        if manifest is None or not manifest.sources:
            raise ValueError("ico分辨率查看：节点无输入")
        items: list[tuple[str, int, int, Image.Image]] = []
        for source in manifest.sources:
            try:
                with Image.open(source) as image:
                    rgba = image.convert("RGBA")
            except Exception as exc:
                raise ValueError(f"ico分辨率查看：输入清单中的文件不是图片（{source}）：{exc}") from exc
            items.append((str(source), rgba.width, rgba.height, rgba))
        items.sort(key=lambda item: item[1], reverse=True)  # 从大到小
        from PIL import ImageDraw, ImageFont

        gap = 12       # 图标间距
        label_h = 16   # 尺寸标注行高
        pad = 10       # 画布边距
        max_w = items[0][1]
        rows: list[list[tuple[str, int, int, Image.Image]]] = []
        row: list[tuple[str, int, int, Image.Image]] = []
        row_w = 0
        for item in items:
            width = item[1]
            if row and row_w + gap + width > max_w + gap:
                rows.append(row)
                row, row_w = [], 0
            row.append(item)
            row_w += gap + width
        if row:
            rows.append(row)
        row_heights = [max(item[2] for item in items_in_row) for items_in_row in rows]
        row_widths = [
            sum(item[1] for item in items_in_row) + gap * (len(items_in_row) - 1)
            for items_in_row in rows
        ]
        total_w = max(row_widths) + 2 * pad
        total_h = sum(h + label_h for h in row_heights) + gap * (len(rows) - 1) + 2 * pad
        montage = Image.new("RGBA", (total_w, total_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(montage)
        font = ImageFont.load_default()
        y = pad
        for row_index, items_in_row in enumerate(rows):
            x = pad
            for _source, width, height, rgba in items_in_row:
                montage.alpha_composite(rgba, (x, y))
                draw.text((x, y + height + 2), f"{width}*{height}", fill=(200, 205, 215), font=font)
                x += width + gap
            y += row_heights[row_index] + label_h + gap
        output = self._job_dir("ico_analysis")
        target = output / "montage.png"
        montage.save(target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL)
        sizes = "、".join(f"{width}*{height}" for _s, width, height, _i in items)
        metadata = {
            "分辨率数量": len(items),
            "分辨率": sizes,
            "最大分辨率": f"{items[0][1]}*{items[0][2]}",
            "画布尺寸": f"{total_w} * {total_h}",
        }
        return str(target), metadata

    # ===== 区段 7：缓存/工作区管理（原 MediaBackendCacheMixin） =====

    def clear_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        for child in self.workspace.iterdir():
            _remove_path(child)


    def clear_cache(self) -> None:
        if self.workspace == self.root_workspace:
            self.clear_workspace()
            return
        _remove_path(self.workspace)


    def snapshot_workspace(self) -> list[Path]:
        """列出本节点工作区顶层的既有产物（上一次运行留下的缓存）。

        在节点重新运行前调用；执行成功后把这些条目交给
        :meth:`clear_previous_run` 删除。
        """
        if not self.workspace.exists():
            return []
        return list(self.workspace.iterdir())


    def clear_previous_run(self, snapshot: list[Path], keep: set[Path] | None = None) -> None:
        """删除节点重新运行前的旧缓存（尽力而为，失败不中断运行）。

        ``snapshot`` 须在本次运行开始前捕获；``keep`` 中的路径跳过
        （例如被本次运行原地覆盖的固定文件 gif_export 的 preview.gif）。
        """
        keep = keep or set()
        for path in snapshot:
            if path in keep:
                continue
            try:
                _remove_path(path)
            except Exception:
                pass

    def cache_size(self) -> int:
        if not self.workspace.exists():
            return 0
        return sum(path.stat().st_size for path in self.workspace.rglob("*") if path.is_file())

    # ------------------------------------------------------------------
    # 缓存总量限制（用户需求：缓存总大小上限，超限自动淘汰最旧中间缓存）
    # ------------------------------------------------------------------

    # job 目录命名：_job_dir 生成 ``<prefix>_<uuid4().hex[:10]>``，末尾为
    # 10 位十六进制。固定缓存（preview.gif / preview_frames 等）不含该后缀，
    # 天然区分「可淘汰的中间产物」与「必须保留的固定缓存」。
    _JOB_DIR_RE = re.compile(r"^.+_[0-9a-f]{10}$")


    def total_cache_size(self) -> int:
        """整个缓存根（root_workspace）下所有文件的字节总和。"""
        if not self.root_workspace.exists():
            return 0
        return sum(path.stat().st_size for path in self.root_workspace.rglob("*") if path.is_file())


    def _collect_evictable_jobs(self) -> tuple[list[Path], int]:
        """收集可淘汰的 job 目录与当前总大小（供 enforce_cache_limit 与测试用）。

        保护规则（不进入淘汰候选）：
        - 每个节点工作区（``root/nodes/<id>/``）下**最新**（mtime 最大）的
          job 目录——预览/帧滑条依赖它；
        - 非 job 条目（名字不含 ``<prefix>_<10位hex>`` 的目录/文件，即
          导出固定缓存 preview.gif / preview_frames 等）。
        返回 ``(候选 job 目录列表, 当前总字节数)``；候选按 mtime 从旧到新排序。
        """
        total = self.total_cache_size()
        candidates: list[Path] = []
        nodes_dir = self.root_workspace / "nodes"
        if not nodes_dir.is_dir():
            return candidates, total
        for node_dir in nodes_dir.iterdir():
            if not node_dir.is_dir():
                continue
            jobs = [
                path
                for path in node_dir.iterdir()
                if path.is_dir() and self._JOB_DIR_RE.match(path.name)
            ]
            if not jobs:
                continue
            latest = max(jobs, key=lambda path: path.stat().st_mtime)
            candidates.extend(job for job in jobs if job is not latest)
        candidates.sort(key=lambda path: path.stat().st_mtime)
        return candidates, total


    def enforce_cache_limit(self, limit_bytes: int, *, keep_fraction: float = 0.8) -> tuple[int, int]:
        """缓存总大小超限时淘汰最旧中间缓存，返回 ``(已清理字节, 已清理条目数)``。

        - 只有总量 > ``limit_bytes × keep_fraction`` 才动手（回退系数避免
          临界抖动）；保留规则见 ``_collect_evictable_jobs``（每节点最新
          job + 固定缓存不淘汰）；
        - 逐个按 mtime 从旧到新删除 job 目录，删除尽力而为
          （``_remove_path`` 容忍 Windows 句柄竞争），失败跳过不中断；
        - 未超限时零开销（只做一次 rglob 统计，不扫描节点目录）。
        """
        limit_bytes = max(0, int(limit_bytes))
        target = limit_bytes * max(0.0, min(1.0, keep_fraction))
        candidates, total = self._collect_evictable_jobs()
        if total <= target or not candidates:
            return (0, 0)
        freed = 0
        removed = 0
        for job in candidates:
            if total - freed <= target:
                break
            size = sum(path.stat().st_size for path in job.rglob("*") if path.is_file())
            try:
                _remove_path(job)
            except Exception:
                continue  # 文件占用等删除失败：跳过该条目，不中断淘汰
            freed += size
            removed += 1
        return (freed, removed)

