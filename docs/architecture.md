# 架构分层

| 层 | 模块 | 职责 |
|---|---|---|
| 入口/引导 | 包根 `__init__.py` / `__main__.py` / `app.py` / `_nuitka_av_shim.py` | 进程入口、Nuitka 打包、应用引导（`--test` 自检） |
| 核心（领域模型+基建） | `core/domain.py` / `core/options.py` / `core/paths.py` / `core/logging_setup.py` | 不可变清单/产物描述符、参数选项唯一源头、路径解析（只读程序文件 / 可写用户数据）、日志与卡死诊断 |
| 图语义/执行 | `ui/ui.py` 内建计划（执行区段）+ `runner/async_worker.py` | 脏传播、拓扑执行、异步编排（QtAsyncio 运行中循环 + 守护线程池，`AsyncExecutionWorker`） |
| 媒体后端 | `media/backend.py`（MediaBackend 状态核心 + 七区段薄转发，见决策 #120）/ `media/backend_format.py` 等七个纯函数模块 / `media/palettes.py` / `media/image_utils.py` / `media/imagemagick.py` / `media/media_info.py` | 解码、变换、编码、探测 |
| UI 适配 | `nodes/node_base.py` + `nodes/parameter_panel.py` + `nodes/widgets.py` 等 / `ui/ui.py`（MainWindow 单类，六职责区段分组，见决策 #98） | 节点图形、参数面板、预览、事件分发 |

原则：媒体命令、图遍历、预设序列化不放入 UI 回调；工作线程只调用**类级** `execute`（不触碰 Qt/NodeGraphQt 节点对象），产物回传 UI 线程应用。
