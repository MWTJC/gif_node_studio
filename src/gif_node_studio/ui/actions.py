"""动作定义唯一登记处（单 action 管理器）。

每个动作的 **标签/图标/快捷键/处理函数/工具栏别名** 只在此定义一次；
画布右键菜单（``GRAPH_MENU``）、节点右键菜单（``NODE_MENU``）、顶栏
（``TOOLBAR``）三个构建方都只消费这份目录——不再把同一动作的定义分散在
``hotkey_functions.py``（仅实现）与 ``MainWindow`` 的两个构建方法里。

约定：
- ``handler`` 为 str 时 = MainWindow 方法名（构建时 ``getattr`` 解析）；
  为 callable 时 = graph 级函数（hotkey_functions），签名 ``func(graph)``；
- 菜单命令函数统一接受 ``graph`` 参数（GraphAction.executed 发出 graph），
  MainWindow 方法统一 ``*_args`` 兼容工具栏无参 triggered；
- 工具栏别名（``alias``）供测试/内部逻辑引用（如 ``save_json_action``）；
- 同一逻辑操作在画布菜单与节点菜单中为**不同键**（快捷键单点登记的归属不同，
  如 ``edit.clone`` 持 Ctrl+C，``node.clone`` 无快捷键）。

⚠ **模块级常量禁止构造 Qt 对象**：本文件在导入期执行（早于 QApplication），
`shortcut` 一律用字符串（如 ``"Ctrl+S"``、``["Ctrl+Y", "Ctrl+Shift+Z"]``）；
在目录里直接写 ``QtGui.QKeySequence(...)`` 会因无 QApplication 触发
Windows 原生访问违例（实测段错误，见关键决策 #65）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from NodeGraphQt import BackdropNode

from .hotkeys.hotkey_functions import (
    clear_node_connections,
    clear_undo,
    invert_node_selection,
    layout_graph_smart,
    layout_graph_up,
    layout_h_mode,
    layout_v_mode,
    show_undo_view,
    toggle_node_search,
    zoom_in,
    zoom_out,
)
from ..nodes.node_base import StudioNode

# 分隔线哨兵（菜单/工具栏布局中表示 addSeparator）。
SEP = object()

# 动态子菜单哨兵（仅 MENUBAR）：菜单栏「文件」下的「导入预设」子菜单由
# MainWindowMenusMixin._build_presets_submenu 构建——内容随 node_presets
# 目录动态刷新（aboutToShow），不是静态动作键，故用哨兵占位（见决策 #89）。
PRESETS_SUBMENU = object()


@dataclass(frozen=True)
class ActionDef:
    """单个动作的完整定义（唯一源头）。

    Args:
        key: 唯一键（"file.save" 等；_ctx_commands 以键为索引）。
        label: 菜单/按钮文本。
        icon: qtawesome 图标名（None = 无图标）。
        icon_color: qtawesome 图标颜色（None = 默认）。
        shortcut: str / QKeySequence / 列表（多快捷键，如重做 Ctrl+Y+Ctrl+Shift+Z）。
        handler: MainWindow 方法名（str）或 graph 级函数（callable）。
        alias: 构建后挂到 MainWindow 的属性名（工具栏/测试引用）。
        checkable: 是否为开关型动作（连接 toggled 而非 triggered）。
    """

    key: str
    label: str
    icon: str | None = None
    icon_color: str | None = None
    shortcut: Any = None
    handler: str | Callable = ""
    alias: str | None = None
    checkable: bool = False


# ---------------------------------------------------------------------------
# 动作目录：键 → 定义（标签/图标/快捷键/处理函数/别名）
# ---------------------------------------------------------------------------

ACTIONS: dict[str, ActionDef] = {
    # --- 文件 ---
    "file.open": ActionDef("file.open", "打开方案…", "msc.folder-opened", shortcut="Ctrl+O", handler="load_preset", alias="read_json_action"),
    "file.import": ActionDef("file.import", "导入方案…", "mdi.file-import", handler="import_preset"),
    "file.preset.open_folder": ActionDef("file.preset.open_folder", "打开预设文件夹…", "mdi.folder-open-outline", handler="open_presets_folder", alias="open_presets_folder_action"),
    "file.clear": ActionDef("file.clear", "清空方案…", "mdi.file", handler="clear_session_prompt"),
    "file.save": ActionDef("file.save", "保存方案…", "msc.save", shortcut="Ctrl+S", handler="save_preset", alias="save_json_action"),
    "file.save_as": ActionDef("file.save_as", "另存方案…", "msc.save-as", shortcut="Ctrl+Shift+S", handler="save_preset_as"),
    "file.quit": ActionDef("file.quit", "退出", shortcut="Ctrl+Shift+Q", handler="_ctx_quit"),
    # --- 编辑 ---
    "edit.clear_undo": ActionDef("edit.clear_undo", "清空撤销历史", handler=clear_undo),
    "edit.show_undo": ActionDef("edit.show_undo", "显示撤销历史", handler=show_undo_view),
    "edit.delete": ActionDef("edit.delete", "删除", "ri.chat-delete-fill", shortcut="Del", handler="delete_selection", alias="del_node_action"),
    "edit.select_all": ActionDef("edit.select_all", "全选", "mdi.select-all", shortcut="Ctrl+A", handler="select_all_nodes", alias="select_all_action"),
    "edit.deselect": ActionDef("edit.deselect", "取消全选", shortcut="Ctrl+Shift+A", handler="clear_selection"),
    "edit.invert": ActionDef("edit.invert", "反选", handler=invert_node_selection),
    "edit.clone": ActionDef("edit.clone", "克隆", "mdi.content-copy", shortcut="Ctrl+C", handler="clone_selection", alias="clone_action"),
    "edit.clear_connections": ActionDef("edit.clear_connections", "清空连线", handler=clear_node_connections),
    "edit.fit": ActionDef("edit.fit", "适配选中", "mdi.arrow-expand-all", handler="fit_to_selection", alias="fit_to_selection_action"),
    "edit.zoom_in": ActionDef("edit.zoom_in", "放大", "mdi6.magnify-plus-outline", shortcut="=", handler=zoom_in),
    "edit.zoom_out": ActionDef("edit.zoom_out", "缩小", "mdi6.magnify-minus-outline", shortcut="-", handler=zoom_out),
    "edit.reset_zoom": ActionDef("edit.reset_zoom", "重置缩放", "mdi6.magnify-expand", shortcut="H", handler="reset_zoom", alias="reset_zoom_action"),
    # --- 画布（背景/布局） ---
    "graph.bg.none": ActionDef("graph.bg.none", "无", shortcut="Alt+1", handler="_grid_none"),
    "graph.bg.lines": ActionDef("graph.bg.lines", "网格线", shortcut="Alt+2", handler="_grid_lines"),
    "graph.bg.dots": ActionDef("graph.bg.dots", "圆点", shortcut="Alt+3", handler="_grid_dots"),
    "graph.layout.h": ActionDef("graph.layout.h", "水平", shortcut="Shift+1", handler=layout_h_mode),
    "graph.layout.v": ActionDef("graph.layout.v", "垂直", shortcut="Shift+2", handler=layout_v_mode),
    # --- 节点 ---
    "node.search": ActionDef("node.search", "节点搜索", "mdi6.magnify", shortcut="Tab", handler=toggle_node_search),
    "node.layout.up": ActionDef("node.layout.up", "上游自动布局", shortcut="L", handler=layout_graph_up),
    "node.layout.down": ActionDef("node.layout.down", "下游自动布局", "mdi.arrow-down-bold", shortcut="Ctrl+L", handler="layout_graph_down", alias="layout_down_action"),
    "node.layout.smart": ActionDef("node.layout.smart", "智能布局", "mdi.auto-fix", shortcut="Ctrl+Shift+L", handler=layout_graph_smart, alias="smart_layout_action"),
    # --- 连线 ---
    "pipe.curved": ActionDef("pipe.curved", "曲线", shortcut="Ctrl+1", handler="_pipe_curved"),
    "pipe.straight": ActionDef("pipe.straight", "直线", shortcut="Ctrl+2", handler="_pipe_straight"),
    "pipe.angle": ActionDef("pipe.angle", "折线", shortcut="Ctrl+3", handler="_pipe_angle"),
    # --- 节点右键（选中集语义；快捷键单点登记：Ctrl+D/F 由节点菜单持有） ---
    "node.delete": ActionDef("node.delete", "删除节点", handler="_ctx_delete_node"),
    "node.clone": ActionDef("node.clone", "克隆", handler="_ctx_duplicate_node"),
    "node.clear_connections": ActionDef("node.clear_connections", "清空连线", shortcut="Ctrl+D", handler="_ctx_clear_connections"),
    "node.fit": ActionDef("node.fit", "适配选中", shortcut="F", handler="_ctx_fit_to_selection"),
    "node.rename_backdrop": ActionDef("node.rename_backdrop", "重命名背景框", handler="_ctx_rename_backdrop"),
    "node.backdrop_title_height": ActionDef("node.backdrop_title_height", "标题栏高度…", handler="_ctx_backdrop_title_height"),
    # --- 纯工具栏动作（无菜单项；定义同样集中于此） ---
    "auto": ActionDef("auto", "自动", "msc.run-all", icon_color="green", handler="_on_auto_toggled", alias="auto_action", checkable=True),
    "stop": ActionDef("stop", "停止", "msc.stop-circle", icon_color="red", handler="_on_stop_clicked", alias="stop_action"),
    "undo": ActionDef("undo", "撤销", "mdi.undo", shortcut="Ctrl+Z", handler="undo", alias="undo_action"),
    "redo": ActionDef("redo", "重做", "mdi.redo", shortcut=["Ctrl+Y", "Ctrl+Shift+Z"], handler="redo", alias="redo_action"),
    "cache.clear": ActionDef("cache.clear", "清缓存", "mdi.delete-empty-outline", handler="clear_cache", alias="clc_cache_action"),
    "settings": ActionDef("settings", "设置", "mdi.cog-outline", handler="open_settings", alias="settings_action"),
    "pipe.toggle": ActionDef("pipe.toggle", "连线样式", "mdi.vector-polyline", handler="toggle_pipe_style", alias="pipe_toggle_action"),
    "bg.toggle": ActionDef("bg.toggle", "背景网格", "mdi.grid", handler="toggle_grid_mode", alias="bg_toggle_action"),
}

# ---------------------------------------------------------------------------
# 画布右键菜单布局：[(顶级菜单标题, [动作键 | 子菜单(标题, [键…]) | SEP])]
# ---------------------------------------------------------------------------

GRAPH_MENU: list[tuple[str, list]] = [
    ("&文件", [
        "file.open", "file.import", "file.clear", SEP,
        "file.save", "file.save_as", SEP,
        "file.quit",
    ]),
    ("&编辑", [
        "edit.clear_undo", "edit.show_undo", SEP,
        "edit.delete", SEP,
        "edit.select_all", "edit.deselect", "edit.invert",
        "edit.clone", "edit.clear_connections", "edit.fit", SEP,
        "edit.zoom_in", "edit.zoom_out", "edit.reset_zoom",
    ]),
    ("&画布", [
        ("&背景", ["graph.bg.none", "graph.bg.lines", "graph.bg.dots"]),
        ("&布局", ["graph.layout.h", "graph.layout.v"]),
    ]),
    ("&节点", [
        "node.search", SEP,
        "node.layout.up", "node.layout.down", "node.layout.smart",
    ]),
    ("&连线", [
        "pipe.curved", "pipe.straight", "pipe.angle",
    ]),
]

# ---------------------------------------------------------------------------
# 节点右键菜单布局：[(节点类, [命令组[动作键…], …])]，组间插入分隔线
# ---------------------------------------------------------------------------

NODE_MENU: list[tuple[type, list[list[str]]]] = [
    (StudioNode, [
        ["node.delete"],                      # Del 由画布「删除」持有
        ["node.clone"],                       # Ctrl+C 由画布「克隆」持有
        ["node.clear_connections", "node.fit"],
    ]),
    (BackdropNode, [
        ["node.rename_backdrop", "node.backdrop_title_height", "node.delete"],
    ]),
]

# ---------------------------------------------------------------------------
# 顶栏布局：[动作键 | SEP]（菜单+工具栏双挂的动作直接复用菜单 QAction）
# ---------------------------------------------------------------------------

TOOLBAR: list = [
    "file.clear", "file.open", "file.save",SEP,
    "auto", "stop",
    "undo", "redo", SEP,
    "edit.fit", "edit.reset_zoom", "node.layout.smart",
    "pipe.toggle", "bg.toggle", SEP,
    "settings",
]

# ---------------------------------------------------------------------------
# 主窗口菜单栏布局（QMenuBar，参照常规软件；多级子菜单用嵌套元组表示）。
#
# 与 GRAPH_MENU/TOOLBAR 一样只含动作键：构建方（MainWindow._build_menu_bar）
# 通过 _action_for_key 解析到**同一** QAction（画布菜单的 _ctx_commands 或
# 工具栏别名），因此菜单栏项目与右键菜单/工具栏共享对象，快捷键、图标、
# 勾选状态自动一致，不产生重复快捷键注册。
# 节点级动作（NODE_MENU）依赖右键菜单注入的 node_id，不放入菜单栏。
# ---------------------------------------------------------------------------

MENUBAR: list[tuple[str, list]] = [
    ("文件(&F)", [
        "file.open", "file.import", PRESETS_SUBMENU, "file.clear", SEP,
        "file.save", "file.save_as", SEP,
        "file.quit",
    ]),
    ("编辑(&E)", [
        "undo", "redo", SEP,
        "edit.clear_undo", "edit.show_undo", SEP,
        "edit.delete", "edit.clone", SEP,
        "edit.select_all", "edit.deselect", "edit.invert",
        "edit.clear_connections", "edit.fit", SEP,
        "edit.zoom_in", "edit.zoom_out", "edit.reset_zoom",
    ]),
    ("画布(&G)", [
        ("&背景", ["graph.bg.none", "graph.bg.lines", "graph.bg.dots"]),
        "bg.toggle", SEP,
        ("&布局", ["graph.layout.h", "graph.layout.v"]), SEP,
        ("连线样式(&S)", ["pipe.curved", "pipe.straight", "pipe.angle"]),
        "pipe.toggle",
    ]),
    ("节点(&N)", [
        "node.search", SEP,
        "node.layout.up", "node.layout.down", "node.layout.smart",
    ]),
    ("运行(&R)", [
        "auto", "stop",
    ]),
    ("工具(&T)", [
        "cache.clear", "settings",
    ]),
]
