"""参数选项的唯一源头（single source of truth）。

一个 ``choice`` 参数的三元组「显示标签 / 机器键 / 实际传参值」历史上散落在
backend 的 ``*_CHOICES`` / ``*_KEYS`` / ``*_MAP`` 三套平行全局与节点内的默认值
中，改一处漏一处（曾发生「兰索斯/Lanczos」标签漂移导致运行时 KeyError）。
本模块用 ``ChoiceGroup`` 把它们绑定为**一个对象**：

- ``labels``          —— 下拉显示文本（也是预设持久化的值）；
- ``key_of(label)``   —— 传给后端处理函数的机器键（后端分支/校验用，稳定）；
- ``value_of(label)`` —— 实际参与处理的传参值（如 PIL ``Resampling`` 枚举）；
- ``labels_of_keys``  —— 互斥规则（``ParamDefinition.enabled_when``）用的标签集合。

定义选项组后，节点声明与执行都只引用该组；修改选项只需改这里一处。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PIL import Image


# 「未提供传参值」哨兵：ChoiceOption 未显式传 value 时默认回落为机器键；
# 显式传 None 的选项（如「自由」纵横比）保留 None，表示「无值」选项。
_NONE_VALUE = object()


@dataclass(frozen=True)
class ChoiceOption:
    """单个下拉选项：显示标签 + 机器键 + 实际传参值。"""

    label: str
    key: str
    value: Any = _NONE_VALUE

    def __post_init__(self) -> None:
        # 未显式给出传参值时，机器键即传参值（后端直接按键分支的选项）；
        # 显式传 None 表示「无值」选项（value 保持 None，如纵横比「自由」）。
        if self.value is _NONE_VALUE:
            object.__setattr__(self, "value", self.key)


@dataclass(frozen=True)
class ChoiceGroup:
    """一个 choice 参数的全部选项（唯一源头），派生标签集/键映射/值映射与默认值。"""

    name: str
    options: tuple[ChoiceOption, ...]
    default: str | None = None  # 默认标签；None = 第一个选项

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError(f"选项组 {self.name} 至少需要一个选项")
        if self.default is None:
            object.__setattr__(self, "default", self.options[0].label)
        elif self.default not in self.labels:
            raise ValueError(f"选项组 {self.name} 的默认标签 {self.default!r} 不在选项内")

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------

    @property
    def labels(self) -> tuple[str, ...]:
        """下拉显示文本（按选项顺序）。"""
        return tuple(option.label for option in self.options)

    @property
    def key_set(self) -> frozenset[str]:
        """全部机器键（后端校验「键是否合法」用）。"""
        return frozenset(option.key for option in self.options)

    @property
    def keys(self) -> dict[str, str]:
        """标签 → 机器键。"""
        return {option.label: option.key for option in self.options}

    @property
    def values(self) -> dict[str, Any]:
        """标签 → 实际传参值。"""
        return {option.label: option.value for option in self.options}

    # ------------------------------------------------------------------
    # 查询方法
    # ------------------------------------------------------------------

    def key_of(self, label: str) -> str:
        """标签 → 机器键（节点 execute 传给后端）。"""
        try:
            return self.keys[label]
        except KeyError:
            raise KeyError(f"未知选项标签 {label!r}（{self.name} 可选：{self.labels}）") from None

    def value_of(self, label: str) -> Any:
        """标签 → 实际传参值（后端处理函数的入参）。"""
        try:
            return self.values[label]
        except KeyError:
            raise KeyError(f"未知选项标签 {label!r}（{self.name} 可选：{self.labels}）") from None

    def value_for_key(self, key: str) -> Any:
        """机器键 → 实际传参值（后端校验并取值用）。"""
        for option in self.options:
            if option.key == key:
                return option.value
        raise KeyError(f"未知选项键 {key!r}（{self.name} 可选键：{sorted(self.key_set)}）")

    def labels_of_keys(self, *keys: str) -> tuple[str, ...]:
        """机器键集合 → 标签集合（``ParamDefinition.enabled_when`` 的允许取值）。"""
        result = tuple(option.label for option in self.options if option.key in keys)
        missing = set(keys) - self.key_set
        if missing:
            raise KeyError(f"选项组 {self.name} 无这些键：{sorted(missing)}")
        return result


# ---------------------------------------------------------------------------
# 具体选项组（唯一源头：改选项只改这里）
# ---------------------------------------------------------------------------

# 序列相加节点：B 序列缩放算法（标签 → 键 → PIL Resampling）。Lanczos 质量最高，
# 作为默认（与 PS「存储为 Web 所用格式」的默认双三次同属高质量重采样）。
RESAMPLE = ChoiceGroup(
    "resample",
    (
        ChoiceOption("最近邻", "nearest", Image.Resampling.NEAREST),
        ChoiceOption("双线性", "bilinear", Image.Resampling.BILINEAR),
        ChoiceOption("双三次", "bicubic", Image.Resampling.BICUBIC),
        ChoiceOption("Lanczos", "lanczos", Image.Resampling.LANCZOS),
    ),
    default="Lanczos",
)

# 序列处理节点：缩放策略。拉伸（不保纵横比，铺满画布）/ 填充（保纵横比放大到铺满，
# 溢出居中裁剪，保证画面填满）/ 适合（保纵横比缩小到完整容纳，未填满区域用
# 全透明黑 rgba(0,0,0,0) 填充，保证画面无裁剪）/ 不缩放（保持原尺寸：单轴超出
# 画布时该轴居中裁剪到画布大小，未超出轴保持原尺寸居中摆放，未覆盖区域透明）。
# 默认「适合」。「不缩放」为「序列叠加」节点新增（用户需求，决策 #94），
# 供像素画素材按 1:1 叠放；序列相加/分辨率统一共享同一下拉，行为一致。
SCALE_STRATEGY = ChoiceGroup(
    "strategy",
    (
        ChoiceOption("拉伸", "stretch"),
        ChoiceOption("填充", "fill"),
        ChoiceOption("适合", "fit"),
        ChoiceOption("不缩放", "none"),
    ),
    default="适合",
)

# 色相/饱和度（选区）节点：编辑目标色域（标签 → 键 → 中心色相度数）。
# 「全图」= PS 全图(Master)（蒙版恒 1）；红/黄/绿/青/蓝/洋红 = PS 预设色域
# 中心（0/60/120/180/240/300，色环六等分）；「自定义(取色器)」中心来自
# 「目标颜色」色块（PS 吸管「把中心色域移到所点颜色」的等效入口，决策 #134）。
HUE_SAT_TARGET = ChoiceGroup(
    "hue_sat_target",
    (
        ChoiceOption("全图", "master", None),
        ChoiceOption("自定义(取色器)", "custom", None),
        ChoiceOption("红色", "red", 0.0),
        ChoiceOption("黄色", "yellow", 60.0),
        ChoiceOption("绿色", "green", 120.0),
        ChoiceOption("青色", "cyan", 180.0),
        ChoiceOption("蓝色", "blue", 240.0),
        ChoiceOption("洋红", "magenta", 300.0),
    ),
    default="全图",
)

# 颜色深度节点（已过时，见关键决策 #76）：降低颜色深度算法（PS「存储为
# Web 所用格式」同款选项；「可选择 Selective」为 Adobe 私有算法，ImageMagick
# 无等价物，不实现）。该节点基于 PS 语义设计，与 IM 原生特性差距较大，
# 不再建议使用——新工作流用 QUANTIZE_COLORSPACE/QUANTIZE_DITHER（颜色量化节点）。
# 保留仅为旧存档兼容；后端 color_reduce_sequence 仍按机器键分支。
COLOR_REDUCE_ALGORITHM = ChoiceGroup(
    "algorithm",
    (
        ChoiceOption("可感知(自适应)", "adaptive"),
        ChoiceOption("随样性(感知)", "perceptual"),
        ChoiceOption("受限(Web)", "restrictive_web"),
        ChoiceOption("灰度", "grayscale"),
        ChoiceOption("黑白", "bw"),
        ChoiceOption("Windows", "windows"),
        ChoiceOption("Mac OS", "macos"),
    ),
    default="可感知(自适应)",
)

# 颜色深度节点（已过时，见关键决策 #76）：仿色算法。后端 color_reduce_sequence
# 直接按机器键分支。
DITHER = ChoiceGroup(
    "dither",
    (
        ChoiceOption("扩散", "diffusion"),
        ChoiceOption("无仿色", "none"),
        ChoiceOption("图案", "pattern"),
        ChoiceOption("杂色", "noise"),
    ),
    default="扩散",
)

# 颜色量化节点（IM 原生，见关键决策 #76）：量化分桶色彩空间，直接映射
# ImageMagick `-quantize <space>`（wand COLORSPACE_TYPES 子集；键 = 枚举名，
# 后端经 COLORSPACE_TYPES.index(key) 取索引）。
# 特殊语义：gray = 先转灰度再量化（等价 `-colorspace gray -colors N`）；
# transparent = 把 Alpha 纳入量化（GIF 1-bit 透明语义，等价 `-quantize transparent`）。
QUANTIZE_COLORSPACE = ChoiceGroup(
    "colorspace",
    (
        ChoiceOption("sRGB", "srgb"),
        ChoiceOption("RGB", "rgb"),
        ChoiceOption("灰度", "gray"),
        ChoiceOption("透明(含Alpha)", "transparent"),
        ChoiceOption("Lab", "lab"),
        ChoiceOption("Luv", "luv"),
        ChoiceOption("LCH", "lch"),
        ChoiceOption("HCL", "hcl"),
        ChoiceOption("YCbCr", "ycbcr"),
        ChoiceOption("HSB", "hsb"),
        ChoiceOption("HSV", "hsv"),
        ChoiceOption("HSL", "hsl"),
        ChoiceOption("HWB", "hwb"),
        ChoiceOption("CMYK", "cmyk"),
        ChoiceOption("XYZ", "xyz"),
        ChoiceOption("Log", "log"),
    ),
    default="sRGB",
)

# 颜色量化节点（IM 原生）：量化仿色方法，直接映射 MagickQuantizeImages 的
# dither 参数（wand DITHER_METHODS 子集，键 = 枚举名）。默认 floyd_steinberg
# 与 IM CLI `-colors` 默认一致；「图案/杂色」等 PS 式仿色不属于 IM 原生，
# 由有序仿色（-ordered-dither）与海报化（-posterize）参数承载。
QUANTIZE_DITHER = ChoiceGroup(
    "dither",
    (
        ChoiceOption("扩散(Floyd-Steinberg)", "floyd_steinberg"),
        ChoiceOption("Riemersma", "riemersma"),
        ChoiceOption("无仿色", "no"),
    ),
    default="扩散(Floyd-Steinberg)",
)

# 画面裁剪节点：裁剪框纵横比锁定。传参值 = 宽/高 比值（None = 自由拖拽）。
# 后端与 overlay 均按该值约束裁剪框（见 CropOverlayWidget 的纵横比逻辑）。
CROP_ASPECT = ChoiceGroup(
    "aspect",
    (
        ChoiceOption("自由", "free", None),
        ChoiceOption("1:1", "1:1", 1.0),
        ChoiceOption("3:2", "3:2", 3.0 / 2.0),
        ChoiceOption("4:3", "4:3", 4.0 / 3.0),
        ChoiceOption("16:10", "16:10", 16.0 / 10.0),
        ChoiceOption("16:9", "16:9", 16.0 / 9.0),
        ChoiceOption("21:9", "21:9", 21.0 / 9.0),

        ChoiceOption("2:3", "2:3", 2.0 / 3.0),
        ChoiceOption("3:4", "3:4", 3.0 / 4.0),
        ChoiceOption("10:16", "10:16", 10.0 / 16.0),
        ChoiceOption("9:16", "9:16", 9.0 / 16.0),
        ChoiceOption("9:21", "9:21", 9.0 / 21.0),
    ),
    default="自由",
)

# 序列长度统一节点：B 短于 A 时的延长方式。
# - 循环复制（现行）：按原序列周期重复（B=[abc]、A 长 5 → [abcab]）；
# - 均匀采样：按目标长度把重复帧均匀分配到各帧（整数倍时每帧均等复制，
#   B=[abcd]、A 长 8 → [aabbccdd]；非整数倍时重复帧尽量均匀，如 → [abbcdd]）。
LENGTH_ALIGN_METHOD = ChoiceGroup(
    "method",
    (
        ChoiceOption("循环复制", "loop"),
        ChoiceOption("均匀采样", "sample"),
    ),
    default="循环复制",
)

# GIF 优化节点（gifsicle 后处理，见关键决策 #78）：优化级别映射 gifsicle
# `-O1/-O2/-O3`（-O1 只存变化区域；-O2 另做透明优化；-O3 多策略择优，
# 不保证缩小、罕见变大）；「无」不传 -O。
GIFSICLE_OPTIMIZE = ChoiceGroup(
    "optimize",
    (
        ChoiceOption("无", "none"),
        ChoiceOption("O1(基础)", "o1"),
        ChoiceOption("O2(透明)", "o2"),
        ChoiceOption("O3(择优)", "o3"),
    ),
    default="O3(择优)",
)

# GIF 优化节点：GIF 级再降色的取色方法（--color-method）。
# diversity（xv 算法，默认）= 从现有颜色取**严格子集**（不产生原图不存在
# 的中间色）；blend-diversity = 对色群取混合色；median-cut = Heckbert 经典
# 中位切割。仅配合 --colors 生效。
GIFSICLE_COLOR_METHOD = ChoiceGroup(
    "color_method",
    (
        ChoiceOption("diversity(严格子集)", "diversity"),
        ChoiceOption("blend-diversity(混合)", "blend-diversity"),
        ChoiceOption("median-cut(中位切割)", "median-cut"),
    ),
    default="diversity(严格子集)",
)

# GIF 优化节点：GIF 级再降色的仿色方法（--dither）。floyd-steinberg 为
# gifsicle 默认；atkinson 为 1.96 新增（局部化图案，适合大片纯色）；
# ro64/o3/o4/o8/ordered 为有序模式（专为避免动画帧间闪烁伪影设计，
# 1.77 起）。仅配合 --colors/--use-colormap 生效。
GIFSICLE_DITHER = ChoiceGroup(
    "dither",
    (
        ChoiceOption("无仿色", "none"),
        ChoiceOption("Floyd-Steinberg", "floyd-steinberg"),
        ChoiceOption("Atkinson", "atkinson"),
        ChoiceOption("ro64", "ro64"),
        ChoiceOption("o3", "o3"),
        ChoiceOption("o4", "o4"),
        ChoiceOption("o8", "o8"),
        ChoiceOption("有序(ordered)", "ordered"),
    ),
    default="Floyd-Steinberg",
)

# GIF 优化节点：固定色板（--use-colormap）。web=内建 216 色 Web-safe 色板；
# gray/bw=灰度/黑白内建色板；自定义文件=文本色板（每行 `r g b` 或
# `#rrggbb`）或 GIF 文件的全局色表；配合 --colors N 可取色板子集。
GIFSICLE_COLORMAP = ChoiceGroup(
    "colormap",
    (
        ChoiceOption("不使用", "none"),
        ChoiceOption("web(216色)", "web"),
        ChoiceOption("灰度(gray)", "gray"),
        ChoiceOption("黑白(bw)", "bw"),
        ChoiceOption("自定义文件", "file"),
    ),
    default="不使用",
)

# GIF 合成(gifski)节点（决策 #124）：速度-质量档位——标准不传参数；
# --fast 快 50% 但质量约降 10%、体积略大；--extra 慢 50% 但质量略升。
GIFSKI_FAST_MODE = ChoiceGroup(
    "fast_mode",
    (
        ChoiceOption("标准", "normal"),
        ChoiceOption("快速(快50%)", "fast"),
        ChoiceOption("精细(慢50%)", "extra"),
    ),
    default="标准",
)

# 平移滚动节点（动效处理）：平移方向——画面内容向该方向滚动，
# 被推出画布一侧的像素从对侧绕回（无缝衔接）。
PAN_DIRECTION = ChoiceGroup(
    "direction",
    (
        ChoiceOption("向上", "up"),
        ChoiceOption("向下", "down"),
        ChoiceOption("向左", "left"),
        ChoiceOption("向右", "right"),
    ),
    default="向右",
)

# 平移滚动节点：速度曲线——第 k 帧进度 p = k/持续帧数，位移 = curve(p) ×
# 画面宽（左右）/ 高（上下），整段动画恰好走满一个画面循环（首尾无缝）。
# - linear：匀速（位移 ∝ p）；
# - accelerate：线性加速（p²，加速度恒定，越滚越快）；
# - decelerate：线性减速（1−(1−p)²，减速度恒定，越滚越慢）；
# - win10_decelerate：Win10/Fluent 动效官方减速曲线
#   cubic-bezier(0.1, 0.9, 0.2, 1.0)（起步快、平滑减速至停）。
SPEED_CURVE = ChoiceGroup(
    "curve",
    (
        ChoiceOption("匀速", "linear"),
        ChoiceOption("线性加速", "accelerate"),
        ChoiceOption("线性减速", "decelerate"),
        ChoiceOption("win10减速", "win10_decelerate"),
    ),
    default="匀速",
)

# 平移滚动节点：亚像素位移（offset 为小数）的插值方式。
# - bilinear：双线性插值，运动平滑（视频/动效常规观感）；
# - nearest：位移取整，像素边缘锐利（像素画风素材）。
PAN_INTERPOLATION = ChoiceGroup(
    "interpolation",
    (
        ChoiceOption("双线性", "bilinear"),
        ChoiceOption("最近邻", "nearest"),
    ),
    default="双线性",
)

# 帧差静止保持节点（序列处理，见关键决策 #96）：静止像素判定的参考帧。
# - prev（前一帧，默认）：参考 = 上一帧的「保持后」结果（流式因果，
#   内容变化后自动恢复静止判定，噪声不回流）；
# - first（首帧）：参考 = 首帧的「保持后」结果（静止背景整体统一到首帧；
#   内容一旦变化，该区域与首帧恒不同，将永久视为运动，不再被保持）。
STATIC_HOLD_REFERENCE = ChoiceGroup(
    "reference",
    (
        ChoiceOption("前一帧", "prev"),
        ChoiceOption("首帧", "first"),
    ),
    default="前一帧",
)

# 帧冻结节点（序列处理）：冻结首帧/末帧，把边界帧的静态内容延长若干帧。
# - first（第一帧，默认）：在序列开头插入 K 份首帧副本（首帧定格在开头）；
# - last（最后一帧）：在序列末尾追加 K 份末帧副本（末帧定格在结尾）。
FREEZE_END = ChoiceGroup(
    "end",
    (
        ChoiceOption("第一帧", "first"),
        ChoiceOption("最后一帧", "last"),
    ),
    default="第一帧",
)

# GIF 合成(FFmpeg)节点（PyAV 进程内 palettegen/paletteuse，见关键决策 #100）：
# palettegen 的 stats_mode（机器键 = ffmpeg 滤镜参数值）。
# - full（默认）：对**整段序列**统计颜色，生成全局共享调色板——与项目
#   MagickQuantizeImages 共享调色板防闪烁哲学一致（区别于 gifski 的每帧
#   本地色表）；
# - diff：只统计相邻帧差异区域的颜色（运动区域主导，静止帧颜色不被稀释）；
# - single：只取第一帧统计。
FFMPEG_STATS_MODE = ChoiceGroup(
    "stats_mode",
    (
        ChoiceOption("整段统计(full)", "full"),
        ChoiceOption("帧间差异(diff)", "diff"),
        ChoiceOption("单帧(single)", "single"),
    ),
    default="整段统计(full)",
)

# 画面翻转节点（一般处理）：翻转方向。键直接映射 PIL ImageOps 的转置操作
# （horizontal → ImageOps.mirror = 左右镜像；vertical → ImageOps.flip = 上下翻转），
# 几何变换逐像素搬运，Alpha 随像素正确翻转。
FLIP_DIRECTION = ChoiceGroup(
    "direction",
    (
        ChoiceOption("水平", "horizontal"),
        ChoiceOption("垂直", "vertical"),
    ),
    default="水平",
)

# GIF 合成(FFmpeg)节点：paletteuse 的仿色方法（机器键 = ffmpeg 滤镜参数值，
# 直接拼进滤镜参数串）。floyd_steinberg 与项目「颜色量化」默认一致；
# sierra2_4a 为 ffmpeg paletteuse 默认。⚠️ FS 系误差扩散对相同帧输出不同
# 图案（决策 #96 实测「变化敏感」）——录屏冻结工作流配 none 或确定性
# 有序仿色 bayer（bayer_scale 调图案粒度）。
FFMPEG_DITHER = ChoiceGroup(
    "dither",
    (
        ChoiceOption("Floyd-Steinberg", "floyd_steinberg"),
        ChoiceOption("无仿色(none)", "none"),
        ChoiceOption("Atkinson", "atkinson"),
        ChoiceOption("Sierra2-4A", "sierra2_4a"),
        ChoiceOption("Bayer(有序)", "bayer"),
        ChoiceOption("Heckbert", "heckbert"),
    ),
    default="Floyd-Steinberg",
)
