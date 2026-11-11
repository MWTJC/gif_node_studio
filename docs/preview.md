# 预览机制（清单携带预览图）

## 输入节点：默认首帧预览

- 输入节点（`VideoInputNode` / `ImageSequenceInputNode` / `GifInputNode`，各自类内
  `execute`，决策 #112）调用 `backend.extract_first_frame(manifest)` 并把结果写入
  `manifest.preview`：
  - `STATIC_SEQUENCE` → 直接引用 `sources[0]`（本身即图像）；
  - `ANIMATED_IMAGE` → PIL 取第 0 帧保存 RGBA PNG；
  - `VIDEO` → PyAV 解码第一帧保存 PNG。
- 预览图写入**该节点自己的缓存目录**（`backend.for_node(node_id)`）；清缓存后重跑即重建。

## 裁剪节点：序列级裁剪 + 1:1 可视化交互（[决策 #95](decisions/91-100.md#d95)）

- 画面裁剪已从「清单级」（`crop`，清单→清单，只能在格式化解码前生效）改为
  **「序列级」**（`sequence_crop`，序列→序列，一般处理）：`SequenceCropNode.execute`
  调用 `backend.crop_sequence`，直接裁剪**已格式化**的图片序列，节点可放在处理链
  任意位置（旋转/缩放/叠加等前后均可）。旧 kind `crop` 已删除，旧存档读取时该
  节点被跳过并弹窗提示（决策 #80 的 unknown_types 路径）。
- **可视化裁剪（1:1）**：`SequenceCropNode` 声明接管型参数
  `CropOverlayParam("crop", ..., owned=("left","top","right","bottom"),
  linked=("aspect",), data_source="first_frame", widget_factory=make_crop_panel)`
  + `PanelSpec(preview_1to1=True)`（决策 #109）——面板按声明自动构造
  `CropOverlayPanel`（`nodes/crop_overlay.py`：画布 + 结果缩略图 + 数值只读，
  内嵌纵横比联动），**1:1 模式**：预览框 = 素材物理像素 ÷ 当前 DPR（跟随图片，
  与 `CheckerPreviewLabel` 1:1 终版同语义，见决策 #86），内容按设备像素 1:1
  绘制（关平滑），裁剪交互直接面对原始像素；`set_1to1(False)` 可退回旧「固定
  200×200 等比适配」交互区。
  在源图上绘制四条红色裁剪线（框外压暗即被裁部分，含三分构图辅助线与四角手柄），
  支持鼠标拖边/拖角/框内平移，越界自动钳制（保证左<右、上<下，最小约 2px，
  杜绝非法 `CropSpec`）；拖拽实时发 `values_changed`（归一化）→ 面板 `changed`
  （百分比）→ 参数/自动运行（限频 1/3s）；面板「结果」缩略图（96×96）按当前
  参数**实时裁剪源图**（不依赖运行）。
- overlay 的源图 = 上游输出序列首帧（未应用本节点裁剪）：`ui.preview_path_for_node`
  按 `panel.takeover_data_sources()` 能力探测（含 `"first_frame"` 时）返回
  `_upstream_first_frame_path`（上游 `SequenceArtifact.frames[0]`，链式语义一致：
  第二个裁剪的红框画在第一个裁剪的结果上；多输出上游（序列剃刀/RGBA 通道分离）
  按**实际连接的输出端口名**取对应分支首帧——连剃刀「段B」则红框画在段B上，
  决策 #128）；无上游时回退自身输出预览。
- **1:1 框跟随图片 + 跨屏重建**：overlay 尺寸 = 素材物理像素 ÷ 当前 DPR，换图/
  跨屏后 `_update_size` 发 `geometry_changed` → 面板 → 节点重排；窗口跨屏时
  `MainWindow._refresh_all_preview_dpr` → `panel.refresh_preview_dpr(dpr)` →
  `overlay.refresh_dpr(dpr)` 显式重建（嵌入代理的控件 `devicePixelRatioF` 跨屏后
  实测不更新，必须用窗口实时 DPR）。
- **拖拽撤销折叠**：`MainWindow._param_gesture_*` 用 `graph.begin_undo/end_undo` 把
  滑条/裁剪的整个拖拽手势折叠为**一条**撤销记录；宏在首次真实变化时才开启
  （保证宏内第一条 `PropertyChangedCmd` 记录手势前的值），空手势（按下未拖动）
  不产生任何撤销记录。

## 序列剃刀节点：胶片条拖拽切割（[决策 #107](decisions/101-110.md#d107)）

