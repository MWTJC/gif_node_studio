# NodeGraphQt 显示层修复（`StudioNodeItem`）

两处 0.6.44 默认布局缺陷，通过**继承**修复：`BaseNode.__init__(qgraphics_item=...)` 是官方扩展点，
`StudioNode.__init__` 传入 `StudioNodeItem(NodeItem)` 子类，无需 monkeypatch。

## 节点宽度被标题撑大（已修复）

- 原公式：`width = port_width + max(标题宽, 端口标签宽) + padding + widget宽`
  —— 标题宽度被**无条件累加**，每多一个字节点等宽变宽；0.6.44 全库无文字省略机制。
- 修复公式：`width = port_width + max(标题宽, 端口标签宽 + widget宽) + padding`
  —— 标题只在比整体内容更宽时才撑大节点；标题比内容短时节点宽由内容决定，标题居中且余量充足。
- 实测：修复前「视频输入」421 px /「静态图片序列输入」469 px（Δ=标题差 48）；
  修复后两者均 365 px；超长标题（356 px）节点 403 px（标题比内容宽时才增长，且完整显示）。

## 单侧端口节点 widget 不居中（已修复）

- 原 `_align_widgets_horizontal`：仅输出 → 贴左（`rect.left()+10`）；仅输入 → 贴右；双侧 → 居中。
- 修复：无条件 `x = rect.center().x() - widget宽/2` 且 `setTitleAlign('center')`。
- 实测：修复前 输出型 −53.5 px、输入型 +53.5 px；修复后均为 0。

## 视野缩小（proxy 模式）时运行节点 → 节点板塌缩（已修复）

**现象**：把视图缩小到节点开始略去内嵌组件（proxy 模式）后，运行任一节点
（或改参数/刷新预览），**所有被略去组件的节点板塌缩成 160×60 极小尺寸**；
再放大视图，组件重新显示但节点板无法恢复。复现（`samples/存档/logo.json`）：
先全尺寸运行「ico分辨率查看」让节点变大，缩小视图到有节点略去组件，重连
渐变序列使全部节点 dirty，再点「ico分辨率查看」运行键即可看到塌缩。

**根因**（NodeGraphQt 0.6.44）：缩小视图时 `paint()` → `auto_switch_mode()` →
`set_proxy_mode(True)`（qgraphics/node_base.py:729-785）把内嵌 widget、
端口标签、标题、图标**设为不可见**——这只是显示层降级，组件的真实几何
（`boundingRect()`）保持原尺寸。但尺寸/对齐计算按 `isVisible()` 跳过不可见
组件：

- `_calc_size_horizontal`：`not widget.isVisible()` 跳过 widget、`not
  text.isVisible()` 跳过端口标签 → 算出的节点尺寸只剩「标题+端口」；
- `_align_widgets_horizontal`：同样跳过不可见 widget。

而 `draw_node()` 的触发远不止缩放：`set_property`（自定义属性）、
`sync_geometry()`（内嵌面板每次 `geometry_changed`——运行节点、状态文本、
耗时/缓存信息、1:1 预览尺寸变化都会发）都会重排。于是**缩小时运行节点** →
`sync_geometry` → `draw_node` → `_set_base_size` 用塌缩尺寸重写
`_width/_height`；放大视图时 `set_proxy_mode(False)` 只恢复可见性、**不重排**，
塌缩永久化。

**修复**（`StudioNodeItem`）：布局计算不区分「被 proxy 隐藏」与「真不可见」：

- widget：`laid_out = self._proxy_mode or widget.isVisible()`（proxy 模式下
  widget 全部按真实几何参与尺寸与对齐；非 proxy 保持原语义）；
- 端口标签：`text.isVisible() or (self._proxy_mode and port.display_name)`
  （proxy 只隐藏「本来会显示」的标签，宽度计算按 display_name 门控）。

端口本身不被 proxy 隐藏，端口循环无需改动。修复后缩小时运行节点：节点板
按完整内容重排（若内容确实变化，板随内容正确增减），放大后组件与板始终
吻合；全尺寸行为与非 proxy 完全一致（两个谓词在非 proxy 时退化为原判断）。

验证：直接驱动 `set_proxy_mode` + `geometry_changed → sync_geometry →
draw_node` 链路，断言 proxy 下 `calc_size()` 与组件可见时一致、无 160×60
塌缩、板包得住组件、恢复后正确；另以真实 `viewer.set_zoom` 缩放路径触发
paint → proxy 模式复验同一套断言。

## 内嵌下拉框弹出列表脱离节点飞向上方（已修复，`StudioComboBox`）

**现象**：新建带下拉框的节点（画面裁剪/颜色深度等），选中下拉列表**最底部一项**后
缩小视图，再次点开下拉菜单，列表整体跑到节点上方很远；项越多越明显。
NodeGraphQt 示例的 `add_combo_menu` 节点（同样扩展列表长度）**不出现**此现象。

