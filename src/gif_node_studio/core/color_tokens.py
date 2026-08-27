"""颜色令牌引擎：Material 2014 色板 + B-1 OKLCH 等距环 + 派生函数（唯一颜色算法层）。

角色（决策 #111 延伸、#114、#117）：
- ``MATERIAL`` —— Material Design 2014 手工色板（hue → tone → hex）。
  数据源：MUI ``packages/mui-material/src/colors/*.js``（2026-08 抓取，与
  material.io 2014 色板逐值一致）。
- ``OKLCH_ANCHORS`` —— **分类锚点主色板**（决策 #117 方案 B-1）：OKLCH
  等距环 9 业务分类 + 背景低彩度锚点，``coloraide`` 预生成写死，全项目
  分类主色改查这里（生成/优化脚本见 scripts/palette_proposals.py 与
  scripts/optimize_category_mapping.py）。
- ``ColorSpec`` —— 分类锚点表示：``(hue, tone)`` 查 Material 表，或
  ``ColorSpec.ring(key)`` 查 OKLCH_ANCHORS（决策 #114 选型 B 延伸；
  NodeCategory 成员已全部切 ring）。
- 工具函数 —— ``mix``/``rgba``/``hsl`` 等派生运算；所有派生色（图标底板、
  裁剪强调、手柄色等）由锚点经函数算出，不在组件里写死。
- ``Palette``/``DARK`` —— 全局主题参数（背景/边框/文字/强调 + 底板压暗
  比例 + 节点图壳色）；组件色统一读这里（决策 #114 完整主题收编、#117 壳色）。

约定：hex 一律大写 ``#RRGGBB``；``tone`` 接受 ``400`` 或 ``"400"``（A 档
必须字符串 ``"A100"``）。

**唯一写死 hex 处**：仅 ``MATERIAL``、``OKLCH_ANCHORS``、``PORT_ANCHORS``
三个色板表；组件/节点/图标一律查表派生，禁止再写 hex。
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 分类锚点主色板（决策 #117 方案 B-1）：OKLCH 等距环（唯一写死 hex 处之二）
# 生成参数：L=0.74, C=0.155, H=12+36k（k=0..9），coloraide 预生成后写死。
# 映射经穷举优化（scripts/optimize_category_mapping.py）：语义锚定优先——
# 输入=绿、输出=红粉、通道=粉、动效=橙、分析=黄、处理=蓝、序列=紫；
# 预格式化/格式化取「绿→青→蓝」数据流色相渐变（INPUT 156° → PREFORMAT 192°
# → FORMAT 228°），保留输入族流程感。H120° 黄绿 (#9FB835) 未用（备用）。
# 背景（BACKDROP）不参与等距环：低彩度蓝灰锚点（L=0.72, C=0.03, H=230），
# 观感接近原 blueGrey 300 (#90A4AE)，保持「背景分类更低调」。
# ---------------------------------------------------------------------------

OKLCH_ANCHORS: dict[str, str] = {
    "output": "#FC7D91",      # H12  红粉
    "motion": "#F8894B",      # H48  橙
    "analysis": "#D8A100",    # H84  黄
    "input": "#41C780",       # H156 绿
    "preformat": "#00C4BF",   # H192 青绿
    "format": "#00BBF2",      # H228 青
    "process": "#7FA9FF",     # H264 蓝
    "sequence": "#BA92FD",    # H300 紫
    "channel": "#E682D0",     # H336 粉
    "backdrop": "#92A9B4",    # 低彩度蓝灰（非环）
}

# ---------------------------------------------------------------------------
# 端口数据语义色（决策 #118 方案 C）：唯一写死 hex 处之三。
# 暖=素材流（MANIFEST）、冷=序列流（SEQUENCE），L=0.62/C=0.18 统一亮度
# （搜索脚本 scripts/port_color_search.py）。端口圆点与连线同源——管线色
# 在连接建立时取输出端口色（theme.py），不再用库默认橙。
# ---------------------------------------------------------------------------

PORT_ANCHORS: dict[str, str] = {
    "manifest": "#DD503F",   # H30  红橙（素材流）
    "sequence": "#0089EA",   # H250 蓝（序列帧流）
}

# ---------------------------------------------------------------------------
# Material 2014 色板（来源见模块 docstring）
# ---------------------------------------------------------------------------

MATERIAL: dict[str, dict[str, str]] = {
    # amber
    "amber": {
        "100": "#FFECB3",
        "200": "#FFE082",
        "300": "#FFD54F",
        "400": "#FFCA28",
        "50": "#FFF8E1",
        "500": "#FFC107",
        "600": "#FFB300",
        "700": "#FFA000",
        "800": "#FF8F00",
        "900": "#FF6F00",
        "A100": "#FFE57F",
        "A200": "#FFD740",
        "A400": "#FFC400",
        "A700": "#FFAB00",
    },
    # blue
    "blue": {
        "100": "#BBDEFB",
        "200": "#90CAF9",
        "300": "#64B5F6",
        "400": "#42A5F5",
        "50": "#E3F2FD",
        "500": "#2196F3",
        "600": "#1E88E5",
        "700": "#1976D2",
        "800": "#1565C0",
        "900": "#0D47A1",
        "A100": "#82B1FF",
        "A200": "#448AFF",
        "A400": "#2979FF",
        "A700": "#2962FF",
    },
    # blueGrey
    "blueGrey": {
        "100": "#CFD8DC",
        "200": "#B0BEC5",
        "300": "#90A4AE",
        "400": "#78909C",
        "50": "#ECEFF1",
        "500": "#607D8B",
        "600": "#546E7A",
        "700": "#455A64",
        "800": "#37474F",
        "900": "#263238",
        "A100": "#CFD8DC",
        "A200": "#B0BEC5",
        "A400": "#78909C",
        "A700": "#455A64",
    },
    # cyan
    "cyan": {
        "100": "#B2EBF2",
        "200": "#80DEEA",
        "300": "#4DD0E1",
        "400": "#26C6DA",
        "50": "#E0F7FA",
        "500": "#00BCD4",
        "600": "#00ACC1",
        "700": "#0097A7",
        "800": "#00838F",
        "900": "#006064",
        "A100": "#84FFFF",
        "A200": "#18FFFF",
        "A400": "#00E5FF",
        "A700": "#00B8D4",
    },
    # green
    "green": {
        "100": "#C8E6C9",
        "200": "#A5D6A7",
        "300": "#81C784",
        "400": "#66BB6A",
        "50": "#E8F5E9",
        "500": "#4CAF50",
        "600": "#43A047",
        "700": "#388E3C",
        "800": "#2E7D32",
        "900": "#1B5E20",
        "A100": "#B9F6CA",
        "A200": "#69F0AE",
        "A400": "#00E676",
        "A700": "#00C853",
    },
    # orange
    "orange": {
        "100": "#FFE0B2",
        "200": "#FFCC80",
        "300": "#FFB74D",
        "400": "#FFA726",
        "50": "#FFF3E0",
        "500": "#FF9800",
        "600": "#FB8C00",
        "700": "#F57C00",
        "800": "#EF6C00",
        "900": "#E65100",
        "A100": "#FFD180",
        "A200": "#FFAB40",
        "A400": "#FF9100",
        "A700": "#FF6D00",
    },
    # pink
    "pink": {
        "100": "#F8BBD0",
        "200": "#F48FB1",
        "300": "#F06292",
        "400": "#EC407A",
        "50": "#FCE4EC",
        "500": "#E91E63",
        "600": "#D81B60",
        "700": "#C2185B",
        "800": "#AD1457",
        "900": "#880E4F",
        "A100": "#FF80AB",
        "A200": "#FF4081",
        "A400": "#F50057",
        "A700": "#C51162",
    },
    # purple
    "purple": {
        "100": "#E1BEE7",
        "200": "#CE93D8",
        "300": "#BA68C8",
        "400": "#AB47BC",
        "50": "#F3E5F5",
        "500": "#9C27B0",
        "600": "#8E24AA",
        "700": "#7B1FA2",
        "800": "#6A1B9A",
        "900": "#4A148C",
        "A100": "#EA80FC",
        "A200": "#E040FB",
        "A400": "#D500F9",
        "A700": "#AA00FF",
    },
    # red
    "red": {
        "100": "#FFCDD2",
        "200": "#EF9A9A",
        "300": "#E57373",
        "400": "#EF5350",
        "50": "#FFEBEE",
        "500": "#F44336",
        "600": "#E53935",
        "700": "#D32F2F",
        "800": "#C62828",
        "900": "#B71C1C",
        "A100": "#FF8A80",
        "A200": "#FF5252",
        "A400": "#FF1744",
        "A700": "#D50000",
    },
    # teal
    "teal": {
        "100": "#B2DFDB",
        "200": "#80CBC4",
        "300": "#4DB6AC",
        "400": "#26A69A",
        "50": "#E0F2F1",
        "500": "#009688",
        "600": "#00897B",
        "700": "#00796B",
        "800": "#00695C",
        "900": "#004D40",
        "A100": "#A7FFEB",
        "A200": "#64FFDA",
        "A400": "#1DE9B6",
        "A700": "#00BFA5",
    },
}


def material_hex(hue: str, tone: int | str) -> str:
    """查 Material 2014 色板（tone 可为 int 或 "A100" 字符串）。"""
    try:
        return MATERIAL[hue][str(tone)]
    except KeyError as exc:  # 拼写错误/档位越界立刻暴露，不静默回退
        raise KeyError(f"Material 色板无 {hue}/{tone}（见 core.color_tokens.MATERIAL）") from exc


@dataclass(frozen=True)
class ColorSpec:
    """分类锚点：``(hue, tone)`` 查 Material 表，或 ``ring(key)`` 查
    OKLCH_ANCHORS（决策 #114 选型 B 延伸；tone=None = OKLCH_ANCHORS 键）。
    """

    hue: str
    tone: int | str | None = None

    @classmethod
    def ring(cls, key: str) -> "ColorSpec":
        """B-1 OKLCH 等距环锚点（决策 #117）：查 OKLCH_ANCHORS[key]。"""
        return cls(key, None)

    @property
    def hex(self) -> str:
        if self.tone is None:
            try:
                return OKLCH_ANCHORS[self.hue]
            except KeyError as exc:  # 拼写错误立刻暴露，不静默回退
                raise KeyError(
                    f"OKLCH_ANCHORS 无锚点 {self.hue!r}（见 core.color_tokens.OKLCH_ANCHORS）"
                ) from exc
        return material_hex(self.hue, self.tone)