- `SequenceRazorNode`（`sequence_razor`，序列处理）：输入序列 → 输出两个序列
  （段A = frames[:cut]、段B = frames[cut:]，`cut` 为 0 基切片下标即帧边界），
  由节点参数 `cut` 承载；`execute` 调用 `backend.split_sequence`（复制到
  `razor_a_*` / `razor_b_*` 两个 job 目录，越界/单帧序列报清晰中文错误），
  按 `MultiOutput` 端口名 `段A` / `段B` 输出（多输出端口名解析，与 RGBA
  通道分离同款）。
- `SequenceRazorNode` 声明接管型参数 `RazorCutParam("cut", "切割帧", ...,
  owned=("cut",), data_source="sequence_frames", widget_factory=make_razor_panel)`
  （决策 #109）——面板按声明自动构造 `RazorStripPanel`（`nodes/razor_strip.py`：
  胶片条 + 切割处两侧预览 + 只读，替代旧 `ParameterPanel(..., razor_strip=True)`），
  帧缩略图横向铺成胶片条（等比缩放到固定行高 56px，条宽上限 420px；**长序列
  跨帧采样显示**——采样只影响显示，切割位置始终按全部帧边界精确映射），红色
  剃刀线 + 顶部三角刀柄标记切割边界；鼠标在条上按下/拖动移动剃刀（吸附最近帧
  边界，两端各留 1 帧保证两段非空）；拖拽实时发 `cut_changed(int)` → 面板
  `changed`（携带 cut）→ 参数/自动运行（限频 1/3s）。
- 面板在胶片条下方显示**切割处两侧帧实时预览**：段A末帧（切割线左侧）/
  段B首帧（切割线右侧），96×96 适配框，从上游帧直接读图（**不依赖运行**）；
  只读行显示「切割位置：第 i 帧后（段A 1..i / 段B i+1..N）」。
- 剃刀条帧 = 上游输出序列产物（未应用本节点切割）：`ui.preview_path_for_node`
  按 `panel.takeover_data_sources()` 能力探测（含 `"sequence_frames"` 时）调用
  `_upstream_sequence_frames`（上游 `SequenceArtifact` 全帧；上游为多输出节点
  （序列剃刀/RGBA 通道分离）按**实际连接的输出端口名**取对应分支——链式剃刀
  连「段B」喂段B、连「段A」喂段A，随端口切换刷新，决策 #128 修复「链式剃刀
  预览固定显示段A」；上游为清单时无帧，胶片条清空显示「无预览」）；无上游时同样清空。
- **拖拽撤销折叠**：与裁剪节点同款 `MainWindow._param_gesture_*`——整个拖拽
  手势折叠为一条撤销记录；空手势不产生撤销记录。

## 帧滑动预览（格式化 / 序列往复 / 颜色深度 / 抽帧节点）

- `FormatNode`、`PingPongNode`（序列倒带）、`DitherNode`、`SamplingNode` 等节点的
  `NodeDefinition` 声明 `panel=PanelSpec(scrub_frames=True)`（决策 #109），
  预览区下方有帧滑条 + 「帧 i/N」标签；无输出时禁用。
- UI 在步骤成功/单节点成功后，若结果为 `SequenceArtifact`，
  调用 `panel.set_sequence_frames(result.frames)` 喂入帧路径；拖动滑条即时切换预览帧。

## 逐帧查看判据与产物播放口径（[决策 #144](decisions/141-150.md#d144)）

- **判据**：序列型节点默认 `panel=PanelSpec(scrub_frames=True)`（预览区下方帧滑条）；豁免只有
  两类，且必须在节点 docstring 的「组件」行写明原因：① **产物本身可播放**（用播放代替滑条，
  且必须播放**真实产物**）；② **接管控件占据预览区**且参数在序列上逐帧同构（首帧锚定，
  如画面裁剪的归一化 `CropSpec`）。豁免与待补清单见决策 #144 的登记表。
- **产物播放（已落地，决策 #144）**：可播放产物的导出节点声明 `PREVIEW_PRODUCT = True`
  （GIF 合成 ×3 / GIF 优化 / WebP / APNG），`ui` 把产物路径写入 `preview_output`，预览框
  播放**真实产物**而非上游首帧：
  - GIF → 自定义 Wand 播放器（透明索引）或 `QMovie`；
  - WebP → `QMovie`（Qt 内置 `qwebp` 插件，实测含 alpha、可循环）；
  - APNG → `ApngPreviewPlayer`（Qt 不认 `acTL` 动画块，Pillow 逐帧解码 + `QTimer`；
    同样受窗口最小化/失焦暂停，帧数/内存沿用 GIF 预览预算）；
  - `png_export` 产物是帧目录（不可播放），**不标注**，改用帧滑条；其结果形态（文件元组）
    由 `ui._frame_path_list` 归一为帧列表，喂滑条的同时也补上了此前缺失的首帧预览。