**根因**（Qt 6.11 + NodeGraphQt 0.6.44，两条件叠加）：

1. 应用 `apply_theme` 强制 **Fusion** 样式 → `SH_ComboBox_Popup = True` →
   Qt 走 popup 分支，把弹出列表定位成「**当前选中项对齐下拉框**」：
   `popupTop = 下拉框全局顶 − 当前索引 × 行高`（qcombobox.cpp `showPopup`，
   `listRect.moveTop(above.y() + offset - listRect.top())`）。
2. `QGraphicsProxyWidget::setWidget` 给内嵌控件设置
   `Qt::WA_DontShowOnScreen`（qgraphicsproxywidget.cpp:638）——下拉框的
   `window()`（`_NodeGroupBox`）带此属性 → `boundToScreen == false` →
   Qt 跳过弹出列表的屏幕钳制（qcombobox.cpp:2924, 3032-3037）。

于是选中第 N 项时列表以 `(N−1)×行高` 悬在控件上方；节点在屏幕中部时列表整体
仍在屏幕内、窗口管理器不干预，视觉上完全脱离节点；节点贴近屏幕顶边时列表
出屏，Windows 才会把它拉回（掩盖问题，属假象）。

**为什么示例节点不飞**：示例用 Windows 原生样式（`windows11`），
`SH_ComboBox_Popup = False` → 走常规下拉分支：列表出现在下拉框**下方**并
滚动到当前项（qcombobox.cpp:3038 `moveTopLeft(below)`、3124 `EnsureVisible`），
与项数无关。**示例与应用的差异 = 样式差异，不是项数。**

**修复**：`nodes.widgets.StudioComboBox(QComboBox)` 构造器设置样式表
`QComboBox { combobox-popup: 0; }`——Qt Stylesheet Reference 官方属性，让
`SH_ComboBox_Popup` 返回 False、走**常规分支**：列表出现在下拉框**下方**并
滚动到当前项（qcombobox.cpp `moveTopLeft(below)` / `EnsureVisible`），定位不
依赖屏幕钳制 → 代理环境（WA_DontShowOnScreen）天然安全。行为差异：列表高度
按 `maxVisibleItems`（默认 10）截断、超出出现滚动条（popup 分支是全部项可见）；
需要更多行时 `setMaxVisibleItems(20)`。`ParameterPanel` 的 choice 分支统一
使用 `StudioComboBox`。

> 曾用方案「showPopup 后手动重定位容器再钳制回屏」已弃用——要推算容器几何、
> 枚举屏幕、协调截高滚动；且「QProxyStyle 把 SH_ComboBox_Popup 报告为 False」
> 会接管基样式所有权（`style->setParent(proxy)`，qproxystyle.cpp:96）导致退出
> 双重释放崩溃（实测 SMOKE_OK 后 0xC0000409）。样式表属性一行、官方路径、无
> 所有权风险。

offscreen 实测（2026-08）：`combobox-popup: 0` 后列表顶部 ≈ 控件底部（贴合），
裸控件与 QGraphicsProxyWidget 内嵌均通过；无样式表裸 QComboBox 走 popup 分支
（会飞）。回归：真实平台 + offscreen 双路径（检查「贴合」而非仅「在屏内」）。

**NodeComboBox 同样受影响**：NodeGraphQt 的 `NodeComboBox` 是同一机制
（QGraphicsProxyWidget + 裸 QComboBox），在 Fusion 主题下选中靠后项同样会飞；
示例没触发只是因为它没走 Fusion。若要用 NodeComboBox，套同样子类
（样式表 `combobox-popup: 0`）即可。

> 第一版修复曾只做「钳制回屏幕」，offscreen 验证只检查了「列表在屏幕内」而未
> 检查「贴合下拉框」，故误判成功——节点在屏幕中部时列表本来就在屏内，
> 钳制不动它，问题依旧。本次验证以「列表顶部 ≈ 控件底部」为准。

## 已知边界

- NodeGraphQt 的 `PropertyChangedCmd` 对非自定义属性 `name` 不触发 `draw_node`，
  因此**重命名不会重新布局节点尺寸**（尺寸在创建时定型）——属于库行为，当前接受。
- **参数撤销不刷新面板控件**：`PropertyChangedCmd` 撤销/重做只写模型属性
  （`model.set_property`），不回调 `EmbeddedPanelWidget.set_value`，因此撤销滑条/裁剪
  拖拽后，滑条位置与裁剪红框仍停留在撤销前状态（`node.params` 亦不回滚，仅在下次
  参数变化/克隆/读预设时重新同步）——既有行为，接受。
