# 触屏 / 笔适配可行性分析（2026-08，触控输入调研）

> 结论先行：**可行，且属低风险增量改造**。不涉及执行/存档/渲染链路，无新第三方
> 依赖。缺口集中在**画布平移/缩放的输入源**（现只认滚轮与 MMB/Alt+LMB 鼠标手势，
> 触屏一个都产不出来）与若干**触屏特有交互**（长按右键、无 hover 的帮助面板、
> 触点尺寸）。推荐路径：子类化 `NodeViewer`（注入点已存在）→ **手写 QTouchEvent
> 双指捏合/平移**（`viewportEvent()` 拦截，单指放行合成鼠标）→ 复用 NodeGraphQt
> 现成的 `scale(pos)`/`_set_viewer_pan` 出口；笔（作为精确鼠标）**零改动即可用**，
> 压力/倾角才需额外走 Wintab/WM_POINTER。
>
> **Phase 0 spike 已在本机（无触控硬件的 Windows 11）用 QTest.touchEvent 软件注入
> 完成端到端实测**（探针 `_touch_spike/`）：单指触摸→合成鼠标→拖节点全通（零改动）；
> 双指捏合→`viewer.scale(锚点)` zoom 0→1.95 逐级放大、上限钳制生效；双指平移→
> 场景位移与手势一致。**QGesture/QPinchGesture 首选路径被证伪**：本机 Windows +
> QTest 注入下 grabGesture 从不触发手势事件，且 Qt 6.11 源码证实手势识别不在
> 触摸事件投递路径（`qguiapplication.cpp` `processTouchEvent` 无手势调用）——
> 生产实现直接走手写方案（见 §4.1 与附录 A）。

---

## 1. 现状输入模型（代码核实，NodeGraphQt 0.6.44 / PySide6 6.11）

### 1.1 画布平移与缩放是「鼠标专属」

`.venv/Lib/site-packages/nodegraphqt/widgets/viewer.py`（下称 viewer.py）：

| 交互 | 实现 | 位置 |
|---|---|---|
| 缩放（滚轮） | `wheelEvent` → `_set_viewer_zoom(delta, pos=event.pos())` | viewer.py:660 |
| 平移 | `mouseMoveEvent` 中 `MMB_state or (LMB_state and ALT_state)` → `_set_viewer_pan` | viewer.py:593–601 |
| 缩放（MMB+Alt 拖拽） | `MMB_state and ALT_state` → `_set_viewer_zoom(±0.1, 0.05, pos)` | viewer.py:593–596 |
| 选择/拖节点/框选/连线 | LMB 系列（`mousePressEvent`/`mouseMoveEvent`/`mouseReleaseEvent`） | viewer.py:388/581/512 |
| 修饰键 | `keyPressEvent` 维护 `ALT_state`/`CTRL_state`/`SHIFT_state` | viewer.py:704 |
| 右键菜单 | `contextMenuEvent` → `context_menu_prompt` | viewer.py:344–386 |

触屏只能通过「合成鼠标事件」进入这条链路（见 §2），而合成鼠标：
- **没有修饰键**（Alt/MMB 无法从触屏产生）→ 平移（Alt+LMB）与 MMB 缩放**完全断链**；
- **没有滚轮** → 缩放（wheel）**完全断链**。

这正是「较为根源的画布缩放问题」的两半：输入层（无触控/手势事件源）+ 模型层（见 1.2）。

### 1.2 缩放模型根源：scene-rect 制，不是 transform 制

```python
# viewer.py:213-263
def _set_viewer_zoom(self, value, sensitivity=None, pos=None):
    if pos:
        pos = self.mapToScene(pos)          # 锚点是视口坐标，自动换算到场景
    ...
    scale = (0.9 + sensitivity) if value < 0.0 else (1.1 - sensitivity)
    ...
    self.scale(scale, scale, pos)

def scale(self, sx, sy, pos=None):
    center = pos or self._scene_range.center()
    ...  # 以 pos 为不动点调整 _scene_range（QRectF）
    self._update_scene()                    # setSceneRect + fitInView
```

- NodeGraphQt 的缩放**不是** `QGraphicsView::scale()`（transform 制），而是**改写
  `_scene_range` + `fitInView`**。`get_zoom()` 读的是 `transform().m11()-1`（viewer.py:1552），
  两者互为表里。
