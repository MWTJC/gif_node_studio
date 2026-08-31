from __future__ import annotations

from pathlib import Path
from typing import Any

import av
from PIL import Image, ImageSequence

from ..core.domain import AnalysisResult, MediaKind, MediaManifest, MultiOutput, SequenceArtifact

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv",
    ".wmv", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def format_bytes(size: int) -> str:
    value = float(max(0, size))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def _format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds >= 3600:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        remainder = seconds % 60
        return f"{hours}:{minutes:02d}:{remainder:05.2f}"
    if seconds >= 60:
        minutes = int(seconds // 60)
        remainder = seconds % 60
        return f"{minutes}:{remainder:05.2f}"
    return f"{seconds:.2f} s"


def probe_video(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    info: dict[str, Any] = {"文件名": path.name, "文件大小": path.stat().st_size}
    try:
        with av.open(path) as container:
            duration = getattr(container, "duration", None)
            fps: float | None = None
            if container.streams.video:
                rate = container.streams.video[0].average_rate or container.streams.video[0].base_rate
                if rate is not None and float(rate) > 0:
                    fps = float(rate)
            info["总时长"] = _format_duration(duration / 1_000_000) if duration else "未知"
            info["帧率"] = f"{fps:g} fps" if fps else "未知"
    except Exception:
        pass
    return info


def _gif_parse(path: str | Path) -> dict[str, Any] | None:
    """轻量解析 GIF 文件结构，统计帧数、每帧延迟与颜色板大小（不解码像素）。

    返回 ``{"width", "height", "frame_count", "durations_ms", "loop",
    "palette_size", "regions"}``；``palette_size`` 为颜色板（颜色表）条目数：优先全局
    颜色表，无全局表时取各帧局部颜色表的最大值；``regions`` 为各帧图像描述符
    声明的存储区域 ``(left, top, width, height)``（帧优化的局部帧在此直接可见，
    不解码像素）；文件损坏/无法解析时返回
    None（调用方回退 PIL 逐帧迭代）。

    大 GIF 的 PIL 迭代需要解码全部帧（帧数越多越慢），UI 会因此卡死；
    本解析只扫描块结构（GCE 延迟、图像描述符计数、颜色表大小），耗时与
    像素无关。
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width = data[6] + data[7] * 256
    height = data[8] + data[9] * 256
    pos = 6 + 7
    packed = data[10]  # 逻辑屏幕描述符第 4 字节（偏移 10），bit7=全局颜色表标志
    global_colors: int | None = None
    global_table: list[tuple[int, int, int]] | None = None
    if packed & 0x80:  # 全局颜色表
        global_colors = 1 << ((packed & 0x07) + 1)
        # 顺带记录条目内容（≤768B）：probe_gif 据此判断「非退化全局表」
        # （≥2 种不同条目）以给出准确的色表口径（全局表 vs 每帧并集）。
        global_table = _read_table(data, pos, global_colors)
        pos += 3 * global_colors
    size = len(data)
    frame_count = 0
    local_table_frames = 0
    durations: list[int] = []
    pending_delay: int | None = None  # 最近一个 GCE 的延迟（毫秒），作用于下一个图像
    loop: int | None = None
    local_colors_max = 0
    regions: list[tuple[int, int, int, int]] = []
    while pos < size:
        block = data[pos]
        if block == 0x3B:  # trailer
            break
        if block == 0x2C:  # 图像描述符 → 一帧
            frame_count += 1
            durations.append(pending_delay if pending_delay is not None else 0)
            pending_delay = None
            if pos + 10 > size:
                return None
            # 图像描述符：left/top/width/height 各占 2 字节（小端），
            # 帧优化的局部帧在此声明 < 画布的存储区域。
            regions.append((
                data[pos + 1] + data[pos + 2] * 256,
                data[pos + 3] + data[pos + 4] * 256,
                data[pos + 5] + data[pos + 6] * 256,
                data[pos + 7] + data[pos + 8] * 256,
            ))
            image_packed = data[pos + 9]
            pos += 10
            if image_packed & 0x80:  # 局部颜色表
                local_table_frames += 1
                local_colors = 1 << ((image_packed & 0x07) + 1)
                local_colors_max = max(local_colors_max, local_colors)
                pos += 3 * local_colors
            if pos >= size:
                return None
            pos += 1  # LZW 最小码长
            while pos < size:
                sub_size = data[pos]
                pos += 1
                if sub_size == 0:
                    break
                pos += sub_size
            continue
        if block == 0x21:  # 扩展块
            pos += 1
            if pos >= size:
                return None
            label = data[pos]
            pos += 1
            if label == 0xF9:  # 图形控制扩展：size=4, packed, delay_lo, delay_hi, transparent, 0x00
                if pos + 6 > size:
                    return None
                sub_size = data[pos]
                if sub_size == 4:
                    pending_delay = (data[pos + 2] + data[pos + 3] * 256) * 10
                pos += sub_size + 2  # 数据 + 终结字节 0x00
                continue
            if label == 0xFF:  # 应用扩展（NETSCAPE2.0 循环次数）
                if pos < size:
                    sub_size = data[pos]
                    pos += 1
                    if (
                        sub_size == 11
                        and pos + sub_size <= size
                        and data[pos : pos + sub_size] == b"NETSCAPE2.0"
                    ):
                        pos += sub_size
                        if pos + 4 <= size and data[pos] == 0x03 and data[pos + 1] == 0x01:
                            loop = data[pos + 2] + data[pos + 3] * 256
                            pos += 4
                        while pos < size:
                            s = data[pos]
                            pos += 1
                            if s == 0:
                                break
                            pos += s
                        continue
                    pos -= 1  # 非 NETSCAPE：回退，按通用子块读取
            while pos < size:
                sub_size = data[pos]
                pos += 1
                if sub_size == 0:
                    break
                pos += sub_size
            continue
        return None  # 未知块：放弃解析
    if not frame_count:
        return None
    return {
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "durations_ms": durations,
        "loop": loop,
        "palette_size": global_colors if global_colors is not None else (local_colors_max or None),
        "global_table": global_table,
        "local_table_frames": local_table_frames,
        "local_colors_max": local_colors_max,
        "regions": regions,
    }


def _read_table(data: bytes, pos: int, count: int) -> list[tuple[int, int, int]]:
    """读取 GIF 颜色表：count 个 RGB 三元组。"""
    return [
        (data[pos + index * 3], data[pos + index * 3 + 1], data[pos + index * 3 + 2])
        for index in range(count)
    ]


def gif_palette_entries(path: str | Path) -> tuple[list[tuple[int, int, int]], int | None]:
    """读取 GIF 颜色板条目（与 ``probe_gif`` 的「颜色板颜色数」同源，不解码像素）。

    返回 ``(条目列表, 透明索引或 None)``。规则：全局颜色表存在且**非退化**
    （至少 2 种不同条目，PIL 等编码器写的全黑占位表视为退化）时用全局表；
    否则取各帧局部颜色表的并集（保持出现顺序）。**并集不设上限**——GIF 只
    约束单帧 ≤256 色，帧间并集可远超 256（gifski 式每帧独立色表正是如此），
    返回完整并集不抛错；展示口径（全局表 / 并集）由调用方按
    ``_gif_parse`` 的 ``global_table`` 判断。透明索引来自图形控制扩展
    （GCE）的透明标志。
    """
    data = Path(path).read_bytes()
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("不是有效的 GIF 文件")
    pos = 6 + 7
    packed = data[10]  # 逻辑屏幕描述符，bit7=全局颜色表标志
    global_table: list[tuple[int, int, int]] = []
    transparent: int | None = None
    if packed & 0x80:
        count = 1 << ((packed & 0x07) + 1)
        global_table = _read_table(data, pos, count)
        pos += 3 * count
    size = len(data)
    local_union: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    while pos < size:
        block = data[pos]
        if block == 0x3B:  # trailer
            break
        if block == 0x2C:  # 图像描述符
            if pos + 10 > size:
                break
            image_packed = data[pos + 9]
            pos += 10
            if image_packed & 0x80:  # 局部颜色表
                count = 1 << ((image_packed & 0x07) + 1)
                table = _read_table(data, pos, count)
                for entry in table:
                    # 集合去重保序（O(1) 判重）：旧实现 `entry not in local_union`
                    # 是 O(n²) 列表线性扫描——162 帧 × 256 色本地表 ≈ 4 万条，
                    # 实测 probe_gif 单次 5.1s（describe_output 与预览播放都会调）。
                    if entry not in seen:
                        seen.add(entry)
                        local_union.append(entry)
                pos += 3 * count
            if pos >= size:
                break
            pos += 1  # LZW 最小码长
            while pos < size:
                sub_size = data[pos]
                pos += 1
                if sub_size == 0:
                    break
                pos += sub_size
            continue
        if block == 0x21:  # 扩展块
            pos += 1
            if pos >= size:
                break
            label = data[pos]
            pos += 1
            if label == 0xF9:  # 图形控制扩展：size=4, packed, delay_lo, delay_hi, transparent, 0x00
                if pos + 6 > size:
                    break
                sub_size = data[pos]
                if sub_size == 4 and (data[pos + 1] & 0x01):  # 透明标志
                    transparent = data[pos + 4]
                pos += sub_size + 2
                continue
            while pos < size:
                sub_size = data[pos]
                pos += 1
                if sub_size == 0:
                    break
                pos += sub_size
            continue
        break  # 未知块：放弃
    if global_table and len(set(global_table)) >= 2:
        return global_table, transparent
    # 并集不设上限（docstring）：返回完整条目，调用方自行决定展示/截断。
    return local_union, transparent


def gif_palette_type(path: str | Path) -> str:
    """GIF 调色板类型（色表形态）：全局色表(GCT) / 每帧局部色表(LCT) 组合（决策 #126）。

    - 非退化全局色表（≥2 种不同条目，全黑占位表视为退化）视为「有全局色表」；
    - 带局部色表的帧数 > 0 视为「有每帧局部色表」。

    供 gif输入 / gif优化分析 节点元数据展示（probe_gif / analysis_gif_frames）。
    """
    parsed = _gif_parse(path)
    if parsed is None:
        return "未知"
    has_gct = bool(parsed.get("global_table") and len(set(parsed["global_table"])) >= 2)
    has_lct = bool(parsed.get("local_table_frames"))
    if has_gct and has_lct:
        return "全局色表 + 每帧局部色表"
    if has_gct:
        return "仅全局色表"
    if has_lct:
        return "仅每帧局部色表"
    return "无调色板"


def gif_frame_palettes(path: str | Path) -> tuple[list[list[tuple[int, int, int]]], list[bool]]:
    """逐帧解析 GIF 各帧的**生效色表**与透明标志（决策 #126）。

    生效规则：帧带局部色表（LCT）→ 用该帧 LCT；否则 → 非退化全局色表
    （全黑占位表视为退化）；两者皆无 → 空表。透明标志 = 该帧图形控制
    扩展（GCE）是否声明透明。

    只做结构扫描不解码像素（与 ``_gif_parse`` 同层）；供调色板查看节点的
    逐帧 slider（gifski 式每帧独立色表的文件，逐帧色表差异大）。

    返回 ``(每帧色表列表, 每帧透明标志列表)``；解析失败抛 ``ValueError``。
    """
    data = Path(path).read_bytes()
    if len(data) < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("不是有效的 GIF 文件")
    pos = 6 + 7
    packed = data[10]  # 逻辑屏幕描述符，bit7=全局颜色表标志
    global_table: list[tuple[int, int, int]] = []
    if packed & 0x80:
        count = 1 << ((packed & 0x07) + 1)
        global_table = _read_table(data, pos, count)
        pos += 3 * count
    global_table = global_table if len(set(global_table)) >= 2 else []
    size = len(data)
    palettes: list[list[tuple[int, int, int]]] = []
    transparent_flags: list[bool] = []
    pending_transparent = False  # 最近一个 GCE 的透明标志，作用于下一个图像
    while pos < size:
        block = data[pos]
        if block == 0x3B:  # trailer
            break
        if block == 0x2C:  # 图像描述符 → 一帧
            if pos + 10 > size:
                break
            image_packed = data[pos + 9]
            pos += 10
            frame_table: list[tuple[int, int, int]] = []
            if image_packed & 0x80:  # 局部颜色表
                count = 1 << ((image_packed & 0x07) + 1)
                frame_table = _read_table(data, pos, count)
                pos += 3 * count
            else:
                frame_table = list(global_table)
            palettes.append(frame_table)
            transparent_flags.append(pending_transparent)
            pending_transparent = False
            if pos >= size:
                break
            pos += 1  # LZW 最小码长
            while pos < size:
                sub_size = data[pos]
                pos += 1
                if sub_size == 0:
                    break
                pos += sub_size
            continue
        if block == 0x21:  # 扩展块
            pos += 1
            if pos >= size:
                break
            label = data[pos]
            pos += 1
            if label == 0xF9:  # 图形控制扩展：size=4, packed, delay_lo, delay_hi, transparent, 0x00
                if pos + 6 > size:
                    break
                sub_size = data[pos]
                if sub_size == 4 and (data[pos + 1] & 0x01):  # 透明标志
                    pending_transparent = True
                pos += sub_size + 2
                continue
            while pos < size:
                sub_size = data[pos]
                pos += 1
                if sub_size == 0:
                    break
                pos += sub_size
            continue
        break  # 未知块：放弃
    if not palettes:
        raise ValueError("未能解析 GIF 帧色表")
    return palettes, transparent_flags


def _format_frame_times(durations: list[int]) -> str:
    """帧时间显示：恒定时单值；不恒定时按从小到大列举（去重）。"""
    if not durations:
        return "未知"
    unique = sorted(set(durations))
    if len(unique) <= 1:
        return f"{durations[0]} ms"
    return "、".join(f"{value} ms" for value in unique)


def probe_gif(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    parsed = _gif_parse(path)
    if parsed is not None:
        durations = parsed["durations_ms"]
        frame_count = parsed["frame_count"]
        constant = len(set(durations)) <= 1
        # 颜色板口径（决策 #123）：非退化全局表 → 全局表色数；否则 → 每帧
        # 局部表并集（gifski 式每帧独立色表，并集常超 256——GIF 只约束单帧
        # ≤256 色）。与调色板查看节点同源（gif_palette_entries），保证一致。
        gct = parsed.get("global_table") or []
        scope = "全局表" if len(set(gct)) >= 2 else "每帧色表并集"
        try:
            entries, _transparent = gif_palette_entries(path)
            palette_size = len(entries)
        except Exception:
            palette_size = parsed.get("palette_size")
        return {
            "文件名": path.name,
            "文件大小": path.stat().st_size,
            "尺寸": f"{parsed['width']} × {parsed['height']}",
            "帧数": frame_count,
            "总时长": f"{sum(durations)} ms",
            "帧时间是否恒定": "是" if constant else "否",
            "帧时间": _format_frame_times(durations),
            "调色板类型": gif_palette_type(path),
            "颜色板颜色数": f"{palette_size} 色（{scope}）" if palette_size else "无颜色板",
            "循环次数": parsed["loop"] if parsed["loop"] is not None else "未指定",
        }
    with Image.open(path) as image:
        durations = [int(frame.info.get("duration", image.info.get("duration", 0)) or 0) for frame in ImageSequence.Iterator(image)]
        frame_count = len(durations)
        constant = len(set(durations)) <= 1
        palette = image.getpalette()
        palette_size = len(palette) // 3 if palette else None
        return {
            "文件名": path.name,
            "文件大小": path.stat().st_size,
            "尺寸": f"{image.width} × {image.height}",
            "帧数": frame_count,
            "总时长": f"{sum(durations)} ms",
            "帧时间是否恒定": "是" if constant else "否",
            "帧时间": _format_frame_times(durations),
            "颜色板颜色数": f"{palette_size} 色" if palette_size else "无颜色板",
            "循环次数": image.info.get("loop", "未指定"),
        }


def video_duration_seconds(path: str | Path) -> float | None:
    """返回视频总时长（秒）；无法探测时返回 None。"""
    try:
        with av.open(Path(path)) as container:
            duration = getattr(container, "duration", None)
            if duration:
                return duration / 1_000_000
    except Exception:
        return None
    return None


def gif_playback_info(path: str | Path) -> dict[str, Any] | None:
    """解析 GIF 的播放信息（帧数/尺寸/每帧延迟/循环/透明索引），供预览播放器使用。

    只扫描块结构不解码像素（与 ``_gif_parse`` 同源）；``transparent_index``
    来自各帧图形控制扩展（GCE）的透明标志（``gif_palette_entries`` 同源）；
    解析失败返回 ``None``（调用方回退 QMovie）。``loop``：0=无限循环，
    None=未声明（播放一次），正数=循环次数。
    """
    parsed = _gif_parse(path)
    if parsed is None:
        return None
    transparent: int | None = None
    try:
        _entries, transparent = gif_palette_entries(path)
    except Exception:
        pass
    return {
        "frame_count": parsed["frame_count"],
        "width": parsed["width"],
        "height": parsed["height"],
        "durations_ms": parsed["durations_ms"],
        "loop": parsed["loop"],
        "transparent_index": transparent,
        "regions": parsed["regions"],
    }


def frame_optimization_ratio(
    durations_ms: list[int], frame_count: int
) -> tuple[float, float, int] | None:
    """帧优化占比（以最短帧时间为基准）：相对等时长全最短帧序列节省的帧数比例。

    等时长的「全是最短帧」序列帧数 = ``总时长 ÷ 最短帧时间``；优化后的 GIF
    通过把静止内容用更长帧时间（定格/保持）表达来减少帧数，因此
    ``节省比例 = 1 − 实际帧数 ÷ 基准帧数``。返回 ``(节省比例 0..1, 基准帧数,
    实际帧数)``；最短帧时间 ≤ 0（帧时间缺失或含 0，无法确定基准）或无帧时
    返回 ``None``。零延迟帧只可能使基准帧数 < 实际帧数（比例钳制为 0）。
    """
    if frame_count <= 0:
        return None
    shortest = min((value for value in durations_ms if value > 0), default=None)
    if shortest is None:
        return None
    total = sum(durations_ms)
    baseline = total / shortest  # 等时长全最短帧序列的帧数
    ratio = (baseline - frame_count) / baseline if baseline > 0 else 0.0
    return max(0.0, ratio), baseline, frame_count


def gif_native_fps(path: str | Path) -> float:
    """GIF 源原生帧率：帧时间（毫秒）中位数换算为 fps（1000 / 中位数 ms）。

    优先走 ``_gif_parse`` 结构解析（不解码像素）；失败回退 PIL 逐帧迭代；
    均不可得时返回 12.0（与静态序列默认一致）。
    """
    durations: list[int] = []
    parsed = _gif_parse(path)
    if parsed is not None:
        durations = [value for value in parsed["durations_ms"] if value > 0]
    if not durations:
        try:
            with Image.open(Path(path)) as image:
                durations = [
                    int(frame.info.get("duration", 0) or 0)
                    for frame in ImageSequence.Iterator(image)
                ]
            durations = [value for value in durations if value > 0]
        except Exception:
            durations = []
    if not durations:
        return 12.0  # 与静态序列默认帧率一致（backend.DEFAULT_SEQUENCE_FPS）
    median = sorted(durations)[len(durations) // 2]
    return 1000.0 / median


def source_frame_count(manifest: MediaManifest) -> int | None:
    """返回源媒体的总帧数：序列=帧文件数，GIF=帧数，视频=流帧数或 时长×帧率；未知返回 None。"""
    if manifest.kind is MediaKind.STATIC_SEQUENCE:
        return len(manifest.sources)
    first = Path(manifest.sources[0])
    try:
        if manifest.kind is MediaKind.ANIMATED_IMAGE:
            parsed = _gif_parse(first)
            if parsed is not None:
                return parsed["frame_count"]
            with Image.open(first) as image:
                return sum(1 for _ in ImageSequence.Iterator(image))
        with av.open(str(first)) as container:
            stream = container.streams.video[0]
            if getattr(stream, "frames", None):
                return int(stream.frames)
            duration = getattr(container, "duration", None)
            rate = stream.average_rate or stream.base_rate
            if duration and rate and float(rate) > 0:
                return max(1, round(duration / 1_000_000 * float(rate)))
    except Exception:
        return None
    return None


def default_describe_output(output: Any) -> dict[str, Any]:
    """节点输出元数据**默认行为**：按输出值类型给出通用摘要（与产出节点无关）。

    具体节点无特殊需求时无需覆写；需要自定义展示的节点覆写基类
    ``StudioNode.describe_output``（本函数即基类默认实现，见
    ``nodes/node_base.py``）。MultiOutput 按端口名逐通道摘要；导出类节点
    （GIF 合成/优化、WebP/APNG/ico 导出）覆写后仅显示 execute 附的摘要，
    见 ``nodes/export_nodes.py``。
    """
    if isinstance(output, MultiOutput):
        # 多输出节点（RGBA 通道分离 / gif 合成 / ico 合成）：按端口名逐通道给出摘要。
        # 清单端口（gif 合成/ico 合成的「格式化清单」）合并首个源文件信息。
        info: dict[str, Any] = {}
        for name, value in output.ports.items():
            if isinstance(value, SequenceArtifact):
                info[f"{name} 帧数"] = len(value.frames)
                info[f"{name} 尺寸"] = f"{value.width} × {value.height}"
            elif isinstance(value, MediaManifest):
                first = Path(value.sources[0]) if value.sources else None
                if first is not None and first.is_file():
                    if first.suffix.lower() == ".gif":
                        for key, item in probe_gif(first).items():
                            info[f"{name} {key}"] = item
                    elif first.suffix.lower() in IMAGE_EXTENSIONS:
                        info[f"{name} 源文件数"] = len(value.sources)
                        try:
                            with Image.open(first) as image:
                                info[f"{name} 首帧尺寸"] = f"{image.width} × {image.height}"
                        except Exception:
                            pass
                    else:
                        info[f"{name} 文件"] = first.name
        info.update(output.metadata or {})
        return info
    if isinstance(output, AnalysisResult):
        # 分析类节点：合并预览文件信息与附加元数据（颜色数/分辨率等）。
        info: dict[str, Any] = {}
        path = Path(output.path)
        if path.is_file():
            info.update(default_describe_output(path))
        info.update(output.metadata or {})
        return info
    if isinstance(output, MediaManifest):
        info: dict[str, Any] = {
            "媒体类型": output.kind.value,
            "源文件数": len(output.sources),
        }
        first = Path(output.sources[0])
        if first.suffix.lower() == ".gif" and first.is_file():
            info.update(probe_gif(first))
        elif first.suffix.lower() in VIDEO_EXTENSIONS and first.is_file():
            info.update(probe_video(first))
        elif first.is_file():
            info.update({"文件名": first.name, "文件大小": first.stat().st_size})
        return info
    if isinstance(output, SequenceArtifact):
        # 序列图片元数据不携带帧率/帧速信息（用户需求）：
        # 需要帧率工作的节点（GIF 合成、抽帧）由用户把帧速作为参数输入。
        return {
            "帧数": len(output.frames),
            "尺寸": f"{output.width} × {output.height}",
            "包含透明通道": "是" if output.has_alpha else "否",
        }
    if isinstance(output, Path) and output.is_file():
        if output.suffix.lower() == ".gif":
            return probe_gif(output)
        if output.suffix.lower() in IMAGE_EXTENSIONS:
            info = {"文件名": output.name, "文件大小": output.stat().st_size}
            try:
                with Image.open(output) as image:
                    info["尺寸"] = f"{image.width} × {image.height}"
            except Exception:
                pass
            return info
        return {"文件名": output.name, "文件大小": output.stat().st_size}
    if isinstance(output, (tuple, list)) and output:
        files = [Path(item) for item in output if isinstance(item, (str, Path))]
        return {"输出文件数": len(files), "输出总大小": sum(path.stat().st_size for path in files if path.is_file())}
    return {}


def describe_output(output: Any, node: Any = None) -> dict[str, Any]:
    """节点输出元数据统一出口：优先委托给**产出节点自身**定义的展示。

    ``node`` 为产出节点的类或实例（可为 None）：有节点时调用
    ``node.describe_output(output)``——不同节点的元数据显示定义由节点类
    通过继承实现（无特殊需求时基类 ``StudioNode.describe_output`` 回落
    ``default_describe_output`` 默认行为，见 ``nodes/node_base.py``）；
    无节点（单操作运行等）时直接走默认行为。

    工作线程只持有节点类（``step[1]``），UI 兜底分支持有节点实例，
    二者均可经类方法分派，故 ``describe_output`` 为类方法。
    """
    if node is not None:
        return node.describe_output(output)
    return default_describe_output(output)


def display_metadata(metadata: dict[str, Any]) -> str:
    lines = []
    for label, value in metadata.items():
        if label in {"文件大小", "输出总大小"} and isinstance(value, int):
            value = format_bytes(value)
        lines.append(f"{label}：{value}")
    return "\n".join(lines) if lines else "运行节点后将在此显示输出元数据。"
