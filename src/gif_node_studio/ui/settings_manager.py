"""设置管理器（QSettings，INI 文件存储于用户数据目录）与设置对话框。

- ``SettingsManager``：QSettings（``Format.IniFormat``）封装的读写入口，
  设置文件固定为**用户数据目录** ``settings.ini``（开发=包目录，打包=
  ``%LOCALAPPDATA%\\Ghooost\\GIF Node Studio``，与 ``paths.user_data_dir()``
  一致，见关键决策 #84——程序目录对非管理员只读）；构造时可传入自定义
  路径（测试用）。
- 设定项：
  * 颜色主题：**固定为深色**（关键决策 #90，暂无细化亮色主题的计划）；
    设置页仅只读展示「深色」（下拉置灰，不可调整），``theme()`` 恒返回
    ``dark``，忽略存储值——旧 settings.ini 的 ``light``/``system`` 不再生效；
  * 连线样式（``view/pipe_style``）与背景网格（``view/grid_mode``）：
    由工具栏「连线样式」「背景网格」按钮切换，**不在设置界面显示**，
    但每次切换后自动保存到设置（应用启动时恢复）；
  * 自动保存间隔（``autosave/interval_min``，分钟，0=关闭，默认 10 分钟
    开启）：方案有未保存更改时按此间隔把当前方案自动保存到设置文件旁的
    ``autosave.json``；程序正常退出清理该文件，异常退出残留 → 下次启动
    弹窗提示恢复（见关键决策 #131）；
  * 文件对话框起始目录记忆（``dialog/export_dir`` / ``dialog/import_dir``）：
    导出保存框记住上次导出目录、输入/打开文件行记住上次导入目录，
    下次对话框直接落到记忆目录（不记忆则回落程序当前目录）。与连线样式/
    背景网格同类——**不在设置界面显示**，仅存取（见关键决策 #133）。
- ``SettingsDialog``：固定尺寸对话框，QTabWidget 切换「设置」/「关于」页；
  「关于」页为 QScrollArea（内容超高时纵向滚动），头部应用图标 + 名称 +
  版本，后端小节带各自图标（``img_resource.qrc`` 编译资源）；
  「重置设置」恢复默认（深色主题、网格线背景、曲线连线样式、自动保存
  10 分钟开启）并发出 ``reset_requested`` 信号，由主窗口重新应用连线/网格样式。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import av
from PySide6 import QtCore, QtGui, QtWidgets

# 资源注册（qInitResources）：关于页图标经 :/ico/* 读取（build_src/img_resource.qrc
# 编译进 img_resource_rc.py）。该模块只 import PySide6.QtCore，属于轻量模块，
# 任意入口（含 --test / 测试，不经 splash）都能保证图标可用。
from .. import img_resource_rc  # noqa: F401
from ..media.gifsicle import configure_gifsicle
from ..media.gifski import configure_gifski
from ..media.imagemagick import configure_imagemagick
from ..core.paths import APP_NAME, user_data_dir

# 关于页图标资源（build_src/img_resource.qrc 中 ico/ 下；见 splash.LOGO_RESOURCE）。
APP_ICON_RESOURCE = ":/ico/app_icon.ico"
WAND_ICON_RESOURCE = ":/ico/wand.png"
GIFSICLE_ICON_RESOURCE = ":/ico/gifsicle.gif"
GIFSKI_ICON_RESOURCE = ":/ico/gifski.png"
PYAV_ICON_RESOURCE = ":/ico/pyav.png"

# ---------------------------------------------------------------------------
# 设定项取值
# ---------------------------------------------------------------------------
# 一组设置 = 一个 SettingGroup（唯一源头，镜像 options.ChoiceGroup 的思路）：
# 「显示标签 / 存储值 / 运行时载荷」绑定为一个对象，派生下拉 choices、
# 合法取值集合 value_set 与默认值。新增/改名设置项只改组定义一处，
# 校验、对话框、重置、应用函数全部由组派生——不再维护多套平行全局变量
# （曾出现「主题/透明背景色」的常量、CHOICES 元组、getter/setter 校验元组、
# apply 映射四处分列，改一处漏一处）。


@dataclass(frozen=True)
class SettingOption:
    """单个设置项：显示标签 + 存储值（QSettings 字符串键）+ 运行时载荷。"""

    label: str
    value: str
    payload: Any = None  # 运行时载荷（如 Qt 枚举）；None = 存储值即载荷


@dataclass(frozen=True)
class SettingGroup:
    """一组设置项（唯一源头），派生下拉选项、合法取值集合与默认值。"""

    name: str
    options: tuple[SettingOption, ...]
    default: str | None = None  # 默认存储值；None = 第一个选项的 value

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError(f"设置组 {self.name} 至少需要一个选项")
        if self.default is None:
            object.__setattr__(self, "default", self.options[0].value)
        elif self.default not in self.value_set:
            raise ValueError(f"设置组 {self.name} 的默认值 {self.default!r} 不在选项内")

    @property
    def choices(self) -> tuple[tuple[str, str], ...]:
        """下拉选项 (显示标签, 存储值)，按声明顺序。"""
        return tuple((option.label, option.value) for option in self.options)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(option.label for option in self.options)

    @property
    def value_set(self) -> frozenset[str]:
        """合法存储值集合（getter/setter 校验用）。"""
        return frozenset(option.value for option in self.options)

    def value_of(self, label: str) -> str:
        """显示标签 → 存储值。"""
        for option in self.options:
            if option.label == label:
                return option.value
        raise KeyError(f"设置组 {self.name} 无标签 {label!r}（可用：{self.labels}）")

    def label_of(self, value: str) -> str:
        """存储值 → 显示标签（value_of 的反向）。"""
        for option in self.options:
            if option.value == value:
                return option.label
        raise KeyError(f"设置组 {self.name} 无存储值 {value!r}（可用：{sorted(self.value_set)}）")

    def payload_of(self, value: str) -> Any:
        """存储值 → 运行时载荷（如 Qt 枚举）；非法值抛 KeyError。"""
        for option in self.options:
            if option.value == value:
                return option.payload if option.payload is not None else option.value
        raise KeyError(f"设置组 {self.name} 无存储值 {value!r}（可用：{sorted(self.value_set)}）")


# 颜色主题（QSettings 中存储的字符串键；载荷 = Qt 颜色方案枚举）。
# 主题固定为深色后（决策 #90），UI 只展示「深色」一项；浅色/跟随系统保留在
# 词汇表内（apply_color_scheme 的 payload 映射源头 + 兼容别名不漂移），
# 但 theme() 恒返回 default，存储值不再被读取。
THEME = SettingGroup(
    "theme",
    (
        SettingOption("浅色", "light", QtGui.Qt.ColorScheme.Light),
        SettingOption("深色", "dark", QtGui.Qt.ColorScheme.Dark),
        SettingOption("跟随系统", "system", QtGui.Qt.ColorScheme.Unknown),  # Unknown = 跟随系统
    ),
    default="dark",
)

# 透明背景显示选项（1:1 查看节点的「透明背景」勾选后使用的预览框背景；
# 绿幕/品红的存储值即 CSS 色值，直接用于预览框背景样式；「棋盘格」为
# PS 式透明底纹，由面板绘制（CheckerPreviewLabel），存储值 "checker" 仅作
# 选项标识，不是 CSS 色值）
ALPHA_BG = SettingGroup(
    "alpha_bg",
    (
        SettingOption("绿幕色", "#00FF00"),
        SettingOption("品红色", "magenta"),
        SettingOption("棋盘格", "checker"),
    ),
    default="checker",
)

# ---------------------------------------------------------------------------
# 缓存管理设置（自由值，不走 SettingGroup——那是枚举类选项的唯一源头）
# ---------------------------------------------------------------------------
# - cache/dir：临时缓存根目录。默认 = 用户数据目录 cache/（与 MainWindow
#   现状一致；开发=包目录，打包=%LOCALAPPDATA%，见 paths.user_data_dir）；
#   可在设置对话框修改，下次启动生效。
# - cache/limit_mb：缓存总大小上限（MB）。默认 4 GB，钳制到 [64, 102400]；
#   超限时自动淘汰最旧中间缓存（见 backend.enforce_cache_limit）。
DEFAULT_CACHE_DIR = str(user_data_dir() / "cache")
CACHE_LIMIT_MIN_MB = 64
CACHE_LIMIT_MAX_MB = 102400
DEFAULT_CACHE_LIMIT_MB = 4096

# 自动保存间隔（分钟；0 = 关闭）。默认 10 分钟开启（用户需求）：方案有未保存
# 更改（dirty）时按此间隔把当前方案自动保存到设置文件旁的 autosave.json；
# 程序正常退出清理自动保存文件，异常退出残留 → 下次启动弹窗提示恢复。
AUTOSAVE_MIN_MIN = 0
AUTOSAVE_MAX_MIN = 120
DEFAULT_AUTOSAVE_INTERVAL_MIN = 10

# 兼容别名：均由选项组派生（非平行定义）——组内改名时这里在导入期抛 KeyError，
# 立刻暴露漂移而不是静默沿用旧值。
THEME_LIGHT = THEME.value_of("浅色")
THEME_DARK = THEME.value_of("深色")
THEME_SYSTEM = THEME.value_of("跟随系统")
THEME_CHOICES = THEME.choices
ALPHA_BG_GREEN = ALPHA_BG.value_of("绿幕色")
ALPHA_BG_MAGENTA = ALPHA_BG.value_of("品红色")
ALPHA_BG_CHECKER = ALPHA_BG.value_of("棋盘格")
ALPHA_BG_CHOICES = ALPHA_BG.choices


def _format_bytes(size: int) -> str:
    """字节数人类可读格式（与 media_info.format_bytes 同规则；此处内联避免
    引入 av/PIL 导入链——settings_manager 是轻量模块，被测试广泛导入）。"""
    value = float(max(0, size))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def default_cache_usage_bytes(settings: "SettingsManager") -> int:
    """默认缓存用量回调：统计设置中的缓存目录下全部文件字节总和。"""
    root = Path(settings.cache_dir())
    if not root.is_dir():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


class SettingsManager:
    """QSettings 驱动的设置管理器；设置文件为用户数据目录 settings.ini。"""

    # 默认设置（用户约定）：Dark 主题、网格线背景（GRID_DISPLAY_LINES）、
    # 曲线连线样式（PipeLayoutEnum.CURVED）。
    # 主题默认值由 THEME 选项组派生（唯一源头）。
    DEFAULT_THEME = THEME.default
    DEFAULT_PIPE_STYLE = 1   # NodeGraphQt PipeLayoutEnum.CURVED.value
    DEFAULT_GRID_MODE = 2    # NodeGraphQt ViewerEnum.GRID_DISPLAY_LINES.value
    # 缓存管理默认值（模块级常量，见文件顶部；类属性镜像供实例/对话框引用）。
    DEFAULT_CACHE_DIR = DEFAULT_CACHE_DIR
    CACHE_LIMIT_MIN_MB = CACHE_LIMIT_MIN_MB
    CACHE_LIMIT_MAX_MB = CACHE_LIMIT_MAX_MB
    DEFAULT_CACHE_LIMIT_MB = DEFAULT_CACHE_LIMIT_MB
    # 自动保存默认值（模块级常量；类属性镜像）。
    AUTOSAVE_MIN_MIN = AUTOSAVE_MIN_MIN
    AUTOSAVE_MAX_MIN = AUTOSAVE_MAX_MIN
    DEFAULT_AUTOSAVE_INTERVAL_MIN = DEFAULT_AUTOSAVE_INTERVAL_MIN

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else user_data_dir() / "settings.ini"
        self._settings = QtCore.QSettings(str(self.path), QtCore.QSettings.Format.IniFormat)
        self._settings.setFallbacksEnabled(False)

    # --- 通用读写 ---

    def get(self, key: str, default=None):
        return self._settings.value(key, default)

    def set(self, key: str, value) -> None:
        self._settings.setValue(key, value)
        self._settings.sync()

    @staticmethod
    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    # --- 颜色主题 ---

    def theme(self) -> str:
        """颜色主题。**固定为深色**（决策 #90，暂无细化亮色主题的计划）：
        恒返回默认值，忽略 settings.ini 存储值——旧存档的 ``light``/``system``
        静默回退深色，无需迁移。
        """
        return self.DEFAULT_THEME

    def set_theme(self, theme: str) -> None:
        """主题固定为深色后此方法不再生效（保留仅为 API 兼容：非法值仍抛错，
        但不再写入设置文件）。"""
        if theme not in THEME.value_set:
            raise ValueError(f"未知颜色主题：{theme}")

    # --- 连线样式 / 背景网格（工具栏切换后自动保存；设置界面不可见） ---

    def pipe_style(self) -> int:
        return self._as_int(self.get("view/pipe_style", self.DEFAULT_PIPE_STYLE), self.DEFAULT_PIPE_STYLE)

    def set_pipe_style(self, style: int) -> None:
        self.set("view/pipe_style", int(style))

    def grid_mode(self) -> int:
        return self._as_int(self.get("view/grid_mode", self.DEFAULT_GRID_MODE), self.DEFAULT_GRID_MODE)

    def set_grid_mode(self, mode: int) -> None:
        self.set("view/grid_mode", int(mode))

    # --- 透明背景显示选项（1:1 查看节点「透明背景」勾选后的预览框底色：
    # 绿幕/品红纯色（存储值即 CSS 色值）或 PS 式棋盘格（"checker"）） ---

    def alpha_bg(self) -> str:
        value = self.get("view/alpha_bg", ALPHA_BG.default)
        if value not in ALPHA_BG.value_set:
            return ALPHA_BG.default
        return value

    def set_alpha_bg(self, value: str) -> None:
        if value not in ALPHA_BG.value_set:
            raise ValueError(f"未知透明背景色：{value}")
        self.set("view/alpha_bg", value)

    # --- 缓存管理（临时缓存目录 / 总大小上限） ---

    def cache_dir(self) -> str:
        value = self.get("cache/dir", self.DEFAULT_CACHE_DIR)
        return value if value and str(value).strip() else self.DEFAULT_CACHE_DIR

    def set_cache_dir(self, value: str) -> None:
        raw = str(value).strip()
        if not raw:
            raise ValueError("缓存目录不能为空")
        path = Path(raw).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("probe", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise ValueError(f"缓存目录不可用（无法创建或写入）：{raw}（{exc}）") from exc
        # 存储绝对化路径（相对路径/家目录展开后落盘，避免下次启动时基准变化）。
        self.set("cache/dir", str(path.resolve()))

    def cache_limit_mb(self) -> int:
        value = self._as_int(self.get("cache/limit_mb", self.DEFAULT_CACHE_LIMIT_MB), self.DEFAULT_CACHE_LIMIT_MB)
        return max(self.CACHE_LIMIT_MIN_MB, min(self.CACHE_LIMIT_MAX_MB, value))

    def set_cache_limit_mb(self, mb: int) -> None:
        clamped = max(self.CACHE_LIMIT_MIN_MB, min(self.CACHE_LIMIT_MAX_MB, int(mb)))
        self.set("cache/limit_mb", clamped)

    # --- 自动保存（间隔分钟；0 = 关闭） ---

    def autosave_interval_min(self) -> int:
        """自动保存间隔（分钟）。0 = 关闭（默认开启，10 分钟）。"""
        value = self._as_int(
            self.get("autosave/interval_min", self.DEFAULT_AUTOSAVE_INTERVAL_MIN),
            self.DEFAULT_AUTOSAVE_INTERVAL_MIN,
        )
        return max(self.AUTOSAVE_MIN_MIN, min(self.AUTOSAVE_MAX_MIN, value))

    def set_autosave_interval_min(self, minutes: int) -> None:
        clamped = max(self.AUTOSAVE_MIN_MIN, min(self.AUTOSAVE_MAX_MIN, int(minutes)))
        self.set("autosave/interval_min", clamped)

    def autosave_path(self) -> Path:
        """自动保存文件路径：与设置文件同目录（隔离到设置/测试注入的目录）。"""
        return self.path.parent / "autosave.json"

    # --- 文件对话框起始目录记忆（导出保存框/导入打开框；设置界面不可见，
    # 与连线样式/背景网格同类——工具栏切换式自动保存，见决策 #133） ---

    def last_export_dir(self) -> str:
        """上次导出保存目录；未记忆或目录已失效返回 ''（对话框回落默认位置）。"""
        return self._remembered_dir("dialog/export_dir")

    def set_last_export_dir(self, value: str) -> None:
        """记录上次导出目录；无效值（空/不存在）不落盘。"""
        self._store_dir("dialog/export_dir", value)

    def last_import_dir(self) -> str:
        """上次导入/打开文件所在目录；未记忆或目录已失效返回 ''。"""
        return self._remembered_dir("dialog/import_dir")

    def set_last_import_dir(self, value: str) -> None:
        """记录上次导入目录；无效值（空/不存在）不落盘。"""
        self._store_dir("dialog/import_dir", value)

    def _remembered_dir(self, key: str) -> str:
        value = self.get(key, "")
        if value and Path(str(value)).is_dir():
            return str(value)
        return ""

    def _store_dir(self, key: str, value: str) -> None:
        raw = str(value or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if not path.is_dir():
                return  # 只记忆仍然存在的目录（移动介质拔除/删除后自动失效）
            raw = str(path)
        self.set(key, raw)

    # --- 重置 ---

    def reset(self) -> None:
        """恢复默认设置：深色主题（固定，决策 #90）、网格线背景、曲线连线样式、
        棋盘格透明背景。"""
        # 主题固定为深色，无需重置（set_theme 已不再生效）。
        self.set_pipe_style(self.DEFAULT_PIPE_STYLE)
        self.set_grid_mode(self.DEFAULT_GRID_MODE)
        self.set_alpha_bg(ALPHA_BG.default)
        # 缓存设置：默认值即合法，直接写存储值（不做可用性探测，避免副作用）。
        self.set("cache/dir", self.DEFAULT_CACHE_DIR)
        self.set_cache_limit_mb(self.DEFAULT_CACHE_LIMIT_MB)
        # 自动保存：默认 10 分钟开启。
        self.set_autosave_interval_min(self.DEFAULT_AUTOSAVE_INTERVAL_MIN)
        # 文件对话框目录记忆：重置后回到无记忆状态（'' → 对话框回落当前目录）。
        for key in ("dialog/export_dir", "dialog/import_dir"):
            self._settings.remove(key)
        self._settings.sync()


# ---------------------------------------------------------------------------
# 应用函数
# ---------------------------------------------------------------------------


def apply_color_scheme(app, theme: str) -> None:
    """按主题设置 QApplication 的颜色方案（浅色/深色/跟随系统）。

    存储值 → Qt 枚举的映射在 THEME 选项组的 payload 里（唯一源头），
    不再维护平行映射字典；非法值回退默认主题的载荷。
    主题固定为深色后（决策 #90），生产路径只会传入 ``THEME.default``。
    """
    scheme = THEME.payload_of(theme if theme in THEME.value_set else THEME.default)
    app.styleHints().setColorScheme(scheme)


def apply_pipe_style(graph, style: int) -> None:
    """把连线样式应用到 NodeGraphQt 图（style 为 PipeLayoutEnum.value，非法时回退默认）。"""
    from NodeGraphQt.constants import PipeLayoutEnum

    valid = {
        PipeLayoutEnum.STRAIGHT.value,
        PipeLayoutEnum.CURVED.value,
        PipeLayoutEnum.ANGLE.value,
    }
    style = int(style) if str(style).isdigit() else -1
    graph.set_pipe_style(style if style in valid else PipeLayoutEnum.CURVED.value)


def apply_grid_mode(graph, mode: int) -> None:
    """把背景网格样式应用到 NodeGraphQt 图（mode 为 ViewerEnum.value，非法时回退默认）。"""
    from NodeGraphQt.constants import ViewerEnum

    valid = {
        ViewerEnum.GRID_DISPLAY_NONE.value,
        ViewerEnum.GRID_DISPLAY_DOTS.value,
        ViewerEnum.GRID_DISPLAY_LINES.value,
    }
    mode = int(mode) if str(mode).isdigit() else -1
    graph.set_grid_mode(mode if mode in valid else ViewerEnum.GRID_DISPLAY_LINES.value)


# ---------------------------------------------------------------------------
# 设置对话框
# ---------------------------------------------------------------------------


class _AboutScrollArea(QtWidgets.QScrollArea):
    """关于页滚动区：内容按视口宽度换行后重算高度，超高时启用纵向滚动。

    **必须配合 ``setWidgetResizable(False)`` 使用**：``widgetResizable=True``
    时 QScrollArea 在每次自身 resize 时把容器重新钳回视口尺寸（滚动条
    出现/消失会再触发 resize，形成反馈循环），覆盖掉本类按真实换行内容
    高度回写的容器高度——内容被裁剪且无纵向滚动条（实测）。

    对 word-wrap 标签（QLabel 换行高度依赖宽度），``sizeHint`` 高度按
    未换行宽度估算，会**低估**换行后的真实内容高度（实测：视口 478、
    sizeHint 395、换行后实高 493）。本类在 ``resizeEvent`` 里把容器宽度
    钉到视口宽度、强制布局一次，从最后一个非 spacer 子项底部量出真实
    内容高度再回写容器尺寸（widgetResizable=False 时该高度不会被覆盖，
    滚动条范围 = 内容高度 − 视口高度）。
    """

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        widget = self.widget()
        layout = widget.layout() if widget is not None else None
        if layout is None:
            return
        viewport_width = self.viewport().width()
        # 先按视口宽、sizeHint 高做一次完整布局（换行标签此时按视口宽换行）。
        widget.resize(viewport_width, widget.sizeHint().height())
        layout.activate()
        # 内容高度 = 最后一个非 spacer 子项底部 + 布局下边距（addStretch 在
        # 末尾吸收多余高度，内容始终从顶部排布，量底部即真实内容高度）。
        bottom = layout.contentsMargins().top()
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.spacerItem() is None:
                bottom = max(bottom, item.geometry().bottom())
        height = bottom + layout.contentsMargins().bottom()
        widget.resize(viewport_width, max(height, widget.minimumSizeHint().height()))


def _about_label(text: str = "", *, selectable: bool = False) -> QtWidgets.QLabel:
    """关于页正文标签：自动换行 + 横向不撑大布局。

    长路径（运行时依赖文件、设置文件位置等）不含空格、不可在单词边界断行，
    ``QLabel`` 的 ``minimumSizeHint`` 会按未换行的自然宽度返回（实测最长
    路径约 840px），把容器布局的最小宽度撑到远超对话框宽度（440px）——
    关于页整体横向溢出。把横向尺寸策略置 ``Ignored`` 后，布局的最小宽度
    计算不再计入标签自然宽度（``qSmartMinSize`` 对 Ignored 策略返回 0），
    标签按实际分配宽度换行；超出宽度的超长单词由 Qt 文本布局在字符处断行。
    """
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Ignored,
        QtWidgets.QSizePolicy.Policy.Preferred,
    )
    if selectable:
        label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
    return label


class SettingsDialog(QtWidgets.QDialog):
    """设置对话框：固定尺寸；QTabWidget 切换「设置」/「关于」页。

    - 「设置」页：颜色主题固定为深色（决策 #90，下拉置灰只读展示，不可
      调整；暂无细化亮色主题的计划）、透明背景色、缓存管理（目录/上限/
      实时用量）、自动保存间隔（分钟，0=关闭）、「重置设置」按钮（恢复
      默认：深色主题、网格线背景、曲线连线样式、自动保存 10 分钟）；
      连线样式/背景网格**不在此界面显示**（由工具栏切换并自动保存）。
    - 「关于」页：QScrollArea 滚动展示（内容超高时纵向滚动）——头部应用
      图标（``:/ico/app_icon.ico``）+ 应用名 + 版本，简介、技术栈，
      ImageMagick / gifsicle / gifski / pyav 后端小节（带各自图标与运行时
      摘要）与设置文件位置。
    - 点击「重置设置」后发出 ``reset_requested`` 信号，主窗口据此把连线/网格
      样式重新应用到画布。
    """

    reset_requested = QtCore.Signal()
    # 透明背景色变更（str：green / magenta）——主窗口据此把新颜色应用到
    # 所有「图片1:1分辨率查看」节点的预览框背景。
    alpha_bg_changed = QtCore.Signal(str)

    DIALOG_WIDTH = 440
    DIALOG_HEIGHT = 580

    def __init__(
        self,
        settings: SettingsManager,
        parent=None,
        cache_usage_cb=None,
    ):
        super().__init__(parent)
        self.settings = settings
        # 缓存实时用量回调：返回当前缓存字节数；None = 默认统计设置里的缓存目录。
        # 主窗口传入 backend.total_cache_size() 以显示实际在用的缓存。
        self._cache_usage_cb = cache_usage_cb or (lambda: default_cache_usage_bytes(settings))
        self.setWindowTitle("设置")
        self.setFixedSize(self.DIALOG_WIDTH, self.DIALOG_HEIGHT)
        self.setModal(True)

        tabs = QtWidgets.QTabWidget(self)

        # ---- 设置页 ----
        settings_page = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(settings_page)
        form.setContentsMargins(14, 14, 14, 14)
        form.setSpacing(10)

        # 颜色主题：固定为深色（决策 #90），置灰只读展示、不可调整——
        # 暂无细化亮色主题的计划，下拉仅含「深色」一项。
        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItem(THEME.label_of(THEME.default), THEME.default)
        self.theme_combo.setCurrentIndex(0)
        self.theme_combo.setEnabled(False)
        form.addRow("颜色主题", self.theme_combo)
        theme_hint = QtWidgets.QLabel("主题固定为深色（暂无亮色主题计划）。")
        theme_hint.setWordWrap(True)
        theme_hint.setStyleSheet("color:#909090;")
        form.addRow(theme_hint)

        self.alpha_bg_combo = QtWidgets.QComboBox()
        for label, key in ALPHA_BG.choices:
            self.alpha_bg_combo.addItem(label, key)
        index = self.alpha_bg_combo.findData(settings.alpha_bg())
        if index < 0:
            index = self.alpha_bg_combo.findData(ALPHA_BG.default)
        self.alpha_bg_combo.setCurrentIndex(index)
        self.alpha_bg_combo.currentIndexChanged.connect(self._on_alpha_bg_changed)
        form.addRow("透明背景色", self.alpha_bg_combo)

        # ---- 缓存管理：缓存目录（可调路径，下次启动生效）+ 总大小上限 ----
        cache_dir_row = QtWidgets.QWidget()
        cache_dir_layout = QtWidgets.QHBoxLayout(cache_dir_row)
        cache_dir_layout.setContentsMargins(0, 0, 0, 0)
        self.cache_dir_edit = QtWidgets.QLineEdit(settings.cache_dir())
        self.cache_dir_edit.setReadOnly(True)
        self.cache_dir_browse = QtWidgets.QPushButton("浏览…")
        self.cache_dir_browse.clicked.connect(self._on_browse_cache_dir)
        cache_dir_layout.addWidget(self.cache_dir_edit, 1)
        cache_dir_layout.addWidget(self.cache_dir_browse)
        form.addRow("缓存目录", cache_dir_row)
        cache_hint = QtWidgets.QLabel("临时缓存目录；更改将于下次启动生效。")
        cache_hint.setWordWrap(True)
        cache_hint.setStyleSheet("color:#909090;")
        form.addRow(cache_hint)

        self.cache_limit_spin = QtWidgets.QSpinBox()
        self.cache_limit_spin.setRange(settings.CACHE_LIMIT_MIN_MB, settings.CACHE_LIMIT_MAX_MB)
        self.cache_limit_spin.setSuffix(" MB")
        self.cache_limit_spin.setValue(settings.cache_limit_mb())
        self.cache_limit_spin.valueChanged.connect(self._on_cache_limit_changed)
        form.addRow("缓存上限", self.cache_limit_spin)
        limit_hint = QtWidgets.QLabel("缓存总大小超过上限时，自动清理最旧的中间缓存（保留各节点最新结果与导出缓存）。")
        limit_hint.setWordWrap(True)
        limit_hint.setStyleSheet("color:#909090;")
        form.addRow(limit_hint)

        # 缓存实时用量：对话框打开期间每秒刷新一次（后台运行节点时缓存会增长）。
        self.cache_usage_label = QtWidgets.QLabel()
        self.cache_usage_label.setStyleSheet("color:#909090;")
        form.addRow("缓存用量", self.cache_usage_label)
        self._cache_usage_timer = QtCore.QTimer(self)
        self._cache_usage_timer.setInterval(1000)
        self._cache_usage_timer.timeout.connect(self._refresh_cache_usage)
        self._refresh_cache_usage()

        # ---- 自动保存：间隔分钟（0 = 关闭），变更立即保存 ----
        self.autosave_interval_spin = QtWidgets.QSpinBox()
        self.autosave_interval_spin.setRange(settings.AUTOSAVE_MIN_MIN, settings.AUTOSAVE_MAX_MIN)
        self.autosave_interval_spin.setSuffix(" 分钟")
        self.autosave_interval_spin.setValue(settings.autosave_interval_min())
        self.autosave_interval_spin.valueChanged.connect(self._on_autosave_interval_changed)
        form.addRow("自动保存间隔", self.autosave_interval_spin)
        autosave_hint = QtWidgets.QLabel(
            "0 = 关闭。方案有未保存更改时，按此间隔自动保存到设置文件旁的 autosave.json；"
            "程序正常退出自动清理，上次异常退出后下次启动会弹窗提示恢复。"
        )
        autosave_hint.setWordWrap(True)
        autosave_hint.setStyleSheet("color:#909090;")
        form.addRow(autosave_hint)

        hint = QtWidgets.QLabel("「连线样式」「背景网格」由工具栏按钮切换，变更后自动保存到设置。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#909090;")
        form.addRow(hint)

        self.reset_button = QtWidgets.QPushButton("重置设置")
        self.reset_button.setToolTip("恢复默认设置")
        self.reset_button.clicked.connect(self._on_reset)
        form.addRow(self.reset_button)
        form.addItem(
            QtWidgets.QSpacerItem(
                20, 40,
                QtWidgets.QSizePolicy.Policy.Minimum,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
        )
        tabs.addTab(settings_page, "设置")

        # ---- 关于页（_AboutScrollArea：内容超高时纵向滚动 + 按视口宽钉住内容宽度）----
        # 横向溢出根因：word-wrap 标签（长路径无空格）把 minimumSizeHint 撑到
        # 自然宽度（实测 872px > 对话框 440px），QScrollArea/widgetResizable
        # 无法把容器缩到视口以下，关于页整体被撑宽——正文标签一律走
        # _about_label（横向 Ignored 策略，见其 docstring）。
        # widgetResizable 必须为 False：True 时 QScrollArea 会把容器重新钳回
        # 视口尺寸（滚动条出现/消失反馈循环），覆盖掉 _AboutScrollArea 按真实
        # 换行内容高度回写的容器高度，内容被裁剪且无滚动条（实测）。
        about_page = _AboutScrollArea()
        about_page.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        about_page.setWidgetResizable(False)
        about_page.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        about_container = QtWidgets.QWidget()
        about_layout = QtWidgets.QVBoxLayout(about_container)
        about_layout.setContentsMargins(16, 16, 16, 16)
        about_layout.setSpacing(8)
        about_page.setWidget(about_container)

        # 头部：应用图标 + 应用名 + 版本（经典关于对话框布局）。
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(12)
        app_icon_label = QtWidgets.QLabel()
        app_icon_label.setPixmap(
            QtGui.QPixmap(APP_ICON_RESOURCE).scaled(
                48, 48, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        )
        header.addWidget(app_icon_label)
        title_box = QtWidgets.QVBoxLayout()
        title_box.setSpacing(2)
        title = QtWidgets.QLabel(APP_NAME)
        title.setStyleSheet("font-size:17px;font-weight:bold")
        title_box.addWidget(title)
        version_label = QtWidgets.QLabel(f"版本 {self._version()}")
        version_label.setStyleSheet("color:#909090;")
        title_box.addWidget(version_label)
        header.addLayout(title_box, 1)
        about_layout.addLayout(header)

        desc = _about_label(
            "节点式 GIF 制作器：通过连接节点构建处理流程，"
            "输出 GIF 或 PNG 序列。"
        )
        about_layout.addWidget(desc)

        stack = _about_label(
            "技术栈：PySide6 · NodeGraphQt · PyAV · Pillow · Wand (ImageMagick) · gifsicle · gifski"
        )
        about_layout.addWidget(stack)

        about_layout.addSpacing(4)
        about_layout.addWidget(self._separator())

        # ImageMagick：wand 图标 + 运行时摘要。
        im_header = QtWidgets.QHBoxLayout()
        im_header.setSpacing(8)
        im_icon = QtWidgets.QLabel()
        im_icon.setPixmap(
            QtGui.QPixmap(WAND_ICON_RESOURCE).scaled(
                24, 24, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        )
        im_header.addWidget(im_icon)
        im_title = QtWidgets.QLabel("ImageMagick(Wand)")
        im_title.setStyleSheet("font-weight:bold")
        im_header.addWidget(im_title)
        im_header.addStretch()
        about_layout.addLayout(im_header)
        im_runtime = _about_label(self._im_runtime_text(), selectable=True)
        about_layout.addWidget(im_runtime)

        about_layout.addSpacing(4)
        about_layout.addWidget(self._separator())

        # gifsicle：图标（60×132 竖版，按高度等比缩放）
        # + 运行时摘要。
        gifsicle_header = QtWidgets.QHBoxLayout()
        gifsicle_header.setSpacing(8)
        gifsicle_icon = QtWidgets.QLabel()
        gifsicle_icon.setPixmap(
            QtGui.QPixmap(GIFSICLE_ICON_RESOURCE).scaledToHeight(
                40, QtCore.Qt.TransformationMode.SmoothTransformation
            )
        )
        gifsicle_header.addWidget(gifsicle_icon)
        gifsicle_title = QtWidgets.QLabel("gifsicle")
        gifsicle_title.setStyleSheet("font-weight:bold")
        gifsicle_header.addWidget(gifsicle_title)
        gifsicle_header.addStretch()
        about_layout.addLayout(gifsicle_header)
        gifsicle_runtime = _about_label(self._gifsicle_runtime_text(), selectable=True)
        about_layout.addWidget(gifsicle_runtime)

        about_layout.addSpacing(4)
        about_layout.addWidget(self._separator())

        # gifski：横版 logo（582×190，按高度等比缩放）+ 运行时摘要。
        gifski_header = QtWidgets.QHBoxLayout()
        gifski_header.setSpacing(8)
        gifski_icon = QtWidgets.QLabel()
        gifski_icon.setPixmap(
            QtGui.QPixmap(GIFSKI_ICON_RESOURCE).scaledToHeight(
                36, QtCore.Qt.TransformationMode.SmoothTransformation
            )
        )
        gifski_header.addWidget(gifski_icon)
        gifski_title = QtWidgets.QLabel("gifski")
        gifski_title.setStyleSheet("font-weight:bold")
        gifski_header.addWidget(gifski_title)
        gifski_header.addStretch()
        about_layout.addLayout(gifski_header)
        gifski_runtime = _about_label(self._gifski_runtime_text(), selectable=True)
        about_layout.addWidget(gifski_runtime)

        about_layout.addSpacing(4)
        about_layout.addWidget(self._separator())

        # pyav：图标
        # + 运行时摘要。
        pyav_header = QtWidgets.QHBoxLayout()
        pyav_header.setSpacing(8)
        pyav_icon = QtWidgets.QLabel()
        pyav_icon.setPixmap(
            QtGui.QPixmap(PYAV_ICON_RESOURCE).scaledToHeight(
                32, QtCore.Qt.TransformationMode.SmoothTransformation
            )
        )
        pyav_header.addWidget(pyav_icon)
        pyav_title = QtWidgets.QLabel("pyav")
        pyav_title.setStyleSheet("font-weight:bold")
        pyav_header.addWidget(pyav_title)
        pyav_header.addStretch()
        about_layout.addLayout(pyav_header)
        pyav_runtime = _about_label(self._pyav_text(), selectable=True)
        about_layout.addWidget(pyav_runtime)

        about_layout.addSpacing(4)
        about_layout.addWidget(self._separator())

        settings_location = _about_label(
            f"设置文件：{self.settings.path}", selectable=True
        )
        about_layout.addWidget(settings_location)

        about_layout.addStretch()
        tabs.addTab(about_page, "关于")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(tabs)
        close_row = QtWidgets.QHBoxLayout()
        close_row.addStretch()
        close_button = QtWidgets.QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

    @staticmethod
    def _version() -> str:
        try:
            from .. import version_info
            text = f"{version_info.__version__} {version_info.__build__ if version_info.__build__ else '非发行'}"
            return text
        except Exception:
            return "err"

    @staticmethod
    def _im_runtime_text() -> str:
        """关于页的 ImageMagick 运行时摘要：当前 Wand 实际加载的依赖文件与版本。

        通过 ``configure_imagemagick()``（幂等，进程内只探测一次）拿到实际加载的
        MagickWand 动态库路径与版本；随包运行时 runtime/imagemagick/ 缺失时，
        Wand 会回退到系统解析（PATH/注册表），此时附带说明，避免用户误以为
        应用仍在使用自带的运行时。
        """
        runtime = configure_imagemagick()
        if not runtime.wand_available:
            return (
                "依赖文件：未找到（Wand 不可用）\n"
                "版本：—\n"
            )
        lines = [
            f"依赖文件：{runtime.library_path or '（未知）'}",
            f"版本：{runtime.version or '（未知）'}",
        ]
        if runtime.home is None:
            lines.append("随包运行时 runtime/imagemagick/ 未找到，当前由系统解析。")
        return "\n".join(lines)

    @staticmethod
    def _gifsicle_runtime_text() -> str:
        """关于页的 gifsicle 运行时摘要：随包 gifsicle.exe 路径与版本探测。

        通过 ``configure_gifsicle()``（幂等，进程内只探测一次）拿到可执行
        文件路径与 ``--version`` 解析的版本号；运行时缺失时明确提示，
        避免用户误以为 GIF 优化节点可用。
        """
        runtime = configure_gifsicle()
        if not runtime.available or runtime.exe is None:
            return (
                "依赖文件：未找到（runtime/gifsicle/gifsicle.exe）\n"
                "版本：—\n"
                "GIF 优化节点不可用（CLI 子进程后端缺失）。"
            )
        lines = [
            f"依赖文件：{runtime.exe}",
            f"版本：{runtime.version or '（未知）'}",
        ]
        if runtime.version_line:
            lines.append(runtime.version_line)
        return "\n".join(lines)

    @staticmethod
    def _gifski_runtime_text() -> str:
        """关于页的 gifski 运行时摘要：随包 gifski.exe 路径与版本探测（决策 #124）。

        通过 ``configure_gifski()``（幂等，进程内只探测一次）拿到可执行
        文件路径与 ``--version`` 解析的版本号；运行时缺失时明确提示。
        附带许可说明（AGPL-3.0-or-later 随包分发）与官网。
        """
        runtime = configure_gifski()
        if not runtime.available or runtime.exe is None:
            return (
                "依赖文件：未找到（runtime/gifski/gifski.exe）\n"
                "版本：—\n"
                "GIF 合成(gifski) 节点不可用（CLI 子进程后端缺失；"
                "运行 scripts/prepare_gifski_runtime.py）。"
            )
        lines = [
            f"依赖文件：{runtime.exe}",
            f"版本：{runtime.version or '（未知）'}",
            "许可：AGPL-3.0-or-later（随包分发；项目为单人私有仓库）",
            "官网：https://gif.ski",
        ]
        if runtime.version_line:
            lines.append(runtime.version_line)
        return "\n".join(lines)

    @staticmethod
    def _pyav_text() -> str:
        """关于页的 pyav 运行时摘要
        """
        return (f"版本：{av.__version__ or '（未知）'}\n"
                f"ffmpeg版本：{av.ffmpeg_version_info}")

    # --- 事件 ---

    def _on_alpha_bg_changed(self, _index: int) -> None:
        """透明背景色变更：立即保存，并把新颜色应用到所有 1:1 查看节点面板。"""
        value = self.alpha_bg_combo.currentData()
        self.settings.set_alpha_bg(value)
        self.alpha_bg_changed.emit(value)

    def _on_browse_cache_dir(self) -> None:
        """选择缓存目录：校验可写后保存（下次启动生效），失败弹警告并保持原值。"""
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择缓存目录", self.cache_dir_edit.text()
        )
        if not directory:
            return
        try:
            self.settings.set_cache_dir(directory)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, "缓存目录不可用", str(exc))
            return
        self.cache_dir_edit.setText(self.settings.cache_dir())

    def _on_cache_limit_changed(self, mb: int) -> None:
        """缓存大小上限变更：立即保存（钳制到合法范围后落盘）。"""
        self.settings.set_cache_limit_mb(mb)
        self._refresh_cache_usage()

    def _on_autosave_interval_changed(self, minutes: int) -> None:
        """自动保存间隔变更：立即保存（钳制到合法范围后落盘）。"""
        self.settings.set_autosave_interval_min(minutes)

    def _refresh_cache_usage(self) -> None:
        """刷新「当前缓存用量」标签：用量 / 上限（MB），附带占用百分比。"""
        limit_mb = self.settings.cache_limit_mb()
        try:
            used = max(0, int(self._cache_usage_cb()))
        except Exception:
            self.cache_usage_label.setText("（无法读取缓存目录）")
            return
        limit_bytes = limit_mb * 1024 * 1024
        percent = used / limit_bytes * 100 if limit_bytes else 0.0
        self.cache_usage_label.setText(
            f"{_format_bytes(used)} / {limit_mb} MB（占用 {percent:.1f}%）"
        )

    def showEvent(self, event) -> None:
        """对话框显示期间每秒刷新缓存用量（exec() 的事件循环驱动 QTimer）。"""
        super().showEvent(event)
        self._refresh_cache_usage()
        self._cache_usage_timer.start()

    def hideEvent(self, event) -> None:
        self._cache_usage_timer.stop()
        super().hideEvent(event)

    def _on_reset(self) -> None:
        self.settings.reset()
        # 主题固定为深色（决策 #90），设置页为只读展示，无需重置/应用。
        alpha_index = self.alpha_bg_combo.findData(self.settings.alpha_bg())
        self.alpha_bg_combo.setCurrentIndex(alpha_index)
        if self.settings.alpha_bg() != self.alpha_bg_combo.currentData():
            self.alpha_bg_changed.emit(self.settings.alpha_bg())
        # 缓存设置同步回默认（目录直接回写，上限经 valueChanged 自动保存）。
        self.cache_dir_edit.setText(self.settings.cache_dir())
        self.cache_limit_spin.setValue(self.settings.cache_limit_mb())
        # 自动保存间隔同步回默认（经 valueChanged 自动保存）。
        self.autosave_interval_spin.setValue(self.settings.autosave_interval_min())
        self.reset_requested.emit()

    @staticmethod
    def _separator() -> QtWidgets.QFrame:
        """关于页小节之间的水平分隔线。"""
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        return line
