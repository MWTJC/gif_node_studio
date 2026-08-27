"""runner：执行编排层。

- engine.py       —— 框架无关的 ExecutionGraph（拓扑执行，历史遗留未使用）
- executors.py    —— execute_node(kind, …) 兼容边界
- async_worker.py —— 生产异步执行 worker（AsyncExecutionWorker）
- async_runner.py —— QtAsyncio 迁移原型（AsyncPlanRunner）
"""
