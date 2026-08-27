"""QtAsyncio 迁移原型：异步执行计划 runner（独立模块，不触碰生产 ui.py/worker.py）。

目标：证明「编排协程跑在 GUI 线程、重活经 ``to_thread`` 执行、await 后立即
提交」的模式可完整替代现 worker.py + ui.py 的信号编排，且支持：

- 逐节点提交：``await`` 返回后下一步即 UI 提交（无排队信号、无跨线程时序窗口）；
- 协作取消：``cancel_event`` 在进度检查点抛 ``PlanCancelled``（对齐生产
  ``worker.ExecutionCancelled`` 语义，原生卡死依旧只能靠超时兜底）；
- 单步超时看门狗：``asyncio.wait_for`` 超时放弃该步（底层线程可能仍在后台
  运行，随专属 ThreadPoolExecutor 存活——这正是生产 QThread 做不到的
  「卡死可恢复」能力）；
- 多输出端口解析：``resolve_inputs`` 镜像 ``ui._execute_step`` 的
  none / planned / value 三种输入源 + ``MultiOutput`` 按端口名取值。

生产迁移时本模块对应 worker.py 的整体删除 + ui.py 编排段的协程化：
``_run_one`` ≈ 原 ``_execute_step``（缓存快照/清理逻辑在此补回），
``on_step`` 回调 ≈ 原 ``_step_succeeded`` 的 UI 提交主体。
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from ..media.backend import MediaBackend
from ..core.domain import MultiOutput


class PlanCancelled(Exception):
    """协作取消：进度检查点抛出的中断信号（对齐 worker.ExecutionCancelled 语义）。"""


class StepTimedOut(Exception):
    """单步看门狗超时：放弃该步（底层线程可能仍卡在原生调用里，无法强杀）。"""


@dataclass(frozen=True)
class PlanStep:
    node_id: str
    node_class: type
    params: dict[str, Any]
    # 输入源三元组 (source_kind, value, port_name)，镜像 ui._execution_plan：
    #   ("none", None, "")          未连接端口 → None
    #   ("planned", node_id, port)  本次运行的前序节点 → 从 produced 取
    #   ("value", result, port)     已缓存的上游产物 → 直接取值
    upstream: tuple[tuple[str, Any, str], ...] = ()


@dataclass
class StepEvent:
    node_id: str
    kind: str  # started | committed
    elapsed: float = 0.0


def resolve_inputs(step: PlanStep, produced: dict[str, Any]) -> list[Any]:
    """镜像 ui._execute_step 的输入解析：none/planned/value + 多输出端口。"""
    inputs: list[Any] = []
    for source_kind, value, port_name in step.upstream:
        if source_kind == "none":
            inputs.append(None)
            continue
        result = produced[value] if source_kind == "planned" else value
        if isinstance(result, MultiOutput) and port_name:
            inputs.append(result.ports.get(port_name))
        else:
            inputs.append(result)
    return inputs


class AsyncPlanRunner:
    """一次运行 = 一个 asyncio.Task；重活经专属线程池执行，提交发生在编排协程
    （GUI 线程）。调用方负责单飞（同时只允许一个运行任务，等价生产 busy 守卫）。"""

    def __init__(
        self,
        root_backend: MediaBackend,
        *,
        step_timeout: float = 300.0,
        cancel_event: threading.Event | None = None,
        on_step: Callable[[StepEvent], None] | None = None,
        max_workers: int = 2,
    ) -> None:
        self.root_backend = root_backend
        self.step_timeout = step_timeout
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.on_step = on_step if on_step is not None else (lambda event: None)
        # 专属线程池：被超时弃置的卡死线程占用一个槽位也不影响默认执行器，
        # 留一个备用槽位保证「弃置后仍可继续运行」。
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise PlanCancelled()

    def _run_one(self, step: PlanStep, produced: dict[str, Any]) -> Any:
        """线程池内执行（≈ 生产 ui._execute_step 的后端部分，不触碰 Qt）。"""
        self._check_cancelled()
        backend = self.root_backend.for_node(step.node_id, progress_callback=self._report)
        inputs = resolve_inputs(step, produced)
        return step.node_class.execute(inputs, dict(step.params), backend)

    def _report(self, fraction: float | None, label: str) -> None:
        # 进度检查点：协作取消的落点（与生产 report 闭包一致）。
        self._check_cancelled()

    async def run(self, steps: list[PlanStep]) -> dict[str, Any]:
        """按拓扑序逐节点执行；await 后立即提交（GUI 线程），
        失败 / 取消 / 超时分别以异常形式清晰分支。"""
        produced: dict[str, Any] = {}
        loop = asyncio.get_running_loop()
        for step in steps:
            self.on_step(StepEvent(step.node_id, "started"))
            started_at = time.perf_counter()
            future = loop.run_in_executor(self._executor, self._run_one, step, produced)
            try:
                result = await asyncio.wait_for(future, timeout=self.step_timeout)
            except asyncio.TimeoutError:
                # 放弃该步：底层线程无法强杀，随线程池存活（可能仍在写旧 job 目录，
                # 随机目录名不会污染后续运行）。
                raise StepTimedOut(f"步骤 {step.node_id} 超时（>{self.step_timeout}s，疑似卡死）") from None
            self._check_cancelled()  # 提交前最后检查：运行期间用户已取消则丢弃结果
            produced[step.node_id] = result
            self.on_step(StepEvent(step.node_id, "committed", elapsed=time.perf_counter() - started_at))
        return produced

    def close(self) -> None:
        """释放线程池；被超时弃置的卡死线程无法强杀，仅停止接收新任务。"""
        self._executor.shutdown(wait=False, cancel_futures=True)