- **反模式红线**：若手势代码直接调 `QGraphicsView.scale()` 或 `setTransform()`，
  `_scene_range` 不再更新 → `reset_zoom`/`set_zoom`/`fit_to_selection` 全部错位。
  新手势必须只走三个出口：`viewer.scale(sx, sy, pos)`、`_set_viewer_zoom(value, sens, pos)`、
  `set_zoom(value)`（内部已钳制 `ZOOM_MIN=-0.95`/`ZOOM_MAX=2.0`，viewer.py:26-27）。
- **好消息**：`scale(pos)` 的锚点参数天然支持「以捏合中心为不动点」——与
  `QPinchGesture.centerPoint()`（视口坐标）直接对接，无需改库。

### 1.3 应用层（gif_node_studio）现状

- `ui/ui.py:277` `self.graph = NodeGraph()`；NodeGraphQt 支持注入自定义 viewer
  （`base/graph.py:151` `kwargs.get('viewer') or NodeViewer(...)`）→ **子类化扩展点已存在，
  无需 fork NodeGraphQt**。
- 全局事件过滤器（ui.py:436 `eventFilter`）：空格切自动模式、输入框聚焦放行——与触控无冲突，
  但说明「应用级事件过滤」是既有模式，长按右键等可照此实现。
- 节点内拖拽交互均走 `QGraphicsSceneMouseEvent`：裁剪 overlay（`crop_overlay.py:553`）、
  剃刀条（`razor_strip.py:180`）、修剪条（`trim_strip.py:219`）、背景框双击改名
  （`backdrop.py:172`）——单指合成鼠标路径下**原样可用**（见 §4 冲突仲裁）。
- 节点库帮助面板依赖 `enterEvent`/`leaveEvent` 悬停（`ui/widgets.py:74-99` LibraryButton）
  → 触屏无 hover，帮助面板永远停在默认文案（次要缺口，§4.2）。
- 全仓 grep：`src/` 与 NodeGraphQt 库内均无 `QTouchEvent`/`QGesture`/`QTabletEvent`/
  `WA_AcceptTouchEvents` 代码；GitHub `repo:jchanvfx/NodeGraphQt` issues 搜 touch = 0 条
  → 上游无触控支持、无先例可抄，属本项目自研增量。
- 触控目标尺寸实测：端口命中区 `PortEnum.SIZE=22` + `CLICK_FALLOFF=15`（x 向放大，
  `port.py:42-44` → 实际约 37×22 px）；节点默认 160×60（`constants.py:104-118`）；
  库按钮为整行 QPushButton（图标 36px，`widgets.py` LibraryButton.ICON_SIZE=36）。

## 2. Qt 6 触控/笔机制事实表（官方文档核实，Qt 6.11.2）

| 机制 | 事实 | 对本项目意义 |
|---|---|---|
| `QTouchEvent` | 需要 `WA_AcceptTouchEvents`（QAbstractScrollArea 设在 **viewport** 上）；场景项需 `acceptTouchEvents`；在 `QWidget::event()`/`QAbstractScrollArea::viewportEvent()` 接收 | 原始多点触控入口（备选方案） |
| `AA_SynthesizeMouseForUnhandledTouchEvents`（默认开） | **未被接受的触控会被合成鼠标事件**（单点第一触点 → 鼠标按下/移动/释放） | 现状单指可用性的来源；**接受 TouchBegin 即关闭该手势的合成**——冲突仲裁的关键 |
| `QNativeGestureEvent` | 官方表列触发源仅 **macOS / Wayland**（Zoom/Pan/Rotate） | **Windows 触屏捏合不会走到这**，勿选此路 |
| `QGesture`/`QPinchGesture`/`QPanGesture` | 跨平台手势识别（从 touch 事件识别）；社区有 PySide6 6.10.1 实证（平台存疑）；坑：Qt6 中 `QPinchGesture(gesture)` 拷贝构造得到空对象。**本机实测（2026-08，Windows 11 + QTest 注入）：grabGesture 后从不触发 Gesture 事件**；Qt 6.11 源码证实手势识别不在触摸投递路径 | **已证伪（本验证环境），生产不采用** |
| `QScroller` | kinetic 惯性滚动；`TouchGesture` 识别器（触屏单指 / 触控板双指） | 画布惯性平移可选项 |
| `QTabletEvent` | Windows 官方文档：需 **wintab32.dll**（Wacom 驱动）；Qt 先发 tablet 事件，未接受则**合成鼠标** | 笔=精确鼠标开箱即用；压力/倾角仅在有 Wintab 的驱动上可用 |
| 触控板（precision touchpad） | 双指滚动 = `WM_MOUSEWHEEL` → Qt `wheelEvent` | **触控板缩放现在就能用**，缺口只在触屏 |

