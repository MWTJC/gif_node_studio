"""伪影度量（纯 numpy，无 IO / 不依赖 ImageMagick）：源侧伪影的客观指标。

供「伪影诊断」分析节点（决策 #135）与回归测试使用。口径与 2026-09 源侧伪影
调查的探针脚本完全一致（结论见
[源侧伪影调查与候选新节点评估](../docs/research/gif-source-artifacts-and-node-candidates.md)），
因此节点显示的数值可以直接与那次调查的实测数字对照：

- ``ringing`` —— 强边亮侧过冲（「白边」）：沿 |Δluma| > 60 的强边取两条平台
  （暗侧 x-5..x-1、亮侧 x+4..x+7；要求亮-暗 > 80），过冲 = 亮侧紧邻 3 像素
  最大值 − 亮侧平台；
- ``edge_width`` —— 同批强边的 10%–90% 过渡宽度（边缘「软硬」）；
- ``flat_noise`` —— 最平坦 5% 的 16×16 亮度块标准差均值（抖动/压缩杂色）；
- ``speckle`` —— 局部跨度 ≤ 30 的平坦区里偏离 3×3 中值 > 15 的像素占比
  （孤立杂点；误差扩散仿色的「白点斑点」属此类）；
- ``color_stats`` —— 每帧唯一色数 / 最大单色占比（平色内容应单色占比很高）。

亮度按 Rec.601（0.299/0.587/0.114）计算，与探针一致。
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np

STRONG_EDGE = 60.0  # 强边阈值（luma 差）
MIN_CONTRAST = 80.0  # 计入统计所需的最小暗/亮平台对比
OVERSHOOT_LEVEL = 8.0  # 计为「有过冲」的亮侧过冲级数
SPECKLE_DELTA = 15.0  # 偏离 3×3 中值的量
SPECKLE_RANGE = 30.0  # 3×3 邻域跨度上限（判定为「平坦区」）
FLAT_BLOCK = 16  # 平坦区噪声的块边长
FLAT_BLOCK_SHARE = 20  # 取最平坦的 1/20 块（= 5%）
PROGRESS_LABEL = "伪影诊断"


def luma(rgb: np.ndarray) -> np.ndarray:
    """Rec.601 亮度（输入 RGB/RGBA uint8 → float64 二维数组）。"""
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


def color_stats(rgb: np.ndarray) -> tuple[int, float, np.ndarray]:
    """返回 ``(唯一色数, 最大单色占比 0..1, 唯一色打包数组)``。

    打包为 ``r<<16 | g<<8 | b`` 的 uint32，便于跨帧做并集。
    """
    packed = (
        (rgb[..., 0].astype(np.uint32) << 16)
        | (rgb[..., 1].astype(np.uint32) << 8)
        | rgb[..., 2].astype(np.uint32)
    )
    values, counts = np.unique(packed, return_counts=True)
    share = float(counts.max()) / float(packed.size)
    return int(values.size), share, values


def _strong_edges(lum: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """筛出用于振铃/边缘统计的强边：返回 ``(ys, xs, 暗侧平台, 亮侧平台)``。

    边沿位置 ``x`` 表示 ``lum[y, x] → lum[y, x+1]`` 的跳变（与 np.diff 对齐）。
    左右各留 6/8 像素余量，避免索引越界（与探针一致）。
    """
    height, width = lum.shape
    dx = np.diff(lum, axis=1)
    ys, xs = np.nonzero(np.abs(dx) > STRONG_EDGE)
    keep = (xs >= 6) & (xs <= width - 8)
    ys, xs = ys[keep], xs[keep]
    if ys.size == 0:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, np.zeros(0), np.zeros(0)
    dark = np.zeros(xs.size, dtype=np.float64)
    bright = np.zeros(xs.size, dtype=np.float64)
    for offset in range(2, 6):  # x-5 .. x-2（与调查探针口径严格一致）
        dark += lum[ys, xs - offset]
    for offset in range(4, 8):  # x+4 .. x+7
        bright += lum[ys, xs + offset]
    dark /= 4.0
    bright /= 4.0
    keep = (bright - dark) > MIN_CONTRAST
    return ys[keep], xs[keep], dark[keep], bright[keep]


def overshoot_mask(lum: np.ndarray) -> np.ndarray:
    """亮侧过冲（白边）像素掩码：过冲 ≥ ``OVERSHOOT_LEVEL`` 级。"""
    mask = np.zeros(lum.shape, dtype=bool)
    ys, xs, _dark, bright = _strong_edges(lum)
    if ys.size == 0:
        return mask
    for offset in (1, 2, 3):
        hit = (lum[ys, xs + offset] - bright) >= OVERSHOOT_LEVEL
        mask[ys[hit], xs[hit] + offset] = True
    return mask


def ringing(lum: np.ndarray) -> tuple[float, float]:
    """返回 ``(过冲均值 级, 有过冲(≥8级)边缘占比 0..1)``。"""
    ys, xs, _dark, bright = _strong_edges(lum)
    if ys.size == 0:
        return 0.0, 0.0
    peak = np.maximum.reduce([lum[ys, xs + 1], lum[ys, xs + 2], lum[ys, xs + 3]])
    overshoot = peak - bright
    return float(overshoot.mean()), float((overshoot >= OVERSHOOT_LEVEL).mean())


def edge_width(lum: np.ndarray) -> float:
    """强边 10%–90% 过渡宽度中位数（px）；无合格边缘返回 0.0。

    纯数字线稿通常 1–2 px，视频/重采样后的软边常见 3–5 px。
    """
    ys, xs, _dark, _bright = _strong_edges(lum)
    if ys.size == 0:
        return 0.0
    offsets = np.arange(-5, 8)
    seg = lum[ys[:, None], xs[:, None] + offsets[None, :]]  # (n, 13)
    low = seg.min(axis=1)
    high = seg.max(axis=1)
    span = high - low
    keep = span > STRONG_EDGE
    seg, low, span = seg[keep], low[keep], span[keep]
    if span.size == 0:
        return 0.0
    at_high = seg >= (low + 0.9 * span)[:, None]
    at_low = seg <= (low + 0.1 * span)[:, None]
    first_high = np.argmax(at_high, axis=1)
    last_low = seg.shape[1] - 1 - np.argmax(at_low[:, ::-1], axis=1)
    return float(np.median(np.abs(first_high - last_low)))


def flat_noise(lum: np.ndarray) -> float:
    """最平坦 5% 的 16×16 亮度块 std 均值（越小越干净）。"""
    height, width = lum.shape
    blocks_h, blocks_w = height // FLAT_BLOCK, width // FLAT_BLOCK
    if blocks_h == 0 or blocks_w == 0:
        return float(lum.std())
    blocks = (
        lum[: blocks_h * FLAT_BLOCK, : blocks_w * FLAT_BLOCK]
        .reshape(blocks_h, FLAT_BLOCK, blocks_w, FLAT_BLOCK)
        .std(axis=(1, 3))
        .ravel()
    )
    blocks.sort()
    count = max(1, blocks.size // FLAT_BLOCK_SHARE)
    return float(blocks[:count].mean())


def _neighbourhood(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """3×3 邻域：返回 ``(中值, 跨度, 与中值的最大通道偏差)``（int16，省内存）。"""
    height, width = rgb.shape[:2]
    padded = np.pad(rgb.astype(np.int16), ((1, 1), (1, 1), (0, 0)), mode="edge")
    window = np.stack(
        [
            padded[dy : dy + height, dx : dx + width]
            for dy in range(3)
            for dx in range(3)
        ],
        axis=0,
    )
    median = np.partition(window, 4, axis=0)[4]  # 9 个样本的第 5 小 = 中值
    span = (window.max(axis=0) - window.min(axis=0)).max(axis=-1)
    deviation = np.abs(rgb.astype(np.int16) - median).max(axis=-1)
    return median, span, deviation


def speckle_mask(rgb: np.ndarray) -> np.ndarray:
    """孤立斑点掩码：局部平坦（跨度 ≤30）却偏离 3×3 中值 >15 的像素。"""
    _median, span, deviation = _neighbourhood(rgb)
    return (deviation > SPECKLE_DELTA) & (span <= SPECKLE_RANGE)


def speckle(rgb: np.ndarray) -> float:
    """孤立斑点像素占比（0..1）。"""
    return float(speckle_mask(rgb).mean())


def measure_frames(frames: Iterable[np.ndarray]) -> dict[str, float]:
    """对若干帧做逐帧度量后聚合（口径与调查探针一致）。

    返回键：``每帧色数``（各帧中位）、``并集色数``、``最大单色占比``（%）、
    ``平坦区噪声``、``振铃过冲``、``有过冲边缘``（%）、``边缘过渡宽度``、
    ``孤立斑点``（%）。
    """
    frames = list(frames)
    if not frames:
        raise ValueError("伪影诊断：没有可度量的帧")
    per_frame_colors: list[int] = []
    shares: list[float] = []
    flats: list[float] = []
    overshoots: list[float] = []
    overshoot_shares: list[float] = []
    widths: list[float] = []
    speckles: list[float] = []
    union = np.zeros(0, dtype=np.uint32)
    for rgb in frames:
        colors, share, packed = color_stats(rgb)
        per_frame_colors.append(colors)
        shares.append(share)
        union = np.union1d(union, packed)
        lum = luma(rgb)
        flats.append(flat_noise(lum))
        mean_overshoot, overshoot_share = ringing(lum)
        overshoots.append(mean_overshoot)
        overshoot_shares.append(overshoot_share)
        widths.append(edge_width(lum))
        speckles.append(speckle(rgb))
    return {
        "每帧色数": float(np.median(per_frame_colors)),
        "并集色数": float(union.size),
        "最大单色占比": float(np.mean(shares)) * 100.0,
        "平坦区噪声": float(np.mean(flats)),
        "振铃过冲": float(np.mean(overshoots)),
        "有过冲边缘": float(np.mean(overshoot_shares)) * 100.0,
        "边缘过渡宽度": float(np.mean(widths)),
        "孤立斑点": float(np.mean(speckles)) * 100.0,
    }


def heatmap(base_rgb: np.ndarray, *, overshoot: tuple[int, int, int] = (255, 48, 48),
            speckle_color: tuple[int, int, int] = (64, 144, 255)) -> np.ndarray:
    """在基帧上标出伪影：红 = 亮侧过冲（白边），蓝 = 孤立斑点。"""
    out = base_rgb.astype(np.uint8).copy()
    marks = (
        (overshoot_mask(luma(base_rgb)), overshoot),
        (speckle_mask(base_rgb), speckle_color),
    )
    for mask, color in marks:
        out[mask] = np.array(color, dtype=np.uint8)
    return out
