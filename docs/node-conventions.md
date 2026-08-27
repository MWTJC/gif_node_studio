# 节点体系约定

> 用户强制约定，与 某个旧项目的 `某种功能模块` 一致。

## 构造注入声明（`TestModule`模式）

- 每个具体节点类在**自身的无参 `__init__`** 中，把完整声明通过
  `super().__init__(definition=NodeDefinition(...), help=...)` 传给基类 `StudioNode`，
  等价于 `TestModule` 的 `super().__init__(test_type=..., name=..., ...)`。
- `StudioNode.__init__(self, definition, *, help="")` 把声明存入 `self.definition`，并
  `self.NODE_NAME = definition.title`（实例遮蔽类属性，使新创建节点按声明标题命名）。
- 声明只存在于实例上（`self.definition`），**不得**放模块级全局变量，也不得拆成平行类属性。

## 面板声明（`PanelSpec` + 接管型参数，决策 #109）

- **禁止覆写 `create_panel()`**：面板完全由节点声明驱动，
  `ParameterPanel(definition)` 单参数构造（基类默认实现即全部行为）。
- **显示/装饰特征** → `NodeDefinition(..., panel=PanelSpec(...))`：
  - `scrub_frames=True` —— 预览区加可拖动帧滑条（不承载参数值）；
  - `preview_1to1=True` —— 预览框按素材原始像素 1:1（框 = 物理像素 ÷ 当前 DPR）；
  - `preview_bg_param="..."` —— 该 bool 参数变化只刷新预览框背景色，不触发运行；
  - `export_enabled=True` —— 面板显示「导出…」按钮。
- **接管型复合控件** → params 里声明 `TakeoverParam` 子类（如
  `RazorCutParam` / `CropOverlayParam`）：
  - `owned` —— 接管（不生成常规行）的参数名；值由控件统一读写；
  - `linked` —— 保留常规行并联动本控件的参数名（如裁剪纵横比下拉）；
  - `data_source` —— 外部数据源需求（`"sequence_frames"` / `"first_frame"`），
    ui 按 `panel.takeover_data_sources()` 能力探测喂数据，不再按节点 KIND 特判；
  - `widget_factory` —— 控件工厂 `callable(panel, param) -> QWidget`，定义在
    控件侧模块（声明层不 import Qt）。
  - 接管控件契约：`values()` / `set_values(dict)` / `changed` 信号，可选
    `gesture_begin` / `gesture_end` / `geometry_changed` / `refresh_dpr` /
    `release_content` 与数据源方法。新增交互组件只需「参数类型 + 复合控件」，
    **面板与 ui 无需改动**。
- 存档兼容：`default=None` 的接管声明（纯声明，如裁剪）不进入参数字典/模型
  属性；有默认值的接管声明（如剃刀 `cut`）照常参与属性与存档。

## 允许保留的类属性（仅限框架契约或单类执行常量）

