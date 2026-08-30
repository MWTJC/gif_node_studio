"""MediaBackend 区段 1：解码/格式化/预览提取（纯函数模块，决策 #120 拆出）。

本模块与 #82 时期的同名 mixin 文件无继承关系；所有函数为模块级纯函数，
工作区/进度等依赖由调用方显式注入（``workspace`` / ``progress``）。"""

from __future__ import annotations

from ..core.domain import MediaKind
from ..core.domain import MediaManifest
from ..core.domain import SequenceArtifact
from .image_utils import DEFAULT_SEQUENCE_FPS
from .image_utils import PNG_CACHE_COMPRESS_LEVEL
from .image_utils import PNG_EXPORT_WORKERS
from .image_utils import _wand_rgba_bytes
from .imagemagick import require_wand
from .media_info import gif_native_fps
from PIL import Image
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
import av
import os
import threading
ProgressReporter = Callable[[float | None, str], None]


def _drain_save_futures(futures: list[Future]):
    """等待流式解码路径提交的全部保存任务完成；异常在此重新抛出。"""
    for future in futures:
        future.result()

def _extract_video_frame(workspace, container_or_path, seconds: float):
    """解码视频在 seconds 时刻的帧并物化为 PNG；失败返回 None。"""
    try:
        if isinstance(container_or_path, (str, Path)):
            with av.open(container_or_path) as container:
                return _extract_video_frame(workspace, container, seconds)
        container = container_or_path
        stream = container.streams.video[0]
        if seconds > 0 and stream.time_base is not None:
            offset = int(seconds * stream.time_base.denominator / stream.time_base.numerator)
            container.seek(offset, stream=stream, backward=True)
        for frame in container.decode(stream):
            current = float(frame.time) if frame.time is not None else 0.0
            if current < seconds - 1e-9:
                continue  # backward seek 落在关键帧，跳过窗口前的帧
            target = _job_dir(workspace, "preview") / "frame.png"
            frame.to_image().convert("RGBA").save(target, "PNG")
            return str(target)
    except Exception:
        return None
    return None

def _format_animated_image(imagemagick, progress, manifest: MediaManifest, output: Path):
    """GIF 输入：Wand 合并部分帧（coalesce）后逐帧导出，再走裁剪/缩放。

    注意：这里不再安装 MagickSetImageProgressMonitor 进度监视器——实测该
    监视器与 wand/ImageMagick 组合会产生随机性原生内存崩溃（access
    violation / illegal instruction，进程内偶发），导致解包失败；进度改为
    逐帧显式上报（``_progress``），coalesce 阶段显示不确定进度。

    帧率仅用于窗口换算（帧号 → 秒，见 ``gif_native_fps``），不写入产物。
    """
    require_wand(imagemagick, "GIF 解码")
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
                futures.append(pool.submit(_parallel_pil_save_bounded, image, target, save_sem))
                written.append(str(target))
                _progress(progress, (index + 1) / total, "解包 GIF")
            # 等待全部保存完成（异常在此重新抛出）。
            _drain_save_futures(futures)
    if not written:
        raise ValueError("selected range produced no frames")
    return SequenceArtifact(tuple(written), width, height, True, str(output))

def _format_static_sequence(progress, manifest: MediaManifest, output: Path):
    total = len(manifest.sources)

    def process(index: int) -> Image.Image | None:
        path = manifest.sources[index]
        seconds = index / DEFAULT_SEQUENCE_FPS
        if not _in_range(manifest, index, seconds):
            return None
        with Image.open(path) as image:
            return _crop_scale(image.convert("RGBA"), manifest)

    written = _parallel_pil_export(progress, total, output, "处理静态序列", process)
    if not written:
        raise ValueError("selected range produced no frames")
    # 尺寸以首帧为准（产物帧尺寸一致）。
    with Image.open(written[0]) as image:
        width, height = image.size
    return SequenceArtifact(written, width, height, True, str(output))