- `sequence_crop` 明确**不加**帧滑条：接管控件占预览区、裁剪框逐帧同构，且帧滑条的帧源是
  运行结果（对裁剪节点即已裁剪帧），接上会语义反向。

## 播放开销与暂停机制（[决策 #145](decisions/141-150.md#d145)）

- 暂停只由**窗口状态**驱动（`MainWindow._apply_visibility_pause`：最小化/隐藏 → 暂停播放并冻结
  画布重绘；可见但失焦 → 只暂停播放）。**没有视野剔除**：滚出视野或缩小视图（代理模式只隐藏
  内嵌控件）的产物照常解码，只是不再绘制——实测换帧数与可见时相同、paint 归零。
- 多播放器之间**无统一帧钟**：各自定时器按自身帧延迟出帧，一次换帧一次 `set_content()` →
  `update()`。实测 1→8 个播放器时每个仍 ≈21 帧/秒（标称 25，定时器粒度量化所致，非解码负担），
  互不拖慢，刷新总量 = 各播放器帧率之和。

## 预览出帧上限与状态栏读数（[决策 #146](decisions/141-150.md#d146)）

- **限帧 = skip（丢帧保时长）**：设置页「预览帧速」开关 + 10–240 fps（默认关闭、默认 30）。
  开启后 `GifPreviewPlayer` / `ApngPreviewPlayer` 改用 `PreciseTimer` 固定周期出拍，按原生帧
  延迟累积决定本拍落第几帧——**序列总时长与原生一致**，只是少画（少解码）若干帧。Coarse 定时器
  会把 33ms 量化成 46.9ms（实测「限 30fps」变 21.3fps），故必须 Precise。
- **QMovie 路径的保底**：`jumpToFrame` 只能 +1、GIF/WebP 解码是顺序的，跳帧省不了解码（实测
  CPU 无收益），动画 WebP 也只有 Qt 的 qwebp 插件能播（PyAV 不支持）——所以这条路径**不能
  skip**。上限低于素材原生帧速（5% 容差）时，预览退化为**静态首帧**；上限 ≥ 原生帧速时照常
  播放。素材原生帧速：GIF 取块解析的逐帧延迟，动画 WebP 采样 8 帧 `nextFrameDelay()`。
- **统一帧钟（决策 #147）**：限帧开启时，全部在播预览由**单一 `Precise` 定时器**按限帧速率出拍
  （`nodes/preview_clock.py`）；每拍依次推进各预览，同一轮事件循环里的换帧被 Qt 合并成**一次**
  画布合成。因此「每预览 ≤N fps、时长不变」与「界面每秒刷新 N 次」同时成立，且与预览个数无关
  （实测 8 个预览：各自定时器 57.5 次/秒 → 统一帧钟 30.5 次/秒）。
- **状态栏「渲染 N fps」**（常驻，每秒刷新一次）：口径 = 画布 viewport 的刷新次数，**按事件循环
  批次合并**（真实场景一次换帧会触发两次局部重绘，逐次计数会把 30 读成 60）。tooltip 给出「预览框
  绘制 / 换帧」次数作对照——限帧 30、3 个预览时典型值是「渲染 30 / 预览框绘制 90 / 换帧 90」。
  窗口最小化/失焦暂停播放时读数标注「（已暂停）」。
- **工作量不变的部分**：统一帧钟约束的是刷新次数与出帧密度，解码+绘制仍是各预览之和；要降总量
  用 #145 的视野剔除或后续的全局帧预算。
- **不影响导出**：限帧只作用于预览播放；导出帧速、抽帧/合成节点参数不变。

## UI 预览路径优先级

```
node.preview_output（可播放产物：GIF / WebP / APNG） → SequenceArtifact.frames[0] → MediaManifest.preview → MediaManifest.sources[0] → Path(.gif)
```

