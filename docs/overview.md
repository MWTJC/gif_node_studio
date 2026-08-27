# 项目概览

- 用途：面向动漫/视频片段的节点式 GIF 制作器。
- 技术栈：PySide6 + NodeGraphQt 0.6.44 + PyAV + Pillow + Wand（ImageMagick 动态库，不再依赖 `magick.exe` CLI）。
- 中间帧始终为 RGBA PNG；GIF 只在最终导出时量化（避免把 GIF 当中间格式导致反复量化与透明度损失）。
- 运行：`PYTHONPATH=src uv run python -m anime_gif_node_studio`（需 ImageMagick 7 运行时：MagickWand/MagickCore DLL + modules + 配置 XML；见[发行打包](packaging.md)清单）。项目为 virtual project（`[tool.uv] package = false`、无 `[build-system]`），uv 只同步依赖不构建 wheel，故无 `anime-gif-node-studio` 命令、须加 `PYTHONPATH=src`。
- 模块（`src/anime_gif_node_studio/`，按范畴分子包，2026-08 重构，见[关键决策 #68](decisions/61-70.md#d68)）：
  - **包根 = 入口与引导**：`__init__.py`（`main()`）、`__main__.py`（Nuitka 入口）、`app.py`（应用引导）、`splash.py`（启动画面，QSplashScreen + logo.gif 动画，**先于其它库加载**，见[关键决策 #88](decisions/81-90.md#d88)）、`_nuitka_av_shim.py`（PyAV 打包兼容）。
  - `core/` — 核心基础设施与领域模型（无 Qt / 媒体后端依赖）：
    - `domain.py` — 领域模型（`MediaManifest` / `SequenceArtifact` / `CropSpec`）。
    - `options.py` — **参数选项唯一源头**（`ChoiceOption` / `ChoiceGroup`）：把
      choice 参数的三元组「显示标签 / 机器键 / 实际传参值」绑定为一个对象，
      派生 `labels` / `keys` / `values` / 默认值；`RESAMPLE`、`SCALE_STRATEGY`、
      `COLOR_REDUCE_ALGORITHM`、`DITHER` 四个选项组定义于此（详见[关键决策 #51](decisions/51-60.md#d51)）。
    - `paths.py` / `logging_setup.py` — 路径解析（`app_root_dir` 只读程序文件 / `user_data_dir` 可写用户数据 / `node_presets_dir` 项目预设目录，见[关键决策 #84](decisions/81-90.md#d84) 与 [#89](decisions/81-90.md#d89)）与 loguru 文件日志初始化（`logs/app.log`，1 MB 覆盖、只保留最新一份，目录不可写时降级仅终端）。
  - `media/` — 媒体后端（2026-08 收敛为单类，见[关键决策 #99](decisions/91-100.md#d99)）：
    - `backend.py` — `MediaBackend` 单类（实例状态 + 全部行为；格式化/颜色/序列/导出/量化/分析/缓存七职责以区段注释分组，无状态辅助为模块级纯函数）。
    - `palettes.py` — 调色板/阈值图/系统色板辅助（`ORDERED_DITHER_MAPS` / `POSTERIZE_LEVELS` / `system_palette_blob` 等）。
    - `image_utils.py` — wand/PIL 像素与字节转换、缓存 PNG 压缩常量（`PNG_EXPORT_WORKERS` / `_wand_rgba_bytes` 等）。
    - `imagemagick.py` — ImageMagick 运行时探测与环境变量注入（仅 Wand：定位 DLL 目录并验证可用性，不派生 CLI）。
    - `gifsicle.py` — gifsicle 运行时探测与 CLI 参数构造（GIF 优化后端）。
    - `ffmpeg_gif.py` — FFmpeg GIF 管线（PyAV 进程内 palettegen/paletteuse，零新依赖；GIF 合成(FFmpeg)后端，见[关键决策 #100](decisions/91-100.md#d100)）。
    - `media_info.py` — 输出元数据描述（GIF 探测、视频探测、格式化显示）；截取换算用探测（`video_duration_seconds` / `source_frame_count`）。
    - `sequence.py` — 文件名连续数字序列发现。
  - `nodes/` — 节点类（nodeclass，2026-08 拆分，见[关键决策 #82](decisions/81-90.md#d82)）：
    - `definitions.py` — 纯声明层：`PortType` / `NodeCategory` / `PortDefinition` / `ParamDefinition` 及全部参数子类 / `NodeDefinition`（与 Qt 无关）。
    - `node_base.py` — 领域无关节点基座：`StudioNode` / `StudioNodeItem` / `EmbeddedPanelWidget`。
    - `widgets.py` — 输入控件（`SliderSpinBox` / `StudioComboBox` / `FilePathWidget` / `ColorPickerWidget`）。
    - `crop_overlay.py` — `CropOverlayWidget` 裁剪可视化（1:1 交互预览，见[决策 #95](decisions/91-100.md#d95)）。
    - `preview_widgets.py` — `GifPreviewPlayer` GIF 逐帧播放器 + `CheckerPreviewLabel` 棋盘格透明预览。
    - `parameter_panel.py` — `ParameterPanel` 节点参数面板。
    - `input_nodes.py` / `manifest_nodes.py` / `sequence_nodes.py` / `process_nodes.py` / `channel_nodes.py` / `export_nodes.py` / `analysis_nodes.py` — 按类别分组的全部具体节点类。
    - `registry.py` — **唯一注册表 `NODE_CLASSES`**（49 类）+ 派生目录函数（`node_definitions` / `node_class_by_kind` / `node_help_by_kind` / `definition_by_kind`）。
    - `backdrop.py` — 项目版背景框（`EditableBackdropNode`，见[关键决策 #67](decisions/61-70.md#d67)）。
  - `runner/` — 执行编排：
    - `async_worker.py` — 异步执行 worker（`AsyncExecutionWorker`，QtAsyncio 运行中循环编排 + 守护线程池执行重活 + 单步看门狗超时恢复），并在每个节点执行处写 loguru 日志（开始/完成/失败，含 kind 与参数）。
    - `executors.py` — `execute_node(kind, ...)` 兼容边界（当前无调用方，保留给持久化 kind 场景）。
    - `engine.py` — 框架无关的 `ExecutionGraph`/`RuntimeNode`，**当前无任何模块引用**（历史遗留，见[已知限制](limitations.md)）。
    - `async_runner.py` — QtAsyncio 迁移原型（`AsyncPlanRunner` / `PlanStep`）。
  - `ui/` — UI 适配层（2026-08 收敛为单类，见[关键决策 #98](decisions/91-100.md#d98)）：
    - `ui.py` — `MainWindow` 单类（实例状态 + 信号接线 + 全部行为；菜单/节点/视图/执行/预览/存档六职责以区段注释分组，无状态辅助为模块级纯函数）。
    - `session.py` — 节点方案存档保存/旧存档兼容清洗（`save_session_clean` / `sanitize_session_data` / `SessionLoadReport`）。
    - `widgets.py` — 节点说明面板（`HelpWidget`）/ 节点库按钮（`LibraryButton`）/ 底部状态栏（`StatusBar`）。
    - `theme.py` — 全局主题应用（`apply_theme`）。
    - `actions.py` — 动作定义唯一登记处（见[关键决策 #65](decisions/61-70.md#d65)）。
    - `settings_manager.py` — 设置管理器（QSettings，用户数据目录 `settings.ini`，见[关键决策 #84](decisions/81-90.md#d84)）、主题/连线/网格应用函数与设置对话框（`SettingsDialog`）；
      设置项以 `SettingGroup`（`SettingOption`：显示标签/存储值/运行时载荷）为**唯一源头**，
      `THEME` / `ALPHA_BG` 两组定义于此，下拉选项、合法取值、默认值、apply 载荷均由组派生（[关键决策 #51](decisions/51-60.md#d51)）。
    - `hotkeys/` — 画布右键菜单/顶栏共用的 graph 级功能函数（`hotkey_functions.py`）。
