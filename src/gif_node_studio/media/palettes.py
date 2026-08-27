"""调色板相关辅助：阈值图名、系统调色板、remap affinity PNG blob。

独立于 MediaBackend（不持有实例状态），供量化/分析/导出路径共用；
调色板文件 data/palettes/*.png 与阈值图 data/thresholds.xml 位于包根
（Nuitka --include-data-dir 清单的一部分，见 docs/packaging.md）。
"""

from __future__ import annotations

import io
from typing import Callable
from ..core.paths import im_data_dir
from PIL import Image


# ImageMagick 官方 thresholds.xml 中的全部有序仿色阈值图（IM 6.9 / 7.0 / 7.1 实测一致；
# 应用自带的 data/thresholds.xml 另有自定义映射，见 ordered_dither_map_names）。
ORDERED_DITHER_MAPS: tuple[str, ...] = (
    "threshold", "checks", "o2x2", "o3x3", "o4x4", "o8x8",
    "h4x4a", "h6x6a", "h8x8a", "h4x4o", "h6x6o", "h8x8o", "h16x16o",
    "c5x5b", "c5x5w", "c6x6b", "c6x6w", "c7x7b", "c7x7w",
)

# Posterization levels 下拉预设（作为 ordered_dither 的 `,levels` 后缀）：
# 单值 = 每通道级别数；`8,8,4` = 每通道分别为 R8/G8/B4（332 均匀立方体）。
POSTERIZE_LEVELS: tuple[str, ...] = ("2", "6", "8,8,4", "13")

_custom_maps_cache: tuple[str, ...] | None = None


def custom_threshold_maps() -> tuple[str, ...]:
    """解析应用自带 data/thresholds.xml 中的自定义阈值图名（记忆化）。"""
    global _custom_maps_cache
    if _custom_maps_cache is None:
        import xml.etree.ElementTree as ElementTree

        path = im_data_dir() / "thresholds.xml"
        try:
            tree = ElementTree.parse(path)
            _custom_maps_cache = tuple(
                node.attrib["map"] for node in tree.getroot().findall("threshold")
            )
        except (OSError, ElementTree.ParseError):
            _custom_maps_cache = ()
    return _custom_maps_cache


def ordered_dither_map_names() -> tuple[str, ...]:
    """仿色方式下拉：官方全部阈值图 + data/thresholds.xml 自定义映射。"""
    return ORDERED_DITHER_MAPS + custom_threshold_maps()


def _palette_png_blob(colors: list[tuple[int, int, int]]) -> bytes:
    """把色表写成一列像素的 PNG blob，作为 ``-remap`` 的 affinity 图像。"""
    image = Image.new("RGB", (len(colors), 1))
    image.putdata(colors)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _websafe_map_blob() -> bytes:
    """生成 216 色 web-safe 调色板图像（PNG blob），作为 ``-remap netscape:`` 的等价物。

    颜色为 R/G/B ∈ {0, 51, 102, 153, 204, 255} 的全组合（6³ = 216 色）。
    """
    colors = [(r * 51, g * 51, b * 51) for r in range(6) for g in range(6) for b in range(6)]
    return _palette_png_blob(colors)


# ---------------------------------------------------------------------------
# 降低颜色深度算法 / 仿色算法（PS「存储为 Web 所用格式」同款选项）
# 与序列相加节点的缩放算法 / 缩放策略：唯一源头在 options.py（ChoiceGroup），
# 此处只消费机器键，不再维护平行 CHOICES/KEYS/MAP 常量（防止标签↔键漂移）。
# 「可选择 Selective」为 Adobe 私有算法，ImageMagick 无等价物，不实现。
# ---------------------------------------------------------------------------


def windows_palette_colors() -> tuple[tuple[int, int, int], ...]:
    """Windows 3.1 256 色系统调色板（近似）。

    结构 = 首 10 系统色 + 216 web-safe + 20 灰阶 + 末 10 系统色（10+216+20+10 = 256），
    对应「Windows 保留首尾各 10 色给系统界面」的经典布局。灰阶为 0..255 均匀 20 级。
    索引位置不保证与真实系统色板逐位一致；data/palettes/windows.png 可直接替换。
    """
    web = [(r * 51, g * 51, b * 51) for r in range(6) for g in range(6) for b in range(6)]
    grays = [tuple(round(i * 255 / 19) for _ in range(3)) for i in range(20)]
    head = [
        (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0), (0, 0, 128),
        (128, 0, 128), (0, 128, 128), (192, 192, 192), (192, 220, 192), (166, 202, 240),
    ]
    tail = [
        (255, 251, 240), (160, 160, 164), (128, 128, 128), (255, 0, 0), (0, 255, 0),
        (255, 255, 0), (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ]
    return tuple(head + web + grays + tail)


def macos_palette_colors() -> tuple[tuple[int, int, int], ...]:
    """Mac OS 经典 256 色系统调色板（近似）。

    结构 = 216 web-safe + 灰/红/绿/蓝各 10 级渐变（40 色），共 256，
    对应「Macintosh 默认调色板含四组十级渐变色」的记载。渐变由亮到暗步进 17。
    索引位置不保证与真实系统色板逐位一致；data/palettes/macos.png 可直接替换。
    """
    web = [(r * 51, g * 51, b * 51) for r in range(6) for g in range(6) for b in range(6)]
    shades = [238 - 17 * i for i in range(10)]  # 238..85
    grads = (
        [(v, v, v) for v in shades]
        + [(v, 0, 0) for v in shades]
        + [(0, v, 0) for v in shades]
        + [(0, 0, v) for v in shades]
    )
    return tuple(web + grads)


_SYSTEM_PALETTE_DIR = im_data_dir() / "palettes"
_SYSTEM_PALETTE_TABLES: dict[str, Callable[[], tuple[tuple[int, int, int], ...]]] = {
    "windows": windows_palette_colors,
    "macos": macos_palette_colors,
}


def system_palette_blob(name: str) -> bytes:
    """读取系统调色板 PNG blob。

    优先读 ``data/palettes/<name>.png``（用户可直接替换/自定义色板文件）；
    缺失时用内置色表生成并写盘（自愈，打包后写不进只读目录也不影响运行）。
    """
    if name not in _SYSTEM_PALETTE_TABLES:
        raise ValueError(f"未知系统色板：{name}")
    target = _SYSTEM_PALETTE_DIR / f"{name}.png"
    if target.is_file():
        return target.read_bytes()
    blob = _palette_png_blob(list(_SYSTEM_PALETTE_TABLES[name]()))
    try:
        _SYSTEM_PALETTE_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    except OSError:
        pass
    return blob