- 预览框固定为 **200×200（1:1）**，不随预览内容变化；显示内容大于预览框时保持纵横比缩小显示，小于等于预览框时原尺寸显示（`CheckerPreviewLabel` 适配模式，见下方 DPI 说明）。
  例外：**图片1:1分辨率查看**（`res1to1_view`）与**可播放产物的导出节点**（GIF 合成 ×3 /
  GIF 优化 / WebP / APNG，见决策 #144）通过 `PanelSpec(preview_1to1=True)` 按素材原始像素
  尺寸 1:1 显示（产物预览框 = 动画画布尺寸，不缩放；见[关键决策 #40](decisions/31-40.md#d40)）。

## 预览框高 DPI 像素校正（[决策 #86](decisions/81-90.md#d86)）

- 模糊根源：QLabel 内建 `setPixmap` 把 pixmap 当「UI 元素」按逻辑单位绘制——
  256px 的图在 175% 屏幕被放大到 448 设备像素（1.75× 插值）→ 边缘模糊。
- 修复（`CheckerPreviewLabel` 自绘，最终策略）：**优先保证图片内容像素精准**
  ——1:1 内容按设备像素 1:1 绘制（物理÷当前 DPR 逻辑矩形，关平滑），与 100%
  屏渲染逐位相同；**预览框跟随图片**（框 = 物理÷当前 DPR 逻辑尺寸）。
- **跨屏重建**：窗口跨屏（显示器放大倍率变化）后，`MainWindow.event()` 拦截
  `ScreenChangeInternal`（该事件不转发 changeEvent，须在 event() 覆盖中拦截），
  250ms 防抖（≈鼠标松开后）按**窗口句柄实时 DPR** 重建全部 1:1 预览——嵌入
  代理的标签 `devicePixelRatioF()` 跨屏后实测不更新（重跑/refresh/update 无效），
  必须以窗口实时 DPR 显式传入（`refresh_preview_dpr` → `refresh_dpr`）。
- 棋盘格/背景/边框维持旧行为（棋盘格瓦片不设 DPR、逻辑平铺随 UI 缩放——用户
  实测任何缩放下都清晰，非本问题）；空载预览框固定 200×200（与原方案一致）。
- 适配模式（200×200 固定框）目标 = 固定框内 contain 逻辑矩形（默认不放大），
  平滑单轮设备分辨率缩放（替代旧的「先 scaled 再被 QLabel 放大」两轮插值）。
- **滑条帧状态与预览内容强一致**（[关键决策 #57](decisions/51-60.md#d57)）：
  `ui._feed_sequence_frames` 对不携带帧的结果（1:1 查看节点改显 GIF/视频清单项等）
  统一 `panel.set_sequence_frames([])` 清空滑条——先预览序列、再浏览清单项时
  不再「串台」（旧序列帧残留、拖动滑条混入上一轮帧）。

## 透明背景底纹（绿幕 / 品红 / 棋盘格）

- 「透明背景」/「透明预览」勾选（1:1 查看节点与 GIF 合成节点的纯显示选项）后，
  预览框底纹由设置管理器「透明背景色」决定（`view/alpha_bg`，见
  [关键决策 #50](decisions/41-50.md#d50)）：
  - **绿幕 / 品红**：样式表纯色背景（存储值即 CSS 色值）；
  - **棋盘格**（2026-08 新增，[关键决策 #57](decisions/51-60.md#d57)）：
    `CheckerPreviewLabel` 以**固定 8px 格子**平铺（白/浅灰 `#FFFFFF`/`#C8C8C8`），
    图样尺寸**不受预览框尺寸与长宽比影响**（200×200 固定框与 1:1 大画布格子一致）。
- 透明像素直接透出底纹：静态图（QLabel pixmap）、GIF 动画（QMovie）与自定义
  播放器（`GifPreviewPlayer` RGBA 帧）路径均生效。

## 截取节点：起始帧预览

- `TimeTrimNode` / `FrameTrimNode` 运行后（**无需下游格式化**），`backend.extract_start_frame(manifest)`
  物化截取起点帧并写入 `manifest.preview`，预览框直接显示起点帧：
  - time 模式（视频）→ PyAV seek 到起点秒解码；
  - frame 模式：静态序列直接引用 `sources[index]`；GIF 用 PIL `seek(index)`；
    视频按帧号换算秒后 seek 解码。
- 尽力而为：提取失败返回 `None`，保留上游预览不中断运行。
- 语义一致性：下游裁剪节点会基于该起点帧做链式预览，与解码范围一致。

## gif 输入节点：自身预览框直接播放 gif

- `manifest.preview` 仍为首帧静态 PNG（供下游裁剪示意/格式化前预览）；
- 但 `ui.preview_path_for_node` 对 `gif_input` 节点特判：其**自身预览框**直接显示源 gif
  （`QMovie` 播放动画），而非首帧静态图。源 gif 是用户文件，不参与缓存清理。