## 3. 现状行为矩阵：今天直接上手会怎样

| 交互 | 现状 | 触屏缺口 |
|---|---|---|
| 点选/拖节点/框选/连线/拖裁剪框/剃刀/修剪条 | 合成鼠标 → 已可用 | 无（手感另说） |
| 参数滑块/数值框/下拉/按钮 | 合成鼠标 → 已可用 | 触点偏小（§4.3） |
| **画布平移** | 需 MMB 或 Alt+LMB | ❌ 完全不可用 |
| **画布缩放** | 需滚轮（或 MMB+Alt） | ❌ 完全不可用 |
| 右键菜单 | OS 长按→右键在 Qt 接管 WM_POINTER 后不成立 | ❌ 需自实现长按 |
| 双击改名 | 合成双击是否触发 DblClick 待真机验证 | 待验证 |
| 节点库悬停帮助 | 无 hover | ❌ 需点按替代 |
| 笔（作鼠标） | Windows 对笔合成鼠标事件 | ✅ 零改动全功能 |
| 笔压力/倾角 | 无 Wintab 驱动时拿不到 | 当前无消费场景，暂不需要 |

## 4. 方案设计（最小改动路径）

### 4.1 画布手势（核心，Phase 1；**手写方案已端到端验证，见附录 A**）

1. **子类化 `NodeViewer`**（如 `TouchNodeViewer`），经 `NodeGraph(viewer=...)` 注入
   （`base/graph.py:151`）；不动库、不动 `ui.py` 的 graph 构造以外代码。
2. **viewport 设 `WA_AcceptTouchEvents`**；在 `viewportEvent()` 拦
   `TouchBegin/TouchUpdate/TouchEnd/TouchCancel`。
3. **单指（points==1）不处理**：`return super().viewportEvent(event)` → 触摸事件
   未接受 → Qt 合成鼠标（`AA_SynthesizeMouseForUnhandledTouchEvents`，Qt 6.11 源码
   确认条件：`!e->synthetic() && !touchEvent.isAccepted()`）→ NodeGraphQt 鼠标链路
   全通（实测：拖节点端到端可用）。
4. **双指（points>=2）接管**：`event.accept()`（合成鼠标随之关闭，双指不误拖）：
   - **捏合缩放**：两点欧氏距离比 s = d_now/d_prev → 按 `get_zoom()` 与
     ZOOM_MIN/ZOOM_MAX（-0.95/2.0，`viewer.py:26-27`）钳制 →
     `viewer.scale(s, s, center.toPoint())`（center=两指中点，视口坐标，
     与 `scale(pos)` 契约一致，锚点=捏合中心）。
   - **双指平移**：两指质心位移 (dx, dy) → `viewer._set_viewer_pan(dx, dy)`。
5. **冲突仲裁（已实测）**：单指合成鼠标与双指捏合互不干扰——双指时 accept 触摸
   即关闭该事件的鼠标合成；单指时不 accept 即放行。节点拖拽/裁剪/剃刀/滑块等
   无需任何改动。
6. ~~QGesture/QPinchGesture 首选路径~~：**已证伪**（本机 Windows + QTest 注入不触发；
   Qt 6.11 源码 `qguiapplication.cpp` `processTouchEvent` 无手势识别调用）。
   不采用；如需惯性滚动可另行评估 `QScroller`（其识别器同样依赖手势系统，
   真机验证前不引入）。

### 4.2 触屏特有交互补全（Phase 1/2）

- **长按 → 右键菜单**：`TouchNodeViewer` 内 `QTimer`（约 500ms）+ 位移阈值判定，
  触发时发 `context_menu_prompt`（复用 NodeGraphQt 现成信号，viewer.py:379 同款语义）。
- **节点库帮助**：LibraryButton 增加按下（`pressed`）即显示帮助（复用
  `_on_library_hover` 逻辑）；hover 路径保留给鼠标。
- **双击改名**：真机验证合成双击；若不可用，TouchNodeViewer 自实现 tap-tap 判定
  （两次 touch end 时间/位移阈值）。