# ---------------------------------------------------------------------------
# 颜色工具（派生运算）
# ---------------------------------------------------------------------------

def parse_hex(hex_color: str) -> tuple[int, int, int]:
    """``#RRGGBB`` → (r, g, b)。"""
    s = hex_color.lstrip("#")
    return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def to_hex(rgb: tuple[int, int, int]) -> str:
    """(r, g, b) → ``#RRGGBB``（大写）。"""
    return "#%02X%02X%02X" % rgb


def mix(a: str, b: str, ratio: float) -> str:
    """a 按 ratio 压向 b（ratio=0 → 纯 a，1 → 纯 b）。派生底板/明度阶梯用。"""
    return to_hex(mix_rgb(parse_hex(a), parse_hex(b), ratio))


def mix_rgb(
    a: tuple[int, int, int], b: tuple[int, int, int], ratio: float
) -> tuple[int, int, int]:
    """RGB 元组版 mix（免两次 hex 转换，给 QPainter 直用）。"""
    return tuple(round(av * (1 - ratio) + bv * ratio) for av, bv in zip(a, b))  # type: ignore[return-value]


def rgba(color: str, alpha: int) -> tuple[int, int, int, int]:
    """hex + alpha → (r, g, b, a)，给 ``QColor``/tint 元组用。"""
    return (*parse_hex(color), alpha)  # type: ignore[return-value]


