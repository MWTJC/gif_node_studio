"""MediaBackend 区段 3：序列结构处理（纯函数模块，决策 #120 拆出）。

与 #82 同名 mixin 无继承关系；无实例状态，依赖显式注入。"""

from __future__ import annotations

from ..core.domain import CropSpec
from ..core.domain import SequenceArtifact
from ..core.options import PAN_DIRECTION
from ..core.options import PAN_INTERPOLATION
from ..core.options import RESAMPLE
from ..core.options import SCALE_STRATEGY
from ..core.options import SPEED_CURVE
from .image_utils import PNG_CACHE_COMPRESS_LEVEL
from .image_utils import PNG_EXPORT_WORKERS
from .image_utils import _sample_source_indices
from PIL import Image
from PIL import ImageColor
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
import math
import numpy as np
import shutil
import threading
ProgressReporter = Callable[[float | None, str], None]
from .backend_format import _drain_save_futures
from .backend_format import _parallel_pil_export
from .backend_format import _parallel_pil_save_bounded


def align_length(workspace, progress, a: SequenceArtifact, b: SequenceArtifact, method: str='loop'):
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
    output = _job_dir(workspace, "len_align")
    paths: list[str] = []
    for index, source in enumerate(selected):
        target_path = output / f"frame_{index:06d}.png"
        shutil.copy2(source, target_path)
        paths.append(str(target_path))
        _progress(progress, (index + 1) / target, "序列长度统一")
    return SequenceArtifact(tuple(paths), b.width, b.height, b.has_alpha, str(output))

def align_resolution(workspace, progress, a: SequenceArtifact, b: SequenceArtifact, *, resample: str='lanczos', strategy: str='fit'):
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
    output = _job_dir(workspace, "res_align")
    target_w, target_h = a.width, a.height
    resampler = RESAMPLE.value_for_key(resample)

    def process(index: int) -> Image.Image:
        with Image.open(b.frames[index]) as image:
            rgba = image.convert("RGBA")
        return _scale_to_canvas(rgba, target_w, target_h, strategy, resampler)

    # 逐帧缩放互不依赖：并行处理+保存（实测 24 帧 1080p：0.88s → 0.38s，2.3×，
    # 日志真实工作流 3.7–4.0s 场景同构）。
    paths = _parallel_pil_export(progress, len(b.frames), output, "分辨率统一", process)
    return SequenceArtifact(paths, target_w, target_h, True, str(output))

def blank_sequence(workspace, progress, width: int, height: int, frames: int, color: str):
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
    output = _job_dir(workspace, "blank")

    def process(index: int) -> Image.Image:
        return Image.new("RGB", (width, height), color)

    paths = _parallel_pil_export(progress, frames, output, "生成空白序列", process)
    return SequenceArtifact(paths, width, height, False, str(output))

def concat_sequences(workspace, progress, a: SequenceArtifact, b: SequenceArtifact, *, resample: str='lanczos', strategy: str='fit'):
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
    output = _job_dir(workspace, "concat")
    target_w, target_h = a.width, a.height
    paths: list[str] = []
    # 1) A 序列帧原样复制（分辨率基准，像素不变）。
    for index, source in enumerate(a.frames):
        target = output / f"frame_{index:06d}.png"
        shutil.copy2(source, target)
        paths.append(str(target))
    # 2) B 序列帧追加。分辨率与 A 相同时直接复制（缩放 = 恒等变换，各策略
    #    输出像素一致），跳过打开/转换/缩放/合成；否则按策略并行缩放。
    same_size = b.width == target_w and b.height == target_h
    if same_size:
        for index, source in enumerate(b.frames):
            target = output / f"frame_{len(a.frames) + index:06d}.png"
            shutil.copy2(source, target)
            paths.append(str(target))
            _progress(progress, (index + 1) / len(b.frames), "序列相加")
    else:
        resampler = RESAMPLE.value_for_key(resample)

        def job(index: int) -> str:
            with Image.open(b.frames[index]) as image:
                rgba = image.convert("RGBA")
            scaled = _scale_to_canvas(rgba, target_w, target_h, strategy, resampler)
            target = output / f"frame_{len(a.frames) + index:06d}.png"
            scaled.save(target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL)
            return str(target)

        # 逐帧缩放互不依赖：并行处理+保存（pool.map 保持帧序）。
        with ThreadPoolExecutor(max_workers=PNG_EXPORT_WORKERS) as pool:
            for index, target in enumerate(pool.map(job, range(len(b.frames)))):
                paths.append(target)
                _progress(progress, (index + 1) / len(b.frames), "序列相加")
    return SequenceArtifact(tuple(paths), target_w, target_h, True, str(output))

