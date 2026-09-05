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


def adjust_color(workspace, progress, artifact: SequenceArtifact, brightness: float=0, saturation: float=0):
    """亮度/饱和度调整（序列 → 序列，全图）：PIL ImageEnhance 近似。

    - ``brightness``（-100..100）：亮度 %，factor = 1 + 值/100（乘 RGB，
      曝光式近似，非 PS 色相/饱和度对话框的「明度」向黑白混合语义）；
    - ``saturation``（-100..200）：饱和度 %，factor = 1 + 值/100。

    只作用于 RGB，Alpha 原样保留。PS 式全图/选区调色见
    ``hue_sat_range_sequence``（决策 #134 取代本函数的 hue 参数——色相调整
    节点已删除，全图色相旋转由色相/饱和度节点的「全图」承担）。
    """
    output = _job_dir(workspace, "color")
    total = len(artifact.frames)
    brightness, saturation = float(brightness), float(saturation)

    def process(index: int) -> Image.Image:
        image = Image.open(artifact.frames[index]).convert("RGBA")
        alpha = image.getchannel("A")
        rgb = image.convert("RGB")
        rgb = ImageEnhance.Brightness(rgb).enhance(max(0, 1 + brightness / 100))
        rgb = ImageEnhance.Color(rgb).enhance(max(0, 1 + saturation / 100))
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

def color_key_tolerance_sequence(workspace, progress, artifact: SequenceArtifact, key_color: tuple[int, int, int], tolerance: float = 50.0, feather: float = 50.0):
    """颜色键（容差式抠像，序列 → 序列）：按用户选取的背景色键出，容差/羽化由用户控制。

    与「超级键」（``color_key_sequence``，固定中心阈值 0.25 + 边缘处理强弱）的
    滤色逻辑区别：键控半径由用户**容差**直接决定，羽化带自容差边界**向外**过渡
    （容差内像素完全键出，不会因羽化而在中心出现半透明）：
    - ``key_color``：RGB 三元组（如 (0, 255, 0) 绿幕）；
    - ``tolerance``（0..100 百分数）：键出半径——归一化距离 ≤ 中心
      ``center = tolerance% × 0.5`` 的像素**完全透明**（50% ≡ 0.25，与超级键
      默认中心一致；0% 仅键出与背景色完全相同的像素）；
    - ``feather``（0..100 百分数）：自容差边界向外柔和过渡带宽度
      （0% = 硬边二值；100% = 带宽 0.5 归一化距离的大范围羽化），
      过渡带内距背景色越近越透明。

    距离度量 = RGB 欧氏距离归一化（除以 √3·255 → 0..1），与超级键同一度量。
    输出 RGB 保持原值；``alpha = 原 alpha × (1 − 键控系数)``（只减不增，
    原本透明的像素保持透明）。numpy 向量化逐帧计算。
    """
    key_r, key_g, key_b = (int(channel) for channel in key_color)
    tol = max(0.0, min(100.0, float(tolerance)))
    fea = max(0.0, min(100.0, float(feather)))
    # 键控中心 = 容差半径（50% ≡ 0.25，与超级键固定中心一致，行为可预期）。
    center = tol / 100.0 * 0.5
    # 羽化带：自容差边界向外延伸（100% → 带宽 0.5 归一化距离）。
    band = fea / 100.0 * 0.5
    outer = min(1.0, center + band)
    key = np.array((key_r, key_g, key_b), dtype=np.float32)
    output = _job_dir(workspace, "color_key")
    total = len(artifact.frames)

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = np.asarray(image.convert("RGBA"))  # uint8 (h, w, 4)
        diff = rgba[..., :3].astype(np.float32) - key
        distance = np.sqrt((diff * diff).sum(axis=2) / 3.0) / 255.0
        if band > 0 and outer > center:
            # 容差内完全键出（factor=1）；容差外到 outer 之间线性衰减到 0（羽化）。
            factor = np.clip((outer - distance) / (outer - center), 0.0, 1.0)
        else:
            # 0% 羽化硬边二值：距离 ≤ 容差全键出，其余全保留。
            factor = (distance <= center).astype(np.float32)
        out = rgba.copy()
        # 只减不增：键控系数 1 = 完全透明、0 = 保留原 alpha。
        out[..., 3] = np.round(rgba[..., 3] * (1.0 - factor)).astype(np.uint8)
        return Image.fromarray(out, "RGBA")

    paths = _parallel_pil_export(progress, total, output, "颜色键", process)
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