def hex_to_hsl(hex_color: str) -> tuple[float, float, float]:
    """hex → (h, s, l)；h∈[0,360)，s/l∈[0,1]。整体明度/饱和度变换入口。"""
    r, g, b = (v / 255.0 for v in parse_hex(hex_color))
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = (g - b) / d + (6 if g < b else 0)
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60, s, l


def hsl_to_hex(h: float, s: float, l: float) -> str:
    """(h, s, l) → hex（``hex_to_hsl`` 的逆）。"""
    h = h % 360 / 360
    if s == 0:
        v = round(l * 255)
        return to_hex((v, v, v))
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    rgb = []
    for k in (h + 1 / 3, h, h - 1 / 3):
        if k < 0:
            k += 1
        elif k > 1:
            k -= 1
        if k < 1 / 6:
            c = p + (q - p) * 6 * k
        elif k < 1 / 2:
            c = q
        elif k < 2 / 3:
            c = p + (q - p) * (2 / 3 - k) * 6
        else:
            c = p
        rgb.append(round(c * 255))
    return to_hex(tuple(rgb))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Palette:
    """全局主题参数：组件色与派生参数唯一来源（决策 #114 完整主题收编）。

    组件不再写死 hex（preview/crop/razor/trim 读这里）；未来切亮色主题
    新建一个实例即可，派生函数（``plate`` 等）随主题自动重算。
    """

    name: str = "dark"
    # 基底
    bg: str = "#14161A"  # 画布/预览/条带深色背景（原四处 #14161a 统一）
    border: str = "#444444"
    muted: str = "#8A9099"  # 次要文字/占位
    # 强调/操作色
    danger: str = "#FF4D4D"  # 裁剪边线/剃刀线
    danger_soft: str = "#FF8A8A"  # danger 浅变体（角标小字）
    trim_start: str = "#4DA3FF"  # 起始手柄（蓝）
    trim_end: str = "#FF9A3C"  # 结束手柄（橙）
    checker: tuple[str, str] = ("#232323", "#181818")  # 预览棋盘格（亮格, 暗格）
    # 节点图壳色（决策 #117 方案 A）：画布/网格/节点体/边框/选中描边。
    # 亮度阶梯：画布 < 网格 < 节点体 < 边框（ComfyUI 式三级层次）；选中描边
    # 用白——与全部分类锚点 ΔEOK≥0.29（实测无撞色），节点体上对比 13:1。
    canvas: str = "#191919"       # 画布（比原 #232323 更沉，衬托节点体）
    grid: str = "#2E2E2E"         # 网格线（画布上隐约可见，实测 1.32:1）
    node: str = "#313131"         # 节点体（比画布亮一档）
    node_border: str = "#4E4E4E"  # 节点边框（比节点体亮，轮廓清晰）
    select: str = "#FFFFFF"       # 选中描边（白；替代库默认黄，避开分类色）
    # 派生参数
    plate_ratio: float = 0.12  # 图标底板压暗比例（18px 实渲染对比度定案）

    def plate(self, anchor_hex: str) -> str:
        """图标底板 = 锚点压向 ``bg``（Bforartists 式「着色底+白形」底板）。"""
        return mix(anchor_hex, self.bg, self.plate_ratio)


# 当前主题（深色）。新增主题 = 再实例化一个 Palette。
DARK = Palette()
