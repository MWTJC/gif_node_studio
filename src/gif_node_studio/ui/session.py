"""节点方案存档：保存剥离纯 UI 字段 + 旧存档兼容清洗（决策 #59 / #80）。

- ``save_session_clean`` —— 保存时剥离 color/border_color/text_color；
- ``sanitize_session_data`` —— 加载前按当前节点定义清洗 custom（丢弃失效
  参数键、choice 回退默认值、未知类型记录），返回 ``SessionLoadReport``；
- 节点类型迁移 —— 已删除节点的旧存档 type_ 自动改写为新节点并翻译参数
  （决策 #134 色相调整 → 色相/饱和度，见 ``_DEPRECATED_NODE_MIGRATIONS``）；
- 端口改名兼容 —— 加载前把已改名输入端口的旧连线名改写到新名
  （NodeGraphQt 按端口名反序列化连线，见 ``_PORT_RENAME_MAP``）。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field

from loguru import logger

from ..nodes.definitions import ChoiceParam, NodeDefinition

# ---------------------------------------------------------------------------
# 节点方案存档：剥离纯 UI 字段
# ---------------------------------------------------------------------------
# NodeGraphQt 的 save_session 会写入每个节点的 color/border_color/text_color
# ——这些只影响画布外观（节点颜色、文字颜色），与项目
# 功能（节点类型、参数、连线、位置）无关，还会随主题/布局变化产生无意义 diff。
# 保存时统一剥离；加载时 NodeGraphQt._deserialize 对缺失属性使用模型默认值
# （节点尺寸由内容重新计算），因此旧存档（含这些字段）与新存档都可正常读取。
SESSION_UI_ONLY_KEYS = ("color", "border_color", "text_color", )


def save_session_clean(graph, path) -> None:
    """保存节点方案，但剥离与项目功能无关的纯 UI 字段（节点/边框/文本颜色、宽高）。

    - 序列化仍走 ``graph.serialize_session()``（节点参数、连线、位置、图样式完整保留）；
    - 逐节点移除 ``SESSION_UI_ONLY_KEYS`` 后按 NodeGraphQt 相同的 JSON 风格落盘
      （ensure_ascii=True，文件纯 ASCII，任何区域设置下都可被 load_session 读取）。
    """
    data = graph.serialize_session()
    for node_data in data.get("nodes", {}).values():
        for key in SESSION_UI_ONLY_KEYS:
            node_data.pop(key, None)
    with open(path, "w", encoding="utf-8") as file_out:
        json.dump(data, file_out, indent=2, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 旧存档兼容读取：按当前节点定义清洗会话数据（决策 #80）
# ---------------------------------------------------------------------------
# NodeGraphQt 的 _deserialize 对每个节点 custom 字典里的每个键调用
# model.set_property，未知键直接抛 NodePropertyError——旧存档因节点参数
# 变更（如 #77 移除 GIF 合成的 optimize_layers/optimize_transparency）必然
# 命中，导致整个方案无法读取且界面上无任何提示。方案 = 反序列化前按
# 节点 type_ 清洗 custom：丢弃当前节点不存在的键、choice 取值回退默认值；
# 缺失的参数键无需处理（NodeGraphQt 对缺失属性保持模型默认值，加载后的
# 模型→面板同步也按当前参数定义重建）。


@dataclass
class SessionLoadReport:
    """旧存档兼容读取的调整报告（供加载/导入后的用户提示）。"""

    dropped_params: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)
    # (节点标题, type_, 被丢弃的参数键) —— 节点里已不存在的参数
    unknown_types: list[tuple[str, str]] = field(default_factory=list)
    # (type_, 节点标题) —— 当前未注册的节点类型（NodeGraphQt 静默跳过该节点）
    reset_choices: list[tuple[str, str, str]] = field(default_factory=list)
    # (节点标题, 参数名, 旧值) —— choice 取值已不在当前选项内，回退到默认值

    def has_adjustments(self) -> bool:
        return bool(self.dropped_params or self.unknown_types or self.reset_choices)


# type_ → (该节点类型当前接受的自定义属性名集合, 参数定义或 None)。按
# type_ 记忆化：一个进程内节点注册是静态的，同一类型只需实例化一次。
_NODE_COMPAT_CACHE: dict[str, tuple[frozenset[str], NodeDefinition | None]] = {}


# 端口改名兼容映射（type_ → {旧端口名: 新端口名}）。端口命名新规：多输入节点
# 第一端口 = 待处理/主序列（不显示端口名，内部名字不变 → 旧连线天然兼容），
# 其余端口按作用改简短表意名（分辨率源/长度源/追加物/叠加物）。NodeGraphQt
# 反序列化按端口名匹配连线（``graph._deserialize`` 的 ``inputs().get(pname)``，
# 未命中静默丢线）——加载前把旧存档里的旧名改写到新名。
_PORT_RENAME_MAP: dict[str, dict[str, str]] = {
    "anime_gif.ResolutionAlignNode": {"序列A": "分辨率源"},
    "anime_gif.LengthAlignNode": {"序列A": "长度源"},
    "anime_gif.SequenceAddNode": {"序列B": "追加物"},
    "anime_gif.SequenceOverlayNode": {"序列B": "叠加物"},
}

# 节点类型迁移映射（type_ → (新 type_, custom 参数改名表)）：删除的节点类型在
# 加载时自动改写为新节点，避免旧存档节点被静默丢弃（决策 #134：色相调整 →
# 色相/饱和度——全图语义等价，旧参数 amount → hue_delta，其余参数用新节点
# 默认值：色域=全图、饱和/明度=0）。
_DEPRECATED_NODE_MIGRATIONS: dict[str, tuple[str, dict[str, str]]] = {
    "anime_gif.HueNode": ("anime_gif.HueSaturationNode", {"amount": "hue_delta"}),
}


def _rename_params(container: dict, renames: dict[str, str]) -> None:
    """就地改写参数名（旧名 → 新名）；旧名不存在则不动。"""
    for old_name, new_name in renames.items():
        if old_name in container:
            container[new_name] = container.pop(old_name)


def _migrate_deprecated_node_types(data: dict) -> None:
    """把已删除节点的旧存档 type_ 改写为新节点类型（决策 #134）。

    在 ``sanitize_session_data`` 的逐节点清洗**之前**执行：改写后旧存档按
    新节点的参数定义清洗（choice 回退/未知键丢弃/面板同步），存档文件本身
    不改写。参数翻译覆盖顶层 custom 与旧存档遗留的 ``node_parameters``
    内嵌字典两处。
    """
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        return
    for node_data in nodes.values():
        if not isinstance(node_data, dict):
            continue
        identifier = node_data.get("type_")
        migration = _DEPRECATED_NODE_MIGRATIONS.get(identifier) if isinstance(identifier, str) else None
        if migration is None:
            continue
        new_type, renames = migration
        node_data["type_"] = new_type
        custom = node_data.get("custom")
        if isinstance(custom, dict):
            _rename_params(custom, renames)
            nested = custom.get("node_parameters")
            if isinstance(nested, dict):
                _rename_params(nested, renames)


def _node_compat_info(identifier: str, factory) -> tuple[frozenset[str], NodeDefinition | None] | None:
    """返回 ``(allowed_custom_keys, definition)``，未注册类型返回 None。

    allowed 派生自节点实例模型（``model.properties`` ∪
    ``model.custom_properties``），与 NodeGraphQt _deserialize 里
    ``model.set_property`` 的合法键判定完全一致——参数、内嵌面板属性
    （node_parameters）以及未来任何新增自定义属性都会自动包含，不随
    StudioNode 的参数声明方式漂移。
    """
    if identifier in _NODE_COMPAT_CACHE:
        return _NODE_COMPAT_CACHE[identifier]
    node_class = factory.nodes.get(identifier)
    if node_class is None:
        return None
    node = node_class()  # 无参构造是 NodeGraphQt 契约；面板为 Qt 控件，需已存在 QApplication
    cached = (
        frozenset(node.model.properties) | frozenset(node.model.custom_properties),
        getattr(node, "definition", None),
    )
    _NODE_COMPAT_CACHE[identifier] = cached
    return cached


def sanitize_session_data(data: dict, factory) -> SessionLoadReport:
    """旧存档兼容清洗：过滤当前节点类不支持的参数/取值，返回调整报告。

    在 ``graph.load_session``/``import_session`` 之前对解析出的会话字典逐
    节点处理（存档文件本身不改写）：

    - ``custom`` 中不在当前节点模型属性集合里的键：丢弃并记录（否则
      NodeGraphQt 抛 ``NodePropertyError``，整个方案读不出来）；
    - choice 参数的存档取值已不在当前选项内：回退为该参数默认值并记录
      （QComboBox.setCurrentText 对未知文本静默保持原索引，不回退会导致
      ``node.params`` 与面板控件值分歧）。旧存档 custom 里冗余的
      ``node_parameters`` 内嵌字典同样回退——反序列化期间面板
      ``set_values`` 先于加载后的模型同步执行，未知 aspect 类标签会抛
      ``KeyError``；
    - 存档节点类型未注册：NodeGraphQt 静默跳过该节点及其连线，记录以便
      加载后提示用户。
    """
    report = SessionLoadReport()

    # 已删除节点的旧存档先改写为新节点类型（决策 #134：色相调整 → 色相/饱和度），
    # 之后的清洗按新节点定义进行——旧存档零丢失且不产生 unknown_types 噪音。
    _migrate_deprecated_node_types(data)

    def _coerce_choices(container: dict, definition: NodeDefinition, title: str, reported: set[str]) -> None:
        for param in definition.params:
            if not isinstance(param, ChoiceParam) or not param.choices:
                continue
            value = container.get(param.name)
            if isinstance(value, str) and value not in param.choices:
                container[param.name] = deepcopy(param.default)
                if param.name not in reported:
                    reported.add(param.name)
                    report.reset_choices.append((title, param.name, value))

    for n_id, n_data in data.get("nodes", {}).items():
        if not isinstance(n_data, dict):
            continue
        identifier = n_data.get("type_")
        title = str(n_data.get("name") or identifier or n_id)
        info = _node_compat_info(identifier, factory) if isinstance(identifier, str) else None
        if info is None:
            if identifier is not None:
                report.unknown_types.append((str(identifier), title))
            continue
        allowed, definition = info
        custom = n_data.get("custom")
        if not isinstance(custom, dict):
            continue
        dropped = [key for key in custom if key not in allowed]
        if dropped:
            report.dropped_params.append((title, str(identifier), tuple(dropped)))
            for key in dropped:
                custom.pop(key)
        if definition is not None:
            reported: set[str] = set()
            _coerce_choices(custom, definition, title, reported)
            nested = custom.get("node_parameters")
            if isinstance(nested, dict):
                _coerce_choices(nested, definition, title, reported)
    # 端口改名兼容：在 graph.load_session 之前改写连线端点（见 _PORT_RENAME_MAP）。
    _remap_connection_ports(data)
    return report


def _remap_connection_ports(data: dict) -> None:
    """把已改名输入端口在旧存档连线中的旧名改写到新名（端口命名新规的兼容层）。

    NodeGraphQt 的会话 ``connections`` 形如 ``{"in"/"out": [节点id, 端口名]}``，
    反序列化时 ``_deserialize`` 用 ``node.inputs().get(port_name)`` 按**端口名**
    找端口——端口改名后旧存档该连线会静默丢失（找不到端口直接跳过）。
    这里按连线端点的节点 type_ 查 ``_PORT_RENAME_MAP``，命中则改写端口名；
    未改名的端口（含仅隐藏显示、内部名不变的第一端口）原样保留。
    """
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        return
    for connection in data.get("connections", []):
        if not isinstance(connection, dict):
            continue
        for side in ("in", "out"):
            endpoint = connection.get(side)
            if not (isinstance(endpoint, list) and len(endpoint) == 2):
                continue
            node_id, port_name = endpoint
            node_data = nodes.get(node_id) if isinstance(node_id, str) else None
            identifier = node_data.get("type_") if isinstance(node_data, dict) else None
            mapping = _PORT_RENAME_MAP.get(identifier) if isinstance(identifier, str) else None
            if mapping and isinstance(port_name, str) and port_name in mapping:
                endpoint[1] = mapping[port_name]