def crop_sequence(workspace, progress, artifact: SequenceArtifact, crop: CropSpec):
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
    output = _job_dir(workspace, "crop")

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            return image.convert("RGBA").crop(box)

    paths = _parallel_pil_export(progress, len(artifact.frames), output, "画面裁剪", process)
    return SequenceArtifact(
        paths,
        max(1, right - left),
        max(1, bottom - top),
        artifact.has_alpha,
        str(output),
    )

def freeze_sequence(workspace, progress, artifact: SequenceArtifact, *, end: str='first', count: int=1):
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
    output = _job_dir(workspace, "freeze")
    paths: list[str] = []
    total = len(order)
    for index, source in enumerate(order):
        target = output / f"frame_{index:06d}.png"
        shutil.copy2(source, target)
        paths.append(str(target))
        _progress(progress, (index + 1) / total, "帧冻结")
    return SequenceArtifact(
        tuple(paths), artifact.width, artifact.height, artifact.has_alpha, str(output),
    )

def gradient_sequence(workspace, progress, width: int, height: int, frames: int, start_color: str, end_color: str, angle: float=0.0):
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

    output = _job_dir(workspace, "gradient")

    def process(index: int) -> Image.Image:
        # 每帧内容一致，复制一份避免多线程共享同一 PIL 对象。
        return base.copy()

    paths = _parallel_pil_export(progress, frames, output, "生成渐变序列", process)
    return SequenceArtifact(paths, width, height, False, str(output))

def merge_alpha(workspace, progress, rgb: SequenceArtifact | None, alpha: SequenceArtifact | None):
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
    output = _job_dir(workspace, "alpha_merge")

    def process(index: int) -> Image.Image:
        with Image.open(rgb.frames[index]) as image:
            red, green, blue, _alpha = image.convert("RGBA").split()
        if alpha_frames:
            with Image.open(alpha_frames[index]) as image:
                alpha_band = image.convert("L")
        else:
            alpha_band = Image.new("L", (width, height), 255)
        return Image.merge("RGBA", (red, green, blue, alpha_band))

    paths = _parallel_pil_export(progress, total, output, "A通道合并", process)
    return SequenceArtifact(paths, width, height, True, str(output))

def merge_channels(workspace, progress, red: SequenceArtifact | None, green: SequenceArtifact | None, blue: SequenceArtifact | None, alpha: SequenceArtifact | None):
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
    output = _job_dir(workspace, "merge")

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

    paths = _parallel_pil_export(progress, total, output, "RGBA 通道合并", process)
    return SequenceArtifact(paths, width, height, True, str(output))

def overlay_sequences(workspace, progress, a: SequenceArtifact, b: SequenceArtifact, *, resample: str='lanczos', strategy: str='fit'):
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
    output = _job_dir(workspace, "overlay")
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

    paths = _parallel_pil_export(progress, total, output, "序列叠加", process)
    return SequenceArtifact(paths, target_w, target_h, True, str(output))

def pan_sequence(workspace, progress, artifact: SequenceArtifact, *, direction: str='right', duration: int=30, curve: str='linear', interpolation: str='bilinear'):
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
    # 速度曲线：机器键 → 进度 p∈[0,1] 到位移比例 curve(p)∈[0,1]（单调不减）。
    _SPEED_CURVES: dict[str, Callable[[float], float]] = {
        "linear": lambda p: p,
        "accelerate": lambda p: p * p,
        "decelerate": lambda p: 1.0 - (1.0 - p) ** 2,
        "win10_decelerate": lambda p: _cubic_bezier_ease_out(p, 0.1, 0.9, 0.2, 1.0),
    }
    
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
    output = _job_dir(workspace, "pan")
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

    paths = _parallel_pil_export(progress, total, output, "平移滚动", process)
    return SequenceArtifact(
        tuple(paths),
        artifact.width, artifact.height, artifact.has_alpha, str(output),
    )

def rewind_sequence(workspace, progress, artifact: SequenceArtifact):
    """序列倒带（序列 → 序列）：把序列倒序输出（逆序播放）。

    不再自行追加序列（原「序列往复」行为）：如需往复效果，由用户
    再接「序列相加」节点把倒带结果接到原序列末尾。
    """
    output = _job_dir(workspace, "rewind")
    order = list(reversed(artifact.frames))
    paths: list[str] = []
    total = len(order)
    for index, source in enumerate(order):
        target = output / f"frame_{index:06d}.png"
        shutil.copy2(source, target)
        paths.append(str(target))
        _progress(progress, (index + 1) / total, "序列倒带")
    return SequenceArtifact(tuple(paths), artifact.width, artifact.height, artifact.has_alpha, str(output))

