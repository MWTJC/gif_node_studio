# 实现方案：复合（分级）智能布局（决策草稿，待评审，2026-08）

> 结论先行：**纯计算核心与 UI 适配层均已落地并全量测试通过**（31 项全绿，
> 含 logo.json 真实场景回归与 4 项离屏 UI 适配测试）；评审确认项均已拍板
> （按 #108 接线 + 工具栏、新背景框重设、nest_inset 默认、垂直方向推迟）。
> 前置文档：[可行性分析 v2](compound-layout-feasibility.md)（算法语义、
> 实测数据与败因分析）；[#108 智能布局回退记录](../decisions/101-110.md#d108)。
>
> 用户已确认：带的垂直次序等审美微调**推迟到后续迭代**，不在本方案范围；
> 垂直布局方向同样推迟。

---

## 1. 范围

**落地内容**：

1. 纯计算核心 `core/compound_layout.py`（嵌套背景板 + 终点锚定分层，
   **已完成**，`tests/test_compound_layout.py` 12 项 + 全量回归通过）；
2. `core/graph_layout.py` 最小改动（`protected` 可选参数，默认行为不变，
   已改并回归通过）；
3. UI 适配层（本方案的实现部分）：恢复「智能布局」动作与接线。

**明确不做**：

- 带的垂直次序（用户：等软件迭代再细化）；
- 连线路由美化（真实应用走 NodeGraphQt 管道，布局只需端点方向单调）；
- 垂直布局方向（`layout_direction=1` 暂用水平结果转置，或后续迭代）。

## 2. 代码落地清单

| 文件 | 状态 | 内容 |
|---|---|---|
| `src/gif_node_studio/core/compound_layout.py` | ✅ 已落地 | `layout_compound` + `GroupBox` + 内部实现（纯计算、无 Qt） |
| `src/gif_node_studio/core/graph_layout.py` | ✅ 已改 | `_squarify_layers` / `_split_oversized_columns` 增加 `protected: set[str] | None = None`（含保护节点 id 的层/列不合并/不拆分；None = 原行为） |
| `tests/test_compound_layout.py` | ✅ 已落地 | 12 项契约测试（分层/复合/保护/nest_inset/确定性/logo 回归） |
| `src/gif_node_studio/ui/hotkeys/hotkey_functions.py` | ✅ 已落地 | `layout_graph_smart`（适配层，见 §4） |
| `src/gif_node_studio/ui/actions.py` | ✅ 已落地 | 恢复 `node.layout.smart`「智能布局」`Ctrl+Shift+L`（画布右键 + 菜单栏 + **工具栏**） |
| `tests/test_layout_smart_ui.py` | ✅ 已落地 | 4 项离屏 UI 测试（扇入/撤销往返/作用域/闭包） |

## 3. 核心 API（已完成，摘要）

```python
def layout_compound(
    nodes: list[GraphNode],                              # 普通节点（id/宽/高）
    edges: list[tuple[str, str]],                        # 有向边 (source, target)
    groups: list[GroupBox],                              # 背景板：id + 存档 x/y/width/height
    saved_positions: dict[str, tuple[float, float]],     # 普通节点存档位置（成员判定/内边距派生）
    *,
    anchors: set[str] | None = None,                     # 语义终点（EXPORT 类节点）
    sink_input_order: dict[str, list[str]] | None = None,# {终点: [直连输入按端口定义序]}
    h_gap=80.0, v_gap=24.0, max_h_gap=160.0,
    nest_inset=100.0, iterations=12, max_aspect_ratio=2.2,
) -> tuple[dict[str, tuple[float, float]],               # {节点: (x, y)} 绝对坐标
           dict[str, tuple[float, float, float, float]]] # {组框: (x, y, w, h)}
```

**算法语义**（详见可行性分析 v2）：

- 终点锚定分层：层 = D − 到终点最长距离；终点直连输入同列（扇入钳制处理
  菱形结构），列内次序 = 输入端口定义序（`sink_input_order`）；
- 组 = 输出端口并集节点：组内无终点时以「有组外出边的成员」为伪锚；
- 保护层（锚层 + 锚输入层 + 嵌套超节点邻居层）不合并/不拆分；
- 方向性由构造保证（无后置平移）；无遮挡由逐级 gap 打包复合保证；
- `nest_inset`：含嵌套子框的父框内边距 ≥ 100/60px（嵌套框四周可见父框底色）。

## 4. UI 适配层设计（已实现）

`layout_graph_smart(graph)` 的职责（动作 `node.layout.smart`，
`Ctrl+Shift+L`，`mdi.auto-fix`，挂画布右键菜单 + 菜单栏「节点」组，
位置在「上游/下游自动布局」之后——与 #108 相同的接线位置）：

```python
def layout_graph_smart(graph):
    """智能布局：复合分级（终点锚定）。"""
    # 1) 作用域：选中 ≥2 节点 → 布局选中集；否则布局全部。
    #    （只选中 1 个 = 移到原点无意义；且 connect_to 会意外选中目标节点。）
    nodes = graph.selected_nodes()
    if len(nodes) >= 2:
        layout_nodes = [n for n in nodes if not isinstance(n, BackdropNode)]
        layout_backdrops = [n for n in nodes if isinstance(n, BackdropNode)]
    else:
        layout_nodes = [n for n in graph.all_nodes() if not isinstance(n, BackdropNode)]
        layout_backdrops = [n for n in graph.all_nodes() if isinstance(n, BackdropNode)]

    # 2) 成员快照：backdrop.nodes() 按位置判定，布局前必须记录
    #    （布局会移动节点，判定随之失效）。
    snapshot = {b.id: b.nodes() for b in layout_backdrops}

    # 3) 输入构建
    nodes_in = [GraphNode(n.id, n.view.width, n.view.height) for n in layout_nodes]
    edges_in = [(u, v) for u, v in 组内连接边]          # 两端都在布局集内的边（去重）
    groups_in = [GroupBox(b.id, b.x_pos(), b.y_pos(), b.view.width, b.view.height)
                 for b in layout_backdrops]
    saved = {n.id: (n.x_pos(), n.y_pos()) for n in layout_nodes}

    # 4) 锚点与端口序（语义终点 = EXPORT_KIND 类；端口序 = 实例 input_ports()）
    anchors = {n.id for n in layout_nodes if 类含 EXPORT_KIND}
    port_order = {}
    for sink_id in anchors:
        inst = 对应节点类实例()          # 离屏实例化（或缓存类→端口名序列）
        order = [p.name() for p in inst.input_ports()]
        port_order[sink_id] = [按连接端口名映射到输入节点 id，按 order 排列]

    # 5) 调用核心
    pos, rects = layout_compound(nodes_in, edges_in, groups_in, saved,
                                 anchors=anchors, sink_input_order=port_order)

    # 6) 单撤销宏：一次 Ctrl+Z 恢复全部
    graph.begin_undo("智能布局")
    try:
        for nid, (x, y) in pos.items():
            node = graph.get_node_by_id(nid)
            node.set_pos(x, y)
        for bid, (x, y, w, h) in rects.items():
            b = graph.get_node_by_id(bid)
            b.set_pos(x, y)
            b.set_property("width", w)
            b.set_property("height", h)
    finally:
        graph.end_undo()
```

**实现中的两处 API 修正**（与初稿伪代码不同，已按实测修正）：

- `node.connected_input_nodes()` 实际返回 `{port: [node_list]}`（键为端口
  对象、值为节点列表），边收集须双层展开去重；
- 终点输入端口序**无需实例化新对象**：锚节点自身 `input_ports()` 即定义
  顺序（NodeGraphQt 按定义顺序建端口），直接读取即可（初稿的「离屏实例化
  + 缓存」作废，更简单）；
- 离屏测试选中须用 `node.view.setSelected(True)`（`set_property('selected')`
  不更新场景选中，`graph.selected_nodes()` 读的是 viewer）；撤销用
  `graph.undo_stack().undo()`。

**关键细节**：

- **成员快照**：`backdrop.nodes()` 在布局前调用并保存（#108 沉淀）；
- **锚点提取**：从 `nodes/registry.py` 的 `NODE_CLASSES` 取 `EXPORT_KIND`
  类属性（不实例化）；端口序需实例化节点读 `input_ports()`——原型已验证
  离屏可实例化（`QT_QPA_PLATFORM=offscreen` + QApplication）；可加一层
  「类名 → 端口名序列」缓存避免每次布局实例化；
- **背景框重设**：直接 `set_pos` + `set_property("width"/"height")`
  （不用 `wrap_nodes`——其按位置判定会在成员移动后失真；且会打开自己的
  撤销宏，与步骤 6 的单宏冲突）；
- **undo**：`begin_undo`/`end_undo` 包住全部移动与重设（#108 已验证
  undo/redo 往返一致）；
- **垂直方向**：`graph.viewer().get_layout_direction() == 1` 时对
  `layout_compound` 结果做 (x, y) 转置（核心暂不内置）。

## 5. 测试契约

**已完成**（`tests/test_compound_layout.py`，12 项）：

- 终点锚定分层：菱形直连输入同列、侧输出靠右、中游分叉叶子方向保持、
  无锚退化（源侧最长路）；
- 复合布局：节点/组框不重叠、成员与嵌套框包含、方向单调、端口序扇入、
  保护层不拆散 12 输入列、nest_inset 单调性、确定性；
- graph_layout protected 参数单测（合并/拆分保护）；
- logo.json 真实场景回归：50 节点 / 10 背景框 / 59 边，零重叠、零逃逸、
  方向全对、7 输入同列。

**已完成**（`tests/test_layout_smart_ui.py`，5 项离屏）：

- 全图布局：7 直连输入同列扇入、方向保持、背景框重设后成员仍在框内；
- undo/redo 往返一致：一次 `undo_stack().undo()` 恢复全部位置与框尺寸；
- 选中作用域：选中 ≥2 只动选中集（游离节点不动）；
- 背景框闭合：选中组内节点自动扩展整组（未选中组内成员参与布局、
  组外节点不动）；
- **logo.json 端到端**：全量反序列化 → 智能布局，7 输入同列、
  10 背景框成员均在框内（含嵌套）——用户实测路径的离屏镜像。

## 6. 验证流程

1. `uv run pytest` 全量（27 项）；
2. offscreen 渲染 logo.json 布局 → PNG（~1800px 宽）→ 目检：
   嵌套留白、7 输入单列扇入、三带方向、无重叠（#108 教训：勿只信几何
   断言）；
3. 用户实测：加载 logo.json → Ctrl+Shift+L → 目检 + Ctrl+Z 恢复。

## 7. 风险与开放问题

- **端口序提取成本**：实例化节点读 `input_ports()` 需离屏 QApplication；
  若成为启动/布局热点，做类级缓存（端口名序列与实例无关，仅一次）；
- **无终点预设的退化**：`anchors=None` 时每级退回「组外出边成员为伪锚」，
  仍无锚（如纯分析链）退回源侧最长路——行为与平铺一致但少优雅；
- **背景框重设 vs 用户手动调整**：布局会覆盖用户对框尺寸的微调
  （内边距派生自存档，尽量贴近原风格；大改后按新内容重算）；
- **性能**：50 节点量级微秒级；`_sink_anchored_layers` 的扇入钳制为
  迭代修复点（边数小，无压力）。

## 8. 评审确认结果（已拍板）

1. ✅ UI 适配层按 #108 接线实现（`node.layout.smart`，Ctrl+Shift+L，
   画布右键 + 菜单栏「节点」组），**额外附加到工具栏**；
2. ✅ 背景框重设用新方案（`set_pos` + `set_property("width"/"height")`，
   单 undo 宏内；不用 wrap_nodes）；
3. ✅ `nest_inset` 默认 100/60px（优雅即可，不必对齐手排数值）；
4. ✅ 垂直布局方向推迟到后续迭代。

> 本方案已落地，后续如需正式记录可转决策编号（接 #109）。
