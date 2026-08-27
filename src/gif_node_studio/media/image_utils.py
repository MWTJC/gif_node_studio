"""图像底层辅助：wand/PIL 像素与字节转换、缓存 PNG 压缩常量。

独立于 MediaBackend（不持有实例状态），供格式化/量化/导出/分析路径共用；
``_wand_rgba_bytes`` 亦被节点预览播放器（nodes/preview_widgets.py）引用。
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

import numpy as np
from PIL import Image

# 静态图片序列没有原生帧率：格式化输出默认按此帧率标注（抽帧节点的「定义帧速」
# 由用户输入，可据此调整）。
DEFAULT_SEQUENCE_FPS = 12.0

# ico 合成节点：常见 icon 分辨率阶梯（从小到大）。自动分级时只缩小
# （目标尺寸 ≤ 源分辨率的最小边），最小保证 16×16。
ICON_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


def _wand_quantize_all(
    wand, number_colors: int, dither_index: int, colorspace: str = "srgb", treedepth: int = 0
) -> None:
    """对整个图像序列做共享调色板量化（等价 CLI 的 `-colors`）。

    wand 高层 `Image.quantize` 只作用于当前帧；`MagickQuantizeImages` 才对
    列表内全部帧建立统一调色板，避免逐帧独立调色板导致的闪烁/偏色。

    ``colorspace`` 是量化分桶的色彩空间（IM `-quantize <space>`）：``srgb``
    （默认）/ ``lab`` / ``gray`` / ``transparent`` 等，取值见 wand
    ``COLORSPACE_TYPES``。``treedepth`` 是 octree 树深度（``0`` = 由
    ImageMagick 自动确定，IM `-treedepth`）。
    """
    from wand.api import library
    from wand.image import COLORSPACE_TYPES

    colorspace_index = COLORSPACE_TYPES.index(colorspace)
    library.MagickQuantizeImages.argtypes = [
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.MagickQuantizeImages.restype = ctypes.c_int
    result = library.MagickQuantizeImages(
        ctypes.c_void_p(wand),
        number_colors,
        colorspace_index,
        int(treedepth),
        dither_index,
        0,  # measure_error
    )
    if not result:
        raise RuntimeError("ImageMagick 共享调色板量化失败")

# 序列逐帧 PNG 导出的并行度：PNG 编码是 CPU 密集操作，且编码期间 GIL 会被释放
# （Pillow 的 zlib 压缩段、wand 经 ctypes 调 ImageMagick C 库），因此多线程可以
# 真实利用多核。上限 8，避免核数异常（如容器限制）时过度争抢。
PNG_EXPORT_WORKERS = min(8, max(2, os.cpu_count() or 4))

# 中间帧/缓存 PNG 的压缩级别（用户决策：中间件/缓存不追求体积，速度优先）。
# 实测（30 帧 1920×1080 RGBA，8 线程并行）：level=0（零压缩）241ms/237MB 反而
# 比 level=1 198ms/9.3MB 慢——零压缩会写入约 8MB/帧导致 I/O 瓶颈；level=1
# （zlib 最小压缩）是实测速度最优值，体积仅比 level=6 大 ~45%。
# wand 路径同时显式关掉自适应滤波（png:compression-filter=0），因为默认
# quality=75 的逐扫描线滤波尝试是 ImageMagick PNG 编码耗时的大头。
PNG_CACHE_COMPRESS_LEVEL = 1


def _save_wand_png(image, target: Path) -> None:
    """把独立 wand 图像保存为 PNG（线程池 worker 内执行，只碰自己的实例）。

    缓存帧不追求体积：显式最低压缩级别 + 关闭滤波（默认 quality=75 的
    自适应滤波逐扫描线尝试 5 种滤波，是导出耗时的大头）。
    """
    image.options["png:compression-level"] = str(PNG_CACHE_COMPRESS_LEVEL)
    image.options["png:compression-filter"] = "0"
    image.save(filename=str(target))


def _wand_rgba_bytes(image) -> tuple[bytes, int, int]:
    """C 级读取 wand 图像原始 RGBA 像素字节（CharPixel 存储），返回 (字节, 宽, 高)。

    GIF 解包路径用它替代「wand PNG 编码（make_blob）→ PIL 解码」的双重编码：
    实测 480×270 主循环 40.2ms/帧 → 1.6ms/帧（约 26x），且像素与 PNG blob
    往返完全一致（PNG 无损，两者读到的都是 coalesce 帧的真实 RGBA 值）。
    只读像素、不触碰 alpha 通道（GIF 解包的 alpha 处理见 _format_animated_image
    的注释：coalesce 后的帧已自带正确掩码，不得强制 alpha_channel）。

    2026-08 生态核对：wand 自带 Image.export_pixels(channel_map="RGBA", storage="char")
    内部同样调用 MagickExportImagePixels + ctypes c_ubyte buffer，但其返回
    c_buffer[:size] 切片构造 Python list——大帧（如 956×488）每帧约 186 万元素，
    list 构造开销显著；此处直接 bytes(buffer) 一次 memcpy，故保留手写版。
    """
    from wand.api import library as wlib

    w, h = image.width, image.height
    size = w * h * 4
    buffer = (size * ctypes.c_ubyte)()  # CharPixel = 8-bit 每通道
    ok = wlib.MagickExportImagePixels(image.wand, 0, 0, w, h, b"RGBA", 1, ctypes.byref(buffer))
    if not ok:
        image.raise_exception()
    return bytes(buffer), w, h


def _bmp_dib_bytes(rgba: Image.Image) -> bytes:
    """把 RGBA 图像编码为 ICO 条目的 32bpp BMP DIB（自底向上 BGRA + 全零 AND 掩码）。

    BITMAPINFOHEADER 的 biHeight = 2×高（图像 + AND 掩码两段）；32bpp 的
    alpha 由 DIB 像素直接携带，AND 掩码全零（不参与裁剪）。行宽 4 字节
    天然对齐，无需补位。numpy 批量转 BGRA + 自底向上翻转，避免逐像素循环。
    """
    import struct

    width, height = rgba.size
    array = np.asarray(rgba)  # H×W×4 RGBA
    bgra = array[:, :, [2, 1, 0, 3]]  # R/G/B → B/G/R，A 保留
    pixels = bgra[::-1, :, :].tobytes()  # 自底向上
    header = struct.pack(
        "<IiiHHIIiiII",
        40,                        # biSize
        width,
        height * 2,                # biHeight：图像 + AND 掩码
        1,                         # biPlanes
        32,                        # biBitCount
        0,                         # biCompression = BI_RGB
        width * height * 4,        # biSizeImage（仅像素段）
        0, 0, 0, 0,                # 分辨率/调色板字段
    )
    mask_row = ((width + 31) // 32) * 4
    and_mask = b"\x00" * (mask_row * height)
    return header + pixels + and_mask


def _sample_source_indices(total: int, target: int) -> list[int]:
    """均匀采样：把 target 个输出帧分配到 total 个源帧，返回源帧索引序列。

    - 目标 ≤ 源长度：取前 target 帧（截断，与循环复制方式一致）；
    - 整数倍（target = k·total）：每帧复制 k 次（如 total=4、target=8 →
      [0,0,1,1,2,2,3,3]，即 [abcd] → [aabbccdd]）；
    - 非整数倍：每帧至少复制 base 次，多余 rem 帧**均匀插入**
      （Bresenham 式误差扩散，重复帧尽量分散而非堆在开头/结尾，
      如 total=4、target=6 → [0,1,1,2,3,3]，即 [abcd] → [abbcdd]）。
    """
    if target <= total:
        return list(range(target))
    base, rem = divmod(target, total)
    counts = [base] * total
    # 把 rem 个多余帧均匀插入：位置 = floor((k + 0.5) * total / rem)
    for k in range(rem):
        pos = (2 * k * total + total) // (2 * rem)
        if pos >= total:
            pos = total - 1
        counts[pos] += 1
    result: list[int] = []
    for index, count in enumerate(counts):
        result.extend([index] * count)
    return result
