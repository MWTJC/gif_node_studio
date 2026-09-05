"""节点参数面板：按参数定义 isinstance 分派控件，含运行/导出按钮与预览区。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets
import qtawesome as qta

from ..media.media_info import format_bytes, gif_playback_info
from ..ui.settings_manager import ALPHA_BG, ALPHA_BG_CHECKER
from .definitions import (
    BoolParam,
    ChoiceParam,
    ColorParam,
    FileParam,
    IntParam,
    NodeDefinition,
    ParamDefinition,
    TakeoverParam,
)
from .preview_widgets import CheckerPreviewLabel, GifPreviewPlayer
from .widgets import ColorPickerWidget, FilePathWidget, SliderSpinBox, StudioComboBox

class ParameterPanel(QtWidgets.QWidget):
    changed = QtCore.Signal(dict)
    run_requested = QtCore.Signal()
    export_requested = QtCore.Signal()
    geometry_changed = QtCore.Signal()
    # 连续控件（滑条/可视化裁剪）拖拽手势的开始/结束：MainWindow 据此把整个
    # 手势折叠为一条撤销记录（begin_undo/end_undo 宏），避免逐 tick 压撤销。
    gesture_begin = QtCore.Signal()
    gesture_end = QtCore.Signal()

    PREVIEW_SIZE = 200  # 固定预览框边长（1:1），不随预览内容变化
    # 自定义 GIF 播放器（GifPreviewPlayer）的适用范围：带透明索引的小尺寸 GIF。
    # 估算 RGBA 内存 = 帧数 × 宽 × 高 × 4，超过预算或帧数过多时回退 QMovie
    # 流式播放（big.gif 1185 帧 956×488 估算 ≈2.2GB，coalesce ≈8.7s，不可接受）。
    GIF_PREVIEW_MEMORY_BUDGET = 64 * 1024 * 1024
    GIF_PREVIEW_MAX_FRAMES = 512
    RUN_BUTTON_ICON = "fa6s.play"          # 节点「运行」键图标（空闲：播放）
    STOP_BUTTON_ICON = "fa6s.stop" # 节点「运行」键图标（运行中：可中断）
    STATUS_STYLE = {
        "dirty": ("待运行", "#ffbd59", (125, 82, 24)),
        "running": ("运行", "#62b4ff", (38, 88, 133)),
        "clean": ("完成", "#68df91", (31, 108, 62)),
        "error": ("错误", "#ff6868", (135, 38, 45)),
    }

    def __init__(self, definition: NodeDefinition, parent=None):
        """面板完全由节点声明驱动（决策 #109），不再接收构造标志：

        - ``definition.panel``（``PanelSpec``）—— 显示/装饰特征（scrub 滑条、
          1:1 预览、透明背景选项、导出按钮）；
        - ``definition.params`` 里的 ``TakeoverParam`` —— 接管型组件（序列剃刀
          /可视化裁剪等复合控件）统一构造：跳过 ``owned`` 参数常规行、保留
          ``linked`` 参数常规行并联动、按 ``data_source`` 声明外部数据需求。
        """
        super().__init__(parent)
        self.definition = definition
        self.widgets: dict[str, QtWidgets.QWidget] = {}
        # 设置管理器：MainWindow._bind_node 注入（面板构造期尚不存在）；
        # 文件选择行（FilePathWidget）经 panel.settings 读/写「上次导入目录」记忆。
        self.settings = None
        spec = definition.panel
        self._scrub_frames = spec.scrub_frames
        self._preview_1to1 = spec.preview_1to1
        # 透明背景显示选项（如 1:1 查看节点的「透明背景」勾选框）：参数名
        # 对应的 bool 控件变化时只刷新预览框背景色，不触发运行。
        self._preview_bg_param = spec.preview_bg_param
        export_enabled = spec.export_enabled
        # 接管型组件：params 里的 TakeoverParam 声明（节点声明即唯一源头）。
        self._takeovers: list[TakeoverParam] = [
            p for p in definition.params if isinstance(p, TakeoverParam)
        ]
        self._takeover_owned = {name for p in self._takeovers for name in p.owned}
        self._takeover_widgets: dict[str, QtWidgets.QWidget] = {}
        # 默认绿幕色与设置管理器同源（settings_manager.ALPHA_BG 选项组的默认值）；
        # 绑定节点时由 MainWindow 按当前设置注入（绿幕/品红）。
        self._preview_bg_color = ALPHA_BG.default
        self._frames: list[str] = []
        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(8, 8, 8, 8)
        for parameter in definition.params:
            if isinstance(parameter, TakeoverParam):
                continue  # 接管声明：不生成行
            if parameter.name in self._takeover_owned:
                continue  # 被接管：不生成常规行（值由接管控件统一读写）
            widget = self.make_parameter_widget(parameter)
            self.widgets[parameter.name] = widget
            form.addRow(parameter.label, widget)
        if scrub_frames := spec.scrub_frames:
            self.frame_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.frame_slider.setEnabled(False)
            self.frame_slider.valueChanged.connect(self._on_scrub)
            self.frame_label = QtWidgets.QLabel("—")
            scrub_row = QtWidgets.QHBoxLayout()
            scrub_row.addWidget(self.frame_slider, 1)
            scrub_row.addWidget(self.frame_label)
            form.addRow(scrub_row)
        if self._takeovers:
            # 接管面板：复合控件占据预览区位置（有接管控件则不生成常规预览标签）。
            for param in self._takeovers:
                widget = param.make_widget(self)
                self.widgets[param.name] = widget
                self._takeover_widgets[param.name] = widget
                widget.changed.connect(self._emit_changed)
                if hasattr(widget, "gesture_begin"):
                    widget.gesture_begin.connect(self.gesture_begin)
                    widget.gesture_end.connect(self.gesture_end)
                if hasattr(widget, "geometry_changed"):
                    widget.geometry_changed.connect(self.geometry_changed.emit)
                form.addRow(widget)
        else:
            self.preview = CheckerPreviewLabel("无预览")
            self.preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            # 1:1 查看节点按素材原始像素尺寸 1:1 显示（÷ 当前标签 DPR）；
            # 其余节点预览框固定 200×200（只缩不放，逻辑单位随 UI 缩放）。
            # 背景/边框由标签自绘（CheckerPreviewLabel），不设样式表
            # （QSS border 在分数 DPR 下亚像素偏移，见预览 DPI 修复）。
            self.preview.set_1to1(self._preview_1to1)
            self.preview.set_fit_box(self.PREVIEW_SIZE, self.PREVIEW_SIZE)
            self.preview.geometry_changed.connect(self.geometry_changed.emit)
            form.addRow(self.preview)
        self.node_info = QtWidgets.QLabel("上次运行耗时：—\n缓存大小：0 B")
        self.node_info.setWordWrap(True)
        self.node_info.setStyleSheet("color:#b8bdc7;background:#202329;padding:5px;border-radius:3px")
        self.run_button = QtWidgets.QToolButton()
        # self.run_button.setIconSize(QtCore.QSize(16, 16))
        self.set_run_button_state(False)
        self.export_button = QtWidgets.QPushButton("导出…", self)
        self.export_button.setVisible(export_enabled)
        self.status = QtWidgets.QToolButton()
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(self.run_button)
        actions.addWidget(self.status)
        form.addRow(self.export_button)
        form.addRow(actions)
        form.addRow(self.node_info)
        self.run_button.clicked.connect(self.run_requested)
        self.export_button.clicked.connect(self.export_requested)
        self._wire_enablement()
        self.set_status("dirty")

    def _emit_changed(self) -> None:
        """接管控件统一变更回调：按面板惯例携带完整参数字典发出。"""
        self.changed.emit(self.values())

    # --- 互斥启用规则：依赖参数取值不满足时，对应控件置灰（disabled）---

    def _wire_enablement(self) -> None:
        """按 ParamDefinition.enabled_when 建立依赖并刷新启用状态。

        只在依赖参数变化时刷新，不触发 changed（置灰/恢复不会引发运行）。
        """
        self._enable_rules: dict[str, tuple[str, tuple[str, ...]]] = {}
        for parameter in self.definition.params:
            if parameter.enabled_when is not None:
                self._enable_rules[parameter.name] = parameter.enabled_when
        if not self._enable_rules:
            return
        watched = {rule[0] for rule in self._enable_rules.values()}
        for name in watched:
            widget = self.widgets.get(name)
            signal = self._widget_change_signal(widget)
            if signal is not None:
                signal.connect(lambda _value: self._refresh_enablement())
        self._refresh_enablement()

    @staticmethod
    def _widget_change_signal(widget):
        """返回控件的主变更信号（用于互斥规则监听依赖参数）。"""
        if isinstance(widget, QtWidgets.QComboBox):
            return widget.currentTextChanged
        if isinstance(widget, QtWidgets.QCheckBox):
            return widget.toggled
        if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox, SliderSpinBox)):
            return widget.valueChanged
        return None

    def _refresh_enablement(self) -> None:
        """按当前参数值刷新各控件的启用状态（互斥时不可调整）。"""
        values = self.values()
        for name, (watch, allowed) in self._enable_rules.items():
            widget = self.widgets.get(name)
            if widget is not None:
                widget.setEnabled(values.get(watch) in allowed)

    def show_runtime_info(self, elapsed_seconds: float | None, cache_bytes: int) -> None:
        if elapsed_seconds is None:
            elapsed = "—"
        elif elapsed_seconds < 1:
            elapsed = f"{elapsed_seconds * 1000:.0f} ms"
        else:
            elapsed = f"{elapsed_seconds:.2f} s"
        self.node_info.setText(f"上次运行耗时：{elapsed}\n缓存大小：{format_bytes(cache_bytes)}")
        self.adjustSize()
        self.geometry_changed.emit()

    def make_parameter_widget(self, parameter: ParamDefinition):
        if isinstance(parameter, FileParam):
            widget = FilePathWidget(parameter, panel=self)
            widget.edit.setText(str(parameter.default))
            widget.changed.connect(lambda _value: self.changed.emit(self.values()))
            return widget
        if isinstance(parameter, ColorParam):
            widget = ColorPickerWidget(parameter)
            widget.changed.connect(lambda _value: self.changed.emit(self.values()))
            return widget
        if isinstance(parameter, ChoiceParam):
            widget = StudioComboBox()
            widget.addItems(parameter.choices)
            widget.setCurrentText(str(parameter.default))
            widget.currentTextChanged.connect(lambda _value: self.changed.emit(self.values()))
            return widget
        if isinstance(parameter, BoolParam):
            widget = QtWidgets.QCheckBox()
            widget.setChecked(bool(parameter.default))
            if parameter.name == self._preview_bg_param:
                # 透明背景显示选项：只刷新预览框背景色，不触发 changed/运行。
                widget.toggled.connect(lambda _value: self._refresh_preview_bg())
            else:
                widget.toggled.connect(lambda _value: self.changed.emit(self.values()))
            return widget
        # 数值参数（IntParam / FloatParam）
        is_int = isinstance(parameter, IntParam)
        if parameter.widget == "spin":
            # 仅数值框（无滑条）：用于截取节点的“持续秒数/帧数”等数值输入。
            widget = QtWidgets.QSpinBox() if is_int else QtWidgets.QDoubleSpinBox()
            if not is_int:
                widget.setDecimals(1)
            if parameter.minimum is not None:
                widget.setMinimum(parameter.minimum)
            if parameter.maximum is not None:
                widget.setMaximum(parameter.maximum)
            widget.setKeyboardTracking(False)  # 键盘编辑仅在提交时发值（见 SliderSpinBox 注释）
            widget.setValue(parameter.default)
            widget.valueChanged.connect(lambda _value: self.changed.emit(self.values()))
            return widget
        if parameter.minimum is not None and parameter.maximum is not None:
            widget = SliderSpinBox(
                float(parameter.minimum), float(parameter.maximum), float(parameter.default), is_int
            )
            widget.valueChanged.connect(lambda _value: self.changed.emit(self.values()))
            # 滑条拖拽手势 → 面板手势 → MainWindow 撤销宏折叠（整个拖拽 = 一条撤销）。
            widget.gesture_begin.connect(self.gesture_begin)
            widget.gesture_end.connect(self.gesture_end)
            return widget
        widget = QtWidgets.QSpinBox() if is_int else QtWidgets.QDoubleSpinBox()
        widget.setKeyboardTracking(False)  # 键盘编辑仅在提交时发值（见 SliderSpinBox 注释）
        widget.setValue(parameter.default)
        widget.valueChanged.connect(lambda _value: self.changed.emit(self.values()))
        return widget

    def values(self) -> dict[str, Any]:
        result = {}
        for parameter in self.definition.params:
            if isinstance(parameter, TakeoverParam):
                continue  # 接管声明本身无值
            if parameter.name in self._takeover_owned:
                continue  # 值由接管控件统一读（下方统一收）
            widget = self.widgets.get(parameter.name)
            if widget is None:
                continue  # 安全兜底（不应发生）
            if isinstance(widget, FilePathWidget):
                result[parameter.name] = widget.edit.text()
            elif isinstance(widget, QtWidgets.QComboBox):
                result[parameter.name] = widget.currentText()
            elif isinstance(widget, QtWidgets.QCheckBox):
                result[parameter.name] = widget.isChecked()
            else:
                result[parameter.name] = widget.value()
        for param in self._takeovers:
            result.update(self._takeover_widgets[param.name].values())
        return result

    def set_values(self, values: dict[str, Any]) -> None:
        # 常规写（跳过接管声明与 owned 参数）：linked 参数（如纵横比）在这里
        # 正常写入——setCurrentText 触发接管控件内的联动（投影依赖锁定状态），
        # 先于 owned 裁剪值写入，符合「存档即所见」。
        takeover_decl = {p.name for p in self._takeovers if p.default is None}
        for name, value in values.items():
            if name in takeover_decl:
                continue  # 无值接管声明（不生成控件行，无写入目标）
            if name in self._takeover_owned:
                continue  # 由接管控件统一写（下方）
            widget = self.widgets.get(name)
            if isinstance(widget, FilePathWidget):
                widget.edit.setText(str(value))
            elif isinstance(widget, QtWidgets.QComboBox):
                widget.setCurrentText(str(value))
            elif isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(bool(value))
            elif widget is not None:
                widget.setValue(value)
        # 接管写（owned 参数子集）
        for param in self._takeovers:
            widget = self._takeover_widgets[param.name]
            subset = {k: v for k, v in values.items() if k in param.owned}
            if subset:
                widget.set_values(subset)
        self._refresh_preview_bg()

    def set_preview_bg_color(self, color: str) -> None:
        """注入透明背景色（绿幕/品红，来自设置管理器）；刷新当前预览框背景。"""
        self._preview_bg_color = color
        self._refresh_preview_bg()

    def _refresh_preview_bg(self) -> None:
        """按透明背景勾选状态刷新预览框底纹（纯显示选项，不触发运行）。

        - 未勾选：默认深色背景；
        - 勾选 + 纯色（绿幕/品红）：预览框背景使用注入的透明背景色，
          透明像素直接透出背景色；
        - 勾选 + 棋盘格（设置中可选）：预览框绘制 PS 式棋盘格底纹
          （固定格子设备像素尺寸，不受预览框尺寸/长宽比影响）。

        背景/边框由 CheckerPreviewLabel 自绘（不设样式表）。
        """
        if not self._preview_bg_param or self._takeovers:
            return
        enabled = bool(self.values().get(self._preview_bg_param))
        if enabled and self._preview_bg_color == ALPHA_BG_CHECKER:
            self.preview.set_bg_color(None)
            self.preview.set_checker_enabled(True)
        else:
            self.preview.set_checker_enabled(False)
            if enabled:
                self.preview.set_bg_color(self._preview_bg_color)
            else:
                self.preview.set_bg_color(None)

    def set_status(self, state: str, detail: str = "") -> None:
        text, color, _ = self.STATUS_STYLE[state]
        self.status.setText(text + (f" {detail}" if detail else ""))
        self.status.setStyleSheet(f"background:{color};color:#111;font-weight:bold")
        # 运行键外观跟随节点状态：运行中变为红色停止图标（可中断），
        # 空闲恢复播放图标。图标本身由 qtawesome 提供，不再使用 Qt 内置箭头。
        self.set_run_button_state(state == "running")

    def set_run_button_state(self, running: bool) -> None:
        """节点「运行」键外观：空闲=播放图标（点击运行）；运行中=停止图标（点击中断）。"""
        if running:
            self.run_button.setIcon(qta.icon(self.STOP_BUTTON_ICON, color="red"))
            self.run_button.setToolTip("停止运行")
        else:
            self.run_button.setIcon(qta.icon(self.RUN_BUTTON_ICON, ))
            self.run_button.setToolTip("运行节点")

    def set_sequence_frames(self, frames: list[str]) -> None:
        """Feed decoded sequence frames to the preview scrubber (if enabled).

        传入空列表 = 清空滑条状态（禁用滑条、复位范围与标签）：结果不携带
        帧时（如 1:1 查看节点改显 GIF/视频清单项）必须清空，否则旧序列帧
        残留会导致「串台」（拖动滑条显示上一轮的帧，见 ui._feed_sequence_frames）。
        """
        if not self._scrub_frames:
            return
        self._frames = list(frames or [])
        if not self._frames:
            self.frame_slider.setEnabled(False)
            self.frame_slider.setRange(0, 0)
            self.frame_slider.setValue(0)
            self.frame_label.setText("—")
            return
        self.frame_slider.setEnabled(True)
        self.frame_slider.setRange(0, len(self._frames) - 1)
        self.frame_slider.setValue(0)
        self._on_scrub(0)

    def _on_scrub(self, index: int) -> None:
        if not (0 <= index < len(self._frames)):
            return
        self.frame_label.setText(f"帧 {index + 1}/{len(self._frames)}")
        self.show_preview(self._frames[index])

    def show_preview(self, path: str | Path | None) -> None:
        # A QMovie keeps its source file open on Windows.  Always detach the
        # previous preview before replacing it, otherwise old cache files stay
        # locked even though a different preview is visible.
        if path is None and any(
            takeover.data_source == "sequence_frames" for takeover in self._takeovers
        ):
            # 胶片条类接管（序列剃刀）：内容由 ``feed_sequence_frames`` 显式
            # 管理——``preview_path_for_node`` 在调用本方法前已把上游全帧喂入，
            # 「预览路径」概念不适用（恒返回 None）。此处若照常 release_preview
            # 会把刚喂入的切割处两侧帧预览清空：运行完成后预览框空白，与
            # 一般节点「运行后显示结果」的行为不符。真正的清空由
            # ``release_preview``（节点删除/移除）与 ``set_frames([])``
            # （上游无帧）承担。
            return
        self.release_preview()
        if not path:
            return
        if self._takeovers:
            # 接管控件：``data_source="first_frame"`` 的控件喂入源图（如可视化
            # 裁剪显示「未应用本节点裁剪」的上游源图，结果缩略图按当前参数
            # 实时裁剪）；``sequence_frames`` 由 ``feed_sequence_frames`` 显式
            # 管理（上游全帧，经 ui 能力探测喂入）。
            for param in self._takeovers:
                if param.data_source == "first_frame":
                    self._takeover_widgets[param.name].set_image(path)
            return
        if str(path).lower().endswith(".gif"):
            # 带透明的 GIF 走自定义播放器（透明区域与序列预览一致）；其余回退 QMovie。
            info = gif_playback_info(str(path))
            if self._should_use_custom_gif_player(info) and self._start_custom_gif_player(str(path), info):
                return
            movie = QtGui.QMovie(str(path))
            if movie and movie.isValid():
                # QMovie 不 setMovie（QLabel 按帧物理尺寸绘制，高 DPI 无法
                # 1:1、逻辑尺寸 < 帧尺寸时裁剪）：frameChanged → currentPixmap
                # → set_content，由预览标签统一做 DPR 感知绘制/定框。
                self._movie = movie
                movie.frameChanged.connect(self._on_movie_frame)
                movie.jumpToFrame(0)
                self._on_movie_frame(0)
                movie.start()
                return
        pixmap = QtGui.QPixmap(str(path))
        if pixmap.isNull():
            return
        self.preview.set_content(pixmap)

    # --- 自定义 GIF 播放器（透明区域正确显示；Qt QMovie 会忽略透明色索引） ---

    def _should_use_custom_gif_player(self, info: dict | None) -> bool:
        """是否用自定义播放器：GIF 声明了透明索引，且估算解码成本在预算内。"""
        if info is None or info["transparent_index"] is None:
            return False
        estimated = info["frame_count"] * info["width"] * info["height"] * 4
        return (
            info["frame_count"] <= self.GIF_PREVIEW_MAX_FRAMES
            and estimated <= self.GIF_PREVIEW_MEMORY_BUDGET
        )

    def _start_custom_gif_player(self, path: str, info: dict) -> bool:
        """启动 Wand 解码的自定义 GIF 播放器；失败返回 False（调用方回退 QMovie）。"""
        player = GifPreviewPlayer(self)
        loop = info["loop"] if info["loop"] is not None else 1  # 未声明循环 → 播放一次
        if not player.start(path, info["durations_ms"], loop, self._on_gif_frame):
            player.deleteLater()
            return False
        self._gif_player = player
        return True

    def _on_movie_frame(self, _frame: int) -> None:
        """QMovie 回退路径每帧回调：currentPixmap → set_content（标签统一定框/绘制）。"""
        movie = getattr(self, "_movie", None)
        if movie is None:
            return
        pixmap = movie.currentPixmap()
        if pixmap is not None and not pixmap.isNull():
            self.preview.set_content(pixmap)

    def _on_gif_frame(self, raw: bytes, width: int, height: int) -> None:
        """自定义播放器每帧回调：把 RGBA 字节显示到预览框（透明区域保留 alpha）。"""
        image = QtGui.QImage(raw, width, height, width * 4, QtGui.QImage.Format.Format_RGBA8888)
        if image.isNull():
            return
        image = image.copy()  # 与原始 bytes 缓冲解耦
        pixmap = QtGui.QPixmap.fromImage(image)
        # 1:1/适配统一由标签处理：set_content 在帧尺寸不变时不重排节点
        # （GIF 帧尺寸恒定，逐帧 setFixedSize 会把每帧变成一次完整的节点
        # 重排——sync_geometry → draw_node → align_widgets，空转数分钟后
        # 有几率在 Qt/shiboken 侧触发原生访问违例 0xC0000005）。
        self.preview.set_content(pixmap)

    def release_preview(self) -> None:
        if self._takeovers:
            # 接管控件内容由 set_frames/set_image 显式管理——统一 release_content
            # 清空预览类内容（剃刀不清胶片条帧、裁剪清源图，语义由控件内部承担）。
            for widget in self._takeover_widgets.values():
                if hasattr(widget, "release_content"):
                    widget.release_content()
            return
        player = getattr(self, "_gif_player", None)
        if player is not None:
            player.stop()
            player.deleteLater()
            self._gif_player = None
        movie = getattr(self, "_movie", None)
        if movie is not None:
            movie.stop()
            movie.setFileName("")
            movie.deleteLater()
            self._movie = None
        # 清空内容并复位 1:1 框尺寸（标签内静默复位，不触发重排）。
        self.preview.clear_content()

    def refresh_preview_dpr(self, dpr: float) -> None:
        """窗口跨屏/系统缩放变化后重建 1:1 预览：按显式 DPR 重算框尺寸并重绘。

        仅 1:1 面板需要（框 = 素材物理像素 ÷ 当前 DPR，跟随所在屏幕缩放）；
        固定 200×200 缩略图框不依赖 DPR，跳过。显式传窗口句柄的实时 DPR
        （嵌入代理的标签 devicePixelRatioF 跨屏后实测不更新）。接管面板中
        声明 ``refresh_dpr`` 的复合控件（如可视化裁剪画布）同样重建。
        """
        if not self._preview_1to1:
            return
        if self._takeovers:
            for widget in self._takeover_widgets.values():
                if hasattr(widget, "refresh_dpr"):
                    widget.refresh_dpr(dpr)
            return
        preview = getattr(self, "preview", None)
        if preview is not None:
            preview.refresh_dpr(dpr)

    def set_preview_playing(self, playing: bool) -> None:
        """暂停/恢复预览动画播放（窗口最小化/非前台时由 MainWindow 调用）。

        - 自定义 GIF 播放器（``GifPreviewPlayer``）→ 停止/重启帧定时器；
        - QMovie 回退播放 → ``setPaused``；
        - 静态图片/无预览 → 空操作。暂停时当前帧保持显示，恢复后继续播放。
        """
        player = getattr(self, "_gif_player", None)
        if player is not None:
            if playing:
                player.resume()
            else:
                player.pause()
        movie = getattr(self, "_movie", None)
        if movie is not None:
            movie.setPaused(not playing)

    # --- 接管组件（TakeoverParam）：外部数据源能力探测与喂入 ---

    def takeover_data_sources(self) -> frozenset[str]:
        """接管组件声明的外部数据源需求（ui 层据此喂数据，替代 KIND 特判）。

        取值：``"sequence_frames"``（上游序列全帧）/ ``"first_frame"``（上游
        首帧/清单预览），或空集（无需外部数据）。
        """
        return frozenset(p.data_source for p in self._takeovers if p.data_source)

    def feed_sequence_frames(self, frames: list[str]) -> None:
        """喂入上游序列全帧给声明 ``data_source="sequence_frames"`` 的接管控件。

        空列表 = 清空（控件内清空胶片条显示并复位切割边界）。
        """
        for param in self._takeovers:
            if param.data_source == "sequence_frames":
                self._takeover_widgets[param.name].set_frames(list(frames or []))