def _rgb_to_hsl(rgb):
    """RGB（float64, ...,3, 0..1）→ HSL（...,3：h 0..360、s/l 0..1）。"""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    diff = mx - mn
    light = (mx + mn) / 2.0
    sat = np.zeros_like(light)
    denom = 1.0 - np.abs(2.0 * light - 1.0)
    np.divide(diff, denom, out=sat, where=denom > 0.0)
    hue = np.zeros_like(light)
    colored = diff > 0.0
    red_max = colored & (mx == r)
    green_max = colored & (mx == g)
    with np.errstate(divide="ignore", invalid="ignore"):
        # 灰像素 diff=0：三个 np.where 分支都会被求值，0/0 → nan 后丢弃——
        # 抑制该噪音（hue 对无彩像素无意义，weight 计算按饱和度 0 不参与）。
        hue = np.where(red_max, (60.0 * ((g - b) / diff) + 360.0) % 360.0, hue)
        hue = np.where(green_max, 60.0 * ((b - r) / diff) + 120.0, hue)
        hue = np.where(colored & ~red_max & ~green_max, 60.0 * ((r - g) / diff) + 240.0, hue)
    return np.stack((hue, sat, light), axis=-1)


def _hsl_to_rgb(hsl):
    """HSL（...,3：h 0..360、s/l 0..1）→ RGB float64（0..1）。"""
    h = hsl[..., 0] % 360.0
    s = np.clip(hsl[..., 1], 0.0, 1.0)
    l = np.clip(hsl[..., 2], 0.0, 1.0)
    chroma = (1.0 - np.abs(2.0 * l - 1.0)) * s
    x = chroma * (1.0 - np.abs((h / 60.0) % 2.0 - 1.0))
    zero = np.zeros_like(h)
    c0 = h < 60.0
    c1 = h < 120.0
    c2 = h < 180.0
    c3 = h < 240.0
    c4 = h < 300.0
    rp = np.select([c0, c1, c2, c3, c4, True], [chroma, x, zero, zero, x, chroma])
    gp = np.select([c0, c1, c2, c3, c4, True], [x, chroma, chroma, x, zero, zero])
    bp = np.select([c0, c1, c2, c3, c4, True], [zero, zero, x, chroma, chroma, x])
    m = l - chroma / 2.0
    return np.stack((rp + m, gp + m, bp + m), axis=-1)


