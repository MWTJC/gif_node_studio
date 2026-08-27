"""FFmpeg GIF 管线（PyAV 进程内 palettegen/paletteuse → gif 编码）。

与 gifsicle（CLI 子进程，media/gifsicle.py）不同，本模块完全在进程内运行：
PyAV 已是项目依赖（av>=18.0.0），其 ``av.filter.Graph`` 暴露全部 FFmpeg
滤镜——业界事实标准的 GIF 管线 palettegen（两遍法调色板生成）+ paletteuse
（调色板映射，含仿色/帧优化）因此**零新依赖**可用（见 [gif 生态调研存档]
../../docs/research/gif-ecosystem-evaluation.md 与关键决策 #100）。

可行性验证与全部 API 陷阱见技能 node-based-media-processing-apps →
references/pyav-inprocess-filter-graphs.md（2026-08 实测）：split 动态输出口
用双 buffer 源替代、必须显式连接 buffersink、paletteuse 输入口顺序、
EOF 用 EOFError、gif 编码必须给帧设 pts（否则帧延迟错乱）。

本模块职责：可用性守卫（进程内一次探测）+ 参数构造 + 编码函数（纯函数，
不依赖 MediaBackend 实例，便于独立测试）。
"""

from __future__ import annotations

import os
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av
import numpy as np
from av.filter import Graph
from PIL import Image

# paletteuse 仿色方法（机器键 = ffmpeg 滤镜参数值，直接拼进滤镜参数串）。
# floyd_steinberg 与项目「颜色量化」默认一致；sierra2_4a 为 ffmpeg 默认；
# ⚠️ FS 系误差扩散对相同帧输出不同图案（决策 #96 实测「变化敏感」）——
# 录屏冻结工作流配 none 或确定性有序仿色 bayer（bayer_scale 调粒度）。
DITHER_KEYS = frozenset({"none", "floyd_steinberg", "atkinson", "sierra2_4a", "bayer", "heckbert"})

ProgressReporter = Callable[[float | None, str], None]

_runtime_cache: tuple[bool, str | None] | None = None


def require_ffmpeg_gif() -> None:
    """可用性守卫：PyAV wheel 是否含 palettegen/paletteuse 滤镜与 gif 编码器。

    进程内只探测一次（与 configure_imagemagick/configure_gifsicle 同模式）；
    不可用时抛清晰中文错误，说明缺的是滤镜还是编码器。
    """
    global _runtime_cache
    if _runtime_cache is not None:
        ok, missing = _runtime_cache
        if not ok:
            raise RuntimeError(f"GIF 合成(FFmpeg)不可用：{missing}")
        return
    filters = set(av.filter.filters_available)
    missing: list[str] = []
    if "palettegen" not in filters:
        missing.append("palettegen 滤镜")
    if "paletteuse" not in filters:
        missing.append("paletteuse 滤镜")
    if "gif" not in av.codec.codecs_available:
        missing.append("gif 编码器")
    if missing:
        _runtime_cache = (False, "当前 PyAV 构建缺少 " + "、".join(missing))
        raise RuntimeError(f"GIF 合成(FFmpeg)不可用：{_runtime_cache[1]}")
    _runtime_cache = (True, None)