- **触控板语义确认**：触控板双指滚动现在是「缩放」（wheelEvent）——若期望
  「滚动」，需在 wheelEvent 区分设备（`event.device()`），属可选打磨，默认不改。

### 4.3 触控目标尺寸（Phase 2，手感打磨）

- 端口命中区 37×22 px：建议 hit area 扩到 ≥40×40（改 `port.py` boundingRect 或
  在 TouchNodeViewer 命中测试层放大，改动面小）。
- 库按钮/工具栏按钮 `min-height` ≥ 36 px（手指舒适 ≥ 44 px）。
- 节点内滑块（QSlider 默认高度偏小）可用样式表加高；Fusion 风格下点按/拖拽均可用。

### 4.4 笔（独立于触屏，建议直接启用）

- 笔即鼠标：Windows 为笔合成鼠标事件，NodeGraphQt 与 app 全部鼠标交互原样可用，
  且精度天然适配裁剪框/剃刀条这类小目标拖拽。**无需任何代码**。
- 若未来需要压力/倾角（如笔刷类节点）：Qt 6.11 官方 Windows 路径 = Wintab
  （wintab32.dll，Wacom 驱动）；Surface 系无 Wintab 的笔需自接 WM_POINTER
  （中等工程量，另行评估）。

## 5. 风险与反模式

1. **接受 TouchBegin 会掐断该手势的鼠标合成**（`AA_SynthesizeMouseForUnhandledTouchEvents`
   语义：只对「未处理」的触控合成鼠标）。因此单指 TouchBegin 一律放行，绝不无脑 accept。
2. **勿直接调 `QGraphicsView.scale()`/`setTransform()`**——`_scene_range` 失同步，
   reset_zoom/fit 全错位；只走 NodeViewer 的 `scale`/`_set_viewer_zoom`/`set_zoom` 出口。
3. **QGesture 路径已实测证伪**（本机 Windows 11 + QTest 注入：grabGesture 后零
   Gesture 事件；Qt 6.11 源码确认手势识别不在触摸投递路径）→ 生产直接用手写
   方案（§4.1），不再保留 QGesture 备用路径。若未来真机（真实 WM_POINTER 触摸）
   意外可用，可作实验性选项评估，但不作依赖。
4. **双指捏合不区分手指落在节点/控件上**：双指即缩放画布（含落在节点上）——
   这是 node editor 通行语义（Blender/Figma 同类），可接受；若要更细仲裁再引入
   hit-test 门控（手写方案下可查触摸起点落在哪类 item）。
5. **测试**：offscreen 下 QTest.touchEvent 可注入触控序列，但手势识别器行为需实测；
   建议把「scaleFactor → 钳制 → scale 调用」提成纯函数单测（不依赖平台），
   事件层用真机冒烟 + 少量 offscreen 断言。
6. 与既有 DPR 处理（跨屏 1:1 预览，ui.py:428 `event()` ScreenChangeInternal）无交集；
   高 DPI 触屏（150–200%）由 Qt 自动缩放，NodeGraphQt 场景为逻辑坐标，不受影响。

## 6. 实施阶段建议

| 阶段 | 内容 | 工作量 | 出口 |
|---|---|---|---|
| ~~Phase 0~~（已完成） | **本机 QTest.touchEvent 软件注入端到端实测**（`_touch_spike/`）：单指→合成鼠标→拖节点全通；双指→捏合缩放 zoom 0→1.95 + 钳制；双指平移→场景位移一致；QGesture 证伪 | 已完成 | 手写方案定为生产主线 |
| Phase 1 | TouchNodeViewer 落地（§4.1 原型逻辑）+ 长按右键 + 库按钮点按帮助 + offscreen 测试 | ~2–3 天 | 触屏可完成全部画布操作 |
| Phase 2 | 触控目标尺寸、惯性/灵敏度手感、真机回归（连 node 内部拖拽交互） | ~1–2 天 | 交付 |

总评：**低风险**（QGesture 悬点已消除）；不动执行/存档/渲染/自动模式链路；笔支持
近乎免费。真机（触屏/笔硬件）仍建议回归一遍 §4.1 的手感与坐标精度——QTest 注入
的坐标带窗口偏移怪癖（附录 A），真机上由 QPA 正确换算。

