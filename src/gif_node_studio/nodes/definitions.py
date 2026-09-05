"""节点/参数/端口定义：纯声明层（Qt 对象仅作持有，不直接绘制）。

- PortType / NodeCategory / PortDefinition —— 端口与分类枚举；
- ParamDefinition 及全部参数子类 —— 节点参数声明（isinstance 分派面板控件）；
- NodeDefinition —— 节点完整定义（kind/title/category/icon/端口/参数），
  并在构造期做 enabled_when 互斥规则一致性校验。

``NodeDefinition.icon`` 持有一个 Qt ``QIcon``（qtawesome 叠加图标，构建见
``icon_resource.category_icon``）——图标从节点的单一定义出发，节点标题栏
（``StudioNodeItem``）与节点库（``LibraryButton``）都只读它（决策 #111）。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

from PySide6.QtGui import QIcon

from ..core.color_tokens import ColorSpec
from ..core.options import ChoiceGroup

class PortType(str, Enum):
    MANIFEST = "manifest"
    SEQUENCE = "sequence"




class NodeCategory(str, Enum):
    """节点分类：值=分类显示名；成员自带分类主色与叠加图标 options（唯一颜色源头）。

    每类携带：
    - ``spec`` —— 分类锚点 ``ColorSpec(hue, tone)``（查 Material 2014 色板，
      决策 #114 选型 B：不再写死 hex；换档位/换色相改一处）。
    - ``color`` —— 锚点查表后的分类主色（``spec.hex``，Material 400 系为主；
      深色节点图上可辨）。标题栏分类色条与图标底板同源：
      ``icon_resource.category_color`` 只读这里，不再维护平行色表
      （决策 #111 替代 #110 的 CATEGORY_COLORS）。
    - ``icon_options`` —— 叠加图标 ``qta.icon`` 的**整个 options 参数**（底板色
      + 白形样式），统一维护类型颜色方案；节点定义处只写
      ``category_icon(category, glyph)``（见 icon_resource），上层图样
      （scale_factor/offset/配色）改这里即可全局生效。
    """

    INPUT = ("输入", ColorSpec.ring("input"))
    PREFORMAT = ("预格式化", ColorSpec.ring("preformat"))
    # FORMAT/PREFORMAT/INPUT 取 OKLCH 等距环相邻三点（156°→192°→228°），
    # 「绿→青绿→青」数据流色相渐变（决策 #117 方案 B-1，见 OKLCH_ANCHORS 注释）。
    FORMAT = ("格式化", ColorSpec.ring("format"))
    SEQUENCE = ("序列处理", ColorSpec.ring("sequence"))
    PROCESS = ("画面处理", ColorSpec.ring("process"))
    MOTION = ("动效处理", ColorSpec.ring("motion"))
    CHANNEL = ("通道处理", ColorSpec.ring("channel"))
    OUTPUT = ("输出", ColorSpec.ring("output"))
    ANALYSIS = ("分析", ColorSpec.ring("analysis"))
    # BACKDROP 用低彩度蓝灰锚点（非等距环，更低调，观感接近原 blueGrey 300）。
    BACKDROP = ("背景", ColorSpec.ring("backdrop"))

    spec: ColorSpec
    color: str
    icon_options: tuple[dict, ...]

    def __new__(cls, value: str, spec: ColorSpec) -> "NodeCategory":
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.spec = spec
        obj.color = spec.hex  # 查 Material 2014 色板（大写 #RRGGBB）
        # 叠加图标 options：底层=分类主色实心方块，上层=白形缩小居中。
        #  改用实心方块，色值不再压暗——分类主色即底板色，见 #111。）
        obj.icon_options = (
            {"color": obj.color},
            {"color": "white", "scale_factor": 0.8},
        )
        return obj


@dataclass(frozen=True)


class PortDefinition:
    name: str
    type: PortType
    show_name: bool = False  # 是否在节点上显示端口名；多输入/输出节点置 True 便于区分端口


@dataclass(frozen=True)


class ParamDefinition:
    """参数定义基类：具体参数类型由子类表达（不再用 kind 字符串区分）。

    子类（IntParam/FloatParam/BoolParam/ChoiceParam/FileParam 族）各自携带
    类型专属字段，面板按 ``isinstance`` 分派控件。公共字段：
    - ``name`` —— 参数键（预设持久化、execute 取参、crop_overlay 接管均按此）；
    - ``label`` —— 面板行标签；
    - ``default`` —— 默认值（类型由具体子类约定，基类保持 Any）；
    - ``enabled_when`` —— 互斥启用规则。
    """

    name: str
    label: str
    default: Any = None
    enabled_when: tuple[str, tuple[str, ...]] | None = None
    # 互斥启用规则 (依赖参数名, 允许取值集合)：当依赖参数的值不在集合内时，
    # 该参数对应的控件在面板中置灰（disabled），避免用户调整无效参数
    # （如黑白模式下颜色数、仿色算法非「图案」时的阈值图/均匀色阶）。


@dataclass(frozen=True)


class IntParam(ParamDefinition):
    """整数数值参数：有范围时默认滑条+数值框；``widget="spin"`` 时仅数值框。"""

    minimum: int | None = None
    maximum: int | None = None
    widget: str = ""  # "" = 默认（有范围时 slider+数值框）；"spin" = 仅数值框（如截取持续时长/帧数）


@dataclass(frozen=True)


class FloatParam(ParamDefinition):
    """浮点数值参数：有范围时默认滑条+数值框；``widget="spin"`` 时仅数值框。"""

    minimum: float | None = None
    maximum: float | None = None
    widget: str = ""  # "" = 默认（有范围时 slider+数值框）；"spin" = 仅数值框（如截取持续时长/帧数）


@dataclass(frozen=True)


class BoolParam(ParamDefinition):
    """布尔参数（面板显示为勾选框）。"""


@dataclass(frozen=True)


class ColorParam(ParamDefinition):
    """颜色参数：面板显示色块按钮，点击弹出 ``QColorDialog`` 选取颜色。

    值以 ``'#rrggbb'`` 十六进制字符串持久化/传参（预设序列化友好），
    由 ``ColorPickerWidget`` 提供控件。
    """


@dataclass(frozen=True)
class ChoiceParam(ParamDefinition):
    """下拉选择参数：``choices`` 手写选项元组，或提供 ``options=ChoiceGroup`` 作为唯一源头。

    提供 ``options`` 时：``choices``/``default`` 一律由选项组派生，无需再手写
    平行列表与默认值；任何标签漂移（默认值不在选项内、``choices`` 与选项组
    标签不一致）构造即报错。
    """

    choices: tuple[str, ...] = ()
    options: ChoiceGroup | None = None
    # choice 参数的选项唯一源头（options.py ChoiceGroup）：提供后
    # choices/default 一律由它派生，无需再手写平行列表与默认值。

    def __post_init__(self) -> None:
        # 提供 options 时：choices/default 由选项组派生；
        # 任何标签漂移（默认值不在选项内、choices 与选项组标签不一致）构造即报错。
        if self.options is not None:
            if not self.choices:
                object.__setattr__(self, "choices", self.options.labels)
            elif self.choices != self.options.labels:
                raise ValueError(
                    f"参数 {self.name}: choices 与 options.labels 不一致（{self.choices} vs {self.options.labels}）"
                )
            if self.default is None:
                object.__setattr__(self, "default", self.options.default)
        # 通用一致性校验（手写 choices 的 choice 参数同样受益）：
        # 默认值必须落在选项内，杜绝「改了一处漏了另一处」的漂移。
        # （enabled_when 的允许取值属于**被依赖参数**的选项，需在 NodeDefinition
        #  层校验——那里才有全部参数的上下文。）
        if self.choices:
            if self.default not in self.choices:
                raise ValueError(f"参数 {self.name}: 默认值 {self.default!r} 不在选项 {self.choices} 内")


@dataclass(frozen=True)
class TakeoverParam(ParamDefinition):
    """接管型参数声明：面板不生成常规行，由复合控件接管（序列剃刀/可视化裁剪等）。

    ``ParameterPanel`` 对 ``TakeoverParam`` 子类统一处理（不再按面板构造标志
    特判，见决策 #109）：接管 ``owned`` 参数的值读写、保留 ``linked`` 参数的
    常规行并与控件联动、按 ``data_source`` 声明外部数据需求。

    - ``owned`` —— 本控件接管（不生成常规行）的参数名；值由控件统一读写
      （控件契约：``values()`` / ``set_values(dict)`` / ``changed`` 信号）；
    - ``linked`` —— 保留常规行但需与本控件联动的参数名（如裁剪纵横比下拉）；
    - ``data_source`` —— 外部数据源需求：
      * ``"sequence_frames"`` —— 上游序列全帧（ui 经 ``panel.feed_sequence_frames`` 喂入）；
      * ``"first_frame"`` —— 上游序列首帧/清单预览（ui 按预览路径喂入）；
      * ``None`` —— 无需外部数据；
    - ``widget_factory`` —— 控件工厂 ``callable(panel, param) -> QWidget``，
      由控件侧模块提供（定义处不 import Qt，保持声明层纯净）。

    ``default=None`` 的接管声明（纯声明，无自身值，如可视化裁剪）不进入
    ``default_params()`` / 节点模型属性；有默认值的（如剃刀切割下标）照常
    参与属性与存档，值类型与普通参数一致。
    """

    owned: tuple[str, ...] = ()
    linked: tuple[str, ...] = ()
    data_source: str | None = None
    widget_factory: Any = None

    def make_widget(self, panel):
        """构造接管控件（控件工厂由参数声明提供，Qt 侧模块定义）。"""
        if self.widget_factory is None:
            raise NotImplementedError(f"{type(self).__name__} 未声明 widget_factory")
        return self.widget_factory(panel, self)


@dataclass(frozen=True)
class RazorCutParam(TakeoverParam):
    """序列剃刀切割参数：面板用胶片条（RazorStripPanel）接管，值仍为 int（切割下标）。

    构造示例：``RazorCutParam("cut", "切割帧", default=1, minimum=1, maximum=1000000,
    owned=("cut",), data_source="sequence_frames", widget_factory=make_razor_panel)``
    """

    minimum: int | None = None
    maximum: int | None = None


@dataclass(frozen=True)
class TrimRangeParam(TakeoverParam):
    """序列截取区间参数：面板用双手柄胶片条（TrimStripPanel）接管 start/end。

    与 ``CropOverlayParam`` 同款「纯声明 + 常规参数承载值」模式：``start``/``end``
    仍由常规 ``IntParam`` 声明（默认值/存档兼容不变），本接管声明（``default=None``）
    不进入参数字典——面板跳过被接管参数的常规行，由双手柄控件统一读写区间。

    构造示例：``TrimRangeParam("trim_range", "区间截取", owned=("start", "end"),
    data_source="sequence_frames", widget_factory=make_trim_panel)``
    """


@dataclass(frozen=True)
class CropOverlayParam(TakeoverParam):
    """可视化裁剪接管声明：接管 ``left/top/right/bottom`` 四参数，保留纵横比
    下拉（``linked``）常规行并与裁剪框联动。纯声明（``default=None``），不进入
    节点模型属性。
    """


@dataclass(frozen=True)
class PanelSpec:
    """面板显示/装饰声明（与 Qt 无关）：替代 ParameterPanel 构造标志（决策 #109）。

    - ``scrub_frames`` —— 预览区加可拖动帧滑条（不承载参数值）；
    - ``preview_1to1`` —— 预览框按素材原始像素 1:1（框 = 物理像素 ÷ 当前 DPR）；
    - ``preview_bg_param`` —— 该 bool 参数变化只刷新预览框背景色，不触发运行；
    - ``export_enabled`` —— 面板显示「导出…」按钮。
    """

    scrub_frames: bool = False
    preview_1to1: bool = False
    preview_bg_param: str | None = None
    export_enabled: bool = False


@dataclass(frozen=True)


class FileParam(ParamDefinition):
    """文件/路径选择参数基类：对话框行为由子类的 ``dialog``/``filter`` 类常量决定。

    - ``dialog``：``"open"`` 打开文件 | ``"directory"`` 选择目录 | ``"save"`` 保存路径；
    - ``filter``：文件对话框过滤器文本（``"open"``/``"save"`` 使用）。
    """

    dialog: ClassVar[str] = "open"
    filter: ClassVar[str] = "所有文件 (*)"


@dataclass(frozen=True)


class VideoFileParam(FileParam):
    """打开视频文件（mp4/mkv/mov/webm/avi）。"""

    filter: ClassVar[str] = "视频 (*.mp4 *.mkv *.mov *.webm *.avi)"


@dataclass(frozen=True)


class ImageFileParam(FileParam):
    """打开图片文件（png/jpg/jpeg/webp/bmp）。"""

    filter: ClassVar[str] = "图片 (*.png *.jpg *.jpeg *.webp *.bmp)"


@dataclass(frozen=True)


class GifFileParam(FileParam):
    """打开 GIF 文件。"""

    filter: ClassVar[str] = "GIF (*.gif)"


@dataclass(frozen=True)


class IcoFileParam(FileParam):
    """打开 ICO 图标文件（Windows 图标容器，内含多个分辨率图像）。"""

    filter: ClassVar[str] = "ICO (*.ico)"


@dataclass(frozen=True)


class DirectoryParam(FileParam):
    """选择目录。"""

    dialog: ClassVar[str] = "directory"


@dataclass(frozen=True)


class SaveGifParam(FileParam):
    """保存 GIF 路径（导出对话框）。"""

    dialog: ClassVar[str] = "save"
    filter: ClassVar[str] = "GIF (*.gif)"


@dataclass(frozen=True)


class PaletteFileParam(FileParam):
    """gifsicle 固定色板文件（GIF 优化节点）：文本色板（每行 ``r g b`` 或
    ``#rrggbb``）或 GIF 文件的全局色表。"""

    filter: ClassVar[str] = "色板文件 (*.txt *.gif);;文本色板 (*.txt);;GIF (*.gif)"