def hue_sat_range_sequence(workspace, progress, artifact: SequenceArtifact, *,
                           center_hue: float | None = None, hue_delta: float = 0.0,
                           sat_delta: float = 0.0, light_delta: float = 0.0,
                           range_half: float = 15.0, feather_deg: float = 30.0):
    """色相/饱和度（序列 → 序列）：PS「色相/饱和度」对话框的选区版复刻。

    - ``center_hue``：目标色域中心色相（0..360 度）；``None`` = 全图
      （PS 全图/Master——蒙版恒 1，饱和度/明度作用于全部像素）；
    - ``hue_delta``（-180..180）：色相旋转量（PS 色相滑块，正 = 色轮正向）；
    - ``sat_delta``（-100..100）：饱和度百分比增量（scale = 1 + 值/100，
      在色域内完整生效、过渡带按权重衰减；0 = 不变）；
    - ``light_delta``（-100..100）：明度百分比（PS 式向黑白混合：正 → 向白、
      负 → 向黑，按 |值|/100 × 剩余距离）；
    - ``range_half``（0..180）：中心色域半宽（范围内 100% 生效，默认 15°
      = PS 红色通道中心带 ±15°）；
    - ``feather_deg``（0..180）：中心带外的过渡（羽化）半宽（默认 30°，
      总外缘 = range_half + feather_deg = PS 红 45°）；过渡带内权重从 1
      线性衰减到 0。

    权重蒙版 = 色相环最短距离（min(|h−c|, 360−|h−c|)）：d ≤ range_half → 1，
    range_half < d ≤ range_half+feather_deg → 线性衰减，之外 → 0（PS 中心
    色域 + 衰减区语义）。灰度像素（饱和度 0）不受色相/饱和度影响（色相无
    定义），明度仍可生效（PS 同）。公式为工程近似（PS 私有实现，公开逆向
    只覆盖 Master 形态），曲线细节待差分标定——见
    docs/research/ps-hue-saturation-feasibility.md §3/§4。
    只作用于 RGB，Alpha 原样保留。numpy 向量化逐帧计算。
    """
    master = center_hue is None
    center = 0.0 if master else float(center_hue) % 360.0
    hue_shift = float(hue_delta)
    sat_scale_delta = max(-100.0, min(100.0, float(sat_delta))) / 100.0
    light_shift = max(-100.0, min(100.0, float(light_delta))) / 100.0
    inner = max(0.0, min(180.0, float(range_half)))
    outer = min(180.0, inner + max(0.0, min(180.0, float(feather_deg))))
    output = _job_dir(workspace, "hue_sat")
    total = len(artifact.frames)

    def process(index: int) -> Image.Image:
        with Image.open(artifact.frames[index]) as image:
            rgba = np.asarray(image.convert("RGBA"))  # uint8 (h, w, 4)
        rgb = rgba[..., :3].astype(np.float64) / 255.0
        h, s, l = np.moveaxis(_rgb_to_hsl(rgb), -1, 0)
        if master:
            weight = np.ones_like(h)
        elif outer > inner:
            distance = np.minimum(np.abs(h - center), 360.0 - np.abs(h - center))
            weight = np.clip((outer - distance) / (outer - inner), 0.0, 1.0)
        else:
            # 过渡宽 0：中心带硬边（PS 外三角贴内三角）。
            distance = np.minimum(np.abs(h - center), 360.0 - np.abs(h - center))
            weight = (distance <= inner).astype(np.float64)
        # 色相：蒙版内旋转 hue_shift × w（过渡带部分旋转，防色带断裂）。
        h_new = (h + hue_shift * weight) % 360.0
        # 饱和度：比例缩放（PS 公开近似之外，刻度先按现有全图节点一致）。
        s_new = np.clip(s * (1.0 + sat_scale_delta * weight), 0.0, 1.0)
        # 明度：PS 式向黑白混合（正 → 白、负 → 黑，|Δ| 比例作用于剩余距离）。
        delta_l = light_shift * weight
        l_new = l + delta_l * ((1.0 - l) if light_shift >= 0 else l)
        l_new = np.clip(l_new, 0.0, 1.0)
        out_rgb = _hsl_to_rgb(np.stack((h_new, s_new, l_new), axis=-1))
        out = rgba.copy()
        out[..., :3] = np.clip(np.rint(out_rgb * 255.0), 0, 255).astype(np.uint8)
        return Image.fromarray(out, "RGBA")

    paths = _parallel_pil_export(progress, total, output, "色相/饱和度", process)
    return SequenceArtifact(paths, artifact.width, artifact.height, True, str(output))


def _job_dir(workspace: Path, prefix: str) -> Path:
    """模块版（决策 #120）：等价原 ``MediaBackend._job_dir``。"""
    path = workspace / f"{prefix}_{uuid4().hex[:10]}"
    path.mkdir(parents=True)
    return path

def _progress(progress: ProgressReporter | None, fraction: float | None, label: str) -> None:
    """模块版（决策 #120）：等价原 ``MediaBackend._progress``。"""
    if progress is not None:
        progress(fraction, label)
