"""MediaBackend 区段 4：ico/GIF/PNG/WebP/APNG 导出（纯函数模块，决策 #120 拆出）。

与 #82 同名 mixin 无继承关系；无实例状态，依赖显式注入。"""

from __future__ import annotations

from ..core.domain import MediaKind
from ..core.domain import MediaManifest
from ..core.domain import SequenceArtifact
from .ffmpeg_gif import encode_gif_frames
from .gifsicle import CREATE_NO_WINDOW_FLAG
from .gifsicle import GIFSICLE_TIMEOUT_S
from .gifsicle import build_gifsicle_args
from .gifsicle import configure_gifsicle
from .gifsicle import require_gifsicle
from .image_utils import ICON_SIZES
from .image_utils import PNG_CACHE_COMPRESS_LEVEL
from .image_utils import _bmp_dib_bytes
from .image_utils import _wand_quantize_all
from .imagemagick import require_wand
from PIL import Image
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4
import io
import os
import shutil
import struct
import subprocess
ProgressReporter = Callable[[float | None, str], None]
from .backend_cache import _remove_path
from .backend_sequence import _scale_to_canvas


def _assemble_gif(imagemagick, progress, artifact: SequenceArtifact, path: str | Path, *, delay: int, target_width: int, target_height: int, colors: int, dither_index: int, loop: int, fps: float | None=None):
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

    ``fps`` 传入时帧延迟按**累积舍入**（与 FFmpeg gif muxer 一致，见
    ``ffmpeg_gif.encode_gif_frames`` 实测）：``delay_i = round((i+1)*100/fps)
    - round(i*100/fps)``，把每帧小数延迟的余量滚动累积，总时长精确等于
    帧数 × 100/fps；统一 ``round(100/fps)`` 会让总时长漂移（如 30fps 每帧
    3.33cs → 统一 3cs 短 11%），与「GIF 合成(FFmpeg)」节点产出的时长不一致。
    未传 ``fps``（旧调用方）时回落统一 ``delay``。
    """
    require_wand(imagemagick, "GIF 导出")
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
            _progress(progress, (index + 1) / total, "载入帧")

        # 说明：进度不再依赖 MagickSetImageProgressMonitor（与 wand 组合存在
        # 随机性原生崩溃，见 _format_animated_image 注释）；各阶段用显式
        # 进度上报，长操作（量化）期间为不确定进度。
        skip_quantize = not resized and _sequence_within_color_budget(artifact.frames, 256)
        if not skip_quantize:
            _progress(progress, None, "量化调色板")
            _wand_quantize_all(gif.wand, max(2, min(256, colors)), dither_index)
            _progress(progress, 1.0, "量化调色板")

        if fps and fps > 0:
            # 帧延迟按累积舍入（与 FFmpeg gif muxer 一致，见函数 docstring）：
            # 每帧把小数余量滚动累积，总时长精确等于 帧数×100/fps。
            # 用 away-from-zero 取整（int(x+0.5)，正数域）对齐 av_rescale_q 的
            # AV_ROUND_NEAR_INF（Python round 为银行家舍入，半值处会差 1cs）。
            cumulative = 0
            for index, frame in enumerate(gif.sequence):
                target = int((index + 1) * 100.0 / fps + 0.5)
                frame.delay = max(1, target - cumulative)
                cumulative = target
        else:
            for frame in gif.sequence:
                frame.delay = delay
        gif.loop = loop

        gif.save(filename=str(path))
        _progress(progress, 1.0, "导出完成")
    return path

def _export_animated_pillow(progress, artifact: SequenceArtifact, path: str | Path, *, fmt: str, fps: float | None, width_percent: int, save_kwargs: dict):
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
        _progress(progress, (index + 1) / total, "导出动画")
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
    _progress(progress, 1.0, "导出完成")
    return path

def export_apng(progress, artifact: SequenceArtifact, path: str | Path, *, fps: float | None=None, width_percent: int=100):
    """APNG 动画导出（Pillow 内建，零新依赖，见[关键决策 #101]）。

    APNG 为无损格式（PNG 帧 + acTL 动画块），alpha 全保留。
    """
    return _export_animated_pillow(progress, 
        artifact, path, fmt="PNG", fps=fps, width_percent=width_percent,
        save_kwargs={},
    )

def export_gif(imagemagick, progress, artifact: SequenceArtifact, path: str | Path, fps: float | None=None, colors: int=256, dither: str='FloydSteinberg', loop: int=0, width_percent: int=100):
    """GIF 导出（Wand，无 CLI 子进程）。

    流程：逐帧载入（含缩放）→ MagickQuantizeImages 共享调色板量化
    → 重设帧延迟 → loop → 保存。

    按输入**原样合成**（见[关键决策 #77]）：不做帧优化/透明优化，
    帧间未变化区域全幅存储（旧版 optimize_layers/optimize_transparency
    参数及其内容包围盒裁剪已移除，优化交由后续「GIF 优化」节点/
    gifsicle 承担）。与旧 CLI 命令 `-delay -dispose frames -resize
    -dither -colors -loop` 行为对齐。
    """
    require_wand(imagemagick, "GIF 导出")
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
    return _assemble_gif(imagemagick, progress, 
        artifact, path, delay=delay,
        target_width=target_width, target_height=target_height,
        colors=colors, dither_index=dither_method,
        loop=loop, fps=fps,
    )

def export_gif_ffmpeg(progress, artifact: SequenceArtifact, path: str | Path, *, fps: float | None=None, width_percent: int=100, max_colors: int=256, stats_mode: str='full', dither: str='floyd_steinberg', bayer_scale: int=5, diff_mode: bool=True):
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
        progress=progress,
    )

def export_pngs(progress, artifact: SequenceArtifact, directory: str | Path, prefix: str='frame_'):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    total = len(artifact.frames)
    for index, source in enumerate(artifact.frames):
        target = directory / f"{prefix}{index:06d}.png"
        shutil.copy2(source, target)
        written.append(target)
        _progress(progress, (index + 1) / total, "导出 PNG")
    return tuple(written)

def export_webp(progress, artifact: SequenceArtifact, path: str | Path, *, fps: float | None=None, quality: int=80, lossless: bool=False, width_percent: int=100):
    """WebP 动画导出（Pillow 内建，零新依赖，见[关键决策 #101]）。

    RGBA 逐帧保留 alpha（WebP 动画支持全透明通道）；``lossless`` 勾选时
    用无损编码（体积更大），否则 ``quality`` 0–100 有损编码。

    ⚠️ 透明序列强制无损：实测 Pillow 的 WebP 动画**有损**路径丢失 alpha
    （透明区域写为不透明黑；单帧/无损均正常，Pillow #8101 同类缺陷）。
    含透明通道的序列自动 ``lossless=True``（调用方在元数据中如实报告）。
    """
    if artifact.has_alpha:
        lossless = True
    return _export_animated_pillow(progress, 
        artifact, path, fmt="WEBP", fps=fps, width_percent=width_percent,
        save_kwargs={"quality": quality, "lossless": lossless},
    )

def icon_compose(workspace, progress, inputs: list[SequenceArtifact], auto_grade: bool=True):
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
    output = _job_dir(workspace, "ico")
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
            _progress(progress, (index + 1) / len(sizes), "ico 合成")
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

def optimize_gif(progress, manifest: MediaManifest, path: str | Path, *, optimize: str='o3', lossy: int=0, recolor: bool=False, colors: int=128, color_method: str='diversity', dither: str='floyd-steinberg', colormap: str='none', colormap_file: str | None=None, careful: bool=False):
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
    _progress(progress, None, "gifsicle 优化中")
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
    _progress(progress, 1.0, "优化完成")
    return MediaManifest(MediaKind.ANIMATED_IMAGE, (str(path),)), before, after

def write_ico(artifact: SequenceArtifact, path: str | Path):
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

def _job_dir(workspace: Path, prefix: str) -> Path:
    """模块版（决策 #120）：等价原 ``MediaBackend._job_dir``。"""
    path = workspace / f"{prefix}_{uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

def _progress(progress: ProgressReporter | None, fraction: float | None, label: str) -> None:
    """模块版（决策 #120）：等价原 ``MediaBackend._progress``。"""
    if progress is not None:
        progress(fraction, label)

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
