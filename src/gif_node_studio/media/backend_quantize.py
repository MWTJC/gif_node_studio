"""MediaBackend 区段 5：颜色量化（纯函数模块，决策 #120 拆出）。

与 #82 同名 mixin 无继承关系；无实例状态，依赖显式注入。"""

from __future__ import annotations

from ..core.domain import SequenceArtifact
from .image_utils import PNG_EXPORT_WORKERS
from .image_utils import _save_wand_png
from .image_utils import _wand_quantize_all
from .imagemagick import require_wand
from .palettes import _palette_png_blob
from .palettes import _websafe_map_blob
from .palettes import system_palette_blob
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from pathlib import Path
from uuid import uuid4
ProgressReporter = Callable[[float | None, str], None]


def color_quantize_sequence(workspace, imagemagick, progress, artifact: SequenceArtifact, *, colorspace: str='srgb', colors: int=256, treedepth: int=0, dither: str='floyd_steinberg', use_ordered: bool=False, ordered_map: str='o8x8', levels: str='', posterize_levels: int=0):
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
    require_wand(imagemagick, "颜色量化")
    from wand.image import Image as WandImage

    alpha_in_quantize = colorspace == "transparent"
    gray = colorspace == "gray"
    dither_index = {"no": 1, "riemersma": 2, "floyd_steinberg": 3}.get(dither, 3)
    map_spec = f"{ordered_map},{levels}" if levels else ordered_map
    output = _job_dir(workspace, "quantize")
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
                _progress(progress, (index + 1) / total, "颜色量化")
            # 2) 共享调色板量化（整条序列一个调色板）。
            _progress(progress, None, "量化调色板")
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
            _progress(progress, 1.0, "量化调色板")
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
                        _progress(progress, (futures[future] + 1) / total, "导出序列")
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

def color_reduce_sequence(workspace, imagemagick, progress, artifact: SequenceArtifact, *, algorithm: str='adaptive', colors: int=256, dither: str='diffusion', map_name: str='o8x8', levels: str='13'):
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
    require_wand(imagemagick, "颜色深度")
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
    output = _job_dir(workspace, "dither")
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
                _progress(progress, (index + 1) / total, "颜色深度")
            # 2) 调色板策略：固定色板路径（含黑白）remap 已就位；
            #    octree 路径做共享量化。
            _progress(progress, None, "量化调色板")
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
            _progress(progress, 1.0, "量化调色板")
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
                        _progress(progress, (futures[future] + 1) / total, "导出序列")
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

def _job_dir(workspace: Path, prefix: str) -> Path:
    """模块版（决策 #120）：等价原 ``MediaBackend._job_dir``。"""
    path = workspace / f"{prefix}_{uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

def _progress(progress: ProgressReporter | None, fraction: float | None, label: str) -> None:
    """模块版（决策 #120）：等价原 ``MediaBackend._progress``。"""
    if progress is not None:
        progress(fraction, label)