## 附录 A：Phase 0 spike 实测记录（2026-08，无触控硬件机器）

**验证手段**：QTest.touchEvent 软件注入触摸事件（走 QWindowSystemInterface →
与真实触屏相同的 Qt 事件管线），探针脚本 `_touch_spike/`（probe_gesture*.py /
probe_touchviewer.py）。

**端到端结论**（`probe_touchviewer.py`，真实 NodeGraphQt + TouchNodeViewer）：

| 断言 | 注入 | 结果 |
|---|---|---|
| 单指拖节点 | press(节点中心)→move(+60,+40)→release | 节点 [300,250] → [374.6,299.7] moved=True |
| 双指捏合放大 | 两指间距 120→440（5 步） | zoom 0.07→0.34→0.74→1.28→1.95，上限钳制生效 |
| 双指平移 | 两指同向移 +80px | scene_rect x 310→390（+80，与手势一致） |

**关键事实**（源码核实，Qt 6.11.2 `qguiapplication.cpp` `processTouchEvent`）：
- 合成鼠标条件 = `!e->synthetic() && !touchEvent.isAccepted() &&
  AA_SynthesizeMouseForUnhandledTouchEvents`；触摸事件被 accept 即关闭合成；
  QTest 注入事件 synthetic=false（合成鼠标可发生，与实测一致）。
- `processTouchEvent` **无任何 QGestureManager 调用**——手势识别不在触摸投递
  路径，这是 QGesture 在本机不触发的根因层面证据。

**QTest 注入的坑**（本机实测）：
1. PySide6 6.11.1：`QTouchEventSequence.press/move` 链式调用抛
   "Error evaluating `PySide6.QtTest.QTouchEventSequence.press`"——返回类型注解
   引用嵌套类；workaround：`PySide6.QtTest.QTouchEventSequence =
   PySide6.QtTest.QTest.QTouchEventSequence` 暴露到模块层 + 不用链式调用。
2. 注入坐标被当 **global 坐标**（QTest 实现）→ 窗口内位置 = 传参 - 窗口屏幕位置；
   用 `widget.windowHandle().position()` 修正（真机由 QPA 正确换算，无此问题）。
   捏合**距离比/位移差不受偏移影响**（同事件内相对关系正确）。
3. `grabGesture` 后**不抑制**单指合成鼠标（实测：grabGesture(Pinch) 的 QWidget
   单指触摸仍收到 TouchBegin + 合成 MouseButtonPress）——若 QGesture 未来可用，
   冲突仲裁依然成立。
4. 触摸事件在无 `WA_AcceptTouchEvents` 时投递给 QWindow 层（`TouchBegin -> QWindow`），
   设置后投递给 widget/（QGraphicsView 的）view 本体；`viewportEvent()` 是
   QGraphicsView 的触摸拦截点（已实测收到）。

## 参考（2026-08 查证）

- Qt 6.11.2 QTouchEvent：https://doc.qt.io/qt-6/qtouchevent.html
- Qt 6.11.2 QNativeGestureEvent（触发源表：macOS/Wayland）：https://doc.qt.io/qt-6/qnativegestureevent.html
- Qt 6.11.2 QTabletEvent（Windows 需 wintab32.dll；未接受→合成鼠标）：https://doc.qt.io/qt-6/qtabletevent.html
- Qt 6.11.2 QScroller（TouchGesture 识别器）：https://doc.qt.io/qt-6/qscroller.html
- Qt 6.11.2 Image Gestures 示例（QWidget + grabGesture + QPinchGesture）：https://doc.qt.io/qt-6/qtwidgets-gestures-imagegestures-example.html
- Qt 论坛：QGraphicsView + WA_AcceptTouchEvents 的 viewportEvent 触控接收与拖出视口丢事件问题：https://forum.qt.io/topic/150098
- SO 74998152（PySide6 6.10.1 QPinchGesture 实证；Qt6 拷贝构造坑）：https://stackoverflow.com/questions/74998152
- Qt 官方开发列表（AA_SynthesizeMouseForUnhandledTouchEvents 语义）：https://lists.qt-project.org/pipermail/development/2012-May/003833.html
- Qt 6.11 源码 `qguiapplication.cpp` `processTouchEvent`（合成鼠标条件、无手势调用，2026-08 核实）：https://raw.githubusercontent.com/qt/qtbase/6.11/src/gui/kernel/qguiapplication.cpp
