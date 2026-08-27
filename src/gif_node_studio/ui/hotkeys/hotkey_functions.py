#!/usr/bin/python

# ------------------------------------------------------------------------------
# menu command functions
# ------------------------------------------------------------------------------
# 画布右键菜单/顶栏共用的 graph 级功能函数（单 action 管理器：命令只在此实现，
# 菜单与工具栏共用；快捷键见 ui._build_graph_context_menu/_build_context_menus）。
# 仅保留实际使用的函数；保存/读取/删除/克隆等应用语义在 ui.py（MainWindow 方法）。


def zoom_in(graph):
    """
    Set the node graph to zoom in by 0.1
    """
    zoom = graph.get_zoom() + 0.1
    graph.set_zoom(zoom)


def zoom_out(graph):
    """
    Set the node graph to zoom in by 0.1
    """
    zoom = graph.get_zoom() - 0.2
    graph.set_zoom(zoom)


def reset_zoom(graph):
    """
    Reset zoom level.
    """
    graph.reset_zoom()


def layout_h_mode(graph):
    """
    Set node graph layout direction to horizontal.
    """
    graph.set_layout_direction(0)


def layout_v_mode(graph):
    """
    Set node graph layout direction to vertical.
    """
    graph.set_layout_direction(1)


def clear_undo(graph):
    """
    Prompts a warning dialog to clear undo.
    """
    viewer = graph.viewer()
    msg = 'Clear all undo history, Are you sure?'
    if viewer.question_dialog('Clear Undo History', msg):
        graph.clear_undo_stack()


def select_all_nodes(graph):
    """
    Select all nodes.
    """
    graph.select_all()


def clear_node_selection(graph):
    """
    Clear node selection.
    """
    graph.clear_selection()


def invert_node_selection(graph):
    """
    Invert node selection.
    """
    graph.invert_selection()


def fit_to_selection(graph):
    """
    Sets the zoom level to fit selected nodes.
    """
    graph.fit_to_selection()


def show_undo_view(graph):
    """
    Show the undo list widget.
    """
    graph.undo_view.show()


def clear_node_connections(graph):
    """
    Clear port connection on selected nodes.
    """
    graph.undo_stack().beginMacro('clear selected node connections')
    for node in graph.selected_nodes():
        for port in node.input_ports() + node.output_ports():
            port.clear_connections()
    graph.undo_stack().endMacro()


def curved_pipe(graph):
    """
    Set node graph pipes layout as curved.
    """
    from NodeGraphQt.constants import PipeLayoutEnum
    graph.set_pipe_style(PipeLayoutEnum.CURVED.value)


def straight_pipe(graph):
    """
    Set node graph pipes layout as straight.
    """
    from NodeGraphQt.constants import PipeLayoutEnum
    graph.set_pipe_style(PipeLayoutEnum.STRAIGHT.value)


def angle_pipe(graph):
    """
    Set node graph pipes layout as angled.
    """
    from NodeGraphQt.constants import PipeLayoutEnum
    graph.set_pipe_style(PipeLayoutEnum.ANGLE.value)


def bg_grid_none(graph):
    """
    Turn off the background patterns.
    """
    from NodeGraphQt.constants import ViewerEnum
    graph.set_grid_mode(ViewerEnum.GRID_DISPLAY_NONE.value)


def bg_grid_dots(graph):
    """
    Set background node graph background with grid dots.
    """
    from NodeGraphQt.constants import ViewerEnum
    graph.set_grid_mode(ViewerEnum.GRID_DISPLAY_DOTS.value)


def bg_grid_lines(graph):
    """
    Set background node graph background with grid lines.
    """
    from NodeGraphQt.constants import ViewerEnum
    graph.set_grid_mode(ViewerEnum.GRID_DISPLAY_LINES.value)


def layout_graph_down(graph):
    """
    Auto layout the nodes down stream.
    """
    nodes = graph.selected_nodes() or graph.all_nodes()
    graph.auto_layout_nodes(nodes=nodes, down_stream=True)


def layout_graph_up(graph):
    """
    Auto layout the nodes up stream.
    """
    nodes = graph.selected_nodes() or graph.all_nodes()
    graph.auto_layout_nodes(nodes=nodes, down_stream=False)


