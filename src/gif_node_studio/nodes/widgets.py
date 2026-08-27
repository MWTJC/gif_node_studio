"""领域无关输入控件：滑条+数值框、choice 下拉、文件路径、颜色选择。"""

from __future__ import annotations

from loguru import logger
from PySide6 import QtCore, QtGui, QtWidgets

from .definitions import ParamDefinition

class SliderSpinBox(QtWidgets.QWidget):
    valueChanged = QtCore.Signal(float)
    # 滑条拖拽手势：sliderPressed/sliderReleased（以及数值框编辑结束）触发，
    # 供上层把整个拖拽手势折叠为一条撤销记录（见 MainWindow._param_gesture_*）。
    gesture_begin = QtCore.Signal()
    gesture_end = QtCore.Signal()

    def __init__(self, minimum: float, maximum: float, value: float, integer: bool = False, parent=None):
        super().__init__(parent)
        self.integer = integer
        self.factor = 1 if integer else 10
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setRange(round(minimum * self.factor), round(maximum * self.factor))
        self.spin = QtWidgets.QSpinBox() if integer else QtWidgets.QDoubleSpinBox()
        if not integer:
            self.spin.setDecimals(1)
        self.spin.setRange(minimum, maximum)
        self.spin.setFixedWidth(72)
        # 键盘输入期间不逐键发值：只有提交（Enter/焦点离开）才触发 valueChanged，
        # 避免自动模式在用户键入一半时反复重算（自动模式只在该节点没有正在被
        # 键盘编辑的数值输入组件时才运行）。
        self.spin.setKeyboardTracking(False)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self.slider.sliderPressed.connect(self.gesture_begin)
        self.slider.sliderReleased.connect(self.gesture_end)
        self.spin.editingFinished.connect(self.gesture_end)
        self.setValue(value)

    def _from_slider(self, raw: int) -> None:
        value = raw / self.factor
        blocker = QtCore.QSignalBlocker(self.spin)
        self.spin.setValue(value)
        del blocker
        self.valueChanged.emit(float(value))

    def _from_spin(self, value: float) -> None:
        blocker = QtCore.QSignalBlocker(self.slider)
        self.slider.setValue(round(value * self.factor))
        del blocker
        self.valueChanged.emit(float(value))

    def value(self):
        value = self.spin.value()
        return int(value) if self.integer else float(value)

    def setValue(self, value) -> None:
        self.spin.setValue(value)
        self.slider.setValue(round(float(value) * self.factor))




class StudioComboBox(QtWidgets.QComboBox):
    """节点面板内嵌下拉框：修复弹出列表脱离节点飞向上方的问题。
    系使用fusion主题特有问题，
    旧的复杂算法有些边缘情况有问题，直接换用更普遍直接的方法优雅解决：stylesheet: combobox-popup: 0
    """
    def __init__(self):
        super().__init__()
        self.setStyleSheet("QComboBox { combobox-popup: 0; }")
        # self.setMaxVisibleItems(20)  # 当 combobox-popup: 0 时，默认MaxVisibleItems为10，此处留调节可能性


class FilePathWidget(QtWidgets.QWidget):
    changed = QtCore.Signal(str)

    def __init__(self, parameter: ParamDefinition, parent=None):
        super().__init__(parent)
        self.parameter = parameter
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QtWidgets.QLineEdit()
        self.button = QtWidgets.QPushButton("…", self)
        self.button.setFixedWidth(30)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)
        self.edit.editingFinished.connect(lambda: self.changed.emit(self.edit.text()))
        self.button.clicked.connect(self.browse)

    def browse(self) -> None:
        parameter = self.parameter
        if parameter.dialog == "directory":
            value = QtWidgets.QFileDialog.getExistingDirectory(None, parameter.label, self.edit.text())
        elif parameter.dialog == "save":
            value, _ = QtWidgets.QFileDialog.getSaveFileName(None, parameter.label, self.edit.text(), parameter.filter)
        else:
            value, _ = QtWidgets.QFileDialog.getOpenFileName(
                None, parameter.label, self.edit.text(), parameter.filter
            )
        if value:
            self.edit.setText(value)
            self.changed.emit(value)
            # 文件浏览事件：输入节点选择源文件/目录（排障可回溯用户数据来源）。
            logger.info("文件浏览（{}）→ {}", parameter.label, value)




class ColorPickerWidget(QtWidgets.QWidget):
    """颜色选择控件：色块按钮 + 十六进制文本，点击弹出 ``QColorDialog``。

    供「超级键」等需要用户选取颜色的节点使用。值以 ``'#rrggbb'`` 字符串
    持久化/传参；实现 ``value()``/``setValue()`` 与 ``changed`` 信号，使
    面板通用 ``values()``/``set_values()`` 分支直接可用。
    """

    changed = QtCore.Signal(str)

    def __init__(self, parameter: ParamDefinition, parent=None):
        super().__init__(parent)
        self.parameter = parameter
        self._color = QtGui.QColor(str(parameter.default))
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.swatch = QtWidgets.QPushButton()
        self.swatch.setFixedSize(56, 24)
        self.swatch.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.swatch.setToolTip("点击选择颜色")
        self.swatch.clicked.connect(self._pick)
        self.hex_label = QtWidgets.QLabel()
        self.hex_label.setStyleSheet("color:#b8bdc7;")
        layout.addWidget(self.swatch)
        layout.addWidget(self.hex_label, 1)
        self._apply_color()

    def _apply_color(self) -> None:
        name = self._color.name()
        self.swatch.setStyleSheet(
            f"QPushButton {{ background:{name}; border:1px solid #555; border-radius:2px; }}"
        )
        self.hex_label.setText(name)

    def _pick(self) -> None:
        color = QtWidgets.QColorDialog.getColor(
            self._color, None, "选择颜色",
            # QtWidgets.QColorDialog.ColorDialogOption.ShowAlphaChannel  # colordialog的透明控件就一个spinbox，简陋，交由透明通道合并节点进行透明度叠加
        )
        if color.isValid():
            self.setValue(color.name())
            self.changed.emit(self.value())

    def value(self) -> str:
        return self._color.name()

    def setValue(self, value) -> None:
        color = QtGui.QColor(str(value))
        if color.isValid() and color.name() != self._color.name():
            self._color = color
            self._apply_color()
