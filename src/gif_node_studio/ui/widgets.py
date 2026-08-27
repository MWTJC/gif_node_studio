"""UI 辅助控件：节点说明面板、节点库按钮、底部状态栏。"""

from __future__ import annotations

from NodeGraphQt import BackdropNode
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtWidgets import QSpacerItem

from ..media.media_info import display_metadata


class HelpWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        self.title = QtWidgets.QLabel("节点说明")
        self.title.setStyleSheet("font-size:18px;font-weight:bold")
        # 用 QGroupBox 对右侧面板的输出文本内容进行分割：节点说明一组、输出元数据一组，标题即分组名，便于按区块阅读。
        self.info_box = QtWidgets.QGroupBox("节点说明")
        info_layout = QtWidgets.QVBoxLayout(self.info_box)
        self.category = QtWidgets.QLabel()
        self.text = QtWidgets.QLabel("选择节点以查看用途与操作说明。")
        self.text.setWordWrap(True)
        self.text.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        info_layout.addWidget(self.category)
        info_layout.addWidget(self.text)
        info_layout.addItem(QSpacerItem(20, 20, QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Expanding))
        self.metadata_box = QtWidgets.QGroupBox("输出元数据")
        metadata_layout = QtWidgets.QVBoxLayout(self.metadata_box)
        self.metadata = QtWidgets.QLabel("运行节点后将在此显示输出元数据。")
        self.metadata.setWordWrap(True)
        self.metadata.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        metadata_layout.addWidget(self.metadata)
        metadata_layout.addItem(QSpacerItem(20, 20, QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Expanding))
        layout.addWidget(self.title)
        layout.addWidget(self.info_box)
        layout.addWidget(self.metadata_box, 1)

    def show_node(self, node):
        if isinstance(node, BackdropNode):
            return
        self.title.setText(node.definition.title)
        self.category.setText(f"类型：{node.definition.category.value}")
        self.text.setText(node.help)
        self.metadata.setText(display_metadata(node.output_metadata))

    def show_definition(self, definition, help_text: str) -> None:
        """显示节点库悬停按钮对应的节点类型说明（尚无输出元数据）。"""
        self.title.setText(definition.title)
        self.category.setText(f"类型：{definition.category.value}")
        self.text.setText(help_text)
        self.metadata.setText("")

    def show_default(self) -> None:
        """无悬停按钮、无选中节点时的默认占位文案。"""
        self.title.setText("节点说明")
        self.category.setText("")
        self.text.setText("选择节点以查看用途与操作说明。")
        self.metadata.setText("运行节点后将在此显示输出元数据。")


class LibraryButton(QtWidgets.QPushButton):
    """节点库按钮：列表式「左图标 + 右标题」整行按钮。

    QPushButton 全宽单列排布（每分类一组 QVBoxLayout）。曾改为方形
    「上图下字」QToolButton 网格（2 列），实际使用效率不高，按 git 历史
    （5c1faaf）改回列表式：分类图标（18px 缩略）+ 标题，一行一个节点，
    信息密度与扫读效率更高。

    图标来源 = 节点的单一定义（``definition.icon``，qtawesome 叠加图标，
    与节点标题栏同源，决策 #111）。悬停仍通知主窗口显示对应节点类型说明。
    """

    hover_entered = QtCore.Signal(object)
    hover_left = QtCore.Signal()

    # 节点列表图标缩略尺寸（与标题栏图标同源）。
    ICON_SIZE = 36

    def __init__(self, definition, help_text: str, parent=None):
        super().__init__(definition.title, parent)
        self.definition = definition
        self.help_text = help_text
        # 分类图标：与节点标题栏同源（definition.icon，决策 #111），18px 缩略。
        icon = definition.icon
        if not icon.isNull():
            self.setIcon(icon)
            self.setIconSize(QtCore.QSize(self.ICON_SIZE, self.ICON_SIZE))
        # 列表式整行按钮：标题左对齐（默认 Fusion 按钮居中的是整块内容）。
        self.setStyleSheet("QPushButton { text-align: left; }")
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event):
        self.hover_entered.emit(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hover_left.emit()
        super().leaveEvent(event)


class StatusBar(QtWidgets.QStatusBar):
    """底部状态栏（继承重写）。

    调用方未指定持续时间（``timeout=0``）时，统一按 3Hz 闪烁 3 下（约 1 秒），
    随后把**原消息**持续显示 5 秒后自动恢复（清除）；调用方指定了持续时间则
    直接按原语义显示。所有未指定持续时间的调用行为一致（统一）。
    """

    FLASH_HZ = 3            # 闪烁频率（Hz）：3Hz = 每周期约 333ms
    FLASH_COUNT = 3         # 闪烁次数
    HOLD_MS = 5000          # 闪烁结束后原消息持续显示时长

    def __init__(self, parent=None):
        super().__init__(parent)
        self._flash_message = ""
        self._flash_ticks = 0
        self._flash_timer = QtCore.QTimer(self)
        # 一个闪烁周期 = 亮/灭两拍，3Hz 下每拍约 167ms。
        self._flash_timer.setInterval(round(1000 / (2 * self.FLASH_HZ)))
        self._flash_timer.timeout.connect(self._on_flash_tick)

    def showMessage(self, message, timeout=0):
        # 调用方指定了持续时间：按 QStatusBar 原语义显示（含自动清除），并中止闪烁。
        if timeout:
            self._flash_timer.stop()
            super().showMessage(message, timeout)
            return
        # 未指定持续时间：先亮起消息，再开始 3Hz 闪烁。
        self._flash_timer.stop()
        self._flash_message = message or ""
        self._flash_ticks = 0
        super().showMessage(self._flash_message)
        self._flash_timer.start()

    def _on_flash_tick(self):
        self._flash_ticks += 1
        if self._flash_ticks % 2 == 1:
            super().showMessage(self._flash_message)   # 亮
        else:
            super().clearMessage()                     # 灭
        if self._flash_ticks >= 2 * self.FLASH_COUNT:
            self._flash_timer.stop()
            # 闪烁结束后保持显示原消息（用户需求：不再显示「持续处理」）。
            super().showMessage(self._flash_message, self.HOLD_MS)
