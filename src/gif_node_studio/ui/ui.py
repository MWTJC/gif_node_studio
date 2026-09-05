"""主窗口：MainWindow 单类。

2026-08 代码整理（见关键决策 #98）：撤销 #82 的 mixin 拆分——PySide6 的 C3
线性化把 Qt 类排在 mixin 之前导致 C++ 虚函数遮蔽（决策 #85），且 mixin 内的
跨文件引用无法被 IDE 静态分析定位（PyCharm 不做反向 MRO）。现收敛为单类：
六个职责区段（菜单/节点/视图/执行/预览/存档）以区段注释分组；无状态辅助
提升为模块级纯函数。

- ``session.py`` —— 节点方案存档保存/旧存档兼容清洗（纯函数）；
- ``widgets.py`` —— 节点说明面板/节点库按钮/底部状态栏；
- ``theme.py`` —— 全局主题应用（apply_theme）；
- ``actions.py`` / ``hotkeys/`` —— 动作定义唯一登记处与 graph 级功能函数。

本文件 = MainWindow 完整类（实例状态 + 信号接线 + 全部行为）。
"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import time
from pathlib import Path

from loguru import logger
from NodeGraphQt import BackdropNode, NodeGraph
from PySide6 import QtCore, QtGui, QtWidgets
import qtawesome as qta

from ..core.domain import AnalysisResult, MediaKind, MediaManifest, MultiOutput, SequenceArtifact
from ..core.logging_setup import add_faulthandler_observer, heartbeat, logs_dir, setup_logging
from ..core.paths import node_presets_dir
from ..media.backend import MediaBackend
from ..media.media_info import describe_output, format_bytes
from ..nodes.backdrop import (
    BACKDROP_HELP,
    EditableBackdropNode,
    TITLE_BAR_HEIGHT_MAX,
    TITLE_BAR_HEIGHT_MIN,
    backdrop_definition,
)
from ..nodes.definitions import NodeCategory, PortType
from ..nodes.node_base import StudioNode
from ..nodes.registry import NODE_CLASSES, node_class_by_kind, node_definitions, node_help_by_kind
from ..runner.async_worker import AsyncExecutionWorker, TimedResult
from .actions import ACTIONS, GRAPH_MENU, MENUBAR, NODE_MENU, PRESETS_SUBMENU, SEP, TOOLBAR
from .session import SessionLoadReport, sanitize_session_data, save_session_clean
from .settings_manager import SettingsDialog, SettingsManager, apply_grid_mode, apply_pipe_style
from .theme import apply_graph_theme, recolor_all_pipes
from .widgets import HelpWidget, LibraryButton, StatusBar

# PNG 序列导出默认文件名前缀（导出时弹保存框的默认文件名）。
PNG_EXPORT_DEFAULT_PREFIX = "sequence_"

# ---------------------------------------------------------------------------
# 模块级纯函数（无窗口状态；由 mixin 提升，决策 #98）
# ---------------------------------------------------------------------------




def release_previews(nodes):
    """Synchronously release Qt media handles before touching cache files."""
    for node in nodes:
        if isinstance(node, StudioNode):
            node.panel.release_preview()
    app = QtWidgets.QApplication.instance()
    if app is not None:
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()


def _pump_startup_events() -> None:
    """启动构建期间的轻量事件泵动（决策 #103）。

    ``MainWindow.__init__`` 是主线程同步块：期间 QMovie 帧定时器无法触发，
    splash 动画冻结（决策 #88 只异步化了重量级库导入，未覆盖控件构建段）。
    在构造阶段边界与节点定义缓存热点内部周期调用本函数，让动画持续推进。
    ``ExcludeUserInputEvents``：只处理定时器/投递事件，不重入处理用户
    点击/按键（构造期窗口未就绪，嵌套输入无意义）。测试/自检环境无待处理
    事件时本调用为微秒级空转，无副作用。
    """
    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)






def _set_action_shortcut(action, shortcut) -> None:
    """设置动作快捷键：支持单个（str/QKeySequence）与多个（list）。"""
    if isinstance(shortcut, (list, tuple)):
        action.setShortcuts(shortcut)
    elif shortcut is not None:
        action.setShortcut(shortcut)







def _port_type_of(node, port_name: str, *, is_input: bool):
    if not isinstance(node, StudioNode):
        return None
    ports = node.definition.inputs if is_input else node.definition.outputs
    for port in ports:
        if port.name == port_name:
            return port.type
    return None





def _port_type_label(port_type) -> str:
    return {
        PortType.MANIFEST: "格式化清单",
        PortType.SEQUENCE: "序列图片",
    }.get(port_type, str(port_type))





def _connection_type_mismatch(input_port, output_port):
    """返回端口类型不符的描述；类型匹配时返回 None。"""
    expected = _port_type_of(input_port.node(), input_port.name(), is_input=True)
    actual = _port_type_of(output_port.node(), output_port.name(), is_input=False)
    if expected is None or actual is None or expected == actual:
        return None
    return (
        "非法连接：{out_name} -> {in_name}"
    ).format(
        target=input_port.node().NODE_NAME,
        in_name=input_port.name(),
        expected=_port_type_label(expected),
        source=output_port.node().NODE_NAME,
        out_name=output_port.name(),
        actual=_port_type_label(actual),
    )





def _downstream(node):
    result, queue = [], [node]
    while queue:
        current = queue.pop(0)
        if current in result: continue
        result.append(current)
        for nodes in current.connected_output_nodes().values(): queue.extend(nodes)
    return result





def _ancestors(node):
    order, seen = [], set()
    def visit(current):
        if current.id in seen: return
        seen.add(current.id)
        for nodes in current.connected_input_nodes().values():
            for parent in nodes: visit(parent)
        order.append(current)
    visit(node); return order





def _feed_sequence_frames(panel, result) -> None:
    """把可逐帧查看的帧路径喂给面板滑条。

    - ``SequenceArtifact`` → 全部帧；
    - ``AnalysisResult`` 携带 ``frames``（图片1:1分辨率查看的序列输入）→ 全部帧；
    - ``MultiOutput``（RGBA 通道分离）→ 首个通道的帧（红通道）；
    - 其余结果（1:1 查看节点的 GIF/视频清单项等）→ **清空滑条帧状态**，
      否则旧序列帧残留导致「串台」：先预览序列、再浏览清单项时，
      拖动滑条会混入上一轮的帧（历史回归点，见 docs/testing.md「历史验证清单」）。
    """
    if isinstance(result, SequenceArtifact):
        panel.set_sequence_frames(result.frames)
    elif isinstance(result, AnalysisResult) and result.frames:
        panel.set_sequence_frames(list(result.frames))
    elif isinstance(result, MultiOutput):
        first = next(iter(result.ports.values()), None)
        if isinstance(first, SequenceArtifact):
            panel.set_sequence_frames(first.frames)
        else:
            panel.set_sequence_frames([])
    else:
        panel.set_sequence_frames([])



def _resolve_port_preview(data, output_port) -> Any:
    """多输出上游的预览取值：按**实际连接的上游输出端口名**解析（与执行
    ``_execute_step`` 同源——``produced + port_name → MultiOutput.ports[port_name]``）。

    修复（决策 #128）：胶片条/裁剪框接管的「上游源图」此前取 ``MultiOutput``
    「首个通道」，导致链式剃刀（连段B仍显示段A）、RGBA 通道分离链（连 G 仍
    显示 R）的预览与实际执行喂给 ``execute`` 的输入不一致——本函数让预览
    取数镜像执行取数，两个取数路径不再分叉。

    - 非 ``MultiOutput``（单输出节点）：原样返回；
    - ``MultiOutput`` 且端口名已知：返回 ``ports[port_name]``（分支缺失返回
      ``None``——预览宁可不显示，也不显示错误分支）；
    - 端口名为空（理论不发生）：返回 ``None``。
    """
    if isinstance(data, MultiOutput):
        name = output_port.name() if output_port is not None else ""
        return data.ports.get(name) if name else None
    return data


def cache_usage_warning_text(usage_bytes: int, limit_bytes: int) -> str:
    """缓存用量超过设定上限的纯文本状态栏警告（决策 #130）。

    纯文本约束（用户需求）：不使用 emoji/特殊符号——特殊字符会让带该文本的
    常驻状态栏控件在软件初始化时 addPermanentWidget 变慢，且警告文案应保持
    干净可读。超过上限 = 现有自动清理（enforce_cache_limit，0.8×上限触发）
    已尽力（每节点最新结果受保护）仍无法把总量压回上限以内，用户需要知情。
    """
    usage = format_bytes(max(0, usage_bytes))
    limit = format_bytes(max(0, limit_bytes))
    return f"缓存用量 {usage} 已超过设定上限 {limit}，请清理缓存或调高设置中的缓存上限"


def post_run_cache_message(note: str | None, usage_bytes: int | None, limit_bytes: int | None) -> str | None:
    """节点运行完成后的缓存状态栏消息决策（决策 #130）。

    - 已统计到用量且超过设定上限 → 纯文本警告（优先于清理提示——此时自动
      清理已尽力仍压不回上限，用户需要知情）；
    - 未超限但本次发生过自动淘汰（note 非空）→ 原清理提示；
    - 否则 None（不打扰，维持「节点运行完成」消息）。
    """
    if limit_bytes is not None and usage_bytes is not None and usage_bytes > limit_bytes:
        return cache_usage_warning_text(usage_bytes, limit_bytes)
    return note



class MainWindow(QtWidgets.QMainWindow):
    # 自动模式全局执行限频：每秒最多 3 次（防止拖动参数时频繁重算）。
    AUTO_MIN_INTERVAL = 1.0 / 3.0

    # faulthandler 检测到疑似卡死（主线程心跳过期）时发出；观察者在
    # faulthandler 定时器线程调用 emit → 自动排队到 GUI 线程执行槽。
    unstable_detected = QtCore.Signal(str)

    # --- 工具栏动作别名（构建时经 setattr 挂载，见 actions.ACTIONS[].alias） ---
    # 类级注解仅供 IDE 静态解析（from __future__ import annotations 下为惰性
    # 字符串，不产生类属性、零运行时开销）；实际挂载仍由 _create_pure_action /
    # _build_menu_items / _build_toolbar 完成。
    stop_action: QtGui.QAction
    auto_action: QtGui.QAction
    undo_action: QtGui.QAction
    redo_action: QtGui.QAction
    save_json_action: QtGui.QAction
    read_json_action: QtGui.QAction
    open_presets_folder_action: QtGui.QAction
    del_node_action: QtGui.QAction
    select_all_action: QtGui.QAction
    clone_action: QtGui.QAction
    fit_to_selection_action: QtGui.QAction
    reset_zoom_action: QtGui.QAction
    layout_down_action: QtGui.QAction
    clc_cache_action: QtGui.QAction
    settings_action: QtGui.QAction
    pipe_toggle_action: QtGui.QAction
    bg_toggle_action: QtGui.QAction

    def __init__(self, workspace=None, settings: SettingsManager | None = None):
        super().__init__()
        # 设置管理器（QSettings，用户数据目录 settings.ini）：应用启动时恢复
        # 保存的连线样式/背景网格（颜色主题固定为深色，见决策 #90）；测试可
        # 注入临时路径的管理器。
        self.settings = settings if settings is not None else SettingsManager()
        # 底部状态栏使用继承重写的 StatusBar：未指定持续时间时统一
        # 3Hz 闪烁 3 下 → 原消息持续 5 秒（见 StatusBar 类说明）。
        self.setStatusBar(StatusBar(self))
        # 不稳定警告：faulthandler 检测到疑似卡死时常驻状态栏右侧（红色），
        # 运行成功/保存方案后自动隐藏（软件已恢复响应）。
        self._unstable_warning = QtWidgets.QLabel("软体不稳定！请注意保存！")  # 此处不使用特殊符号能减少addPermanentWidget的阻塞时间
        self._unstable_warning.setStyleSheet("color:#ff6b6b;font-weight:bold;")
        self._unstable_warning.hide()
        _pump_startup_events()
        self.statusBar().addPermanentWidget(self._unstable_warning)  # 此步耗时较长
        _pump_startup_events()
        self.unstable_detected.connect(self._on_unstable_detected)
        add_faulthandler_observer(self.unstable_detected.emit)
        # GUI 心跳：5 秒一次；faulthandler 转储时据此判断主线程是否已卡死
        # （心跳过期 = 超过 30s 未泵动事件循环 → 通知不稳定）。
        self._heartbeat_timer = QtCore.QTimer(self)
        self._heartbeat_timer.setInterval(5000)
        self._heartbeat_timer.timeout.connect(heartbeat)
        self._heartbeat_timer.start()
        # 日志初始化（进程内幂等）：节点执行日志写入用户数据目录 logs/app.log。
        setup_logging()
        self.setWindowTitle("GIF Node Studio")
        # 临时缓存目录：默认用户数据目录下 cache/（打包后为 LOCALAPPDATA），
        # 可在设置中修改（下次启动生效）；workspace 参数（测试注入）优先。
        self.backend = MediaBackend(workspace or self.settings.cache_dir())
        self.backend.clear_workspace()
        self.worker = AsyncExecutionWorker(self); self.active_request = None; self.active_revisions = {}; self.operation_elapsed = {}; self.auto_mode = False
        # 缓存超限淘汰提示：工作线程写入（_execute_step），UI 线程在运行完成时显示并清空。
        self._cache_eviction_note = None
        # 自动保存（决策 #131）：间隔由设置指定（分钟，0=关闭）；仅当方案有未保存
        # 更改时落盘到设置文件旁的 autosave.json；正式保存/正常退出清理该文件，
        # 异常退出残留 → 下次启动弹窗提示恢复（app.py 生产路径调用 prompt_autosave_restore）。
        self._autosave_timer = QtCore.QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave_tick)
        self._apply_autosave_settings()
        self._auto_pending: list[StudioNode] = []
        self._auto_last_run = 0.0
        self._auto_timer = QtCore.QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self._flush_auto_run)
        # 画布重绘冻结状态（窗口最小化时 setUpdatesEnabled(False)，恢复时解冻）。
        self._view_frozen = False
        # 构造期事件泵动（决策 #103）：splash 动画在阶段间隙持续推进。
        _pump_startup_events()
        self.graph = NodeGraph()
        # 节点图壳色（决策 #117 方案 A）：画布/网格/选中描边；节点体/边框在
        # StudioNode 构造时设置（node_base.py），两者读同一 DARK 令牌。
        apply_graph_theme(self.graph)
        # 右键菜单命令引用表（单 action 管理器）：_build_graph_context_menu 登记，
        # _build_toolbar 复用同一 QAction（工具栏不再为同一命令创建独立动作）。
        self._ctx_commands: dict[str, object] = {}
        # 方案会话状态：当前保存路径（None = 尚未保存）与干净标记。
        # 干净标记以撤销栈为唯一事实源（决策 #85）：_clean_undo_pos 记录
        # 保存/读取时的 (count, index)；None = 从未保存（画布有节点即脏）。
        # 是否脏由 _session_dirty 只读属性派生（画布无节点恒为干净），
        # 代码内不再散落赋值。
        self._session_path: Path | None = None
        self._clean_undo_pos: tuple[int, int] | None = None
        _pump_startup_events()
        # 移除 NodeGraphQt 视图自带的撤销/重做菜单项：顶栏撤销/重做为唯一来源，
        # 否则同一窗口内两个 Ctrl+Z 动作在 Qt 快捷键映射中歧义，两者都不触发。
        graph_qmenu = self.graph.get_context_menu("graph").qmenu
        for action in list(graph_qmenu.actions()):
            if action.text() in ("&Undo", "&Redo"):
                graph_qmenu.removeAction(action)
        # 顺带清掉撤销/重做项留下的前导分隔线（viewer 在其后 addSeparator）。
        while graph_qmenu.actions() and graph_qmenu.actions()[0].isSeparator():
            graph_qmenu.removeAction(graph_qmenu.actions()[0])
        # 应用启动时恢复上次保存的连线样式/背景网格（设置界面不可见，
        # 由工具栏切换后自动保存；默认：曲线连线 + 网格线背景）。
        apply_pipe_style(self.graph, self.settings.pipe_style())
        apply_grid_mode(self.graph, self.settings.grid_mode())
        # 上下文菜单（Graph/Nodes/Pipes）由代码构建，不再读取 hotkeys.json。
        self._build_graph_context_menu()

        # 背景框：用支持标题就地重命名的 EditableBackdropNode 替换 NodeGraphQt 的
        # 内建注册（类型字符串不变，见 backdrop 模块 docstring——旧存档反序列化
        # 同样得到就地重命名能力）。内建注册在 NodeGraph.__init__ 时写入工厂，
        # 公共 API 无「按类型替换」能力（重复注册会抛错），故先清空工厂再整体
        # 注册（内建仅注册了 BackdropNode，无其他内置节点，清空无副作用）。
        _pump_startup_events()
        factory = self.graph.node_factory
        factory.clear_registered_nodes()
        self.graph.register_node(EditableBackdropNode, alias="Backdrop")
        self.graph.register_nodes(NODE_CLASSES)
        self.setCentralWidget(self.graph.widget)
        self.help = HelpWidget()
        help_dock = QtWidgets.QDockWidget("用法说明/元数据", self)
        help_dock.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        help_dock.setFixedWidth(280)
        help_dock.setWidget(self.help)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, help_dock)
        self.panels = {}
        self.node_category_groups = {}
        self._hovered_button: LibraryButton | None = None
        self._selected_node: StudioNode | None = None
        _pump_startup_events()
        self._build_node_library()
        _pump_startup_events()
        self._build_toolbar()
        _pump_startup_events()
        self._build_context_menus()
        # 主窗口菜单栏（QMenuBar）最后构建：画布菜单命令经 _ctx_commands 复用，
        # 纯工具栏动作经 alias 解析（_action_for_key 对未构建者按需物化，
        # 因此 TOOLBAR 精简后 MENUBAR 引用的动作仍可用）。
        _pump_startup_events()
        self._build_menu_bar()
        _pump_startup_events()
        self.graph.node_created.connect(self._on_node_created)
        self.graph.node_selected.connect(self._on_selected)
        self.graph.node_selection_changed.connect(self._on_selection_changed)
        self.graph.nodes_deleted.connect(self._on_nodes_deleted)
        self.graph.port_connected.connect(self._on_port_connected)
        self.graph.port_disconnected.connect(self._on_connection_changed)
        # 注：不再连接 property_changed 标记方案未保存——一切状态变更
        # （含节点改名/背景框缩放）都经 QUndoStack 入栈，方案 dirty 由
        # 撤销栈 (count, index) 派生（决策 #85）。
        self.worker.succeeded.connect(self._run_succeeded)
        self.worker.failed.connect(self._run_failed)
        self.worker.cancelled.connect(self._run_cancelled)
        self.worker.started.connect(lambda _request_id: self.stop_action.setEnabled(True))
        self.worker.rejected.connect(lambda _id: self.statusBar().showMessage("已有任务运行中"))
        self.worker.step_started.connect(self._step_started)
        self.worker.step_succeeded.connect(self._step_succeeded)
        self.worker.operation_timing.connect(lambda request_id, elapsed: self.operation_elapsed.__setitem__(request_id, elapsed))
        self.worker.progress.connect(self._on_progress)
        self.statusBar().showMessage(f"缓存：{self.backend.workspace}")
        logger.info(f"缓存：{self.backend.workspace}")
        # 空格键 = 切换自动模式（全局事件过滤器；焦点在数值/文本输入框或弹窗打开时放行）。
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # --- C++ 虚函数覆盖必须在类体直接定义（决策 #85/#98：单类后无 mixin 可移） ---
    # PySide6/shiboken 的 C3 线性化把 Qt 类（QMainWindow/QWidget/QObject）排在
    # 所有 mixin 之前：定义在 mixin 里的 closeEvent/changeEvent/eventFilter 会被
    # C++ method_descriptor 遮蔽（决策 #82 拆分后关闭保存提示、空闲暂停、空格键
    # 切换全部静默失效），且 mixin 内的 super() 因 Qt 类不在其后而抛
    # AttributeError。这三个虚函数覆盖只能写在类体里（类体定义先于 MRO 命中，
    # super() 也正常解析到 QWidget 原实现）。

    def closeEvent(self, event):
        """关闭前保存提示（决策 #64/#85）+ 缓存清理与撤销栈断开。"""
        # 关闭前保存提示：有未保存更改时询问 保存/不保存/取消
        # （取消或另存为被取消则中止关闭）。
        if self._session_dirty and not self._confirm_unsaved_changes("关闭"):
            event.ignore()
            return
        # 正常退出：清理自动保存文件（决策 #131）——正式关闭意味着不再需要
        # 崩溃恢复副本；若此处异常退出/强杀进程，文件残留 → 下次启动提示恢复。
        self._remove_autosave()
        session_path = self._session_path
        # 若方案文件恰好落在缓存工作目录内，清理前先暂挪到系统临时目录、
        # 清理后还原——避免 clear_workspace 把用户刚保存的方案当缓存删掉。
        relocated: Path | None = None
        try:
            if session_path is not None and session_path.exists():
                resolved = session_path.resolve()
                if resolved.parent == self.backend.workspace.resolve():
                    fd, tmp_name = tempfile.mkstemp(prefix="agif_preset_", suffix=".json")
                    os.close(fd)
                    relocated = Path(tmp_name)
                    relocated.write_bytes(resolved.read_bytes())
        except OSError:
            relocated = None
        try:
            self.worker.shutdown()
            release_previews(self.graph.all_nodes())
            self.backend.clear_workspace()
        finally:
            if relocated is not None and session_path is not None:
                try:
                    session_path.parent.mkdir(parents=True, exist_ok=True)
                    session_path.write_bytes(relocated.read_bytes())
                except OSError:
                    pass
                finally:
                    try:
                        relocated.unlink(missing_ok=True)
                    except OSError:
                        pass
        # 断开撤销栈的 indexChanged，避免销毁顺序不定时槽访问已删除的 QAction。
        try:
            self.graph.undo_stack().indexChanged.disconnect(self._update_undo_redo_actions)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (
            QtCore.QEvent.Type.WindowStateChange,
            QtCore.QEvent.Type.ActivationChange,
            QtCore.QEvent.Type.Hide,
            QtCore.QEvent.Type.Show,
        ):
            self._apply_visibility_pause()

    def event(self, event):
        # 窗口跨屏拖动（显示器放大倍率变化）→ 防抖后按当前 DPR 重建 1:1 预览。
        # QEvent.ScreenChangeInternal 是发送给顶层窗口的 Change 事件，但
        # QWidget::event 对它只更新 winId、不会转发 changeEvent，必须在此拦截。
        if event.type() == QtCore.QEvent.Type.ScreenChangeInternal:
            self._on_screen_dpr_changed()
        return super().event(event)

    def eventFilter(self, obj, event):
        """全局空格键绑定：切换「自动」开关。焦点在数值/文本输入组件时放行给输入框
        （避免打断键入）；菜单/下拉等弹出层或模态对话框打开时同样放行。"""
        if event.type() == QtCore.QEvent.Type.ShortcutOverride:
            # 全局快捷键（右键菜单动作：Ctrl+C/X/V、Ctrl+A、D/F/L/H 等）在
            # 文本/数值输入组件聚焦时被拦截：接受 override 让按键落到输入框，
            # 而不是触发节点编辑操作（与空格键放行逻辑同一原则）。
            if QtWidgets.QApplication.activeModalWidget() is not None:
                return super().eventFilter(obj, event)
            if QtWidgets.QApplication.activePopupWidget() is not None:
                return super().eventFilter(obj, event)
            focus = QtWidgets.QApplication.focusWidget()
            if isinstance(
                focus,
                (QtWidgets.QAbstractSpinBox, QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit),
            ):
                event.accept()
                return True
        if event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Space:
            if QtWidgets.QApplication.activeModalWidget() is not None:
                return super().eventFilter(obj, event)
            if QtWidgets.QApplication.activePopupWidget() is not None:
                return super().eventFilter(obj, event)
            focus = QtWidgets.QApplication.focusWidget()
            if not isinstance(
                focus,
                (QtWidgets.QAbstractSpinBox, QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit),
            ):
                self.auto_action.toggle()
                return True
        return super().eventFilter(obj, event)

    @property
    def _session_dirty(self) -> bool:
        """方案是否有未保存更改（只读，以撤销栈为唯一事实源派生，决策 #85）。

        状态机：
        - 画布上没有节点 → clean（无内容可保存，不提示保存）；
        - 画布有节点但从未保存（``_clean_undo_pos is None``）→ dirty；
        - 画布有节点且撤销栈 ``(count, index)`` 与保存/读取时一致 → clean；
        - 其余 → dirty（任一状态变更都会入撤销栈：节点增删/拖拽/连线/参数/
          改名/背景框缩放/克隆，以及撤销、重做移动 index、清空撤销历史）。

        注意：比较的是 ``(count, index)`` 整体——保存后即使撤销回保存时的
        index，count 也已变化（列表里多了新命令），仍算 dirty；只有撤销栈
        完全回到保存时形态才恢复干净。
        """
        if not self.graph.all_nodes():
            return False
        if self._clean_undo_pos is None:
            return True
        try:
            stack = self.graph.undo_stack()
            return (stack.count(), stack.index()) != self._clean_undo_pos
        except RuntimeError:
            # 窗口/撤销栈销毁过程中（C++ 侧对象已删除）保守视为未保存。
            return True

    # ===== 区段 1：菜单/工具栏/节点库/右键菜单构建（原 MainWindowMenusMixin） =====

    def _build_node_library(self):
        self.node_library_dock = QtWidgets.QDockWidget("节点库", self)
        self.node_library_dock.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self.node_library_dock.setFixedWidth(280)
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        for category in NodeCategory:
            box = QtWidgets.QGroupBox(category.value)
            # 列表式：每分类一组单列 QVBoxLayout（整行「左图标+右标题」按钮，
            # 曾改为 2 列方形网格但使用效率不高，已按 git 历史改回列表式）。
            group = QtWidgets.QVBoxLayout(box)
            self.node_category_groups[category] = box
            # pump（决策 #103）：node_definitions 首次调用 = 46 个节点控件
            # 实例化的耗时热点（~0.6s），其内部每 4 个节点泵动一次事件循环，
            # splash 动画在热点期间持续推进；缓存命中后 pump 不再被调用。
            for definition in (d for d in node_definitions(pump=_pump_startup_events) if d.category is category):
                button = LibraryButton(definition, node_help_by_kind(definition.kind))
                button.hover_entered.connect(self._on_library_hover)
                button.hover_left.connect(self._on_library_leave)
                button.clicked.connect(lambda _checked=False, kind=definition.kind: self.create_node(kind))
                group.addWidget(button)
            if category is NodeCategory.BACKDROP:
                # 背景框不是 StudioNode（不在 NODE_CLASSES 注册表/执行链中）：
                # 单独添加按钮，点击走 create_backdrop。
                button = LibraryButton(backdrop_definition(), BACKDROP_HELP)
                button.hover_entered.connect(self._on_library_hover)
                button.hover_left.connect(self._on_library_leave)
                button.clicked.connect(lambda _checked=False: self.create_backdrop())
                group.addWidget(button)
            layout.addWidget(box)
        layout.addStretch()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        # 共用进度条：运行行为决定了同一时刻只会执行一个节点，因此节点库底部只需一根进度条即可反映全部节点的执行进度。
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setVisible(False)
        # 进度文本独立于进度条承载：QProgressBar 在不确定模式（range 0,0，
        # 即 fraction=None 的「忙」状态）下 ``text()`` 恒返回空字符串（Qt
        # 行为），format 里的 label 永远不会被绘制——因此把文本移到独立的
        # QLabel，进度条本体只负责百分比（确定模式）或忙动画（不确定模式）。
        self.progress_label = QtWidgets.QLabel()
        self.progress_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.progress_label.setStyleSheet("color:#9aa0aa;")
        self.progress_label.setVisible(False)
        outer = QtWidgets.QWidget()
        outer_layout = QtWidgets.QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(scroll, 1)
        outer_layout.addWidget(self.progress_label)
        outer_layout.addWidget(self.progress_bar)
        self.node_library_dock.setWidget(outer)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, self.node_library_dock)

    # --- 不稳定警告（faulthandler 心跳检测，GUI 线程执行） ---


    def _build_graph_context_menu(self):
        """画布右键菜单：按 ``actions.GRAPH_MENU`` 布局构建（纯代码，单 action 管理器）。"""
        menu = self.graph.get_context_menu("graph")
        for top_label, items in GRAPH_MENU:
            self._build_menu_items(menu.add_menu(top_label), items)


    def _build_menu_items(self, parent, items) -> None:
        """递归构建画布菜单项：动作键 / 子菜单 / 分隔线。"""
        for item in items:
            if item is SEP:
                parent.add_separator()
                continue
            if isinstance(item, tuple):
                sub_label, sub_items = item
                self._build_menu_items(parent.add_menu(sub_label), sub_items)
                continue
            adef = ACTIONS[item]
            command = parent.add_command(adef.label, self._resolve_handler(adef), shortcut=adef.shortcut)
            self._apply_action_icon(command.qaction, adef)
            self._ctx_commands[adef.key] = command
            if adef.alias:
                # 与 _build_toolbar 复用路径一致：菜单命令的别名也挂到 MainWindow，
                # 别名存在性不依赖 TOOLBAR 成员资格（edit.clone→clone_action 等）。
                setattr(self, adef.alias, command.qaction)


    def _build_context_menus(self):
        """节点右键菜单：按 ``actions.NODE_MENU`` 布局构建（选中集语义）。

        - 命令登记在指定节点类上一次，NodeGraphQt 的 ``NodesMenu.add_command
          (node_class=...)`` 会自动把动作附加到全部子类节点的子菜单
          （``BaseMenu.get_menu`` 按 isinstance 命中）；
        - 删除/克隆作用于**全部选中节点**（右键节点未选中时仅作用于该节点）；
        - 快捷键单点登记：清空连线 Ctrl+D、适配选中 F 由节点菜单持有；
          Ctrl+C（克隆）由画布「克隆」持有——节点级动作的 node_id 仅在
          右键弹菜单时注入，不能作为全局快捷键。
        """
        menu = self.graph.context_nodes_menu()
        for node_class, groups in NODE_MENU:
            for group_index, group in enumerate(groups):
                for key in group:
                    adef = ACTIONS[key]
                    command = menu.add_command(
                        adef.label, self._resolve_handler(adef),
                        node_class=node_class, shortcut=adef.shortcut)
                    self._ctx_commands[adef.key] = command
                    if adef.alias:
                        setattr(self, adef.alias, command.qaction)
                if group_index < len(groups) - 1:
                    sub = menu.qmenu.get_menu(node_class.__name__)
                    if sub is not None:
                        sub.addSeparator()


    def _create_pure_action(self, adef):
        """按 ActionDef 创建纯工具栏动作（非菜单命令）并挂到 alias 属性。

        动作的物化点：被任意布局（TOOLBAR / MENUBAR）引用时按需创建，
        别名存在性不再由 TOOLBAR 成员资格决定——从 TOOLBAR 移除某键而
        菜单栏仍引用时不会 AttributeError。
        """
        action = QtGui.QAction(adef.label, self)
        self._apply_action_icon(action, adef)
        _set_action_shortcut(action, adef.shortcut)
        handler = self._resolve_handler(adef)
        if adef.checkable:
            action.setCheckable(True)
            action.toggled.connect(handler)
        else:
            action.triggered.connect(handler)
        if adef.alias:
            setattr(self, adef.alias, action)
        return action


    def _build_toolbar(self):
        """顶栏：按 ``actions.TOOLBAR`` 布局构建（单 action 管理器）。

        菜单+工具栏双挂的动作直接复用画布菜单的同一 QAction（含快捷键与图标），
        纯工具栏动作经 _create_pure_action 新建；标签/图标/快捷键/处理函数
        均来自 actions.ACTIONS。
        """
        toolbar = self.addToolBar("方案与运行")
        toolbar.setOrientation(QtCore.Qt.Orientation.Horizontal)
        toolbar.setMovable(False)
        self.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, toolbar)
        for key in TOOLBAR:
            if key is SEP:
                toolbar.addSeparator()
                continue
            adef = ACTIONS[key]
            if key in self._ctx_commands:
                action = self._ctx_commands[key].qaction  # 复用菜单同一 QAction
                if adef.alias:
                    setattr(self, adef.alias, action)
            else:
                action = self._create_pure_action(adef)
            toolbar.addAction(action)
        # 撤销/重做（NodeGraphQt 内置 QUndoStack）：可用状态随撤销栈 index 变化
        # 更新（视图自带撤销/重做菜单项已在 __init__ 移除，避免同键歧义）。
        undo_stack = self.graph.undo_stack()
        undo_stack.indexChanged.connect(self._update_undo_redo_actions)
        # 撤销/重做后把模型属性同步回 node.params 与参数面板（PropertyChangedCmd
        # 只更新模型数据，面板/params 不会自动恢复，见 _on_undo_index_changed）。
        undo_stack.indexChanged.connect(self._on_undo_index_changed)
        self._last_undo_pos = (undo_stack.count(), undo_stack.index())
        self._update_undo_redo_actions()
        # 停止按钮初始禁用（仅运行中可用）。
        self.stop_action.setEnabled(False)


    def _action_for_key(self, key):
        """按动作键解析到实际 QAction（单 action 管理器）：

        画布/节点菜单命令 → ``_ctx_commands[key].qaction``；
        纯工具栏动作 → 构建时经 alias 挂在 MainWindow 上；若尚未构建
        （该动作只被 TOOLBAR 之外的布局引用，如 MENUBAR），按需经
        _create_pure_action 物化。
        """
        if key in self._ctx_commands:
            return self._ctx_commands[key].qaction
        adef = ACTIONS[key]
        if not adef.alias:
            return None
        action = getattr(self, adef.alias, None)
        if action is None:
            action = self._create_pure_action(adef)
        return action


    def _build_menu_bar(self):
        """主窗口菜单栏（QMenuBar）：按 ``actions.MENUBAR`` 布局列出全部
        动作，多级子菜单用嵌套元组表达。

        菜单项复用与右键菜单/工具栏**同一** QAction（见 _action_for_key），
        因此快捷键、图标、勾选状态（如「自动」）与别处自动一致；
        节点级动作依赖右键菜单注入的 node_id，不放入菜单栏。
        """
        bar = self.menuBar()
        for top_label, items in MENUBAR:
            top_menu = bar.addMenu(top_label)
            self._populate_menu(top_menu, items)


    def _populate_menu(self, qmenu, items) -> None:
        """按布局表填充 QMenu：动作键 / 子菜单 / 分隔线 / 动态预设子菜单。"""
        for item in items:
            if item is SEP:
                qmenu.addSeparator()
                continue
            if item is PRESETS_SUBMENU:
                self._build_presets_submenu(qmenu)
                continue
            if isinstance(item, tuple):
                sub_label, sub_items = item
                self._populate_menu(qmenu.addMenu(sub_label), sub_items)
                continue
            action = self._action_for_key(item)
            if action is not None:
                qmenu.addAction(action)

    # --- 「导入预设」动态子菜单（用户需求，见决策 #89） ---
    # 菜单栏「文件」下的快捷入口：列出 node_presets 目录中的全部 *.json
    # 项目预设，点击逐个**增量导入**（合并进当前画布，不清空现有节点——
    # 与「导入方案…」相同语义，但路径直接来自预设文件夹，不弹文件框）。
    # 内容在每次打开菜单时刷新（aboutToShow）：预设文件增删后无需重启。

    def _build_presets_submenu(self, parent_qmenu) -> None:
        sub = parent_qmenu.addMenu("导入预设")
        sub.menuAction().setIcon(qta.icon("mdi.folder-multiple-image"))
        sub.aboutToShow.connect(lambda: self._refresh_presets_submenu(sub))
        self._refresh_presets_submenu(sub)

    def _refresh_presets_submenu(self, sub) -> None:
        """重列 node_presets 目录中的预设文件；目录缺失/为空时给出引导项。"""
        sub.clear()
        directory = node_presets_dir()
        presets = sorted(directory.glob("*.json")) if directory.is_dir() else []
        for path in presets:
            action = sub.addAction(path.stem)
            # triggered 会带 checked 参数：用 lambda 吞掉（同节点库按钮模式）。
            action.triggered.connect(lambda _checked=False, p=str(path): self.import_preset_file(p))
        if not presets:
            empty = sub.addAction("（预设文件夹为空：node_presets/）")
            empty.setEnabled(False)
            sub.addSeparator()
        open_action = self._action_for_key("file.preset.open_folder")
        if open_action is not None:
            sub.addAction(open_action)

    # --- 右键菜单命令实现（节点级，func(graph, node)） ---


    def _resolve_handler(self, adef):
        """把 ActionDef.handler 解析为可调用对象（str = MainWindow 方法名）。"""
        if callable(adef.handler):
            return adef.handler
        return getattr(self, adef.handler)

    def _apply_action_icon(self, action, adef) -> None:
        """按动作定义设置 qtawesome 图标（图标名/颜色均在 actions.py 一处定义）。"""
        if adef.icon:
            kwargs = {"color": adef.icon_color} if adef.icon_color else {}
            action.setIcon(qta.icon(adef.icon, **kwargs))


    def _ctx_delete_node(self, graph, node):
        """删除节点：右键节点在选中集中 → 删除全部选中节点（用户需求）；
        右键节点未选中 → 仅删除该节点（不牵连既有选中集）。"""
        if node is not None and node in graph.selected_nodes():
            self.delete_selection()
        elif node is not None:
            self.delete_nodes([node])
        else:
            self.delete_selection()


    def _ctx_duplicate_node(self, graph, node):
        """克隆节点：与删除同款语义——右键节点在选中集中 → 克隆全部选中；
        右键节点未选中 → 仅克隆该节点。"""
        if node is not None and node in graph.selected_nodes():
            self.clone_selection()
        elif node is not None:
            self._clone_nodes([node])
        else:
            self.clone_selection()


    def _ctx_clear_connections(self, graph, node):
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能清空连线")
            return
        graph.undo_stack().beginMacro(f"清空 {node.NODE_NAME} 连线")
        try:
            for port in node.input_ports() + node.output_ports():
                port.clear_connections()
        finally:
            graph.undo_stack().endMacro()
        self._mark_dirty(node)


    def _ctx_fit_to_selection(self, graph, node):
        # 右键节点可能未被选中：以该节点为唯一选中对象执行「适配选中」。
        graph.clear_selection()
        node.set_selected(True)
        graph.fit_to_selection()


    def _ctx_rename_backdrop(self, graph, node):
        # 右键菜单「重命名背景框」：触发标题就地编辑（与双击标题栏同机制，无弹窗）。
        node.view.begin_title_edit()

    def _ctx_backdrop_title_height(self, graph, node):
        """右键菜单「标题栏高度…」：QInputDialog 数值框调整背景框标题栏高度。

        写入节点属性 ``title_bar_height``（存档随背景框属性持久化），
        经 ``EditableBackdropNode.set_property`` 同步到视图重排/重绘。
        """
        value, ok = QtWidgets.QInputDialog.getInt(
            self, "标题栏高度",
            "背景框标题栏高度（像素）：",
            int(node.get_property("title_bar_height")),
            TITLE_BAR_HEIGHT_MIN, TITLE_BAR_HEIGHT_MAX, 1,
        )
        if ok:
            node.set_property("title_bar_height", value)

    # --- 画布菜单命令实现（graph 级，func(graph)） ---


    def _ctx_quit(self, graph):
        """文件菜单「退出」：关闭主窗口（走 closeEvent 的未保存提示与清理）。"""
        self.close()


    def _finalize_pasted(self, pasted) -> list:
        """粘贴/克隆后的通用收尾：同步模型属性 → self.params → 面板并绑定回调。

        与 load_preset 相同的参数同步（见决策 #24），使粘贴产生的节点功能完整
        （可运行/导出/联动自动模式），参数与源节点一致、状态为脏。
        """
        clones = [node for node in pasted if isinstance(node, StudioNode)]
        for node in clones:
            node.params = {name: node.get_property(name) for name in node.params}
            node.panel.set_values(node.params)
            self._bind_node(node)
            node.set_status("dirty")
        return clones

    # ===== 区段 2：节点创建/绑定/选择/参数回调（原 MainWindowNodeMixin） =====

    def create_node(self, kind):
        viewer = self.graph.viewer()
        center = viewer.mapToScene(viewer.viewport().rect().center())
        node_class = node_class_by_kind(kind)
        node = self.graph.create_node(
            f"{node_class.__identifier__}.{node_class.__name__}",
            # color='#1e1e1e'  # fution暗黑色
        )
        bounds = node.view.boundingRect()
        # 随机位置偏移：新节点默认都落在画布中心，连续新建会完全重叠；
        # 以节点尺寸的比例随机错开（±10%），避免多个节点堆在一起。
        jitter_x = random.uniform(-0.10, 0.10) * bounds.width()
        jitter_y = random.uniform(-0.10, 0.10) * bounds.height()
        # push_undo=False：创建后居中定位不应产生撤销记录，否则第一次撤销只会
        # 把节点移回原位而不是删除刚创建的节点。
        node.set_property(
            "pos",
            [
                center.x() - bounds.width() / 2 + jitter_x,
                center.y() - bounds.height() / 2 + jitter_y,
            ],
            push_undo=False,
        )
        # 创建节点后无需点击节点即可在右侧面板查看其说明。
        self._selected_node = node
        self._refresh_help()
        return node


    def create_backdrop(self):
        """创建背景框（EditableBackdropNode，分组框，不参与执行链）。

        - 若当前有节点处于选中状态：新背景框尺寸覆盖选中节点的范围（含 40px 边距）；
        - 否则：在画布中央创建默认尺寸背景框。
        尺寸/位置以 push_undo=False 写入，视为“创建”的一部分——第一次撤销即删除
        背景框（与 create_node 对 pos 的处理一致，避免撤销只把框移回原位）。

        节点类型字符串由 EditableBackdropNode.type_ 派生（与内建 BackdropNode
        相同的 "nodeGraphQt.nodes.BackdropNode"，见 backdrop 模块 docstring）。
        """
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能创建背景框")
            return
        selected = [n for n in self.graph.selected_nodes() if not isinstance(n, BackdropNode)]
        node = self.graph.create_node(EditableBackdropNode.type_, name=backdrop_definition().title)
        if selected:
            # 覆盖选中节点的范围（BackdropNodeItem.calc_backdrop_size：节点外扩 40px）。
            size = node.view.calc_backdrop_size([n.view for n in selected])
        else:
            viewer = self.graph.viewer()
            center = viewer.mapToScene(viewer.viewport().rect().center())
            bounds = node.view.boundingRect()
            size = {
                "pos": [center.x() - bounds.width() / 2, center.y() - bounds.height() / 2],
                "width": bounds.width(),
                "height": bounds.height(),
            }
        node.set_property("width", size["width"], push_undo=False)
        node.set_property("height", size["height"], push_undo=False)
        node.set_property("pos", [float(size["pos"][0]), float(size["pos"][1])], push_undo=False)
        message = f"已创建背景框（覆盖 {len(selected)} 个选中节点）" if selected else "已创建背景框"
        self.statusBar().showMessage(message)
        return node


    def _on_node_created(self, node):
        self._bind_node(node)


    def _bind_node(self, node):
        """Attach application callbacks to both new and deserialized nodes."""
        if not isinstance(node, StudioNode) or node.id in self.panels:
            return
        self.panels[node.id] = node.panel
        node.panel.changed.connect(lambda values, n=node: self._params_changed(n, values))
        node.panel.gesture_begin.connect(lambda n=node: self._param_gesture_begin(n))
        node.panel.gesture_end.connect(lambda n=node: self._param_gesture_end(n))
        node.panel.run_requested.connect(lambda n=node: self._on_node_run_clicked(n))
        node.panel.export_requested.connect(lambda n=node: self.export_node(n))
        # 注入透明背景色（1:1 查看节点「透明背景」勾选后使用；其余节点无该参数则无效果）。
        node.panel.set_preview_bg_color(self._alpha_bg_color())
        # 注入设置管理器：文件选择行（FilePathWidget）读/写「上次导入目录」记忆。
        node.panel.settings = self.settings


    def _on_selected(self, node):
        self._selected_node = node if isinstance(node, StudioNode) else None
        self._refresh_help()
        # node.panel.show_preview(self.preview_path_for_node(node))  # 拟废弃，点击节点时，无需刷新prev

    # --- 节点说明面板：悬停节点库按钮 > 当前选中节点 > 默认文案 ---


    def _on_library_hover(self, button):
        self._hovered_button = button
        self.help.show_definition(button.definition, button.help_text)


    def _on_library_leave(self):
        self._hovered_button = None
        self._refresh_help()


    def _refresh_help(self):
        """刷新右侧说明面板：优先显示鼠标滑过的节点库按钮说明；
        没有滑过按钮时显示当前选中节点的说明；两者皆无则显示默认文案。"""
        if self._hovered_button is not None:
            self.help.show_definition(self._hovered_button.definition, self._hovered_button.help_text)
        elif self._selected_node is not None:
            self.help.show_node(self._selected_node)
        else:
            self.help.show_default()


    def _on_selection_changed(self, _sel_nodes, _unsel_nodes):
        # node_selected 只在单击节点时触发；清空选择（点击空白画布/删除节点）时
        # 靠 selection_changed 把选中跟踪归零，说明面板回退到默认文案。
        selected = [node for node in self.graph.selected_nodes() if isinstance(node, StudioNode)]
        self._selected_node = selected[-1] if selected else None
        self._refresh_help()


    def _on_nodes_deleted(self, node_ids):
        """节点被移除（删除/撤销创建）后清理选中跟踪，避免说明面板引用已移除的节点。"""
        if self._selected_node is not None and self._selected_node.id in node_ids:
            self._selected_node = None
            self._refresh_help()


    def _params_changed(self, node, values):
        gesture = getattr(self, "_param_gesture", None)
        if gesture is not None and gesture["node"] is node:
            gesture["dirty"] = True
            # 延迟开宏：首次真实变化才 begin_undo，确保宏内第一个 PropertyChangedCmd
            # 的 old_val 是手势前的值；空手势（按下未拖动）根本不产生撤销记录。
            if gesture["stack"] is None:
                graph = node.graph
                if graph is not None:
                    graph.begin_undo(f"调整 {node.NODE_NAME} 参数")
                    gesture["stack"] = graph
        node.params = values
        # 只写发生变化的键：减少撤销命令数量（滑条/裁剪拖拽逐 tick 只更新被拖的边）。
        for name, value in values.items():
            if node.get_property(name) == value:
                continue
            node.set_property(name, value)
        self._mark_dirty(node)
        if self.auto_mode: self._request_auto_run(node)

    # --- 参数手势撤销折叠：滑条/可视化裁剪的整个拖拽 = 一条撤销记录 ---
    # （连续控件拖拽逐 tick 触发 changed，若每 tick 都压撤销会刷屏撤销栈；
    #  宏在首次变化时才开启（见 _params_changed），空手势不会有任何撤销记录。）


    def _param_gesture_begin(self, node):
        if getattr(self, "_param_gesture", None) is not None:
            return  # 理论上同一时刻只有一个手势，防御性跳过
        self._param_gesture = {"node": node, "dirty": False, "stack": None}


    def _param_gesture_end(self, node):
        gesture = getattr(self, "_param_gesture", None)
        if gesture is None or gesture["node"] is not node:
            return
        self._param_gesture = None
        stack = gesture["stack"]
        if stack is not None:
            stack.end_undo()
            # 栈顶宏入栈时 index 已就位（宏内首个 push 即 count+1/index+1），
            # endMacro 后 index 不再变化，indexChanged 路径（_on_undo_index_changed）
            # 因 pos==prev 会跳过同步——这里显式对齐一次模型→参数→面板，
            # 覆盖手势最后 tick 与面板控件不一致的残留场景。
            self._sync_params_from_model()

    # --- 顶栏/菜单共用操作（复用 hotkey_functions 的 graph 级功能函数） ---
    # 方法统一接受 ``*_args``：画布右键菜单的 GraphAction.executed 会发出
    # graph 参数（见 _build_graph_context_menu），无参调用（工具栏 triggered）
    # 也兼容。

    # ===== 区段 3：视图操作/样式/克隆/撤销重做/DPR/可见性（原 MainWindowViewMixin） =====

    def select_all_nodes(self, *_args):
        from .hotkeys.hotkey_functions import select_all_nodes as _fn
        _fn(self.graph)


    def fit_to_selection(self, *_args):
        from .hotkeys.hotkey_functions import fit_to_selection as _fn
        _fn(self.graph)


    def reset_zoom(self, *_args):
        from .hotkeys.hotkey_functions import reset_zoom as _fn
        _fn(self.graph)


    def layout_graph_down(self, *_args):
        from .hotkeys.hotkey_functions import layout_graph_down as _fn
        _fn(self.graph)


    def _set_pipe_style_value(self, value: int) -> None:
        """应用连线样式并持久化（工具栏循环切换与画布菜单直接选择共用）。"""
        from NodeGraphQt.constants import PipeLayoutEnum
        from .hotkeys.hotkey_functions import angle_pipe, curved_pipe, straight_pipe

        labels = {PipeLayoutEnum.ANGLE.value: "折线", PipeLayoutEnum.STRAIGHT.value: "直线", PipeLayoutEnum.CURVED.value: "曲线"}
        funcs = {PipeLayoutEnum.ANGLE.value: angle_pipe, PipeLayoutEnum.STRAIGHT.value: straight_pipe, PipeLayoutEnum.CURVED.value: curved_pipe}
        funcs[value](self.graph)
        self.settings.set_pipe_style(value)
        self.statusBar().showMessage(f"连线样式：{labels[value]}")


    def _pipe_curved(self, *_args):
        from NodeGraphQt.constants import PipeLayoutEnum
        self._set_pipe_style_value(PipeLayoutEnum.CURVED.value)


    def _pipe_straight(self, *_args):
        from NodeGraphQt.constants import PipeLayoutEnum
        self._set_pipe_style_value(PipeLayoutEnum.STRAIGHT.value)


    def _pipe_angle(self, *_args):
        from NodeGraphQt.constants import PipeLayoutEnum
        self._set_pipe_style_value(PipeLayoutEnum.ANGLE.value)


    def toggle_pipe_style(self, *_args):
        """循环切换连线样式：折线(angle) → 直线(straight) → 曲线(curved)。"""
        from NodeGraphQt.constants import PipeLayoutEnum

        values = [PipeLayoutEnum.ANGLE.value, PipeLayoutEnum.STRAIGHT.value, PipeLayoutEnum.CURVED.value]
        current = self.graph.pipe_style()
        index = values.index(current) if current in values else -1
        self._set_pipe_style_value(values[(index + 1) % len(values)])


    def _set_grid_mode_value(self, value: int) -> None:
        """应用背景网格并持久化（工具栏循环切换与画布菜单直接选择共用）。"""
        from NodeGraphQt.constants import ViewerEnum
        from .hotkeys.hotkey_functions import bg_grid_dots, bg_grid_lines, bg_grid_none

        labels = {ViewerEnum.GRID_DISPLAY_NONE.value: "无", ViewerEnum.GRID_DISPLAY_DOTS.value: "圆点", ViewerEnum.GRID_DISPLAY_LINES.value: "网格线"}
        funcs = {ViewerEnum.GRID_DISPLAY_NONE.value: bg_grid_none, ViewerEnum.GRID_DISPLAY_DOTS.value: bg_grid_dots, ViewerEnum.GRID_DISPLAY_LINES.value: bg_grid_lines}
        funcs[value](self.graph)
        self.settings.set_grid_mode(value)
        self.statusBar().showMessage(f"背景网格：{labels[value]}")


    def _grid_none(self, *_args):
        from NodeGraphQt.constants import ViewerEnum
        self._set_grid_mode_value(ViewerEnum.GRID_DISPLAY_NONE.value)


    def _grid_lines(self, *_args):
        from NodeGraphQt.constants import ViewerEnum
        self._set_grid_mode_value(ViewerEnum.GRID_DISPLAY_LINES.value)


    def _grid_dots(self, *_args):
        from NodeGraphQt.constants import ViewerEnum
        self._set_grid_mode_value(ViewerEnum.GRID_DISPLAY_DOTS.value)


    def toggle_grid_mode(self, *_args):
        """循环切换背景网格：无 → 圆点 → 网格线。"""
        from NodeGraphQt.constants import ViewerEnum

        values = [
            ViewerEnum.GRID_DISPLAY_NONE.value,
            ViewerEnum.GRID_DISPLAY_DOTS.value,
            ViewerEnum.GRID_DISPLAY_LINES.value,
        ]
        current = self.graph.scene().grid_mode
        index = values.index(current) if current in values else -1
        self._set_grid_mode_value(values[(index + 1) % len(values)])


    def clear_selection(self, *_args):
        """取消全选（画布菜单「取消全选」；graph 参数兼容）。"""
        self.graph.clear_selection()


    def open_settings(self, *_args):
        """打开设置对话框（固定尺寸；QTabWidget 切换「设置」/「关于」）。"""
        dialog = SettingsDialog(
            self.settings,
            parent=self,
            # 缓存实时用量显示实际在用的缓存（backend.root_workspace = 缓存目录）。
            cache_usage_cb=lambda: self.backend.total_cache_size(),
        )
        # 「重置设置」后重新应用连线/网格样式（主题已由对话框即时应用）。
        dialog.reset_requested.connect(self._apply_view_settings)
        # 透明背景色变更 → 应用到所有 1:1 查看节点面板（刷新预览框背景）。
        dialog.alpha_bg_changed.connect(self._apply_alpha_bg_color)
        dialog.exec()
        # 自动保存间隔可能在对话框内被修改/重置：按新设置重启定时器。
        self._apply_autosave_settings()


    def _alpha_bg_color(self) -> str:
        """当前设置的透明背景显示选项（存储值；1:1 查看节点预览框底纹）。

        绿幕/品红的存储值即 CSS 色值（settings_manager.ALPHA_BG_GREEN /
        ALPHA_BG_MAGENTA），直接透传；「棋盘格」（ALPHA_BG_CHECKER）为
        特殊值，由面板按棋盘格绘制（见 CheckerPreviewLabel）。
        """
        return self.settings.alpha_bg()


    def _apply_alpha_bg_color(self, _value: str | None = None) -> None:
        """把当前设置的透明背景色应用到所有节点的预览框（1:1 查看节点勾选后使用）。"""
        color = self._alpha_bg_color()
        for node in self.graph.all_nodes():
            if isinstance(node, StudioNode):
                node.panel.set_preview_bg_color(color)


    def _apply_view_settings(self):
        """把设置中的连线样式/背景网格应用到画布（启动与重置设置时调用）。"""
        apply_pipe_style(self.graph, self.settings.pipe_style())
        apply_grid_mode(self.graph, self.settings.grid_mode())


    def clone_selection(self, *_args):
        """克隆当前选中的节点（Ctrl+C/顶栏/右键菜单共用）：copy+paste 并同步参数
        绑定面板——NodeGraphQt 的 paste 走 add_node 创建，不触发 node_created，
        且序列化只写入模型属性（面板/self.params 仍是默认值），因此这里手动同步
        参数并重新绑定面板回调，使克隆节点功能完整。"""
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能克隆")
            return
        selected = [node for node in self.graph.selected_nodes() if isinstance(node, StudioNode)]
        if not selected:
            self.statusBar().showMessage("请先选择要克隆的节点")
            return
        self._clone_nodes(selected)


    def _clone_nodes(self, nodes) -> list:
        """克隆指定节点：copy+paste 并同步参数/绑定面板（clone_selection 与
        节点右键「克隆」共用）。"""
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能克隆")
            return []
        self.graph.copy_nodes(nodes)
        pasted = list(self.graph.paste_nodes(adjust_graph_style=False) or ())
        # 方案 C（决策 #118）：paste 走 _deserialize 建连，不触发 port_connected，
        # 新管线保持库默认橙色——克隆后重刷全部管线色 = 输出端口色。
        recolor_all_pipes(self.graph)
        clones = self._finalize_pasted(pasted)
        if clones:
            self._selected_node = clones[-1]
            self._refresh_help()
        self.statusBar().showMessage(f"已克隆 {len(clones)} 个节点")
        return clones

    # --- 撤销/重做（NodeGraphQt QUndoStack） ---


    def undo(self, *_args):
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能撤销")
            return
        # 撤销会移除/恢复节点：清空自动运行挂起队列，避免引用已删除的节点。
        self._auto_pending.clear()
        self._auto_timer.stop()
        self.graph.undo_stack().undo()


    def redo(self, *_args):
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能重做")
            return
        self._auto_pending.clear()
        self._auto_timer.stop()
        self.graph.undo_stack().redo()


    def _update_undo_redo_actions(self, *_args):
        try:
            stack = self.graph.undo_stack()
            self.undo_action.setEnabled(stack.canUndo())
            self.redo_action.setEnabled(stack.canRedo())
        except RuntimeError:
            # 窗口/撤销栈销毁过程中（C++ 侧对象已删除）忽略，避免退出时异常。
            pass


    def _on_undo_index_changed(self, *_args):
        """撤销/重做后把模型属性回读同步到 node.params 与参数面板。

        NodeGraphQt 的 PropertyChangedCmd 撤销/重做只更新模型数据（view.widgets
        仅覆盖 NodeGraphQt 原生属性控件，本项目参数面板是自定义 ParameterPanel，
        不在其中），node.params 与面板控件都不会恢复——表现为「撤销参数无效果」。
        这里监听撤销栈 index 变化并按 (count, index) 区分路径：

        - count 不变、index 变化：undo / redo / QUndoView 点击（setIndex）/
          endMacro —— 需要全量对齐模型→params→面板；
        - count 增加：普通 push（set_property），参数已在 _params_changed 同步，
          跳过（避免滑条拖拽逐 tick 全量刷新）；
        - count 归零：清空撤销栈，跳过。
        """
        try:
            stack = self.graph.undo_stack()
            pos = (stack.count(), stack.index())
        except RuntimeError:
            # 窗口/撤销栈销毁过程中（C++ 侧对象已删除）忽略，避免退出时异常。
            return
        prev = getattr(self, "_last_undo_pos", None)
        self._last_undo_pos = pos
        if prev is None or pos == prev or pos[0] != prev[0]:
            return
        self._sync_params_from_model()


    def _sync_params_from_model(self) -> None:
        """把各 StudioNode 模型属性回读为 params，刷新参数面板并标记下游脏。

        仅在实际变化的节点上更新（撤销/重做通常只影响一个节点）。面板
        set_values 触发的 changed→_params_changed 因值与模型一致不会向撤销栈
        压入新命令（set_property 值相同直接返回），同步过程不会污染撤销历史。
        """
        for node in self.graph.all_nodes():
            if not isinstance(node, StudioNode):
                continue
            params = {name: node.get_property(name) for name in node.params}
            # 模型值、node.params、面板控件三者一致才跳过：撤销/重做只改模型，
            # node.params 可能已由 _params_changed 同步而面板控件仍滞后（如手势
            # 结束的宏内路径），必须三处对齐。
            if params == node.params and params == node.panel.values():
                continue
            node.params = params
            node.panel.set_values(params)
            self._mark_dirty(node)
            if self.auto_mode:
                self._request_auto_run(node)

    # --- 窗口跨屏拖动（显示器放大倍率变化）→ 按当前 DPR 重建 1:1 预览 ---
    # （触发源：MainWindow.event() 类体拦截 QEvent.ScreenChangeInternal——
    #  QWidget::event 对它只更新 winId、不会转发 changeEvent；PySide6 也未
    #  暴露 QWindow::devicePixelRatioChanged/screenChanged，故用事件覆盖。）

    def _on_screen_dpr_changed(self) -> None:
        """窗口跨屏拖动 → 防抖后按当前 DPR 重建 1:1 预览。

        ScreenChangeInternal 在拖动跨越屏幕边界时**多次**触发；用单次定时器
        把重建推迟到拖动结束（鼠标松开）后执行。重建用**窗口句柄的实时 DPR**
        ——嵌入代理的标签 ``devicePixelRatioF()`` 跨屏后实测不更新（重跑/
        refresh/update 均无效），必须用外部实时值显式传入。
        """
        timer = getattr(self, "_screen_dpr_timer", None)
        if timer is None:
            timer = QtCore.QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(250)  # 鼠标松开后 ~250ms 执行
            timer.timeout.connect(self._rebuild_1to1_previews)
            self._screen_dpr_timer = timer
        timer.start()

    def _rebuild_1to1_previews(self) -> None:
        """按窗口当前 DPR 重建所有 1:1 预览（框尺寸/绘制跟随新放大倍率）。"""
        try:
            window = self.windowHandle()
            dpr = window.devicePixelRatio() if window is not None else 1.0
            for node in self.graph.all_nodes():
                if isinstance(node, StudioNode):
                    node.panel.refresh_preview_dpr(dpr)
        except RuntimeError:
            # 窗口销毁过程中（C++ 侧对象已删除）忽略。
            pass

    # --- 窗口可见性/激活变化 → 暂停预览动画与画布重绘（空闲 CPU 优化） ---
    # （changeEvent 虚函数覆盖定义在 ui.py 类体——PySide6 的 C++ 虚函数覆盖
    #  必须写在类体，mixin 里的同名方法会被遮蔽，见 ui.py 类体注释。）

    def _apply_visibility_pause(self) -> None:
        """根据窗口可见性/激活状态决定是否暂停预览播放与画布重绘。

        - 最小化/隐藏：暂停全部 GIF 预览播放，并冻结画布重绘（窗口不可见，
          冻结无视觉副作用，恢复时统一重绘）；
        - 可见但失焦（被其他窗口遮挡/后台运行）：仅暂停预览播放，不冻结画布
          （窗口仍可见，避免遮挡解除时出现陈旧画面）。

        预览播放是空闲 CPU 的主要来源（QMovie / 自定义 GIF 播放器的帧定时器
        持续解码 + 重绘，最小化/后台也不停），非前台暂停可显著降低空转占用。
        """
        try:
            minimized = bool(self.windowState() & QtCore.Qt.WindowState.WindowMinimized)
            visible = self.isVisible() and not minimized
            if not visible:
                paused, freeze = True, True
            else:
                paused, freeze = (not self.isActiveWindow()), False
            self._set_playback_paused(paused, freeze_view=freeze)
        except RuntimeError:
            # 窗口销毁过程中（C++ 侧对象已删除）忽略。
            pass


    def _set_playback_paused(self, paused: bool, *, freeze_view: bool = False) -> None:
        """暂停/恢复所有节点的预览动画播放；freeze_view 时同时冻结/解冻画布重绘。"""
        for node in self.graph.all_nodes():
            if not isinstance(node, StudioNode):
                continue
            try:
                node.panel.set_preview_playing(not paused)
            except RuntimeError:
                continue
        if freeze_view != self._view_frozen:
            viewer = self.graph.viewer()
            viewer.setUpdatesEnabled(not freeze_view)
            if not freeze_view:
                # 解冻后补一次重绘，确保冻结期间累积的脏区域一次性刷出。
                viewer.viewport().update()
            self._view_frozen = freeze_view

    # ===== 区段 4：执行计划/运行/进度/自动模式（原 MainWindowExecutionMixin） =====

    def _on_unstable_detected(self, marker: str) -> None:
        """faulthandler 检测到疑似卡死（GUI 线程，经信号排队执行）。

        显示常驻红色警告并提示立即保存；软件恢复响应（下次运行成功/保存
        方案）后自动隐藏。
        """
        self._unstable_warning.show()
        self.statusBar().showMessage("检测到软件不稳定，请立即保存节点方案！")

    # --- 执行进度（左侧节点库底部共用进度条） ---


    def _on_progress(self, request_id, fraction, label):
        """进度回调：fraction 为 None 时进度条走不确定动画、文本进独立标签。

        （QProgressBar 在 range(0,0) 忙模式下 text() 恒为空，label 无法
        显示，故文本统一由 progress_label 承载，进度条只负责百分比/动画。）
        """
        text = label or "处理中"
        if fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(round(max(0.0, min(1.0, fraction)) * 100))
        self.progress_label.setText(text)
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)


    def _show_progress_indeterminate(self, label="运行中…"):
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText(label)
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)


    def _hide_progress(self):
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    # --- 动作构建（单 action 管理器）：定义唯一源头在 actions.py ---
    # 画布菜单 / 节点菜单 / 顶栏都只消费 actions.ACTIONS 里的定义；
    # 命令函数统一接受 graph 参数（GraphAction.executed 发出 graph），
    # MainWindow 方法统一 *_args 兼容工具栏无参 triggered。


    def _mark_dirty(self, node):
        for current in _downstream(node):
            current.revision += 1
            current.dirty = True
            current.set_status("dirty")


    def _on_port_connected(self, input_port, output_port):
        """连线建立后立即校验端口类型：不符则撤销连线并提示非法连接。"""
        mismatch = _connection_type_mismatch(input_port, output_port)
        if mismatch is not None:
            try:
                input_port.disconnect_from(output_port, push_undo=False, emit_signal=False)
            except Exception:
                pass
            QtWidgets.QMessageBox.warning(self, "非法连接", mismatch)
            return
        self._on_connection_changed(input_port, output_port)


    def _on_connection_changed(self, input_port, _output_port):
        target = input_port.node(); self._mark_dirty(target)
        if self.auto_mode: self._request_auto_run(target)


    def _on_auto_toggled(self, enabled: bool) -> None:
        self.auto_mode = enabled
        if not enabled:
            self._auto_pending.clear()
            self._auto_timer.stop()
        else:
            # # 手动启用自动模式时，立即开始自动运行：把全部脏节点的链末端排队。
            # self._queue_dirty_runs()  # 当存在多条独立链路时，也不该全部都运行，先注释掉
            pass


    def _queue_dirty_runs(self) -> None:
        dirty = {n for n in self.graph.all_nodes() if isinstance(n, StudioNode) and n.dirty}
        sinks = [
            n for n in dirty
            if not any(d in dirty for d in _downstream(n) if d is not n)
        ]
        for sink in sinks:
            self._request_auto_run(sink)


    def _request_auto_run(self, node) -> None:
        """自动模式运行请求：合并为待执行节点队列，按全局限频稍后执行。"""
        if not self.auto_mode:
            return
        if node not in self._auto_pending:
            self._auto_pending.append(node)
        self._auto_timer.start(round(self.AUTO_MIN_INTERVAL * 1000))


    def _arm_next_auto(self) -> None:
        if self._auto_pending:
            self._auto_timer.start(round(self.AUTO_MIN_INTERVAL * 1000))


    def _auto_can_run(self, node) -> bool:
        """自动模式下可运行判定：执行链中任何脏节点声明了输入端口却无连线则跳过
        （例如其上游节点被删除），避免自动运行误报“节点无输入”。"""
        for current in _ancestors(node):
            if not current.dirty:
                continue
            if current.definition.inputs and not any(p.connected_ports() for p in current.input_ports()):
                return False
        return True


    def _flush_auto_run(self) -> None:
        """限频执行挂起的自动模式运行；仅重算到当前调整的节点为止。"""
        if not self.auto_mode:
            self._auto_pending.clear()
            return
        if not self._auto_pending:
            return
        if self.worker.busy:
            self._auto_timer.start(60)
            return
        node = self._auto_pending[0]
        if not self._auto_can_run(node):
            # 输入已断开：跳过本次自动运行，等待重新连线后再触发。
            self._auto_pending.pop(0)
            self._arm_next_auto()
            return
        wait = self._auto_last_run + self.AUTO_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            self._auto_timer.start(round(wait * 1000) + 1)
            return
        self._auto_last_run = time.monotonic()
        self._auto_pending.pop(0)
        self._arm_next_auto()
        self.run_to(node)


    def run_to(self, node, *, clear_preview: bool = False):
        if self.worker.busy: self.statusBar().showMessage("已有任务运行中"); return
        order = [current for current in _ancestors(node) if current.dirty]
        if not order: node.panel.show_preview(self.preview_path_for_node(node)); return
        request_id = node.id
        try:
            plan = self._execution_plan(order)
        except ValueError as exc:
            self.statusBar().showMessage(f"无法运行：{exc}")
            logger.warning("运行计划校验失败：{}", exc)
            if not self.auto_mode:
                # 手动运行时计划校验失败（如端口类型不符）弹窗提示；
                # 自动模式下不弹窗、不关闭自动模式（改善用户注意力体验）。
                QtWidgets.QMessageBox.critical(self, "无法运行", str(exc))
            return
        if clear_preview:
            # 手动点击运行时先清空预览框，避免旧结果在执行期间残留；同时释放
            # 本链**下游**节点的预览——下游预览（QMovie/自定义 GIF 播放器）
            # 可能正在播放本链节点即将覆盖的固定缓存文件（如 preview.gif），
            # Windows 下文件被占用会导致写入失败（WinError 5，实测 GIF
            # 合成(FFmpeg) 重跑时被下游「图片1:1分辨率查看」的播放锁定）。
            affected = []
            for current in order:
                affected.extend(_downstream(current))
            release_previews(affected)
        self.active_request = (request_id, order, node)
        self.active_revisions = {current.id: current.revision for current in order}
        # 缓存大小上限在提交前（UI 线程）读取一次，工作线程不再触碰 QSettings。
        self._active_cache_limit_bytes = self.settings.cache_limit_mb() * 1024 * 1024
        for current in order:
            current.set_status("running" if current is order[0] else "dirty", "排队" if current is not order[0] else "")
        self.worker.submit_steps(request_id, plan, self._execute_step)


    def _execution_plan(self, order):
        planned_ids = {node.id for node in order}
        plan = []
        for current in order:
            params = dict(current.params)
            upstream = []
            for input_port in current.input_ports():
                connected = list(input_port.connected_ports())
                if not connected:
                    # 未连接的输入端口生成占位条目：多输入节点（如 RGBA 通道合并
                    # 的透明度通道可空）必须能区分「未连接」与「连接了空值」——
                    # 占位条目在 _execute_step 里解析为 None 输入。
                    upstream.append(("none", None, ""))
                    continue
                for output_port in connected:
                    mismatch = _connection_type_mismatch(input_port, output_port)
                    if mismatch is not None:
                        raise ValueError(mismatch)
                    node = output_port.node()
                    # 记录上游输出端口名：多输出节点（如 RGBA 通道分离）按端口名
                    # 取出对应值；单输出节点输出端口名无影响（值即整个产物）。
                    source = ("planned", node.id) if node.id in planned_ids else ("value", node.output_data)
                    upstream.append((*source, output_port.name()))
            plan.append((current.id, type(current), params, upstream))
        return plan

    # --- 端口类型校验 ---

    def _execute_step(self, step, produced, progress):
        node_id, node_class, params, upstream = step
        inputs = []
        for entry in upstream:
            source_kind, value, port_name = entry
            if source_kind == "none":
                # 未连接的输入端口占位：解析为 None（多输入节点据此判断可空输入）。
                inputs.append(None)
                continue
            result = produced[value] if source_kind == "planned" else value
            if isinstance(result, MultiOutput) and port_name:
                # 多输出节点（RGBA 通道分离）：按上游输出端口名取出对应通道。
                inputs.append(result.ports.get(port_name))
            else:
                inputs.append(result)
        backend = self.backend.for_node(node_id, progress_callback=progress)
        # 节点由脏状态重新运行：捕获上一次运行留下的缓存，成功后删除
        # （新产物的 job 目录均为随机名，不会与旧缓存冲突）。
        snapshot = backend.snapshot_workspace()
        result = node_class.execute(inputs, params, backend)
        # 保留被本次运行原地覆盖的固定文件/目录（导出型节点声明的 CACHE_FILENAME，
        # 如 gif 合成节点的 preview.gif、PNG 输出节点的 preview_frames/）；
        # 删除失败（如 Windows 上文件被占用）不中断运行，留待下次清理。
        cache_name = getattr(node_class, "CACHE_FILENAME", None)
        keep = {backend.workspace / cache_name} if cache_name else set()
        backend.clear_previous_run(snapshot, keep)
        # 重新统计本节点工作区（工作线程）→ GUI 线程后续 cache_size 读到 O(1) 的
        # 账本值，不再为显示缓存大小而全量 stat（6000 帧时一次约 0.5s 卡顿）。
        backend.refresh_node_cache_size()
        # 缓存总量超限时自动淘汰最旧中间缓存（保留各节点最新结果与导出固定缓存）；
        # 限额在提交时已由 UI 线程读入，工作线程只读这个值。
        limit_bytes = getattr(self, "_active_cache_limit_bytes", None)
        removed = 0
        if limit_bytes is not None:
            freed, removed = backend.enforce_cache_limit(limit_bytes)
            if removed:
                self._cache_eviction_note = f"缓存超限，已自动清理 {removed} 项（{format_bytes(freed)}）"
                # 淘汰了其他节点的旧 job：全量重建账本，保证总账与实际磁盘一致。
                backend.refresh_cache_ledger()
        return result


    def _active_node(self, request_id, node_id):
        if not self.active_request or request_id != self.active_request[0]: return None
        return next((node for node in self.active_request[1] if node.id == node_id), None)


    def _step_started(self, request_id, node_id):
        current = self._active_node(request_id, node_id)
        if current is not None and current.revision == self.active_revisions.get(node_id):
            current.set_status("running")
        self._show_progress_indeterminate()


    def _step_succeeded(self, request_id, node_id, timed_result):
        current = self._active_node(request_id, node_id)
        if current is None or current.revision != self.active_revisions.get(node_id): return
        result = timed_result.value if isinstance(timed_result, TimedResult) else timed_result
        current.output_data = result
        current.last_elapsed_seconds = timed_result.elapsed_seconds if isinstance(timed_result, TimedResult) else None
        # 元数据已在工作线程计算（避免大 GIF 探测阻塞 UI），并计入运行耗时。
        current.output_metadata = (
            timed_result.metadata
            if isinstance(timed_result, TimedResult) and timed_result.metadata is not None
            else describe_output(result, current)
        )
        current.dirty = False
        if getattr(type(current), "EXPORT_KIND", None) == "gif":
            current.preview_output = self.preview_export_path(current)
        current.set_status("clean")
        current.panel.show_preview(self.preview_path_for_node(current))
        _feed_sequence_frames(current.panel, result)
        current.panel.show_runtime_info(current.last_elapsed_seconds, self.backend.for_node(current.id).cache_size())
        if current in self.graph.selected_nodes(): self._refresh_help()


    def _run_succeeded(self, request_id, result):
        if not self.active_request or request_id != self.active_request[0]: return
        _, order, target = self.active_request
        timed_result = result if isinstance(result, TimedResult) else None
        result = timed_result.value if timed_result is not None else result
        if (
            not isinstance(result, dict)
            and len(order) == 1
            and order[0].revision == self.active_revisions.get(order[0].id)
        ):
            order[0].output_data = result
            order[0].last_elapsed_seconds = timed_result.elapsed_seconds if timed_result is not None else self.operation_elapsed.pop(request_id, None)
            order[0].output_metadata = (
                timed_result.metadata
                if timed_result is not None and timed_result.metadata is not None
                else describe_output(result, order[0])
            )
            order[0].preview_output = None
            order[0].dirty = False
            order[0].set_status("clean")
            order[0].panel.show_preview(self.preview_path_for_node(order[0]))
            _feed_sequence_frames(order[0].panel, result)
            order[0].panel.show_runtime_info(order[0].last_elapsed_seconds, self.backend.for_node(order[0].id).cache_size())
            if order[0] in self.graph.selected_nodes(): self._refresh_help()
        self.active_request = None; self.active_revisions = {}; self.statusBar().showMessage("节点运行完成")
        # 运行成功 = 软件恢复响应：隐藏不稳定警告（若此前 faulthandler 触发过）。
        self._unstable_warning.hide()
        # 运行后统计缓存用量（决策 #130）：超过设定上限（缓存/limit_mb）→ 状态栏
        # 纯文本警告（自动清理已尽力仍压不回上限时用户知情）；若本次运行发生过
        # 自动淘汰，且总量已回到上限以内，则显示清理提示。警告读完即清空。
        note = getattr(self, "_cache_eviction_note", None)
        self._cache_eviction_note = None
        limit_bytes = getattr(self, "_active_cache_limit_bytes", None)
        usage_bytes = self.backend.total_cache_size() if limit_bytes is not None else None
        message = post_run_cache_message(note, usage_bytes, limit_bytes)
        if message:
            self.statusBar().showMessage(message)
        self.stop_action.setEnabled(False)
        self._hide_progress()


    def _run_cancelled(self, request_id):
        """用户点击「停止」后：运行被中断，相关节点回到待运行状态。"""
        if self.active_request and request_id == self.active_request[0]:
            _, order, _ = self.active_request
            for current in order:
                if current.dirty:
                    # 未完成的节点保持脏状态（运行结果作废，不标记为错误）。
                    current.set_status("dirty")
        self.active_request = None; self.active_revisions = {}
        self.stop_action.setEnabled(False)
        self._hide_progress()
        self.statusBar().showMessage("已停止运行")


    def _on_node_run_clicked(self, node):
        """节点「运行」键：空闲时运行该节点；有任务运行中时该键承担「停止」职责，
        立即中断当前节点运行（与顶栏「停止」等价）。"""
        if self.worker.busy:
            self._stop_running()
            return
        self.run_to(node, clear_preview=True)


    def _stop_running(self):
        """请求工作线程在下一个进度检查点中断当前节点运行
        （顶栏「停止」与节点内「运行」键共用）。"""
        self.stop_action.setEnabled(False)
        self.statusBar().showMessage("正在停止当前节点运行…")
        if self.auto_mode:
            # 自动模式下停止也要关闭自动模式，避免取消后立刻重算。
            self.auto_action.setChecked(False)
        self.worker.cancel()


    def _on_stop_clicked(self):
        """顶栏「停止」：请求工作线程在下一个进度检查点立即中断当前节点运行。"""
        self._stop_running()


    def _run_failed(self, request_id, message):
        if self.active_request and request_id == self.active_request[0]:
            _, order, _ = self.active_request
            for current in order:
                if current.dirty:
                    current.set_status("error", message[:0])  # 改为不显示具体报错信息
                break
        self.active_request = None; self.active_revisions = {}
        self.stop_action.setEnabled(False)
        self._hide_progress()
        logger.error("节点运行失败（request={}）：{}", request_id, message)
        if self.auto_mode:
            # 自动模式下遇节点报错：不弹窗警告、不关闭自动模式（改善用户注意力
            # 体验）——错误由底部状态栏显示即可；节点面板已标记为 error。
            self.statusBar().showMessage(f"运行失败：{message}")
        else:
            self.statusBar().showMessage(f"运行失败：{message}")
            QtWidgets.QMessageBox.critical(
                self,
                "节点运行失败",
                f"{message}\n\n（详见日志 {logs_dir() / 'app.log'}）",
            )

    # ===== 区段 5：预览/导出（原 MainWindowPreviewMixin） =====

    def preview_export_path(self, node):
        # 固定缓存文件/目录名由节点类声明（CACHE_FILENAME），唯一源头在节点。
        return self.backend.for_node(node.id).workspace / type(node).CACHE_FILENAME

    # --- 导出保存框起始目录记忆（决策 #133）：导出按钮当前无路径参数，
    # QFileDialog 默认落在程序当前目录；记住上次导出目录后每次从用户最后
    # 保存处开始（设置键 dialog/export_dir，界面不可见） ---

    def _export_dialog_default(self, default_name: str) -> str:
        """导出保存框默认位置：上次导出目录 + 默认文件名；无记忆/目录已失效
        回落纯文件名（对话框落在程序当前目录，等同旧行为）。"""
        last = self.settings.last_export_dir()
        return str(Path(last) / default_name) if last else default_name

    def _remember_export_dir(self, path: str) -> None:
        """用户选定导出目标后记忆其所在目录（取消/空路径不记）。"""
        if path:
            self.settings.set_last_export_dir(str(Path(path).parent))


    def preview_path_for_node(self, node):
        # 接管组件（TakeoverParam，决策 #109）：按面板声明的数据源需求分发，
        # 不再按节点 KIND 特判——新接管组件（胶片条/裁剪框等）无需改 ui。
        sources = node.panel.takeover_data_sources()
        if "sequence_frames" in sources:
            # 胶片条类接管（序列剃刀）：面板显示「未应用本节点切割」的上游
            # 序列帧（拖拽实时预览切割处两侧帧，不依赖运行）；随上游变化
            # 刷新，无上游时清空。
            source = self._upstream_sequence_frames(node)
            node.panel.feed_sequence_frames(source.frames if source else [])
            return None
        if "first_frame" in sources:
            # 裁剪框类接管（可视化裁剪）：面板 overlay 显示「未应用本节点
            # 裁剪」的源图（上游输出序列首帧/清单预览，不依赖本节点是否已
            # 运行），红框即本节点裁剪范围；裁剪结果由面板的结果缩略图按
            # 当前参数实时裁剪源图生成。
            source = self._upstream_first_frame_path(node)
            if source is not None:
                return source
        output = node.preview_output or node.output_data
        if isinstance(output, AnalysisResult):
            path = Path(output.path)
            return path if path.exists() else None
        if isinstance(output, MultiOutput):
            # 多输出节点（RGBA 通道分离）：预览取首个可显示通道（红通道）。
            for value in output.ports.values():
                if isinstance(value, SequenceArtifact) and value.frames:
                    return Path(value.frames[0])
            return None
        if isinstance(output, SequenceArtifact) and output.frames: return Path(output.frames[0])
        if isinstance(output, MediaManifest):
            if node.KIND == "gif_input" and output.kind is MediaKind.ANIMATED_IMAGE and output.sources:
                # gif 输入节点的预览框直接播放 gif 本身；首帧静态预览图仍经
                # manifest.preview 输出给下游（裁剪示意/格式化前预览）。
                return Path(output.sources[0])
            if output.preview: return Path(output.preview)
            if output.sources: return Path(output.sources[0])
        if isinstance(output, Path) and output.suffix.lower() == ".gif" and output.exists(): return output
        return None


    def _upstream_first_frame_path(self, node):
        """裁剪框类接管的源图：上游输出（未应用本节点裁剪）的首帧路径。

        多输出上游（序列剃刀/RGBA 通道分离）按**实际连接的输出端口名**取
        对应分支的首帧（与执行 ``_execute_step`` 同源，决策 #128）；链式
        裁剪时上游输出序列首帧已是「被上游处理过」的图，与链式语义一致
        （本节点的红框画在上游结果上）。上游输出可能是：
        - 序列产物（``SequenceArtifact``）→ 取首帧（新序列级裁剪的常规上游）；
        - 清单（``MediaManifest``）→ 取携带的预览图（旧存档/清单级上游兜底）。
        """
        for input_port in node.input_ports():
            for output_port in input_port.connected_ports():
                upstream = output_port.node()
                data = _resolve_port_preview(
                    upstream.preview_output or upstream.output_data, output_port
                )
                if isinstance(data, SequenceArtifact) and data.frames:
                    return Path(data.frames[0])
                if isinstance(data, MediaManifest) and data.preview:
                    return Path(data.preview)
        return None

    def _upstream_sequence_frames(self, node):
        """胶片条类接管的源序列：上游输出序列产物（未应用本节点切割）。

        多输出上游按**实际连接的输出端口名**取值——序列剃刀链：连「段A」
        喂段A、连「段B」喂段B；RGBA 通道分离链同理（与执行喂给 ``execute``
        的输入一致，决策 #128；修复「链式剃刀预览固定显示段A」）；上游为
        清单（未格式化）时无帧可显示，返回 None（胶片条清空显示「无预览」）。
        """
        for input_port in node.input_ports():
            for output_port in input_port.connected_ports():
                upstream = output_port.node()
                data = _resolve_port_preview(
                    upstream.preview_output or upstream.output_data, output_port
                )
                if isinstance(data, SequenceArtifact) and data.frames:
                    return data
                if isinstance(data, MediaManifest) and data.preview:
                    return None  # 未格式化：无帧可显示（不取清单预览单帧）
        return None


    def export_gif(self, node):
        """导出缓存 GIF：弹保存框（默认 output.gif），把缓存里计算好的 preview.gif 复制到目标。"""
        if getattr(type(node), "EXPORT_KIND", None) != "gif":
            return
        if self.worker.busy:
            self.statusBar().showMessage("已有任务运行中")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            None, "导出 GIF", self._export_dialog_default("output.gif"), "GIF (*.gif)"
        )
        if not path:
            return
        self._remember_export_dir(path)
        preview = self.preview_export_path(node)
        if node.dirty or not preview.exists():
            logger.info("导出跳过（GIF，节点 {}）：缓存未生成", node.definition.title)
            QtWidgets.QMessageBox.information(self, "尚未生成缓存", "请先运行节点生成缓存 GIF，再点击导出。")
            return
        shutil.copy2(preview, path)
        self.statusBar().showMessage(f"已导出：{path}")
        logger.info("导出 GIF：{}（节点 {}）", path, node.definition.title)


    def export_node(self, node):
        """导出按钮统一出口：按节点类声明的 EXPORT_KIND 分发
        （gif 合成 → 复制缓存 GIF；格式化 PNG 输出 → 复制缓存 PNG 序列到用户目录；
        ico 合成 → 复制缓存 ICO；webp/apng 动画 → 复制缓存动画文件）。"""
        export_kind = getattr(type(node), "EXPORT_KIND", None)
        if export_kind == "gif":
            self.export_gif(node)
        elif export_kind == "png":
            self.export_png(node)
        elif export_kind == "ico":
            self.export_ico(node)
        elif export_kind == "webp":
            self.export_webp(node)
        elif export_kind == "apng":
            self.export_apng(node)


    def _export_cache_file(self, node, title, default_name, file_filter):
        """通用单文件导出：弹保存框（默认文件名），把节点固定缓存文件复制到目标。"""
        if self.worker.busy:
            self.statusBar().showMessage("已有任务运行中")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            None, title, self._export_dialog_default(default_name), file_filter
        )
        if not path:
            return
        self._remember_export_dir(path)
        preview = self.preview_export_path(node)
        if node.dirty or not preview.exists():
            logger.info("导出跳过（{}，节点 {}）：缓存未生成", title, node.definition.title)
            QtWidgets.QMessageBox.information(self, "尚未生成缓存", "请先运行节点生成缓存文件，再点击导出。")
            return
        shutil.copy2(preview, path)
        self.statusBar().showMessage(f"已导出：{path}")
        logger.info("导出成功（{}，节点 {}）：{}", title, node.definition.title, path)


    def export_webp(self, node):
        """导出缓存 WebP 动画：复制节点工作区的 preview.webp 到用户路径。"""
        if getattr(type(node), "EXPORT_KIND", None) != "webp":
            return
        self._export_cache_file(node, "导出 WebP", "output.webp", "WebP (*.webp)")


    def export_apng(self, node):
        """导出缓存 APNG 动画：复制节点工作区的 preview.apng 到用户路径。"""
        if getattr(type(node), "EXPORT_KIND", None) != "apng":
            return
        self._export_cache_file(node, "导出 APNG", "output.apng", "PNG (*.apng *.png)")


    def export_ico(self, node):
        """导出缓存 ICO：弹保存框（默认 output.ico），把缓存里计算好的 preview.ico 复制到目标。"""
        if getattr(type(node), "EXPORT_KIND", None) != "ico":
            return
        if self.worker.busy:
            self.statusBar().showMessage("已有任务运行中")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            None, "导出 ICO", self._export_dialog_default("output.ico"), "图标 (*.ico)"
        )
        if not path:
            return
        self._remember_export_dir(path)
        preview = self.preview_export_path(node)
        if node.dirty or not preview.exists():
            logger.info("导出跳过（ICO，节点 {}）：缓存未生成", node.definition.title)
            QtWidgets.QMessageBox.information(self, "尚未生成缓存", "请先运行节点生成缓存 ICO，再点击导出。")
            return
        shutil.copy2(preview, path)
        self.statusBar().showMessage(f"已导出：{path}")
        logger.info("导出 ICO：{}（节点 {}）", path, node.definition.title)


    def export_png(self, node):
        """导出缓存 PNG 序列：弹保存框（默认文件名 sequence_，即导出文件前缀），
        把缓存里计算好的 preview_frames/ 帧复制到用户选择的目录，命名为
        ``<前缀>0001.png``、``<前缀>0002.png``…（与 gif 合成节点同款交互）。"""
        if getattr(type(node), "EXPORT_KIND", None) != "png":
            return
        if self.worker.busy:
            self.statusBar().showMessage("已有任务运行中")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            None, "导出 PNG 序列",
            self._export_dialog_default(f"{PNG_EXPORT_DEFAULT_PREFIX}.png"),
            "PNG (*.png)",
        )
        if not path:
            return
        self._remember_export_dir(path)
        target = Path(path)
        prefix = target.name
        if prefix.lower().endswith(".png"):
            prefix = prefix[:-4]
        cache_dir = self.backend.for_node(node.id).workspace / type(node).CACHE_FILENAME
        frames = sorted(cache_dir.glob("*.png")) if cache_dir.is_dir() else []
        if node.dirty or not frames:
            logger.info("导出跳过（PNG 序列，节点 {}）：缓存未生成", node.definition.title)
            QtWidgets.QMessageBox.information(self, "尚未生成缓存", "请先运行节点生成 PNG 序列缓存，再点击导出。")
            return
        out_dir = target.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for index, source in enumerate(frames):
            dest = out_dir / f"{prefix}{index + 1:04d}.png"
            shutil.copy2(source, dest)
            written.append(dest)
        self.statusBar().showMessage(f"已导出 {len(written)} 个 PNG：{out_dir}")
        logger.info("导出 PNG 序列：{} 个文件 → {}", len(written), out_dir)

    # ===== 区段 6：预设存取/缓存清理（原 MainWindowSessionMixin） =====

    def import_preset(self, *_args):
        """导入节点方案：弹文件框选择后合并进当前画布（不清空现有节点）。

        预设文件即 NodeGraphQt session JSON（save_session_clean 落盘），
        先经 ``sanitize_session_data`` 做旧存档兼容清洗（丢弃节点里已不
        存在的参数、choice 取值回退默认值），再以 clear_session=False
        反序列化合并；导入的新节点不触发 node_created，需手动绑定面板并
        同步参数（与 load_preset 相同，但只处理新增节点——已绑定的节点
        跳过）。
        """
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能导入方案")
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(None, "导入节点预设", "", "节点方案 (*.json)")
        if not path:
            return
        self.import_preset_file(path)

    def import_preset_file(self, path: str | Path) -> None:
        """把磁盘方案文件**增量合并**进当前画布（文件框「导入方案…」与
        菜单栏「导入预设」子菜单共用，见决策 #89）。

        与 ``import_preset`` 相同的合并语义（``clear_session=False``、不清空
        现有节点），但路径直接传入、不弹文件框；运行中拒绝执行。

        阻塞处理：反序列化（NodeGraphQt 建图）与面板绑定（Qt 控件）必须在
        主线程，弱机器上大方案可能阻塞数秒——无法安全异步化（QGraphicsScene/
        QWidget 线程限制），故在**阻塞前**先展示加载状态 + 忙碌光标，并在
        绑定阶段按批泵动事件循环让状态栏文字实际渲染。
        """
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能导入方案")
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.statusBar().showMessage("正在读取方案…")
            data, report = self._read_session_compat(str(path))
            if data is None:
                return
            self.statusBar().showMessage("正在创建节点…")
            try:
                self.graph.deserialize_session(data, clear_session=False, clear_undo_stack=True)
                # 方案 C：管线色 = 输出端口色（deserialize 建连不触发 port_connected）。
                recolor_all_pipes(self.graph)
            except Exception as exc:
                logger.exception("导入方案失败: %s", path)
                QtWidgets.QMessageBox.critical(self, "无法导入方案", f"导入 {path} 失败：{exc}")
                return
            all_nodes = self.graph.all_nodes()
            total = len(all_nodes)
            imported = 0
            for index, node in enumerate(all_nodes):
                if not isinstance(node, StudioNode) or node.id in self.panels:
                    continue
                self._bind_node(node)
                node.params = {k: node.get_property(k) for k in node.params}
                node.panel.set_values(node.params)
                node.output_data = None
                node.preview_output = None
                node.output_metadata = {}
                node.dirty = True
                node.set_status("dirty")
                imported += 1
                if imported % 10 == 0:
                    self.statusBar().showMessage(f"正在初始化节点 {imported}/{total}…")
                    QtWidgets.QApplication.processEvents()
            # 导入 = 合并进当前画布，属未保存更改。deserialize_session 末尾清空
            # 撤销栈（count/index 归零），不置 None 会误判干净（决策 #85）。
            self._clean_undo_pos = None
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._show_load_report(report, f"已导入 {imported} 个节点")
        logger.info("导入方案：{}（新增 {} 个节点）", path, imported)

    def open_presets_folder(self, *_args):
        """打开（必要时创建）node_presets 预设文件夹（资源管理器）。

        菜单栏「导入预设」子菜单底部的引导入口：用户把预设 JSON 丢进该
        目录即可在子菜单中看到（决策 #89）。
        """
        directory = node_presets_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("创建预设文件夹失败 {}：{}", directory, exc)
            QtWidgets.QMessageBox.warning(self, "无法创建预设文件夹", f"创建 {directory} 失败：{exc}")
            return
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory))):
            logger.warning("无法打开预设文件夹 {}", directory)
            QtWidgets.QMessageBox.warning(self, "无法打开预设文件夹", f"无法打开：{directory}")


    def clear_session_prompt(self, *_args):
        """清空当前方案：确认后移除全部节点并清理面板/缓存。

        破坏性操作按方案 clean/dirty 状态机确认（决策 #85）：
        脏 → 保存/放弃/取消；干净但有内容 → 普通确认；画布已空 → 直接清空
        （无内容可丢，不弹窗）。
        """
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能清空方案")
            return
        if not self.graph.all_nodes():
            self.statusBar().showMessage("画布已为空")
            return
        if self._session_dirty:
            if not self._confirm_unsaved_changes("清空"):
                return
        else:
            ret = QtWidgets.QMessageBox.question(self, "清空方案", "确定清空当前画布上的全部节点吗？")
            if ret != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        nodes = [node for node in self.graph.all_nodes() if isinstance(node, StudioNode)]
        release_previews(nodes)
        for node in nodes:
            self.backend.for_node(node.id).clear_cache()
            self.panels.pop(node.id, None)
        self._auto_pending.clear()
        self._auto_timer.stop()
        self.graph.clear_session()
        self._selected_node = None
        self._refresh_help()
        # 清空后视为全新的未保存文档（画布无节点 → 恒为干净；撤销栈已被
        # clear_session 清空归零，不置 None 会误判干净，决策 #85）。
        self._session_path = None
        self._clean_undo_pos = None
        self.statusBar().showMessage("方案已清空")
        # 清空 = 用户主动放弃画布内容：旧自动保存副本一并清理。
        self._remove_autosave()
        logger.info("清空方案（移除 {} 个节点）", len(nodes))


    def save_preset(self, *_args) -> bool:
        """保存方案：已有会话路径则静默覆盖保存，否则走另存为（弹保存框）。

        Returns:
            bool: 是否真正写入（另存为被取消时返回 False，供关闭提示判断）。
        """
        if self._session_path is not None:
            self._write_session(self._session_path)
            return True
        return self.save_preset_as()


    def save_preset_as(self, *_args) -> bool:
        """另存为：总是弹出保存框；返回是否真正写入。"""
        if self.worker.busy:
            self.statusBar().showMessage("节点运行中，暂不能保存")
            return False
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            None, "保存节点预设", str(self._session_path or ""), "节点方案 (*.json)"
        )
        if not path:
            return False
        self._write_session(Path(path))
        return True


    def _write_session(self, path: Path) -> None:
        """落盘当前方案并更新会话状态（路径 + 干净标记）。

        干净标记 = 当前撤销栈 (count, index)：此后撤销栈整体回到该形态
        才算干净（决策 #85）。
        """
        save_session_clean(self.graph, path)
        self._session_path = Path(path)
        stack = self.graph.undo_stack()
        self._clean_undo_pos = (stack.count(), stack.index())
        # 已保存到正式文件：本次会话的自动保存副本不再需要（避免下次启动误弹恢复）。
        self._remove_autosave()
        # 已保存 = 用户完成数据保全，隐藏不稳定警告。
        self._unstable_warning.hide()
        self.statusBar().showMessage(f"已保存：{path}")
        logger.info("保存方案：{}", path)


    # --- 自动保存（决策 #131）：间隔由设置指定（分钟，0=关闭）；脏时落盘；正常退出清理；启动恢复 ---

    def _apply_autosave_settings(self) -> None:
        """按设置中的自动保存间隔（分钟，0=关闭）启动/停止定时器。"""
        minutes = self.settings.autosave_interval_min()
        if minutes > 0:
            self._autosave_timer.start(minutes * 60 * 1000)
        else:
            self._autosave_timer.stop()

    def _autosave_tick(self) -> None:
        """自动保存定时器触发：仅当方案有未保存更改时写盘。

        空画布恒为干净（决策 #85）→ 不写；运行中/有模态框时跳过本次
        （写 JSON 安全，但避免在用户批量操作画布时插入序列化占用主线程）。
        """
        if not self._session_dirty:
            return
        if self.worker.busy or QtWidgets.QApplication.activeModalWidget() is not None:
            return
        self._write_autosave()

    def _write_autosave(self) -> None:
        """把当前方案落盘到自动保存文件（与正式存档同格式 save_session_clean）。"""
        path = self.settings.autosave_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            save_session_clean(self.graph, path)
            logger.info("自动保存方案：{}", path)
        except OSError as exc:
            logger.warning("自动保存失败 {}：{}", path, exc)

    def _remove_autosave(self) -> None:
        """删除自动保存文件（正式保存/读取/清空/正常退出/恢复后调用；失败不阻断）。"""
        path = self.settings.autosave_path()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理自动保存失败 {}：{}", path, exc)

    def prompt_autosave_restore(self) -> bool:
        """启动时提示恢复上次异常退出残留的自动保存方案（无残留则直接返回）。

        弹窗按钮：恢复（载入自动保存内容，视为未保存的新文档）/ 不恢复
        （删除自动保存文件）。返回是否执行了恢复。
        """
        path = self.settings.autosave_path()
        if not path.is_file():
            return False
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)) if mtime else "未知时间"
        ret = QtWidgets.QMessageBox.question(
            self,
            "自动保存恢复",
            f"检测到上次运行异常退出时自动保存的方案（保存时间 {stamp}）。\n\n"
            "是否恢复该方案？\n选择「不恢复」将删除自动保存文件。",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.Yes,
        )
        if ret != QtWidgets.QMessageBox.StandardButton.Yes:
            self._remove_autosave()
            return False
        return self._load_autosave(path)

    def _load_autosave(self, path: Path) -> bool:
        """把自动保存文件载入画布（启动时画布为空；载入内容 = 未保存草稿）。

        与 load_preset 相同的旧存档兼容清洗/绑定流程，但不弹文件框、不要求
        先确认；恢复内容没有对应的正式文件 → ``_session_path``/``_clean_undo_pos``
        置 None（视为从未保存，之后关闭/清空仍会提示保存）。
        """
        data, report = self._read_session_compat(str(path))
        if data is None:
            return False
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            old_nodes = [node for node in self.graph.all_nodes() if isinstance(node, StudioNode)]
            release_previews(old_nodes)
            for node in old_nodes:
                self.backend.for_node(node.id).clear_cache()
                self.panels.pop(node.id, None)
            try:
                self.graph.deserialize_session(data, clear_session=True, clear_undo_stack=True)
                recolor_all_pipes(self.graph)
            except Exception as exc:
                logger.exception("恢复自动保存失败: %s", path)
                QtWidgets.QMessageBox.critical(
                    self,
                    "无法恢复自动保存",
                    f"恢复 {path} 失败：{exc}\n\n自动保存文件已保留，可手动读取该文件。",
                )
                return False
            for node in self.graph.all_nodes():
                if isinstance(node, StudioNode):
                    self._bind_node(node)
                    node.params = {k: node.get_property(k) for k in node.params}
                    node.panel.set_values(node.params)
                    node.output_data = None
                    node.preview_output = None
                    node.output_metadata = {}
                    node.dirty = True
                    node.set_status("dirty")
            self._session_path = None
            self._clean_undo_pos = None
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._remove_autosave()
        self._show_load_report(report, "已恢复自动保存的方案（请及时另存为）")
        return True


    def load_preset(self, *_args):
        if self.worker.busy: self.statusBar().showMessage("节点运行中，暂不能读取预设"); return
        # 读入新方案前先按当前方案 clean/dirty 判定确认（决策 #85）：
        # 脏 → 保存/放弃/取消（「保存」走另存为被取消则中止）；干净但有
        # 内容 → 确认替换；干净且画布为空 → 直接继续。
        if not self._confirm_open_new():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(None, "读取节点预设", "", "节点方案 (*.json)")
        if not path: return
        # 阻塞处理：反序列化（NodeGraphQt 建图）与面板绑定（Qt 控件）必须在
        # 主线程，弱机器上大方案（数十节点）可能阻塞数秒——无法安全异步化
        # （QGraphicsScene/QWidget 线程限制），故在**阻塞前**先展示加载状态 +
        # 忙碌光标，并在绑定阶段按批泵动事件循环让状态栏文字实际渲染。
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            self.statusBar().showMessage("正在读取方案…")
            data, report = self._read_session_compat(path)
            if data is None:
                return
            old_nodes = [node for node in self.graph.all_nodes() if isinstance(node, StudioNode)]
            release_previews(old_nodes)
            for node in old_nodes:
                self.backend.for_node(node.id).clear_cache()
                self.panels.pop(node.id, None)
            self.statusBar().showMessage("正在创建节点…")
            try:
                self.graph.deserialize_session(data, clear_session=True, clear_undo_stack=True)
                # 方案 C：管线色 = 输出端口色（deserialize 建连不触发 port_connected）。
                recolor_all_pipes(self.graph)
            except Exception as exc:
                # 清洗已拦截「节点参数变更」类失败（NodePropertyError）；这里兜底
                # 其它意外（损坏 JSON 结构等）。load_session 的 clear_session 已在
                # 反序列化前执行，画布可能已被清空——提示用户不要覆盖原文件。
                logger.exception("读取方案失败: %s", path)
                QtWidgets.QMessageBox.critical(
                    self,
                    "无法读取方案",
                    f"读取 {path} 失败：{exc}\n\n画布可能已被清空，请勿保存覆盖原文件。",
                )
                return
            all_nodes = self.graph.all_nodes()
            total = len(all_nodes)
            bound = 0
            for index, node in enumerate(all_nodes):
                if isinstance(node, StudioNode):
                    self._bind_node(node)
                    node.params = {k: node.get_property(k) for k in node.params}
                    node.panel.set_values(node.params)
                    node.output_data = None
                    node.preview_output = None
                    node.output_metadata = {}
                    node.dirty = True
                    node.set_status("dirty")
                    bound += 1
                    if bound % 10 == 0:
                        self.statusBar().showMessage(f"正在初始化节点 {bound}/{total}…")
                        QtWidgets.QApplication.processEvents()
            self._session_path = Path(path)
            # deserialize_session(clear_undo_stack=True) 末尾清栈 → count/index
            # 归零；记录实际位置作为干净标记（绑定面板的 set_values 值与模型
            # 一致，不会压入新撤销命令）。
            stack = self.graph.undo_stack()
            self._clean_undo_pos = (stack.count(), stack.index())
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._show_load_report(report, f"已读取：{path}")
        # 读取正式方案 = 画布内容已被文件内容取代：清理自动保存副本。
        self._remove_autosave()
        logger.info("读取方案：{}（{} 个节点）", path, bound)


    def _read_session_compat(self, path: str) -> tuple[dict | None, SessionLoadReport | None]:
        """读取方案 JSON 并按当前节点定义做旧存档兼容清洗。

        返回 ``(清洗后的数据, 调整报告)``；文件读取失败返回 ``(None, None)``
        （已弹出错误框）。清洗只改内存中的数据，不改写磁盘上的存档文件。
        """
        try:
            with open(path, "r", encoding="utf-8") as file_in:
                data = json.load(file_in)
        except (OSError, ValueError) as exc:
            logger.warning("无法读取方案文件 {}：{}", path, exc)
            QtWidgets.QMessageBox.critical(self, "无法读取方案", f"无法读取文件：{exc}")
            return None, None
        report = sanitize_session_data(data, self.graph.node_factory)
        return data, report


    def _show_load_report(self, report: SessionLoadReport, ok_message: str) -> None:
        """旧存档兼容读取后向用户提示被丢弃/回退/跳过的内容。

        无调整时仅显示成功状态栏消息；有调整时状态栏提示 + 弹窗明细
        （丢弃参数 = 存档数据与当前节点定义不符，用户应知道发生了什么）。
        """
        if not report.has_adjustments():
            self.statusBar().showMessage(ok_message)
            return
        lines: list[str] = []
        if report.unknown_types:
            titles = "、".join(title for _type_, title in report.unknown_types[:5])
            more = "…" if len(report.unknown_types) > 5 else ""
            lines.append(
                f"跳过 {len(report.unknown_types)} 个当前不存在的节点类型：{titles}{more}（相关连线一并忽略）"
            )
        if report.dropped_params:
            for title, type_, keys in report.dropped_params[:8]:
                lines.append(f"「{title}」({type_})：丢弃过时参数 {', '.join(keys)}")
            if len(report.dropped_params) > 8:
                lines.append(f"…另有 {len(report.dropped_params) - 8} 个节点丢弃了过时参数")
        if report.reset_choices:
            for title, param, old in report.reset_choices[:8]:
                lines.append(f"「{title}」参数「{param}」取值 {old!r} 已不存在，恢复为默认值")
            if len(report.reset_choices) > 8:
                lines.append(f"…另有 {len(report.reset_choices) - 8} 个参数恢复默认值")
        self.statusBar().showMessage("方案已读取，部分旧参数不兼容（详见提示）")
        QtWidgets.QMessageBox.warning(self, "旧存档兼容", "\n".join(lines))


    def clear_cache(self, *_args):
        if self.worker.busy: self.statusBar().showMessage("运行中不能清理缓存"); return
        nodes = [node for node in self.graph.all_nodes() if isinstance(node, StudioNode)]
        release_previews(nodes)
        self.backend.clear_workspace()
        for node in nodes:
            node.output_data = None
            node.preview_output = None
            node.output_metadata = {}
            node.dirty = True
            node.set_status("dirty")
            node.panel.show_runtime_info(node.last_elapsed_seconds, 0)
            if node in self.graph.selected_nodes(): self.help.show_node(node)
        self.statusBar().showMessage("临时缓存已清理")


    def delete_selection(self, *_args):
        if self.worker.busy: self.statusBar().showMessage("节点运行中，暂不能删除"); return
        pipes = list(self.graph.selected_pipes())
        for pipe in pipes:
            try: pipe.input_port.disconnect_from(pipe.output_port)
            except AttributeError:
                try: pipe.port_from.disconnect_from(pipe.port_to)
                except AttributeError: pass
        nodes = self.graph.selected_nodes()
        if nodes:
            self.delete_nodes(nodes)


    def delete_nodes(self, nodes):
        if self.worker.busy: self.statusBar().showMessage("节点运行中，暂不能删除"); return
        # 释放被删节点**及其下游节点**的预览：下游预览（QMovie/自定义 GIF
        # 播放器）可能正在播放被删节点的产物文件（如 GIF 合成节点的
        # preview.gif），Windows 下文件被占用会导致缓存目录删除失败
        # （WinError 32，实测「图片1:1分辨率查看」播放上游 GIF 时删除上游失败）。
        affected = list(nodes)
        for node in nodes:
            affected.extend(_downstream(node))
        release_previews(affected)
        for node in nodes:
            self.backend.for_node(node.id).clear_cache()
            self.panels.pop(node.id, None)
        self.graph.delete_nodes(nodes)

    # --- 方案保存/读取：通常保存（有路径静默覆盖）+ 另存为 ---

    def _confirm_unsaved_changes(self, action_text: str) -> bool:
        """脏方案被破坏性操作打断（打开新方案/清空/关闭）时的保存确认。

        按钮：保存（默认）/ 放弃 / 取消。

        Returns:
            True = 可以继续（保存成功或用户选择放弃）；
            False = 中止（用户取消，或「保存」走另存为被取消）。
        """
        ret = QtWidgets.QMessageBox.question(
            self,
            "未保存的更改",
            f"当前方案有未保存的更改，要保存后再{action_text}吗？",
            QtWidgets.QMessageBox.StandardButton.Save
            | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Save,
        )
        if ret == QtWidgets.QMessageBox.StandardButton.Save:
            return self.save_preset()
        return ret == QtWidgets.QMessageBox.StandardButton.Discard

    def _confirm_open_new(self) -> bool:
        """打开新方案前的确认（按当前方案 clean/dirty 判定，决策 #85）。

        Returns:
            False = 中止（用户取消 / 保存走另存为被取消）；True = 可继续。
        """
        if self._session_dirty:
            return self._confirm_unsaved_changes("打开其他方案")
        return True