@dataclass(frozen=True)


class NodeDefinition:
    kind: str
    title: str
    category: NodeCategory
    icon: QIcon
    inputs: tuple[PortDefinition, ...] = ()
    outputs: tuple[PortDefinition, ...] = ()
    params: tuple[ParamDefinition, ...] = ()
    # 面板显示/装饰声明（scrub 滑条/1:1/透明背景/导出按钮）；接管型组件
    # 由 params 里的 TakeoverParam 声明，面板按类型统一处理（决策 #109）。
    panel: PanelSpec = PanelSpec()

    def __post_init__(self) -> None:
        # 互斥规则（enabled_when）一致性校验：被依赖参数必须存在，且允许取值
        # 集合 ⊆ 被依赖参数的选项——这里才有全部参数的上下文。任何漂移
        # （如选项组改名后互斥规则没跟着改）在节点声明时立刻报错。
        by_name = {parameter.name: parameter for parameter in self.params}
        for parameter in self.params:
            if parameter.enabled_when is None:
                continue
            watch, allowed = parameter.enabled_when
            watched = by_name.get(watch)
            if watched is None:
                raise ValueError(f"参数 {parameter.name}: enabled_when 依赖的参数 {watch!r} 不存在")
            if isinstance(watched, ChoiceParam) and watched.choices:
                extra = set(allowed) - set(watched.choices)
                if extra:
                    raise ValueError(
                        f"参数 {parameter.name}: enabled_when 引用了依赖参数 {watch!r} "
                        f"的未知选项 {sorted(extra)}（可用：{watched.choices}）"
                    )

    def default_params(self) -> dict[str, Any]:
        return {
            parameter.name: deepcopy(parameter.default)
            for parameter in self.params
            # 接管声明且无自身值（default=None，如可视化裁剪）不进入参数字典；
            # 有默认值的接管声明（如剃刀切割下标）照常参与属性/存档/执行取参。
            if not (isinstance(parameter, TakeoverParam) and parameter.default is None)
        }
