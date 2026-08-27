# 执行链路

1. UI 线程构建**纯执行计划**：`(node_id, 具体类, params, upstream 引用)`（`ui/ui.py` 执行区段的 `_execution_plan`）。
2. `AsyncExecutionWorker.submit_steps` 编排协程（GUI 线程，QtAsyncio 运行中循环）把重活经守护线程池按序执行 `具体类.execute(inputs, params, backend.for_node(node_id))`；执行期间 `backend` 通过注入的 `progress_callback` 把 `(fraction, label)` 经 `progress` 信号上报 UI。
3. 每步发出 `step_started` / `step_succeeded`，UI 线程立即应用产物、`show_preview`、`set_sequence_frames`、状态与耗时；节点库底部共用进度条由 `progress` 信号驱动（fraction 为 `None` 时进度条走忙动画、文本进独立标签——QProgressBar 忙模式下 `text()` 恒空，label 无法经 format 显示，见[关键决策 #57](decisions/51-60.md#d57)）。
   - 输出元数据（`describe_output`，可能探测大 GIF）在**工作线程**计算并随 `TimedResult.metadata` 返回，
     不阻塞 UI，且计入该步骤「上次运行耗时」。
4. 脏传播：参数/连线变化 → 下游全部 `dirty`；`run_to` 只跑脏祖先；`run_from` 跑脏链路。
   - **手动运行**（节点面板 ▶ 按钮）：`run_to(node, clear_preview=True)`，提交前先清空执行链上各节点预览框；
   - **自动模式**（工具栏「自动」）：仅执行到**用户当前调整的节点**为止（不再联动下游）；全局限频每秒最多 3 次（挂起队列 + 单次 QTimer）；**节点报错时不弹窗警告、不关闭自动模式**——错误由底部状态栏显示，节点面板标记 error（用户需求，[关键决策 #50](decisions/41-50.md#d50)）；
   - **手动启用自动模式时**：立即把全部脏节点的链末端排队执行（启动即开始自动运行）；
   - **删除已连接节点时**：执行链中任何脏节点声明了输入端口却无连线则跳过本次自动运行（不报「节点无输入」），重新连线后自动恢复。
5. 失败：`failed` 信号 + 状态置 `error`；不清除上次成功产物；自动模式下报错不弹窗、不关闭自动模式（报错由底部状态栏显示），手动运行仍弹窗（[关键决策 #50](decisions/41-50.md#d50)）。
6. **重跑清理旧缓存**：节点由脏状态重新运行时，`_execute_step` 在执行前快照该节点工作区顶层条目
   （`backend.snapshot_workspace`），执行成功后删除快照中的旧产物（`backend.clear_previous_run`，
   尽力而为、删除失败不中断运行），仅保留被本次运行原地覆盖的固定文件（gif_export 的 `preview.gif`）；
   新产物均为随机 job 目录，不会与旧缓存冲突。失败时旧缓存保留（上次成功产物仍可显示）。