def _format_video(progress, manifest: MediaManifest, output: Path):
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
                    futures.append(pool.submit(_parallel_pil_save_bounded, image, target, save_sem))
                    written.append(str(target))
                if decoded % 10 == 0:
                    fraction = min(0.99, decoded / estimated) if estimated else None
                    _progress(progress, fraction, f"解码视频 {decoded} 帧")
            # 等待全部保存完成（异常在此重新抛出）。
            _drain_save_futures(futures)
    if not written:
        raise ValueError("selected range produced no frames")
    return SequenceArtifact(tuple(written), width, height, True, str(output))

def _parallel_pil_export(progress, total: int, output: Path, label: str, process: Callable[[int], Image.Image | None], *, compress_level: int=PNG_CACHE_COMPRESS_LEVEL):
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
        _progress(progress, (index + 1) / total, label)
    return tuple(written)

def _parallel_pil_save_bounded(image: Image.Image, target: Path, semaphore: threading.BoundedSemaphore):
    """有界并行保存单帧 PNG（供流式解码路径调用，worker 线程执行）。

    ``semaphore`` 限制在途未保存帧数（≈ 内存上界），保存完成后释放，
    使解码循环可以继续提交下一帧，不整段缓冲。
    """
    try:
        image.save(target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL)
    finally:
        semaphore.release()

def extract_first_frame(workspace, manifest: MediaManifest):
    """Return a representative first-frame image path for the manifest.

    Static sequences already contain images, so their first source is used
    directly; video and animated images get one frame materialized as PNG
    into this backend's workspace.
    """
    if manifest.kind is MediaKind.STATIC_SEQUENCE:
        return manifest.sources[0]
    output = _job_dir(workspace, "preview")
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

def extract_start_frame(workspace, manifest: MediaManifest):
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
            return _extract_video_frame(workspace, manifest.sources[0], seconds)
        index = int(manifest.start) if manifest.start is not None else 0
        if manifest.kind is MediaKind.STATIC_SEQUENCE:
            if not manifest.sources:
                return None
            return manifest.sources[min(index, len(manifest.sources) - 1)]
        if manifest.kind is MediaKind.ANIMATED_IMAGE:
            with Image.open(manifest.sources[0]) as image:
                image.seek(index)
                target = _job_dir(workspace, "preview") / "frame.png"
                image.convert("RGBA").save(target, "PNG")
                return str(target)
        with av.open(manifest.sources[0]) as container:
            stream = container.streams.video[0]
            fps = float(stream.average_rate or stream.base_rate or 1.0)
            return _extract_video_frame(workspace, container, index / fps)
    except Exception:
        return None

def format_manifest(workspace, imagemagick, progress, manifest: MediaManifest):
    """物化源窗口内全部帧为图片序列。

    序列产物**不携带帧率/帧速信息**（用户需求）：帧率只作为格式化解码
    内部的窗口换算依据（视频流帧率 / GIF 帧时间换算 / 静态序列默认 12），
    不写入产物；需要帧率的节点（GIF 合成、抽帧）由用户把帧速作为参数输入。
    """
    output = _job_dir(workspace, "format")
    if manifest.kind is MediaKind.STATIC_SEQUENCE:
        return _format_static_sequence(progress, manifest, output)
    if manifest.kind is MediaKind.ANIMATED_IMAGE:
        return _format_animated_image(imagemagick, progress, manifest, output)
    return _format_video(progress, manifest, output)

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

def _job_dir(workspace: Path, prefix: str) -> Path:
    """模块版（决策 #120）：等价原 ``MediaBackend._job_dir``。"""
    path = workspace / f"{prefix}_{uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

def _progress(progress: ProgressReporter | None, fraction: float | None, label: str) -> None:
    """模块版（决策 #120）：等价原 ``MediaBackend._progress``。"""
    if progress is not None:
        progress(fraction, label)
