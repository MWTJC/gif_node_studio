"""MediaBackend 区段 6：分析（纯函数模块，决策 #120 拆出）。

与 #82 同名 mixin 无继承关系；无实例状态，依赖显式注入。"""

from __future__ import annotations

from ..core.domain import MediaKind
from ..core.domain import MediaManifest
from ..core.domain import SequenceArtifact
from .image_utils import PNG_CACHE_COMPRESS_LEVEL
from .image_utils import _wand_rgba_bytes
from .imagemagick import require_wand
from .media_info import _format_frame_times
from .media_info import frame_optimization_ratio
from .media_info import gif_palette_entries
from .media_info import gif_playback_info
from PIL import Image
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4
import numpy as np
import shutil
ProgressReporter = Callable[[float | None, str], None]
from .backend_format import extract_first_frame


def analysis_first_frame(workspace, manifest: MediaManifest | None=None, sequence: SequenceArtifact | None=None):
    """分析节点用：取一张代表性图片（1:1 原始分辨率）的路径。

    序列优先取第 0 帧；清单优先用其预览图（输入节点已物化的首帧），
    无预览时回退 extract_first_frame。
    """
    if sequence is not None and sequence.frames:
        return sequence.frames[0]
    if manifest is not None:
        if manifest.preview and Path(manifest.preview).is_file():
            return manifest.preview
        return extract_first_frame(workspace, manifest)
    raise ValueError("节点无输入")

def analysis_gif_frames(workspace, imagemagick, progress, manifest: MediaManifest | None=None, mode: str='stored'):
    """GIF 优化分析：特殊解码——按文件实际存储结构逐帧解出 PNG（1:1 供预览滑条）。

    先把输入清单指向的源 GIF **复制进本节点工作区**（分析自有缓存副本）再
    解码：解码全程只打开本节点自己的文件，不持有上游节点的产物；结果
    （副本 + 解出的帧）完全自包含——上游节点删除/重跑不影响本节点预览，
    也不因本节点读取而占用上游文件（Windows 句柄）。

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
    占比）与**时长诊断**（总时长/全局帧率/帧时间汇总——帧延迟非恒定时
    直接暴露「合成帧速 ≠ 参数帧速」的漂移，如 30fps 参数但帧时间 30/40ms
    交替 → 实际 30fps；统一 30ms → 实际 33.3fps）——不含逐帧明细
    （按需求收敛为关键统计，逐帧信息由滑条逐帧查看承担）。
    """
    if manifest is None or not manifest.sources:
        raise ValueError("gif优化分析：节点无输入")
    source = Path(manifest.sources[0])
    if not source.is_file() or source.suffix.lower() != ".gif":
        raise ValueError(f"gif优化分析：输入清单必须指向 GIF 文件（{source}）")
    info = gif_playback_info(str(source))
    if info is None:
        raise ValueError(f"gif优化分析：无法解析 GIF 文件结构：{source}")
    canvas_w, canvas_h = info["width"], info["height"]
    frame_count = info["frame_count"]
    loop = info["loop"]
    durations = info.get("durations_ms") or []
    output = _job_dir(workspace, "gif_analysis")
    # 分析自有缓存副本：先把源 GIF 复制进本节点工作区再解码（docstring）。
    path = output / "source.gif"
    shutil.copy2(source, path)
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
        require_wand(imagemagick, "gif优化分析")
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
                _progress(progress, (index + 1) / frame_count, "gif优化分析")
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
                    _progress(progress, (index + 1) / count, "gif优化分析")
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
        "总时长": f"{sum(durations)} ms" if durations else "未知",
        "全局帧率": _global_fps_label(durations),
        "帧时间": _format_frame_times(durations),
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

def analysis_ico_montage(workspace, manifest: MediaManifest | None=None):
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
    output = _job_dir(workspace, "ico_analysis")
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

def analysis_palette(progress, manifest: MediaManifest | None=None, sequence: SequenceArtifact | None=None):
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
            _progress(progress, (index + 1) / total, "统计调色板")
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
            _progress(progress, (index + 1) / total, "统计调色板")
    else:
        raise ValueError("节点无输入")
    return sorted(colors), has_transparency

def palette_swatch(workspace, colors: list[tuple[int, int, int]], has_transparency: bool):
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
    output = _job_dir(workspace, "palette")
    target = output / "palette.png"
    image.save(target, "PNG")
    return str(target)

def _global_fps_label(durations_ms: list[int]) -> str:
    """全局帧率展示文本：平均帧时间（>0）换算的播放帧率。

    帧优化/漂移补偿 GIF 的帧时间不恒定（如 30fps 合成时 30/40ms 交替），
    用**均值**才反映真实播放速率（1000 ÷ 平均 ms）——中位数会把「多数
    短帧 + 少数长帧」误算成偏快的帧率。无法计算时显示原因。
    """
    positive = [value for value in durations_ms if value > 0]
    if not positive:
        return "—（帧时间缺失或含 0）"
    fps = 1000.0 / (sum(positive) / len(positive))
    return f"{fps:.3f}".rstrip("0").rstrip(".") + " fps"

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

def _job_dir(workspace: Path, prefix: str) -> Path:
    """模块版（决策 #120）：等价原 ``MediaBackend._job_dir``。"""
    path = workspace / f"{prefix}_{uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

def _progress(progress: ProgressReporter | None, fraction: float | None, label: str) -> None:
    """模块版（决策 #120）：等价原 ``MediaBackend._progress``。"""
    if progress is not None:
        progress(fraction, label)