def sample_frames(workspace, progress, artifact: SequenceArtifact, in_fps: int, out_fps: int):
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
    output = _job_dir(workspace, "sampling")
    paths: list[str] = []
    for index, source in enumerate(selected):
        target = output / f"frame_{index:06d}.png"
        shutil.copy2(source, target)
        paths.append(str(target))
        _progress(progress, (index + 1) / len(selected), "抽帧")
    return SequenceArtifact(
        tuple(paths),
        artifact.width, artifact.height, artifact.has_alpha, str(output),
    )

def split_alpha(workspace, progress, artifact: SequenceArtifact):
    """A通道分离（序列 → 序列）：输出 alpha 通道灰度序列（R==G==B=alpha 值，不透明）。

    与 RGBA 通道分离的「透明度通道」语义一致（灰度值 = 原 alpha），但
    只物化 1 份缓存（不额外生成 RGB 分量）——避免「只想分离 alpha」时
    用 RGBA 分离白白占用红/绿/蓝/透明度 4 个通道的缓存。
    """
    output = _job_dir(workspace, "alpha")
    total = len(artifact.frames)

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = image.convert("RGBA")
            gray = rgba.getchannel("A").convert("L")
            return Image.merge("RGB", (gray, gray, gray))

    paths = _parallel_pil_export(progress, total, output, "A通道分离", process)
    return SequenceArtifact(paths, artifact.width, artifact.height, False, str(output))

def split_channels(workspace, progress, artifact: SequenceArtifact):
    """RGBA 通道分离（序列 → 四个序列）。

    每个通道输出为**灰度图**（R==G==B，通道值复制到三通道），Alpha 恒为
    255（不透明）——透明度通道同样以灰度图输出（灰度值 = 原 alpha 值），
    便于直接预览与下游处理（透明像素若保留原 alpha 会在预览中不可见）。
    返回 (红, 绿, 蓝, 透明度) 四个序列产物。
    """
    output = _job_dir(workspace, "channel")
    channel_dirs = {name: output / name for name in ("red", "green", "blue", "alpha")}
    for directory in channel_dirs.values():
        directory.mkdir(parents=True, exist_ok=True)
    total = len(artifact.frames)
    names = ("red", "green", "blue", "alpha")

    def job(index: int) -> dict[str, str]:
        """单帧：拆 4 通道并各自保存（worker 线程执行，互不触碰）。"""
        with Image.open(artifact.frames[index]) as image:
            rgba = image.convert("RGBA")
        written: dict[str, str] = {}
        for name, channel in zip(names, rgba.split()):
            gray = channel.convert("L")
            target = channel_dirs[name] / f"frame_{index:06d}.png"
            Image.merge("RGB", (gray, gray, gray)).save(
                target, "PNG", compress_level=PNG_CACHE_COMPRESS_LEVEL
            )
            written[name] = str(target)
        return written

    all_paths: dict[str, list[str]] = {name: [] for name in names}
    # 逐帧拆通道互不依赖：并行处理+保存（与其它序列处理节点同一套并行导出）。
    # pool.map 保持输入顺序返回，all_paths 天然按帧序。
    with ThreadPoolExecutor(max_workers=PNG_EXPORT_WORKERS) as pool:
        for index, written in enumerate(pool.map(job, range(total))):
            for name in names:
                all_paths[name].append(written[name])
            _progress(progress, (index + 1) / total, "RGBA 通道分离")
    return tuple(
        SequenceArtifact(tuple(all_paths[name]), artifact.width, artifact.height, False, str(channel_dirs[name]))
        for name in names
    )

def split_sequence(workspace, progress, artifact: SequenceArtifact, cut: int):
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
    output_a = _job_dir(workspace, "razor_a")
    output_b = _job_dir(workspace, "razor_b")
    paths_a: list[str] = []
    paths_b: list[str] = []
    done = 0
    for index, source in enumerate(artifact.frames[:cut]):
        target = output_a / f"frame_{index:06d}.png"
        shutil.copy2(source, target)
        paths_a.append(str(target))
        done += 1
        _progress(progress, done / total, "序列剃刀")
    for index, source in enumerate(artifact.frames[cut:]):
        target = output_b / f"frame_{index:06d}.png"
        shutil.copy2(source, target)
        paths_b.append(str(target))
        done += 1
        _progress(progress, done / total, "序列剃刀")
    return (
        SequenceArtifact(
            tuple(paths_a), artifact.width, artifact.height, artifact.has_alpha, str(output_a),
        ),
        SequenceArtifact(
            tuple(paths_b), artifact.width, artifact.height, artifact.has_alpha, str(output_b),
        ),
    )

