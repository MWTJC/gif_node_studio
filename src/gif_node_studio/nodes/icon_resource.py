"""节点图标：qtawesome 叠加图标构建 + 分类色解析（决策 #111，替代 #110 自绘 SVG）。

图标 = 底层分类色实心方块（``fa6s.square``）+ 上层白色字形（qtawesome 多层
堆叠）。颜色方案唯一源头是 ``NodeCategory``（``color`` + ``icon_options``，
整个 options 参数都在分类上统一维护）；本模块只提供：

- ``PLACEHOLDER_GLYPH`` —— 上层图样未敲定前的统一占位字形（#111：由用户自行
  推敲，节点定义处把 ``category_icon(category)`` 改成
  ``category_icon(category, "fa6s.xxx")`` 即可逐节点替换）；
- ``category_icon(category, glyph)`` —— 按分类构建叠加图标 QIcon；
- ``category_color(category)`` —— 分类主色转 (r,g,b)，供标题栏分类色条 QPainter。
"""

from __future__ import annotations

import qtawesome as qta
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ..core.color_tokens import OKLCH_ANCHORS
from .definitions import NodeCategory

# 上层图样未敲定前的统一占位字形（决策 #111：由用户自行推敲，节点定义处一行替换）。
PLACEHOLDER_GLYPH = "fa6s.folder"

# 底板字形：分类色实心方块（qta 堆叠的底层）。
_TILE_GLYPH = "fa6s.square-full"


def category_icon(category: NodeCategory, glyph: str = PLACEHOLDER_GLYPH) -> QIcon:
    """分类图标：底层=分类色方块 + 上层=白形 glyph（qtawesome 多层堆叠）。

    options 直接取自 ``category.icon_options``（整个 options 参数，含底板色与
    白形样式）——上层图样（scale_factor/offset/配色）在 NodeCategory 统一维护，
    节点定义处只写本函数一行。

    前置守卫：qtawesome 要求 QApplication 已建立（字体注册），无应用时其
    ``icon()`` 静默返回空图标且把全局单例的字符表加载成空（charmap 永久缺失，
    之后任何图标调用都抛 “Invalid font prefix”）。这里把这种难以排查的破坏
    变成明确报错——本函数只能在 QApplication 建立后调用（app 引导顺序保证；
    背景框定义因此惰性构造，见 backdrop.backdrop_definition）。
    """
    if QApplication.instance() is None:
        raise RuntimeError(
            "category_icon 需要 QApplication（qtawesome 字体注册）——"
            "不能在模块导入期/无应用时调用；节点定义请放在实例化期，"
            "模块级定义（如背景框）请惰性构造。"
        )
    return qta.icon(_TILE_GLYPH, glyph, options=list(category.icon_options))


def category_color(category: NodeCategory) -> tuple[int, int, int]:
    """分类主色转 (r,g,b)，供标题栏分类色条 QPainter 绘制（读 NodeCategory.color）。"""
    hexc = getattr(category, "color", OKLCH_ANCHORS["backdrop"])
    return tuple(int(hexc[i : i + 2], 16) for i in (1, 3, 5))
