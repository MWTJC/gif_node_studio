"""MediaBackend 区段 2：颜色处理（纯函数模块，决策 #120 拆出）。

与 #82 同名 mixin 无继承关系；无实例状态，依赖显式注入。"""

from __future__ import annotations

from ..core.domain import SequenceArtifact
from PIL import Image
from PIL import ImageEnhance
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4
import numpy as np
ProgressReporter = Callable[[float | None, str], None]
from .backend_format import _parallel_pil_export


def adjust_color(workspace, progress, artifact: SequenceArtifact, brightness: float=0, saturation: float=0, hue: float=0):
    output = _job_dir(workspace, "color")
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

    paths = _parallel_pil_export(progress, total, output, "色彩调整", process)
    return SequenceArtifact(paths, artifact.width, artifact.height, True, str(output))

def binarize_sequence(workspace, progress, artifact: SequenceArtifact, threshold: int):
    """二值化（序列 → 序列）：转灰度后按阈值二值化，输出只有黑/白两色。

    ``threshold`` 0–255：像素值 < 阈值 → 黑 (0,0,0)，≥ 阈值 → 白 (255,255,255)。
    只作用于 RGB，Alpha 原样保留。
    """
    threshold = int(threshold)
    if not 0 <= threshold <= 255:
        raise ValueError(f"二值化阈值必须在 0–255 之间（当前 {threshold}）")
    output = _job_dir(workspace, "binarize")
    total = len(artifact.frames)

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = image.convert("RGBA")
            alpha = rgba.getchannel("A")
            gray = rgba.convert("L")
            binary = gray.point(lambda value: 255 if value >= threshold else 0)
            return Image.merge("RGBA", (binary, binary, binary, alpha))

    paths = _parallel_pil_export(progress, total, output, "二值化", process)
    return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))

def color_key_sequence(workspace, progress, artifact: SequenceArtifact, key_color: tuple[int, int, int], edge_strength: float):
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
    output = _job_dir(workspace, "key")
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

    paths = _parallel_pil_export(progress, total, output, "超级键", process)
    return SequenceArtifact(paths, artifact.width, artifact.height, True, str(output))

def contrast_sequence(workspace, progress, artifact: SequenceArtifact, amount: float):
    """对比度调整（序列 → 序列）：PIL ImageEnhance.Contrast。

    ``amount`` 为百分比增量（-100..100）：0 = 不变，正值增强、负值减弱。
    只作用于 RGB，Alpha 原样保留。
    """
    output = _job_dir(workspace, "contrast")
    total = len(artifact.frames)
    factor = max(0, 1 + float(amount) / 100)

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = image.convert("RGBA")
            alpha = rgba.getchannel("A")
            rgb = rgba.convert("RGB")
            rgb = ImageEnhance.Contrast(rgb).enhance(factor)
            return Image.merge("RGBA", (*rgb.split(), alpha))

    paths = _parallel_pil_export(progress, total, output, "对比度调整", process)
    return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))

def flip_sequence(workspace, progress, artifact: SequenceArtifact, direction: str):
    """画面翻转（序列 → 序列）：水平（左右镜像）或垂直（上下翻转）翻转每一帧。

    - ``direction`` 为机器键：``"horizontal"`` → PIL ``ImageOps.mirror``、
      ``"vertical"`` → PIL ``ImageOps.flip``；
    - 几何变换逐像素搬运，**Alpha 随像素正确翻转**（无需像反相那样拆通道）；
    - 输出尺寸/alpha 标志与原序列一致（翻转不改变画布尺寸）。
    """
    from PIL import ImageOps

    if direction not in ("horizontal", "vertical"):
        raise ValueError(f"未知翻转方向 {direction!r}（可选：horizontal / vertical）")
    output = _job_dir(workspace, "flip")
    total = len(artifact.frames)

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = image.convert("RGBA")
            return ImageOps.mirror(rgba) if direction == "horizontal" else ImageOps.flip(rgba)

    paths = _parallel_pil_export(progress, total, output, "画面翻转", process)
    return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))

def grayscale_sequence(workspace, progress, artifact: SequenceArtifact):
    """灰度化（序列 → 序列）：RGB 转灰度（R==G==B），Alpha 原样保留。"""
    output = _job_dir(workspace, "gray")
    total = len(artifact.frames)

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = image.convert("RGBA")
            alpha = rgba.getchannel("A")
            gray = rgba.convert("L")
            return Image.merge("RGBA", (gray, gray, gray, alpha))

    paths = _parallel_pil_export(progress, total, output, "灰度化", process)
    return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))

def invert_sequence(workspace, progress, artifact: SequenceArtifact):
    """反相（序列 → 序列）：RGB 各通道取反（255 - 原值），Alpha 原样保留。

    与灰度化/对比度同一语义：只作用于 RGB（``PIL.ImageOps.invert``），
    不处理 Alpha 通道（透明像素仍是透明像素，只是颜色反转）。
    """
    from PIL import ImageOps

    output = _job_dir(workspace, "invert")
    total = len(artifact.frames)

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = image.convert("RGBA")
            alpha = rgba.getchannel("A")
            rgb = rgba.convert("RGB")
            rgb = ImageOps.invert(rgb)
            return Image.merge("RGBA", (*rgb.split(), alpha))

    paths = _parallel_pil_export(progress, total, output, "反相", process)
    return SequenceArtifact(paths, artifact.width, artifact.height, artifact.has_alpha, str(output))

def _job_dir(workspace: Path, prefix: str) -> Path:
    """模块版（决策 #120）：等价原 ``MediaBackend._job_dir``。"""
    path = workspace / f"{prefix}_{uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

def _progress(progress: ProgressReporter | None, fraction: float | None, label: str) -> None:
    """模块版（决策 #120）：等价原 ``MediaBackend._progress``。"""
    if progress is not None:
        progress(fraction, label)