def encode_gif_frames(
    frame_paths: list[str] | tuple[str, ...],
    path: str | Path,
    *,
    fps: float,
    width_percent: int = 100,
    max_colors: int = 256,
    stats_mode: str = "full",
    dither: str = "floyd_steinberg",
    bayer_scale: int = 5,
    diff_mode: bool = True,
    progress: ProgressReporter | None = None,
) -> Path:
    """把 RGBA PNG 帧序列编码为 GIF（FFmpeg palettegen/paletteuse 管线）。

    流程：逐帧载入（含尺寸 % 缩放）→ palettegen 生成**整段序列共享调色板**
    （stats_mode=full 与 wand MagickQuantizeImages 共享调色板哲学一致）→
    paletteuse 按调色板映射（含仿色）→ gif 编码器。``diff_mode=rectangle``
    让编码器在**编码时直接产出局部帧**（只存最小变化矩形 + 透明索引，
    gifsicle -O2 级帧优化，无需后处理）。

    与 ``MediaBackend.export_gif``（wand 原样合成）平行：wand 管共享调色板
    精确控制，FFmpeg 管线管「调色板 + 编码时帧优化」一体。

    参数（机器键，见 options.FFMPEG_STATS_MODE/FFMPEG_DITHER）：
    ``stats_mode`` = full/diff/single（palettegen stats_mode）；``dither`` =
    none/floyd_steinberg/atkinson/sierra2_4a/bayer/heckbert（paletteuse dither，
    bayer 时 ``bayer_scale`` 生效）；``diff_mode`` True → diff_mode=rectangle。
    ``max_colors`` = palettegen max_colors（2–256）。

    输出先写临时文件再原子替换到 ``path``（失败不破坏旧缓存）。
    """
    require_ffmpeg_gif()
    if not fps or fps <= 0:
        raise ValueError("导出 GIF 需要帧速参数（请设置「GIF 合成(FFmpeg)」节点的「帧速」）")
    if not 2 <= max_colors <= 256:
        raise ValueError(f"颜色数必须在 2–256 之间（当前 {max_colors}）")
    if stats_mode not in ("full", "diff", "single"):
        raise ValueError(f"未知 stats_mode 键：{stats_mode!r}（可选：full/diff/single）")
    if dither not in DITHER_KEYS:
        raise ValueError(f"未知仿色键：{dither!r}（可选：{sorted(DITHER_KEYS)}）")
    if not 0 <= bayer_scale <= 5:
        raise ValueError(f"bayer_scale 必须在 0–5 之间（当前 {bayer_scale}）")

    # fps 可能是小数（FloatParam 0.1–100）：用微秒精度 Rational 表达
    # time_base = 1/fps，避免 Fraction(1, float) 的 TypeError。
    rate = Fraction(int(round(fps * 1_000_000)), 1_000_000)
    if rate.numerator <= 0:
        raise ValueError("导出 GIF 需要帧速参数（请设置「GIF 合成(FFmpeg)」节点的「帧速」）")
    # add_buffer 的 time_base 是「每 tick 秒数」= 1/fps（不是 rate 本身）；
    # stream rate 传 rate。两者一致才能让 gif muxer 写出正确帧延迟。
    tick = Fraction(1) / rate

    frames = [Path(p) for p in frame_paths]
    if not frames:
        raise ValueError("GIF 合成(FFmpeg)：输入序列为空")

    # 逐帧载入并统一尺寸（以第一帧为准；width_percent 缩放用 PIL Lanczos，
    # 与项目其它缩放路径同语义）。
    target_w = target_h = 0
    arrays: list[np.ndarray] = []
    for index, source in enumerate(frames):
        with Image.open(source) as image:
            rgba = image.convert("RGBA")
            if index == 0:
                target_w = max(1, round(rgba.width * width_percent / 100))
                target_h = max(1, round(rgba.height * width_percent / 100))
            if (rgba.width, rgba.height) != (target_w, target_h):
                rgba = rgba.resize((target_w, target_h), Image.Resampling.LANCZOS)
            arrays.append(np.asarray(rgba))
        if progress:
            progress((index + 1) / len(frames) * 0.4, "FFmpeg 载入帧")

    # 双 buffer 源（主视频 + palettegen）语义等价 ffmpeg 的 split：paletteuse
    # 输入口顺序 input0=主视频、input1=调色板（参考文档实测陷阱 #2/#4）。
    graph = Graph()
    buf_main = graph.add_buffer(width=target_w, height=target_h, format="rgba", time_base=tick)
    buf_pg = graph.add_buffer(width=target_w, height=target_h, format="rgba", time_base=tick)
    palettegen_args = f"max_colors={max_colors}:stats_mode={stats_mode}:reserve_transparent=1"
    pgen = graph.add("palettegen", palettegen_args)
    paletteuse_args = f"dither={dither}"
    if dither == "bayer":
        paletteuse_args += f":bayer_scale={bayer_scale}"
    if diff_mode:
        paletteuse_args += ":diff_mode=rectangle"
    puse = graph.add("paletteuse", paletteuse_args)
    sink = graph.add("buffersink")
    buf_main.link_to(puse, 0, 0)
    buf_pg.link_to(pgen, 0, 0)
    pgen.link_to(puse, 0, 1)
    puse.link_to(sink, 0, 0)
    graph.configure()

    # 推帧（帧必须带显式 pts，否则 gif 编码器写出错误帧延迟——参考文档实测陷阱）。
    total = len(arrays)
    for index, array in enumerate(arrays):
        frame = av.VideoFrame.from_ndarray(array, format="rgba")
        frame.pts = index
        graph.vpush(frame, at=0)
        graph.vpush(frame, at=1)
        if progress:
            progress(0.4 + (index + 1) / total * 0.2, "FFmpeg 调色板生成")
    graph.vpush(None)

    out_frames: list[av.VideoFrame] = []
    while True:
        try:
            out_frames.append(graph.vpull())
        except av.error.EOFError:
            break
        except av.error.BlockingIOError:
            continue
    if progress:
        progress(0.7, "FFmpeg 调色板映射")

    # gif 编码（paletteuse 输出已是带调色板的 pal8 帧，直接喂编码器）。
    # 输出先写临时文件（av.open 显式 format="gif"，不依赖扩展名猜测）再原子替换。
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    container = av.open(str(temp), "w", format="gif")
    stream = container.add_stream("gif", rate=rate)
    stream.width, stream.height = target_w, target_h
    stream.pix_fmt = "pal8"
    try:
        count = len(out_frames)
        for index, frame in enumerate(out_frames):
            frame = frame.reformat(width=target_w, height=target_h, format="pal8")
            for packet in stream.encode(frame):
                container.mux(packet)
            if progress:
                progress(0.7 + (index + 1) / count * 0.3, "GIF 编码")
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    try:
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise
    if progress:
        progress(1.0, "GIF 编码完成")
    return path