- `NODE_NAME` — NodeGraphQt 注册索引与 Tab 搜索按类级读取（`NodeItem` 注册必需）。
- `EXPORT_KIND` / `CACHE_FILENAME`（导出终端节点）— `ui/ui.py` 按类属性派生导出按钮
  分派与固定缓存保留，不再平行维护 kind 字符串集合/缓存文件名常量（[关键决策 #51](decisions/51-60.md#d51)）。
- `PORT_COLORS` / `__identifier__` — 家族共享常量。

> `MEDIA_KIND`（输入族）/ `CHANNEL`（颜色族）已随决策 #112 移除——execute 现在
> 全部写在具体节点类体内，不再需要「被共享 execute 读取的每类执行常量」。

## 注册表与目录派生

- 唯一有序注册表：`NODE_CLASSES`（当前 49 个具体类）。
- 由于声明只在实例上，目录按 `TestModuleLib.get_all_test()` 的方式**实例化每个类一次**并记忆化
  （`_definitions_by_class()`），再派生：
  - `node_definitions()` — 目录（类别分组、按钮标题）；
  - `node_class_by_kind(kind)` — kind → 类（`create_node` 用）；
  - `definition_by_kind(kind)` — kind → 定义。
- 实测：目录构建（实例化 49 个节点）约 **57 ms**，缓存后 kind 查找约 **4 µs**；
  目录只在 QApplication 存在时调用（运行时安全）。
- 禁止平行注册表（`NODE_DEFINITIONS` / `NODE_TYPES_BY_KIND` / `NODE_HELP` 等历史形态已移除）。

## 族基类

- `ManifestNode` / `SequenceNode` 只拥有共享的**输入校验**（`require_input` /
  `manifest` / `sequence`），无自身 `__init__`，**不承载 execute**——决策 #112 后
  全部 49 个具体节点类的 `execute` 都在自身类体内直接调用后端处理函数
  （`backend.xxx`），单看类代码即可确认处理链路。

## 空白节点基线

「空白节点」= 具体节点类的最小骨架：`kind` / `title` / `category` / `icon` /
端口（`NodeDefinition` 最简形态），**无参数、无 PanelSpec 特征、无 execute 覆写**
（基类 `StudioNode` 已提供全部共享机制）。每个节点的代码相对该基线只应多出三样
东西，且都必须能在类代码里直接看清：

1. **参数** —— `params=(...)` 里的 `ParamDefinition` 声明；
2. **组件** —— 由参数类型派生的面板控件 + `PanelSpec` 显示/装饰特征（见下方映射表）；
3. **处理函数** —— 类内 `execute` 调用的 `backend.xxx`（或 PIL/PyAV 等直写算法）。

## 节点类 docstring 自述约定（决策 #112）

每个具体节点类 docstring 必须自述上述三样增量，统一格式：

```
"""<一句话说明>。

处理：backend.<fn>（<关键实参>）
参数：<name>（<Param 类型> <控件形态>）、...
组件：<PanelSpec 特征>、<接管型复合控件>
"""
```

- 处理函数行必须写**实际调用**（族基类已拆，均在类内 execute，写其真实目标）；
- 参数行的控件形态即下方映射表（一眼可读，不必查面板源码）；
- 组件行写 `PanelSpec` 特征（帧滑条 / 1:1 预览 / 导出按钮 / 透明背景显示）与
  接管型复合控件（剃刀胶片条 / 可视化裁剪画布）；
- 复杂节点的补充说明（边界策略、算法动机等）接在三行之后，不破坏统一格式。

## 参数类型 → 面板组件映射（`parameter_panel.make_parameter_widget` 的分派，决策 #109）

| 参数声明 | 面板组件 |
|---|---|
| `IntParam` / `FloatParam` 有 min/max 且 `widget=""` | 滑条 + 数值框（`SliderSpinBox`，拖拽手势折叠为一条撤销） |
| `IntParam` / `FloatParam` `widget="spin"` | 仅数值框（`QSpinBox` / `QDoubleSpinBox`） |
| `BoolParam` | 勾选框（`QCheckBox`） |
| `ChoiceParam` | 下拉框（`StudioComboBox`） |
| `ColorParam` | 色块按钮（`ColorPickerWidget`，QColorDialog 取色） |
| `FileParam` 族（Video/Image/Gif/Palette…） | 文件选择行（`FilePathWidget`） |
| `TakeoverParam` 子类（`RazorCutParam` / `TrimRangeParam` / `CropOverlayParam`） | 接管型复合控件（`widget_factory` 工厂：胶片条 / 区间双手柄胶片条 / 可视化裁剪画布），`owned` 参数不生成常规行 |
| `enabled_when=(依赖, 允许值集)` | 互斥置灰：依赖参数取值不满足时本控件 disabled |

`PanelSpec` 特征：`scrub_frames` → 帧滑条行；`preview_1to1` → 预览框按素材原始
像素 1:1；`preview_bg_param="…"` → 该 bool 参数只刷新预览框背景色不触发运行；
`export_enabled` → 面板显示「导出…」按钮。

## 元数据展示（节点自身定义，默认行为接管）

- 输出元数据由**节点自身**通过继承定义：基类 `StudioNode.describe_output(output)`
  （类方法）为默认行为——按输出值类型给出通用摘要；无特殊需求时**不覆写**。
- 需要自定义展示的节点在类内覆写 `describe_output`（可用
  `super().describe_output(output)` 回落默认行为），例如输出类节点仅显示
  execute 附到 `MultiOutput.metadata` 的关键信息（见 `export_nodes.py`）。
- 禁止把节点特例写回 `media/media_info.py` 的中央函数（曾以
  `EXPORT_KIND` 类属性在 `describe_output` 内 if 分派，节点多样化后不可扩展，
  已改为继承实现，见 [关键决策 #102](decisions/101-110.md#d102)）。
- 类方法约束：元数据探测在工作线程进行且只持有**节点类**（`worker` 传
  `step[1]`），覆写不能依赖实例状态。