def layout_graph_smart(graph):
    """智能布局：复合分级（终点锚定，嵌套背景板感知）。

    算法：``core.compound_layout.layout_compound``（见 docs/research/
    compound-layout-feasibility.md）——以语义终点（EXPORT 类节点，如
    ico合成）为锚反向分层，终点的全部直连输入同列扇入、列内按输入端口
    定义序排；背景板 = 输出端口并集节点（组内无终点时以组外出边成员为
    伪锚）；方向性与无遮挡由构造保证。

    作用域（#108 规则 + 背景框闭合）：
    - 选中 ≥2 项 → 布局选中集，并自动扩展为「含选中节点的背景框及其
      （传递）嵌套框 + 全部成员」——保证框内节点不被裁剪、嵌套框整体
      参与；游离选中节点单独布局；
    - 否则布局全部节点与背景框（只选中 1 个 = 移到原点，无意义；
      且 connect_to 会意外留下单个选中节点）。

    背景框重设：布局结果直接 ``set_pos`` + ``set_property(width/height)``
    （不用 ``wrap_nodes``——其按位置判定会在成员移动后失真，且自带撤销
    宏与整体单宏冲突）；全部包进 ``begin_undo("智能布局")`` 单撤销宏。

    垂直布局方向待后续迭代（当前始终水平排布）。
    """
    from NodeGraphQt import BackdropNode

    from gif_node_studio.core.compound_layout import GroupBox, layout_compound
    from gif_node_studio.core.graph_layout import GraphNode
    from gif_node_studio.nodes.registry import NODE_CLASSES

    all_nodes = graph.all_nodes()
    all_backs = [n for n in all_nodes if isinstance(n, BackdropNode)]
    all_plain = [n for n in all_nodes if not isinstance(n, BackdropNode)]

    selected = graph.selected_nodes()
    sel_backs = [n for n in selected if isinstance(n, BackdropNode)]
    sel_plain = [n for n in selected if not isinstance(n, BackdropNode)]

    if len(sel_plain) + len(sel_backs) < 2:
        # 未选中或只选中 1 个 → 布局全部。
        layout_backs = all_backs
        layout_nodes = all_plain
    else:
        # 背景框闭合：选中框 ∪ 含选中节点的框 ∪ 其内嵌套框（传递）。
        keep = {b.id for b in sel_backs}
        sel_ids = {n.id for n in sel_plain}
        for b in all_backs:
            if sel_ids & {m.id for m in b.nodes()}:
                keep.add(b.id)
        changed = True
        while changed:
            changed = False
            for b in all_backs:
                if b.id in keep:
                    continue
                if any(b.id in {m.id for m in kb.nodes()} for kb in all_backs if kb.id in keep):
                    keep.add(b.id)
                    changed = True
        layout_backs = [b for b in all_backs if b.id in keep]
        member_ids = {m.id for b in layout_backs for m in b.nodes() if not isinstance(m, BackdropNode)}
        layout_nodes = [n for n in all_plain if n.id in member_ids]
        layout_nodes += [n for n in sel_plain if n.id not in member_ids]
        if len(layout_nodes) + len(layout_backs) < 2:
            layout_backs, layout_nodes = all_backs, all_plain

    if not layout_nodes and not layout_backs:
        return

    node_by_id = {n.id: n for n in layout_nodes}
    nodes_in = [GraphNode(n.id, n.view.width, n.view.height) for n in layout_nodes]
    groups_in = [GroupBox(b.id, b.x_pos(), b.y_pos(), b.view.width, b.view.height) for b in layout_backs]
    saved = {n.id: (n.x_pos(), n.y_pos()) for n in layout_nodes}

    # 边：两端都在布局集内（connected_input_nodes → {port: [nodes]}，去重）。
    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for node in layout_nodes:
        for src_nodes in node.connected_input_nodes().values():
            for input_node in src_nodes:
                if input_node.id in node_by_id:
                    key = (input_node.id, node.id)
                    if key not in seen:
                        seen.add(key)
                        edges.append(key)

    # 语义终点 = EXPORT_KIND 类节点（type_ 以类名结尾匹配，不依赖
    # __identifier__ 前缀）。
    anchor_suffix = {cls.__name__ for cls in NODE_CLASSES if getattr(cls, "EXPORT_KIND", None)}
    anchors = {n.id for n in layout_nodes if n.type_.rsplit(".", 1)[-1] in anchor_suffix}

    # 终点输入端口序：锚实例 input_ports() 即定义顺序（无需实例化新对象）。
    port_order: dict[str, list[str]] = {}
    for a in anchors:
        node = node_by_id[a]
        by_port: dict[str, str] = {}
        for port in node.input_ports():
            for conn in port.connected_ports():
                src = conn.node()
                if src.id in node_by_id:
                    by_port.setdefault(port.name(), src.id)
        port_order[a] = [by_port[p.name()] for p in node.input_ports() if p.name() in by_port]

    pos, rects = layout_compound(
        nodes_in, edges, groups_in, saved,
        anchors=anchors,
        sink_input_order=port_order,
    )

    graph.begin_undo("智能布局")
    try:
        for nid, (x, y) in pos.items():
            graph.get_node_by_id(nid).set_pos(x, y)
        for bid, (x, y, w, h) in rects.items():
            backdrop = graph.get_node_by_id(bid)
            backdrop.set_pos(x, y)
            backdrop.set_property("width", w)
            backdrop.set_property("height", h)
    finally:
        graph.end_undo()


def toggle_node_search(graph):
    """
    show/hide the node search widget.
    """
    graph.toggle_node_search()