def squeeze_aspect_sequence(workspace, progress, artifact: SequenceArtifact, factor: float):
    """纵横比挤压（序列 → 序列）：按因子非等比缩放帧宽，高度不变。

    ``factor``（滑条 0.2..5.0，1.0 = 不变）：输出宽度 = round(原宽 ×
    factor)，输出高度 = 原高——即 新纵横比 = 原纵横比 × factor。
    factor < 1 横向压扁（更窄），factor > 1 横向拉宽（更扁）。
    重采样 BICUBIC（平滑）；Alpha 通道原样保留。
    """
    factor = float(factor)
    if factor <= 0:
        raise ValueError(f"纵横比挤压因子必须为正数（当前 {factor}）")
    output = _job_dir(workspace, "aspect")
    total = len(artifact.frames)
    target_w = max(1, round(artifact.width * factor))
    target_h = artifact.height

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = image.convert("RGBA")
        if rgba.size != (target_w, target_h):
            rgba = rgba.resize((target_w, target_h), Image.Resampling.BICUBIC)
        return rgba

    paths = _parallel_pil_export(progress, total, output, "纵横比挤压", process)
    return SequenceArtifact(paths, target_w, target_h, artifact.has_alpha, str(output))

def scale_percent_sequence(workspace, progress, artifact: SequenceArtifact, percent: int = 100, resample: str = 'lanczos'):
    """百分比缩放（序列 → 序列）：按用户百分比等比缩放每一帧（宽高同比例）。

    - ``percent``（1..1000 整数百分数）：100 = 原尺寸不变；< 100 缩小、
      > 100 放大。目标宽高 = ``round(原尺寸 × percent / 100)``，最小 1px；
    - ``resample``（机器键）：nearest / bilinear / bicubic / lanczos
      （``options.RESAMPLE``，与「序列相加」「分辨率统一」同一来源）；
    - Alpha 随 RGBA 整体重采样保留（几何变换逐像素正确搬运）。

    帧数不变；输出尺寸 = 目标宽高。重采样与输出物化走并行 PIL 导出。
    """
    percent = int(percent)
    if not 1 <= percent <= 1000:
        raise ValueError(f"缩放百分比必须在 1–1000（当前 {percent}）")
    if resample not in RESAMPLE.key_set:
        raise ValueError(f"未知缩放算法：{resample}")
    resampler = RESAMPLE.value_for_key(resample)
    output = _job_dir(workspace, "scale")
    total = len(artifact.frames)
    target_w = max(1, round(artifact.width * percent / 100.0))
    target_h = max(1, round(artifact.height * percent / 100.0))

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = image.convert("RGBA")
        if rgba.size != (target_w, target_h):
            rgba = rgba.resize((target_w, target_h), resampler)
        return rgba

    paths = _parallel_pil_export(progress, total, output, "百分比缩放", process)
    return SequenceArtifact(paths, target_w, target_h, artifact.has_alpha, str(output))

def static_hold_sequence(workspace, progress, artifact: SequenceArtifact, *, threshold: int=3, reference: str='prev', neighbors: int=4):
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
    output = _job_dir(workspace, "static_hold")
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
            futures.append(pool.submit(_parallel_pil_save_bounded, image, target, save_sem))
            paths.append(str(target))
            _progress(progress, (index + 1) / total, "帧差静止保持")
        _drain_save_futures(futures)
    return SequenceArtifact(
        tuple(paths), artifact.width, artifact.height, artifact.has_alpha, str(output),
    )

def trim_sequence(workspace, progress, artifact: SequenceArtifact, start: int, end: int):
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
    output = _job_dir(workspace, "trim")
    paths: list[str] = []
    selected = artifact.frames[start:end]
    for index, source in enumerate(selected):
        target = output / f"frame_{index:06d}.png"
        shutil.copy2(source, target)
        paths.append(str(target))
        _progress(progress, (index + 1) / len(selected), "序列截取")
    return SequenceArtifact(
        tuple(paths),
        artifact.width, artifact.height, artifact.has_alpha, str(output),
    )

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

def _job_dir(workspace: Path, prefix: str) -> Path:
    """模块版（决策 #120）：等价原 ``MediaBackend._job_dir``。"""
    path = workspace / f"{prefix}_{uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

def _progress(progress: ProgressReporter | None, fraction: float | None, label: str) -> None:
    """模块版（决策 #120）：等价原 ``MediaBackend._progress``。"""
    if progress is not None:
        progress(fraction, label)

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
