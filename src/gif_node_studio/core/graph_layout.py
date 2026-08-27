"""DAG 分层布局（简化 Sugiyama + 方形化层合并）：画布「智能布局」的纯计算核心。

与 Qt / NodeGraphQt 完全解耦：输入节点尺寸与有向边，输出各节点左上角坐标。
节点实际尺寸参与布局，保证：同层（同列）节点按高度堆叠互不遮挡、列间距
容纳该列最宽节点；整体趋向紧凑方形；确定性（同输入两次结果一致）。

方法（对齐 Sugiyama 框架五步中的可用部分，见 docs/research/ 智能布局存档）：
1. 连通分量划分（并查集）；
2. 每分量 longest-path 分层：拓扑序 DP，源点层 0（项目连线已拒绝成环，
   回边仅在环出现时被 DP 忽略，布局不崩）；
3. 方形化：层数过多且预计宽高比超限时，贪心合并相邻稀疏层，把整体压成
   接近方形（目标列数 ≈ ceil(sqrt(N))）；合并产生的同层边由 barycenter
   阶段忽略、坐标分配阶段自然呈现为列内短连；
4. 层内排序：barycenter 迭代（相邻层邻居位置均值），固定轮数 + 稳定排序
   保证确定性；
5. 坐标分配：列宽 = 该列最宽节点，列内按节点高度 + v_gap 堆叠、整列垂直
   居中；列间距 h_gap；
6. 高长条（h >> w）时在 [h_gap, max_h_gap] 内放大列间距逼近方形；
7. 多分量按节点数降序沿副轴堆叠、主轴居中；整体平移到非负坐标。

节点矩形 = (x, y, x+width, y+height)；任意两矩形不相交（间距 ≥ gap）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class GraphNode:
    """布局输入：一个节点的稳定 id 与实际面板尺寸（像素）。"""

    id: str
    width: float
    height: float


def layout_graph(
    nodes: list[GraphNode],
    edges: list[tuple[str, str]],
    *,
    direction: str = "horizontal",
    h_gap: float = 80.0,
    v_gap: float = 24.0,
    component_gap: float = 60.0,
    max_h_gap: float = 160.0,
    max_aspect_ratio: float = 2.2,
    iterations: int = 12,
) -> dict[str, tuple[float, float]]:
    """返回 ``{node_id: (x, y)}``（左上角坐标）。

    Args:
        nodes: 全部节点（尺寸参与布局，负/零尺寸按 0 处理）。
        edges: 有向边 ``(source_id, target_id)``；端点不在 ``nodes`` 中的边忽略。
        direction: ``"horizontal"`` 层从左到右 | ``"vertical"`` 层从上到下。
        h_gap: 列间距（主轴方向）。
        v_gap: 层内节点间距（副轴方向）。
        component_gap: 不同连通分量之间的间距。
        max_h_gap: 高长条图允许的最大列间距（自适应方形化的上限）。
        max_aspect_ratio: 宽高比超过该值触发层合并（方形化）。
        iterations: barycenter 层内排序最大迭代轮数（固定轮数保证确定性）。
    """
    if not nodes:
        return {}
    ids = [node.id for node in nodes]
    by_id = {node.id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("layout_graph: 节点 id 重复")

    # 忽略端点不在节点集内的边；去重。
    edge_set: set[tuple[str, str]] = set()
    for source, target in edges:
        if source in by_id and target in by_id:
            edge_set.add((source, target))
    edge_list = sorted(edge_set)

    if direction == "vertical":
        # 垂直方向 = 把宽高互换按水平方向布局后坐标转置。
        swapped = [GraphNode(node.id, node.height, node.width) for node in nodes]
        result = layout_graph(
            swapped,
            edge_list,
            direction="horizontal",
            h_gap=h_gap,
            v_gap=v_gap,
            component_gap=component_gap,
            max_h_gap=max_h_gap,
            max_aspect_ratio=max_aspect_ratio,
            iterations=iterations,
        )
        return {node_id: (y, x) for node_id, (x, y) in result.items()}

    components = _connected_components(ids, edge_list)
    # 分量排序（确定性）：节点数降序，平局按最小节点 id 升序。
    components.sort(key=lambda comp: (-len(comp), min(comp)))

    positions: dict[str, tuple[float, float]] = {}
    component_boxes: list[tuple[float, float]] = []  # (width, height)
    for comp in components:
        pos = _layout_component(comp, by_id, edge_list, h_gap, v_gap, max_h_gap, max_aspect_ratio, iterations)
        positions.update(pos)
        component_boxes.append(_bbox(pos, by_id))

    # 多分量沿副轴（y）堆叠、按最宽分量主轴居中。
    total_width = max((box[0] for box in component_boxes), default=0.0)
    offset_y = 0.0
    for index, comp in enumerate(components):
        width, height = component_boxes[index]
        dx = (total_width - width) / 2.0
        for node_id in comp:
            x, y = positions[node_id]
            positions[node_id] = (x + dx, y + offset_y)
        offset_y += height + component_gap

    return positions


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------


def _connected_components(ids: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """无向连通分量（并查集），返回按 id 字典序排序的节点列表。"""
    parent = {node_id: node_id for node_id in ids}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for source, target in edges:
        union(source, target)

    groups: dict[str, list[str]] = {}
    for node_id in ids:
        groups.setdefault(find(node_id), []).append(node_id)
    return [sorted(group) for group in groups.values()]


def _topo_order(ids: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """Kahn 拓扑序；出现环时把环内剩余节点按输入序追加（布局不崩）。

    队列按**输入顺序**维护（不按 id 排序）：反序列化每次生成随机节点 id，
    按 id 排序会让同一图的两次布局产生不同初始列内顺序（barycenter 稳定
    排序保持初始顺序）——跨运行确定性要求一切决胜键不依赖 id 字符串。
    """
    succ: dict[str, list[str]] = {node_id: [] for node_id in ids}
    indegree = {node_id: 0 for node_id in ids}
    for source, target in edges:
        succ[source].append(target)
        indegree[target] += 1
    ready = [node_id for node_id in ids if indegree[node_id] == 0]
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in succ[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    remaining = [node_id for node_id in ids if node_id not in set(order)]
    return order + remaining


def _longest_path_layers(
    comp: list[str],
    edges: list[tuple[str, str]],
) -> tuple[list[list[str]], dict[str, int]]:
    """longest-path 分层 + 源节点就近下放：返回 ``(按层索引的节点列表, {id: 层})``。

    标准 longest-path 把所有入度 0 的节点放层 0——对「大量独立输入汇聚」的图
    （如 logo 预设：17 个空白序列作为独立输入源）会把全部源堆在第一列
    （极端高长条 + 大量跨级长边）。改进：每个源节点下放到「其直接下游
    目标的最小层 - 1」，输入源靠近消费它的节点（与手工排布直觉一致），
    跨级边随之缩短；孤立源保持层 0。层内顺序初始为拓扑序（确定性）；
    回边（环）不更新层号。
    """
    topo = _topo_order(comp, edges)
    succ: dict[str, list[str]] = {node_id: [] for node_id in comp}
    pred: dict[str, list[str]] = {node_id: [] for node_id in comp}
    for source, target in edges:
        if source in comp and target in comp:
            succ[source].append(target)
            pred[target].append(source)
    layer: dict[str, int] = {node_id: 0 for node_id in comp}
    for current in topo:
        current_layer = layer[current]
        for target in succ[current]:
            layer[target] = max(layer[target], current_layer + 1)
    # 源节点就近下放：入度 0 且下游非空 → 层 = min(下游层) - 1。
    for node_id in comp:
        if not pred[node_id] and succ[node_id]:
            layer[node_id] = min(layer[target] for target in succ[node_id]) - 1
    min_layer = min(layer.values(), default=0)
    if min_layer > 0:
        layer = {node_id: level - min_layer for node_id, level in layer.items()}
    count = max(layer.values(), default=0) + 1
    layers: list[list[str]] = [[] for _ in range(count)]
    for node_id in topo:
        layers[layer[node_id]].append(node_id)
    # 移除空层（源下放可能产生空层），保持拓扑顺序。
    layers = [column for column in layers if column]
    layer_of = {node_id: column for column, ids in enumerate(layers) for node_id in ids}
    return layers, layer_of


def _squarify_layers(
    layers: list[list[str]],
    node_count: int,
    node_sizes: list[tuple[float, float]],
    *,
    max_aspect_ratio: float,
    protected: set[str] | None = None,
    floor_pairs: dict[tuple[str, str], int] | None = None,
) -> list[list[str]]:
    """把层压缩到目标列数，使整体接近方形；贪心合并相邻稀疏层。

    目标列数 T = round(sqrt(N · avg_h / avg_w))：由「列宽 ≈ T·W、列高 ≈
    (N/T)·H，令两者相等」解出——**考虑节点实际尺寸比例**（本项目节点
    高≈450 宽≈300，T 需比 sqrt(N) 更大才不会列内堆叠过高）；纯链/低分支
    图受益，宽扇图（本身层少）不合并。贪心合并相邻层对中节点数之和
    最小的（先把稀疏中间层吞并，同层边最少）。返回新的层列表。

    Args:
        protected: 含这些节点 id 的层**不参与合并**（终点锚定分层中保护
            终点输入列/锚层，见 core.compound_layout）。None = 全部可合并。
        floor_pairs: 跨度下限 ``{(源id, 终点id): 最短路径节点数}``——合并
            不得把任一源→终点链压到短于其最短路径节点数的列数（用户语义：
            布局宽度以列数衡量，最小列数 = 起点到对应终点最短路径节点数）。
            None = 不限（平铺布局保持原行为）。
    """
    if node_count <= 1:
        return [list(column) for column in layers]
    avg_width = sum(w for w, _h in node_sizes) / node_count or 1.0
    avg_height = sum(h for _w, h in node_sizes) / node_count or 1.0
    target = max(2, round(math.sqrt(node_count * avg_height / avg_width)))
    if len(layers) <= target:
        return layers
    if max_aspect_ratio <= 0:
        return layers
    merged = [list(column) for column in layers]

    def _is_protected(column: list[str]) -> bool:
        return bool(protected) and any(node_id in protected for node_id in column)

    def _floor_ok(candidate: list[list[str]]) -> bool:
        """候选层列表是否满足所有源→终点跨度下限。"""
        if not floor_pairs:
            return True
        layer_of = {nid: i for i, col in enumerate(candidate) for nid in col}
        for (s, t), floor in floor_pairs.items():
            ls, lt = layer_of.get(s), layer_of.get(t)
            if ls is None or lt is None or lt - ls + 1 < floor:
                return False
        return True

    while len(merged) > target:
        # 选相邻层对 (i, i+1) 使合并后节点数最小；平局取索引小者；
        # 保护层不参与合并；合并不得违反跨度下限（若全部候选被保护/
        # 违规则停止合并）。
        best_index: int | None = None
        best_sum = 0
        for index in range(len(merged) - 1):
            if _is_protected(merged[index]) or _is_protected(merged[index + 1]):
                continue
            candidate = merged[:index] + [merged[index] + merged[index + 1]] + merged[index + 2:]
            if not _floor_ok(candidate):
                continue
            total = len(merged[index]) + len(merged[index + 1])
            if best_index is None or total < best_sum:
                best_index, best_sum = index, total
        if best_index is None:
            break
        merged[best_index] = merged[best_index] + merged[best_index + 1]
        del merged[best_index + 1]
    return merged


def _barycenter_order(
    layers: list[list[str]],
    layer_of: dict[str, int],
    edges: list[tuple[str, str]],
    iterations: int,
) -> list[list[str]]:
    """barycenter 层内排序：相邻层邻居位置均值，稳定排序保证确定性。

    同层边（方形化合并产生）不参与；无相邻邻居的节点稳定保持在层首。
    """
    adjacent: dict[str, list[str]] = {node_id: [] for node_id in layer_of}
    for source, target in edges:
        if source not in layer_of or target not in layer_of:
            continue
        if abs(layer_of[source] - layer_of[target]) != 1:
            continue  # 同层边（合并产生）不参与交叉最小化
        adjacent[source].append(target)
        adjacent[target].append(source)

    ordered = [list(column) for column in layers]
    for _ in range(max(0, iterations)):
        # 交替方向遍历层，加快收敛且保持对称性。
        for pass_index in range(2):
            for column_index in range(len(ordered)):
                if pass_index == 1:
                    column_index = len(ordered) - 1 - column_index
                column = ordered[column_index]
                neighbor_positions: dict[str, float] = {}
                for node_id in column:
                    neighbors = [
                        neighbor
                        for neighbor in adjacent[node_id]
                        if layer_of[neighbor] in (layer_of[node_id] - 1, layer_of[node_id] + 1)
                        and layer_of[neighbor] != layer_of[node_id]
                    ]
                    indexes: list[int] = []
                    for neighbor in neighbors:
                        if neighbor in ordered[column_index]:
                            continue  # 同层邻居不计
                        neighbor_layer = layer_of[neighbor]
                        if 0 <= neighbor_layer < len(ordered):
                            indexes.append(ordered[neighbor_layer].index(neighbor))
                    if indexes:
                        neighbor_positions[node_id] = mean(indexes)
                ordered[column_index] = sorted(
                    column,
                    key=lambda node_id: neighbor_positions.get(node_id, float("-inf")),
                )
    return ordered


def _split_oversized_columns(
    layers: list[list[str]],
    layer_of: dict[str, int],
    max_per_column: int,
    protected: set[str] | None = None,
) -> list[list[str]]:
    """把节点数超过上限的列切成多个子列（列高上限控制）。

    源节点就近下放 + 方形化合并可能让某些层聚集过多节点（如 logo 预设
    中央列 10 个），单列垂直堆叠撑高整体。按**层边界**贪心切段（每段
    ≤ 上限）：切点只落在层与层的交界处，保证跨子列的边方向保持
    （原层号递增 = x 递增）——若按节点数硬切，可能把某层的节点切开、
    其下游却留在原列，产生反向边（实测链 10 的 n4 被切到新列后指向
    左侧的 n5）。

    Args:
        protected: 含这些节点 id 的列**不拆分**（终点锚定分层中保护终点
            输入列，见 core.compound_layout）。None = 全部可拆。
    """
    if max_per_column <= 0:
        return [list(column) for column in layers]
    split: list[list[str]] = []
    for column in layers:
        if (protected and any(node_id in protected for node_id in column)) or len(column) <= max_per_column:
            split.append(list(column))
            continue
        segments: list[list[str]] = []
        for node_id in column:
            level = layer_of[node_id]
            if segments and layer_of[segments[-1][0]] == level:
                segments[-1].append(node_id)
            else:
                segments.append([node_id])
        # 按层号排序段（切分前的 barycenter 可能把低层节点移到列尾，
        # 不重排会切出「下游在左侧」的反向边）。
        segments.sort(key=lambda segment: layer_of[segment[0]])
        current: list[str] = []
        for segment in segments:
            # 单个层段也可能超过上限（同层聚集，如 6 输入同层）：段内按序切。
            while len(segment) > max_per_column:
                if current:
                    split.append(current)
                    current = []
                split.append(segment[:max_per_column])
                segment = segment[max_per_column:]
            if current and len(current) + len(segment) > max_per_column:
                split.append(current)
                current = []
            current.extend(segment)
        if current:
            split.append(current)
    return split


def _layout_component(
    comp: list[str],
    by_id: dict[str, GraphNode],
    edges: list[tuple[str, str]],
    h_gap: float,
    v_gap: float,
    max_h_gap: float,
    max_aspect_ratio: float,
    iterations: int,
) -> dict[str, tuple[float, float]]:
    comp_edges = [
        (source, target)
        for source, target in edges
        if source in comp and target in comp
    ]
    layers, layer_of = _longest_path_layers(comp, comp_edges)
    # 保存原始层号（合并前）：列切分的段边界必须落在原始层交界。
    original_layer_of = dict(layer_of)
    # 方形化：先按原始层估算宽高比，超限再合并（保持方向感优先）。
    if len(layers) > 1:
        layers = _squarify_layers(
            layers,
            len(comp),
            [(by_id[node_id].width, by_id[node_id].height) for node_id in comp],
            max_aspect_ratio=max_aspect_ratio,
        )
        layer_of = {node_id: column for column, ids in enumerate(layers) for node_id in ids}
    layers = _barycenter_order(layers, layer_of, comp_edges, iterations)
    # 列高上限：源下放/层合并可能聚集过多节点（logo 预设中央列 10 个），
    # 切成多列控制单列高度；上限与方形化目标列数联动（= ceil(N/T)），
    # 链类图（列数本已 = T）不拆、聚集层才拆；切出的同层边再经一轮
    # barycenter 对齐。
    if len(comp) > 1:
        avg_width = sum(by_id[node_id].width for node_id in comp) / len(comp) or 1.0
        avg_height = sum(by_id[node_id].height for node_id in comp) / len(comp) or 1.0
        target_cols = max(2, round(math.sqrt(len(comp) * avg_height / avg_width)))
        max_per_column = max(2, math.ceil(len(comp) / target_cols))
    else:
        max_per_column = len(comp)
    if len(layers) > 1 and any(len(column) > max_per_column for column in layers):
        layers = _split_oversized_columns(layers, original_layer_of, max_per_column)
        layer_of = {node_id: column for column, ids in enumerate(layers) for node_id in ids}
        layers = _barycenter_order(layers, layer_of, comp_edges, iterations)

    def _assign(columns: list[list[str]], gap_x: float) -> dict[str, tuple[float, float]]:
        col_widths = [
            max((by_id[node_id].width for node_id in column), default=0.0)
            for column in columns
        ]
        x_offsets: list[float] = []
        x = 0.0
        for width in col_widths:
            x_offsets.append(x)
            x += width + gap_x
        col_heights = [
            sum(by_id[node_id].height for node_id in column) + v_gap * max(0, len(column) - 1)
            for column in columns
        ]
        max_height = max(col_heights, default=0.0)
        result: dict[str, tuple[float, float]] = {}
        for column_index, column in enumerate(columns):
            y = (max_height - col_heights[column_index]) / 2.0
            for node_id in column:
                result[node_id] = (x_offsets[column_index], y)
                y += by_id[node_id].height + v_gap
        return result

    positions = _assign(layers, h_gap)
    width, height = _bbox(positions, by_id)
    # 高长条（h >> w）：在 [h_gap, max_h_gap] 内放大列间距逼近方形。
    if height > width and max_h_gap > h_gap and len(layers) > 1:
        col_widths_sum = sum(
            max((by_id[node_id].width for node_id in column), default=0.0)
            for column in layers
        )
        gap_x = min(max_h_gap, max(h_gap, (height - col_widths_sum) / (len(layers) - 1)))
        positions = _assign(layers, gap_x)
    return positions


def _bbox(
    positions: dict[str, tuple[float, float]],
    by_id: dict[str, GraphNode],
) -> tuple[float, float]:
    """含节点尺寸的整体包围盒 (width, height)；空 → (0, 0)。"""
    if not positions:
        return 0.0, 0.0
    max_x = max(x + by_id[node_id].width for node_id, (x, _y) in positions.items())
    max_y = max(y + by_id[node_id].height for node_id, (_x, y) in positions.items())
    return max_x, max_y
