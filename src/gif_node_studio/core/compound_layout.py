"""复合（分级）布局核心：嵌套背景板 + 终点锚定分层（纯计算，无 Qt）。

与 ``core/graph_layout.py`` 的关系：graph_layout 是平铺 Sugiyama-lite
（无分组概念）；本模块在其之上做「组=超节点」的递归复合布局——每个背景板
（组框）内部先独立布局，再作为超节点参与父级布局，逐级上提到根级。
组内/根级复用 graph_layout 的 barycenter / 方形化 / 列拆分（新增
``protected`` 参数避免拆散终点输入列）。

语义（用户需求，2026-08，见 docs/research/compound-layout-feasibility.md，
用户手排基准 logo_sand.json 的指标对比见 tests/test_compound_layout.py）：

1. **终点节点语义明确性优先**：以语义终点（EXPORT 类节点，如 ico合成）为锚
   做**反向最长路分层**——终点的全部直连输入落在同一层（同一列），列内
   次序按终点输入端口定义顺序（``sink_input_order``，终点语义 > 交叉最小化）。
2. **背景板 = 输出端口并集节点**：组提升为超节点时出边 = 成员出边并集；
   组内无显式终点时以「有组外出边的成员」为伪锚，组内流水线向右对齐到
   组的输出侧。
3. **孤立源节点贴近输出目标**（用户新增）：入度 0 的源节点不再堆在层 0，
   而是下放到 ``min(消费者层) - 1``（``_source_sink``）——节点的直连输入
   中若有孤立 node，贴近其输出目标以强调局部作用性；跨级边随之缩短。
4. **布局宽度以列数衡量，最长路径深度保底**（用户 2026-08 修订）：宽度
   最小宽度 = 从起点到对应终点**最长**路径所经过的节点数——任一层合并
   （方形化）不得把任一源→终点链压到短于其最长路径节点数
   （``_longest_path_floor`` 传入 ``_squarify_layers`` 的 floor_pairs），
   否则扇形扩散节点（同一节点输出到多种级别节点）的消费者会被压到与
   输入同列，破坏「节点恒位于所有输出目标左边」。
5. **方向性由构造保证**：任意边 u→v 恒有 layer(u) < layer(v)（严格递增，
   无同列边——扇入钳制已移除；无任何后置平移——#108 失败的根源是布局后
   组块平移破坏了拓扑方向）。
6. **无遮挡由逐级 gap 打包复合保证**：每级内部节点/超节点间距 ≥ gap，
   组块尺寸 = 内容 bbox + 内边距（含 ``nest_inset``：含嵌套子框的父框
   加大内边距，嵌套框四周可见父框底色）。
7. **保护层**：锚层与锚直连输入层不参与方形化合并与列拆分（否则终点
   输入列会被拆散，破坏「同一级别语义」）；保护以节点 id 集传递
   （层索引在合并后会漂移，不可靠）。
8. **几何容差（eps）**：成员/嵌套判定容忍节点实际渲染尺寸与存档尺寸的
   漂移（NodeGraphQt 重绘后视图尺寸变化，成员可能恰好越出框边几像素），
   默认 20px，防止成员被误判为游离节点后整体脱框。

调用方职责（UI 适配层，见实现方案）：成员快照（``backdrop.nodes()``
按位置判定，布局前必须记录）、语义终点/端口序提取（注册表 EXPORT_KIND
类 + 节点实例 ``input_ports()``）、布局后按返回的组框矩形重设背景板。

确定性：barycenter 固定轮数 + 稳定排序 → 同输入两次结果逐坐标一致。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .graph_layout import (
    GraphNode,
    _barycenter_order,
    _longest_path_layers,
    _split_oversized_columns,
    _squarify_layers,
)


@dataclass(frozen=True)
class GroupBox:
    """布局输入：一个背景板（组框）的稳定 id 与存档几何。

    存档位置/尺寸用于：① 几何包含判定成员关系（与 NodeGraphQt
    ``BackdropNode.nodes()`` 的 ``Qt.ContainsItemShape`` 同语义）；
    ② 派生内边距（框尺寸 − 成员 bbox），使重设后的框贴合应用的包裹风格。
    """

    id: str
    x: float
    y: float
    width: float
    height: float


def layout_compound(
    nodes: list[GraphNode],
    edges: list[tuple[str, str]],
    groups: list[GroupBox],
    saved_positions: dict[str, tuple[float, float]],
    *,
    anchors: set[str] | None = None,
    sink_input_order: dict[str, list[str]] | None = None,
    eps: float = 20.0,
    h_gap: float = 80.0,
    v_gap: float = 24.0,
    max_h_gap: float = 160.0,
    nest_inset: float = 100.0,
    iterations: int = 12,
    max_aspect_ratio: float = 2.2,
) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float, float, float]]]:
    """复合分级布局：返回 ``(节点坐标, 组框矩形)``（左上角坐标）。

    Args:
        nodes: 全部普通节点（非背景板；尺寸参与布局）。
        edges: 有向边 ``(source_id, target_id)``；端点不在 nodes/groups 内的
            边忽略。
        groups: 背景板列表（含存档几何，见 GroupBox）。
        saved_positions: 普通节点的**存档位置** ``{id: (x, y)}``（成员判定与
            内边距派生用；布局前由调用方从画布快照）。
        anchors: 语义终点节点 id 集合（如 EXPORT 类节点）。缺省时每级退化为
            「有组外出边的 item」（组的输出端口）；某级仍无锚（无终点的
            锚定目标）时退回源侧最长路分层（与平铺布局同）。
        sink_input_order: ``{终点_id: [其直连输入 id，按输入端口定义序]}``，
            用于终点输入列内排序（终点语义优先级最高，覆盖 barycenter）。
        nest_inset: 含嵌套子框的父框额外内边距（左/右 ≥ nest_inset，
            上/下 ≥ 0.6·nest_inset；嵌套框四周可见父框底色）。
        eps: 成员/嵌套判定的几何容差（像素）。节点实际渲染尺寸可能与
            存档尺寸有漂移（NodeGraphQt 重绘后视图尺寸变化），导致成员
            恰好越出框边几像素而被误判为游离节点、布局后整体脱框——
            以 eps 容忍越界（默认 20px，足够覆盖渲染漂移，且远小于
            正常内边距 30px+，不会误收真正游离的节点）。
        max_aspect_ratio: 组内方形化阈值；根级固定 0（不合并，保持带结构）。

    Returns:
        ``(positions, group_rects)``：``{节点id: (x, y)}`` 与
        ``{组框id: (x, y, w, h)}``（绝对坐标，已平移到非负）。
    """
    return _CompoundLayout(
        nodes, edges, groups, saved_positions,
        anchors=anchors,
        sink_input_order=sink_input_order,
        eps=eps,
        h_gap=h_gap,
        v_gap=v_gap,
        max_h_gap=max_h_gap,
        nest_inset=nest_inset,
        iterations=iterations,
        max_aspect_ratio=max_aspect_ratio,
    ).run()


# ---------------------------------------------------------------------------
# 几何工具
# ---------------------------------------------------------------------------


def _rect(x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
    return (x, y, x + w, y + h)


def _contains(big: tuple[float, float, float, float], small: tuple[float, float, float, float]) -> bool:
    return big[0] <= small[0] and big[1] <= small[1] and small[2] <= big[2] and small[3] <= big[3]


def _contains_eps(
    big: tuple[float, float, float, float],
    small: tuple[float, float, float, float],
    eps: float,
) -> bool:
    """容差包含：小矩形可越出大矩形边界至多 eps（几何漂移容忍）。"""
    return (
        big[0] - eps <= small[0]
        and big[1] - eps <= small[1]
        and small[2] <= big[2] + eps
        and small[3] <= big[3] + eps
    )


def _group_rect(g: GroupBox) -> tuple[float, float, float, float]:
    return _rect(g.x, g.y, g.width, g.height)


# ---------------------------------------------------------------------------
# 终点锚定分层
# ---------------------------------------------------------------------------


def _sink_anchored_layers(
    comp_ids: list[str],
    edges: list[tuple[str, str]],
    anchors: set[str],
) -> tuple[list[list[str]], dict[str, int]]:
    """终点锚定分层：层 = D − 到锚的最长距离。

    - 锚（语义终点）在最右列 D；锚的直连输入按其**自然最长路深度**分层
      （菱形结构 A→B→锚 且 A→锚：A 深度 2 在 B 左侧一列，B 深度 1 在
      扇入列）——**不做扇入钳制**：钳制会把菱形消费者拉到与输入同列，
      违反「节点恒位于所有输出目标左边」（用户 2026-08 修订，见
      logo_sand.json 手排基准：A通道合并 2 恒在 分辨率统一 18/12/13/14
      左侧）；锚的下游侧输出（如 ico分辨率查看）在核心之后一列；
    - 不达锚的节点（中游分叉叶子，如 gif调色板查看 ← 颜色量化）从
      「最近核心祖先」向后排，方向保持；
    - 无锚/空边时退化为单层（调用方在无锚时改用源侧最长路，见
      ``_CompoundLayout._layout_level``）。
    """
    ids = set(comp_ids)
    succ: dict[str, list[str]] = {n: [] for n in comp_ids}
    pred: dict[str, list[str]] = {n: [] for n in comp_ids}
    for u, v in edges:
        if u in ids and v in ids:
            succ[u].append(v)
            pred[v].append(u)

    # 反向图拓扑序（原图下游先）：反向图边 v→u（原边 u→v），入度 = 原图出度。
    indeg_rev = {n: len(succ[n]) for n in comp_ids}
    dq = deque(n for n in comp_ids if indeg_rev[n] == 0)
    order: list[str] = []
    while dq:
        cur = dq.popleft()
        order.append(cur)
        for up in pred[cur]:
            indeg_rev[up] -= 1
            if indeg_rev[up] == 0:
                dq.append(up)

    # 反向最长路：dist(up) = max(dist(up), dist(cur) + 1)；锚 dist=0。
    # 任意边 u→v 恒有 dist(u) ≥ dist(v)+1 → 层严格递增（无同列边）。
    dist: dict[str, int] = {n: 0 for n in anchors if n in ids}
    for cur in order:
        dc = dist.get(cur)
        if dc is None:
            continue
        for up in pred[cur]:
            dist[up] = max(dist.get(up, -1), dc + 1)

    # 非核心节点（不达锚）：从核心边界向后 —— fwd[核心] = 其层号，
    # 下游分叉叶子 = 最近核心祖先的层号 + 1（方向保持）。
    D = max(dist.values(), default=0)
    fwd: dict[str, int] = {n: D - dist[n] for n in dist}
    indeg2 = {n: len(pred[n]) for n in comp_ids}
    dq = deque(n for n in comp_ids if indeg2[n] == 0)
    while dq:
        cur = dq.popleft()
        fc = fwd.get(cur)
        for dn in succ[cur]:
            # 核心节点用 dist 层；indeg2 递减不可跳过（否则锚的 fwd 传不到
            # 下游叶子，全部落层 0 —— 实测 bug）。
            if dn not in dist and fc is not None:
                fwd[dn] = max(fwd.get(dn, -1), fc + 1)
            indeg2[dn] -= 1
            if indeg2[dn] == 0:
                dq.append(dn)

    layer: dict[str, int] = {}
    for n in comp_ids:
        if n in dist:
            layer[n] = D - dist[n]
        elif n in fwd:
            layer[n] = fwd[n]
        else:
            layer[n] = 0
    count = max(layer.values(), default=0) + 1
    layers: list[list[str]] = [[] for _ in range(count)]
    # 层内初始顺序 = 输入顺序（comp_ids）而非 sorted(id)：反序列化每次生成
    # 随机节点 id，按 id 排序会导致跨运行列内顺序翻转（barycenter 稳定排序
    # 保持初始顺序）——实测 UI 路径两次运行 y 序不一致。
    for n in comp_ids:
        layers[layer[n]].append(n)
    layers = [col for col in layers if col]
    layer_of = {n: i for i, col in enumerate(layers) for n in col}
    return layers, layer_of


# ---------------------------------------------------------------------------
# 源节点下放 + 最长路径列数下限
# ---------------------------------------------------------------------------


def _source_sink(
    layers: list[list[str]],
    layer_of: dict[str, int],
    edges: list[tuple[str, str]],
) -> tuple[list[list[str]], dict[str, int]]:
    """孤立源节点下放：入度 0 的节点下沉到 ``min(消费者层) - 1``。

    用户语义（2026-08，logo_sand.json 手排基准）：节点的直连输入中若有
    孤立 node（入度 0 的源），应贴近其输出目标，强调局部作用性——源节点
    不再全部堆在层 0，而是紧贴消费它的节点前一列（方向恒保持：目标层
    < 消费者层）。源节点无入边，移动不影响其他节点层号，单遍即可；
    空层移除后层号重排。
    """
    ids = {n for col in layers for n in col}
    pred: dict[str, list[str]] = {n: [] for n in ids}
    succ: dict[str, list[str]] = {n: [] for n in ids}
    for u, v in edges:
        if u in ids and v in ids:
            pred[v].append(u)
            succ[u].append(v)
    layers = [list(c) for c in layers]
    # 迭代顺序 = 层顺序快照（确定性；不按 id 排序——反序列化生成的随机 id
    # 会破坏跨运行确定性；且避免迭代中修改 layers 列表）。
    order = [u for col in layers for u in col]
    for u in order:
        if pred[u] or not succ[u]:
            continue
        cur = layer_of[u]
        target = min(layer_of[v] for v in succ[u]) - 1
        if target <= cur:
            continue
        layers[cur].remove(u)
        layers[target].append(u)
        layer_of[u] = target
    layers = [c for c in layers if c]
    layer_of = {n: i for i, c in enumerate(layers) for n in c}
    return layers, layer_of


def _longest_path_floor(
    ids: list[str],
    edges: list[tuple[str, str]],
    sources: set[str],
    terminals: set[str],
) -> dict[tuple[str, str], int]:
    """源→终点**最长**路径节点数（列数下限），用于方形化合并守卫。

    ``{(源id, 终点id): 最长路径节点数}``；对每个源做可达子图拓扑 DP。
    用户语义（2026-08 修订）：布局宽度的最小宽度（列数）= 从起点到对应
    终点**最长**路径所经过的节点数——同一节点输出到多种级别节点（扇形
    扩散）时，所有输出目标必须保持在它右侧：最长路径层已保证任意边
    层严格递增，层合并不得把任一源→终点链压到短于其最长路径深度
    （否则消费者可能被压到与输入同列，破坏「节点恒位于输出目标左边」）。
    """
    id_set = set(ids)
    succ: dict[str, list[str]] = {n: [] for n in ids}
    indeg: dict[str, int] = {n: 0 for n in ids}
    for u, v in edges:
        if u in id_set and v in id_set:
            succ[u].append(v)
            indeg[v] += 1
    # 全局拓扑序（DAG；环忽略，布局不崩）。
    indeg2 = dict(indeg)
    dq = deque(n for n in ids if indeg2[n] == 0)
    topo: list[str] = []
    while dq:
        cur = dq.popleft()
        topo.append(cur)
        for w in succ[cur]:
            indeg2[w] -= 1
            if indeg2[w] == 0:
                dq.append(w)
    floor: dict[tuple[str, str], int] = {}
    for s in sources:
        if s not in id_set:
            continue
        # 拓扑 DP：dist_to[w] = 从 s 到 w 的最长路径节点数（可达子图内）。
        dist_to: dict[str, int] = {s: 1}
        for v in topo:
            if v not in dist_to:
                continue
            dv = dist_to[v]
            for w in succ[v]:
                dist_to[w] = max(dist_to.get(w, 0), dv + 1)
        for t in terminals:
            if t in dist_to and t != s:
                floor[(s, t)] = dist_to[t]
    return floor


# ---------------------------------------------------------------------------
# 复合布局状态机
# ---------------------------------------------------------------------------


class _CompoundLayout:
    """递归复合布局的有状态实现（组块缓存 + 组树 + 逐级展开）。"""

    def __init__(
        self,
        nodes: list[GraphNode],
        edges: list[tuple[str, str]],
        groups: list[GroupBox],
        saved_positions: dict[str, tuple[float, float]],
        *,
        anchors: set[str] | None,
        sink_input_order: dict[str, list[str]] | None,
        eps: float,
        h_gap: float,
        v_gap: float,
        max_h_gap: float,
        nest_inset: float,
        iterations: int,
        max_aspect_ratio: float,
    ) -> None:
        self.by_id = {n.id: n for n in nodes}
        if len(self.by_id) != len(nodes):
            raise ValueError("layout_compound: 节点 id 重复")
        self.saved_positions = dict(saved_positions)
        self.groups = {g.id: g for g in groups}
        if len(self.groups) != len(groups):
            raise ValueError("layout_compound: 组框 id 重复")
        self.anchors = set(anchors or ())
        self.sink_input_order = sink_input_order or {}
        self.eps = eps
        self.h_gap = h_gap
        self.v_gap = v_gap
        self.max_h_gap = max_h_gap
        self.nest_inset = nest_inset
        self.iterations = iterations
        self.max_aspect_ratio = max_aspect_ratio

        # 端点不在 nodes/groups 内的边忽略；去重。
        known = set(self.by_id) | set(self.groups)
        self.edges = sorted({(u, v) for u, v in edges if u in known and v in known})

        self.root, self.tree = _decompose(self.by_id, self.groups, self.saved_positions, self.eps)
        self.group_has_anchor = self._compute_group_anchors()
        # 组块缓存：{gid: {members, item_pos, pos(局部), w, h, parent_local}}
        self.blocks: dict[str, dict] = {}
        self.final: dict[str, tuple[float, float]] = {}
        self.backdrop_rects: dict[str, tuple[float, float, float, float]] = {}

    # -- 组树 --------------------------------------------------------------

    def _compute_group_anchors(self) -> dict[str, bool]:
        """组是否（传递地）含有语义终点：供父级把该组当锚。"""
        has = {}
        for gid in self.groups:
            stack = list(self.tree[gid]["exclusive"])
            found = False
            while stack:
                m = stack.pop()
                if m in self.anchors:
                    found = True
                    break
            has[gid] = found
        for gid in sorted(self.groups, key=lambda g: self._depth(g)):
            if not has[gid]:
                for nb in self.tree[gid]["nested"]:
                    if has[nb]:
                        has[gid] = True
                        break
        return has

    def _depth(self, gid: str) -> int:
        # 深度缓存（由 _decompose 写入 tree）
        return self.tree[gid]["depth"]

    # -- 每级布局 ----------------------------------------------------------

    def _level_items(self, g: dict) -> list[tuple[str, float, float]]:
        items = [(nid, self.by_id[nid].width, self.by_id[nid].height) for nid in g["exclusive"]]
        for bid in g["nested"]:
            items.append((bid, self.blocks[bid]["w"], self.blocks[bid]["h"]))
        return items

    def _owner_map(self, g: dict) -> dict[str, str]:
        owner: dict[str, str] = {}
        for nid in g["exclusive"]:
            owner[nid] = nid
        for bid in g["nested"]:
            for nid in self.blocks[bid]["members"]:
                owner[nid] = bid
        return owner

    def _external_out(self, owner: dict[str, str]) -> set[str]:
        """本层 item 是否有出边连到组外（= 组的输出端口成员）。"""
        outs: set[str] = set()
        for u, v in self.edges:
            if owner.get(u) is not None and owner.get(v) is None:
                outs.add(owner[u])
        return outs

    def _layout_level(
        self,
        g: dict,
        out_edges: list[tuple[str, str]],
        max_aspect: float,
    ) -> dict[str, tuple[float, float]]:
        items = self._level_items(g)
        item_ids = [i[0] for i in items]
        sizes = {i[0]: (i[1], i[2]) for i in items}
        owner = self._owner_map(g)
        local_edges = [
            (owner[u], owner[v])
            for u, v in out_edges
            if owner.get(u) is not None and owner.get(v) is not None and owner[u] != owner[v]
        ]

        # 锚定集：本层 EXPORT 成员 / 嵌套含锚组；无则退化为「有组外出边的
        # item」（= 组的输出端口）；仍无锚 → 源侧最长路（平铺同款）。
        anchors = [i for i in item_ids if i in self.anchors]
        anchors += [nb for nb in g["nested"] if self.group_has_anchor[nb]]
        if not anchors:
            anchors = sorted(self._external_out(owner))

        if anchors:
            layers, layer_of = _sink_anchored_layers(item_ids, local_edges, set(anchors))
        else:
            layers, layer_of = _longest_path_layers(item_ids, local_edges)
        original_layer_of = dict(layer_of)

        # 保护层（节点 id 集）：锚自身层 + 锚的直连输入层 —— 不合并、不拆分。
        protected: set[str] = set()
        for a in anchors:
            if a in layer_of:
                protected.add(a)
                for u, v in local_edges:
                    if v == a and u in layer_of:
                        protected.add(u)

        # 保护层补充：嵌套超节点与其邻居所在层不合并 —— 同列内跨层级边
        # 的成员带框内偏移（source 可能在 target 右侧，方向反转，实测
        # fixture a(框内)→b(框外) 130>100）。跨列边安全（成员恒在框内、
        # 框右缘 < 下一列左缘），仅同列合并需防。
        super_items = set(g["nested"])
        for u, v in local_edges:
            if (u in super_items or v in super_items) and u in layer_of and v in layer_of and layer_of[u] != layer_of[v]:
                protected.add(u)
                protected.add(v)

        # 列数下限（用户语义：宽度最小宽度 = 源→终点**最长**路径节点数）：
        # 层合并不得压垮任一源→终点链（否则扇形扩散节点的消费者可能被
        # 压到与输入同列，违反「节点恒位于所有输出目标左边」）。源 = 本层
        # 入度 0 的 item；终点 = 锚 + 本层出度 0 的 item。
        pred_local: dict[str, list[str]] = {i: [] for i in item_ids}
        succ_local: dict[str, list[str]] = {i: [] for i in item_ids}
        for u, v in local_edges:
            pred_local[v].append(u)
            succ_local[u].append(v)
        sources = {i for i in item_ids if not pred_local[i]}
        terminals = set(anchors) | {i for i in item_ids if not succ_local[i]}
        floor_pairs = _longest_path_floor(item_ids, local_edges, sources, terminals)

        # 方形化（保护层不参与合并；跨度下限不破）
        if len(layers) > 1:
            layers = _squarify_layers(
                layers,
                len(item_ids),
                [(i[1], i[2]) for i in items],
                max_aspect_ratio=max_aspect,
                protected=protected,
                floor_pairs=floor_pairs,
            )
            layer_of = {n: i for i, col in enumerate(layers) for n in col}
        layers = _barycenter_order(layers, layer_of, local_edges, self.iterations)
        layers = _apply_port_order(layers, layer_of, local_edges, anchors, self.sink_input_order)

        # 列拆分（保护层不拆）
        if len(item_ids) > 1:
            avg_w = sum(i[1] for i in items) / len(items) or 1.0
            avg_h = sum(i[2] for i in items) / len(items) or 1.0
            target_cols = max(2, round(math.sqrt(len(item_ids) * avg_h / avg_w)))
            max_per_column = max(2, math.ceil(len(item_ids) / target_cols))
        else:
            max_per_column = len(item_ids)
        if len(layers) > 1 and any(len(col) > max_per_column for col in layers):
            layers = _split_oversized_columns(layers, original_layer_of, max_per_column, protected=protected)
            layer_of = {n: i for i, col in enumerate(layers) for n in col}
            layers = _barycenter_order(layers, layer_of, local_edges, self.iterations)
            layers = _apply_port_order(layers, layer_of, local_edges, anchors, self.sink_input_order)

        # 孤立源节点下放（用户语义：源贴近其输出目标，强调局部作用性）：
        # 入度 0 的 item 下沉到 min(消费者层)-1；再重跑一次层内排序与端口序。
        layers, layer_of = _source_sink(layers, layer_of, local_edges)
        layers = _barycenter_order(layers, layer_of, local_edges, self.iterations)
        layers = _apply_port_order(layers, layer_of, local_edges, anchors, self.sink_input_order)

        return _assign_columns(layers, sizes, self.h_gap, self.v_gap, self.max_h_gap)

    # -- 主流程 ------------------------------------------------------------

    def run(self) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float, float, float]]]:
        # 1) 背景框由深到浅（最内层先布局）
        for gid in sorted(self.groups, key=lambda g: -self._depth(g)):
            self._layout_group(gid)

        # 2) 根级：游离节点 + 顶层背景框
        root_pos = self._layout_level(self.root, self.edges, max_aspect=0.0)
        for nid in self.root["exclusive"]:
            self.final[nid] = root_pos[nid]
        for bid in self.root["nested"]:
            bx, by = root_pos[bid]
            for m in self.blocks[bid]["members"]:
                mx, my = self.blocks[bid]["pos"][m]
                self.final[m] = (bx + mx, by + my)
            self.backdrop_rects[bid] = (bx, by, self.blocks[bid]["w"], self.blocks[bid]["h"])

        # 3) 嵌套框绝对坐标回填：按深度由浅到深（父框坐标先就绪）
        parent_of = {nb: par for par, g in self.tree.items() for nb in g["nested"]}
        for gid in sorted(self.groups, key=lambda g: self._depth(g)):
            if gid in self.backdrop_rects:
                continue
            par = parent_of.get(gid)
            if par is not None and par in self.backdrop_rects and "parent_local" in self.blocks[gid]:
                px, py, _, _ = self.backdrop_rects[par]
                ix, iy = self.blocks[gid]["parent_local"]
                self.backdrop_rects[gid] = (px + ix, py + iy, self.blocks[gid]["w"], self.blocks[gid]["h"])

        # 4) 平移到非负（节点与组框一起，避免框左缘被裁）
        all_x = [x for x, _ in self.final.values()] + [r[0] for r in self.backdrop_rects.values()]
        all_y = [y for _, y in self.final.values()] + [r[1] for r in self.backdrop_rects.values()]
        minx, miny = min(all_x), min(all_y)
        final = {nid: (x - minx, y - miny) for nid, (x, y) in self.final.items()}
        rects = {gid: (x - minx, y - miny, w, h) for gid, (x, y, w, h) in self.backdrop_rects.items()}
        return final, rects

    def _layout_group(self, gid: str) -> None:
        g = self.tree[gid]
        item_pos = self._layout_level(g, self.edges, max_aspect=self.max_aspect_ratio)

        # 展开：成员局部坐标 = item 位置 + 组内相对位置；记录嵌套子框的
        # 父框局部位置（含父框内边距 + 内容原点偏移）。
        member_pos: dict[str, tuple[float, float]] = {}
        for nid in g["exclusive"]:
            member_pos[nid] = item_pos[nid]
        for nb in g["nested"]:
            bx, by = item_pos[nb]
            inner = self.blocks[nb]
            for m in inner["members"]:
                mx, my = inner["pos"][m]
                member_pos[m] = (bx + mx, by + my)

        # 组块尺寸 = 内容 bbox + 内边距（存档框 vs 存档成员 bbox 派生）
        xs = [member_pos[m][0] for m in member_pos] + [member_pos[m][0] + self.by_id[m].width for m in member_pos]
        ys = [member_pos[m][1] for m in member_pos] + [member_pos[m][1] + self.by_id[m].height for m in member_pos]
        if not xs:
            xs, ys = [0, 0], [0, 26]
        bw, bh = max(xs) - min(xs), max(ys) - min(ys)
        saved = _group_rect(self.groups[gid])
        saved_bbox = self._saved_member_bbox(gid, list(member_pos))
        pad_l = max(saved[0] - saved_bbox[0], 30)
        pad_t = max(saved[1] - saved_bbox[1], 26)
        pad_r = max(saved[2] - saved_bbox[2], 30)
        pad_b = max(saved[3] - saved_bbox[3], 30)
        # nest_inset：含嵌套子框时加大内边距，使嵌套框四周可见父框底色。
        if g["nested"]:
            pad_l = max(pad_l, self.nest_inset)
            pad_r = max(pad_r, self.nest_inset)
            pad_t = max(pad_t, round(self.nest_inset * 0.6))
            pad_b = max(pad_b, round(self.nest_inset * 0.6))

        shifted = {}
        for m, (mx, my) in member_pos.items():
            shifted[m] = (mx - min(xs) + pad_l, my - min(ys) + pad_t)
        for nb in g["nested"]:
            nx_, ny_ = item_pos[nb]
            self.blocks[nb]["parent_local"] = (nx_ - min(xs) + pad_l, ny_ - min(ys) + pad_t)

        self.blocks[gid] = {
            "members": list(member_pos),
            "item_pos": item_pos,
            "pos": shifted,
            "w": bw + pad_l + pad_r,
            "h": bh + pad_t + pad_b,
        }
        for m, (mx, my) in shifted.items():
            self.final[m] = (mx, my)

    def _saved_member_bbox(self, gid: str, members: list[str]) -> tuple[float, float, float, float]:
        """存档几何下的成员包围盒（内边距派生用）。"""
        pts = []
        for m in members:
            x, y = self.saved_positions[m]
            w, h = self.by_id[m].width, self.by_id[m].height
            pts.append((x, y, x + w, y + h))
        return (
            min(p[0] for p in pts),
            min(p[1] for p in pts),
            max(p[2] for p in pts),
            max(p[3] for p in pts),
        )


# ---------------------------------------------------------------------------
# 分解
# ---------------------------------------------------------------------------


def _decompose(
    by_id: dict[str, GraphNode],
    groups: dict[str, GroupBox],
    saved_positions: dict[str, tuple[float, float]],
    eps: float = 0.0,
) -> tuple[dict, dict[str, dict]]:
    """几何分解：节点归最深包含框；组框按包含关系成树。

    返回 ``(root, tree)``；root/tree 均为 ``{"exclusive": [...], "nested": [...]}``
    （tree 键为组框 id，另含 "depth"）。

    eps: 包含判定的几何容差（见 layout_compound）；漂移导致成员恰好越出
    框边几像素时仍归属原框，避免被误判为游离节点。
    """
    depth: dict[str, int] = {}
    for gid, g in groups.items():
        r = _group_rect(g)
        depth[gid] = sum(
            1 for g2 in groups.values() if g2.id != gid and _contains_eps(_group_rect(g2), r, eps)
        )
    tree: dict[str, dict] = {gid: {"exclusive": [], "nested": [], "depth": depth[gid]} for gid in groups}
    root: dict = {"exclusive": [], "nested": [], "depth": -1}

    for gid in groups:
        par = None
        for g2 in groups.values():
            if g2.id != gid and _contains_eps(_group_rect(g2), _group_rect(groups[gid]), eps) and (
                par is None or depth[g2.id] > depth[par]
            ):
                par = g2.id
        (tree[par]["nested"] if par else root["nested"]).append(gid)

    for nid, node in by_id.items():
        if nid not in saved_positions:
            root["exclusive"].append(nid)  # 无存档位置：按游离处理，仍参与布局
            continue
        x, y = saved_positions[nid]
        r = _rect(x, y, node.width, node.height)
        best, bestd = None, -1
        for gid, g in groups.items():
            if _contains_eps(_group_rect(g), r, eps) and depth[gid] > bestd:
                best, bestd = gid, depth[gid]
        (tree[best]["exclusive"] if best else root["exclusive"]).append(nid)
    return root, tree


# ---------------------------------------------------------------------------
# 层内排序与坐标分配
# ---------------------------------------------------------------------------


def _apply_port_order(
    layers: list[list[str]],
    layer_of: dict[str, int],
    edges: list[tuple[str, str]],
    anchors: list[str],
    sink_input_order: dict[str, list[str]],
) -> list[list[str]]:
    """锚的直连输入列按终点端口定义序重排（优先级最高，稳定排其余节点）。

    在 barycenter 之后调用：终点输入列内次序 = 输入端口定义顺序（用户
    语义：终点节点明确性 > 交叉最小化）；非输入节点保持原相对次序。
    """
    if not anchors or not sink_input_order:
        return layers
    ordered = [list(c) for c in layers]
    for a in anchors:
        order = sink_input_order.get(a)
        if not order:
            continue
        rank = {nid: i for i, nid in enumerate(order)}
        a_layer = layer_of.get(a)
        if a_layer is None or a_layer <= 0:
            continue
        col = ordered[a_layer - 1]
        # 稳定排序：端口序内的节点按 rank 排，其余保持原相对次序；
        # 同 rank 平局由稳定排序保持输入顺序（不引入 id 决胜，避免
        # 反序列化随机 id 破坏跨运行确定性）。
        keyed = sorted(col, key=lambda n: rank.get(n, 10**9))
        ordered[a_layer - 1] = keyed
    return ordered


def _assign_columns(
    columns: list[list[str]],
    sizes: dict[str, tuple[float, float]],
    h_gap: float,
    v_gap: float,
    max_h_gap: float,
) -> dict[str, tuple[float, float]]:
    """坐标分配：列宽 = 该列最宽项，列内按高度 + v_gap 堆叠、整列垂直居中；
    高长条时在 [h_gap, max_h_gap] 内放大列间距逼近方形（只加宽一次）。"""
    result, width, height = _pack(columns, sizes, h_gap, v_gap)
    if height > width and max_h_gap > h_gap and len(columns) > 1:
        col_w = sum(max(sizes[i][0] for i in col) for col in columns)
        gap = min(max_h_gap, max(h_gap, (height - col_w) / (len(columns) - 1)))
        if gap != h_gap:
            result, _, _ = _pack(columns, sizes, gap, v_gap)
    return result


def _pack(
    columns: list[list[str]],
    sizes: dict[str, tuple[float, float]],
    gap_x: float,
    v_gap: float,
) -> tuple[dict[str, tuple[float, float]], float, float]:
    col_widths = [max(sizes[i][0] for i in col) for col in columns]
    x_offsets: list[float] = []
    x = 0.0
    for width in col_widths:
        x_offsets.append(x)
        x += width + gap_x
    col_heights = [sum(sizes[i][1] for i in col) + v_gap * max(0, len(col) - 1) for col in columns]
    max_height = max(col_heights, default=0.0)
    result: dict[str, tuple[float, float]] = {}
    for ci, col in enumerate(columns):
        y = (max_height - col_heights[ci]) / 2.0
        for i in col:
            result[i] = (x_offsets[ci], y)
            y += sizes[i][1] + v_gap
    width = x_offsets[-1] + col_widths[-1] if columns else 0.0
    return result, width, max_height
