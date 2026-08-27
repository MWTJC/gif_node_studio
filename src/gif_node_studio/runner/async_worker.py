"""异步执行 worker（QtAsyncio 版，生产执行路径；替代原 QThread 版 worker.py）。

架构：
- **编排协程跑在 GUI 线程的事件循环**（app.py 的 QtAsyncio.run 提供
  QAsyncioEventLoop）：逐节点执行、``await`` 返回后立即同步提交（信号直连，
  槽在发出线程同步执行）——消除「工作线程发信号、UI 线程排队处理」的并发
  窗口，UI 提交与下一步执行不再重叠；
- **重活经守护线程池执行**（``ui._execute_step`` → ``node_class.execute``，
  类级方法不触碰 Qt；与旧版工作线程语义一致）；元数据探测（``describe_output``）
  同在线程池内完成，不计入 UI 线程；
- **单步看门狗**：``loop.call_later`` 超时 → 放弃该步（底层守护线程无法强杀，
  但**不阻塞进程退出**）→ ``watchdog_timeout`` + ``failed`` 路径，busy 复位——
  把旧版「单 QThread 卡死即永久瘫痪」降级为「本次失败、可重试」；
- **取消**：``cancel_event`` 协作检查点（对齐旧 ``ExecutionCancelled`` 语义）
  + ``task.cancel()`` 兜底。

信号契约与旧 ExecutionWorker 完全一致，ui.py 的信号连接无需改动。
测试环境兼容：无 QtAsyncio.run 时由 tests/conftest.py 把 QAsyncioEventLoop
安装为当前事件循环，``processEvents()`` 泵动它（call_soon → QTimer.singleShot），
因此现有「手动 pump」测试无需改造。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6 import QtCore

from ..core.logging_setup import logger
from ..media.media_info import describe_output


class ExecutionCancelled(Exception):
    """协作取消：进度检查点抛出的中断信号（语义与旧 worker 一致）。"""


class StepTimedOut(Exception):
    """单步看门狗超时：放弃该步（底层守护线程仍可能在后台运行）。"""


# 单步看门狗超时（秒）：步骤的合法耗时可能很长（如 4K·256 色量化 >120s，
# 见 docs/limitations.md），默认给足余量；超过即视为「疑似卡死」。
STEP_WATCHDOG_SECONDS = 300


@dataclass(frozen=True)
class TimedResult:
    value: Any
    elapsed_seconds: float
    metadata: dict[str, Any] | None = field(default=None)


class DaemonThreadPool:
    """守护线程池（ThreadPoolExecutor 的替代）。

    ThreadPoolExecutor 的线程是非 daemon 的：被超时弃置的卡死线程会在解释器
    退出时挂死 join。本池用 daemon 线程，弃置后随进程退出，不阻塞关闭。
    ``submit`` 返回 ``concurrent.futures.Future``，满足 asyncio 事件循环
    ``run_in_executor`` 对 executor 的接口要求（任意实现 ``submit`` 的对象）。
    """

    def __init__(self, max_workers: int = 2, name: str = "async-worker") -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._closed = False
        for index in range(max_workers):
            thread = threading.Thread(
                target=self._run, name=f"{name}-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _run(self) -> None:
        while True:
            work = self._queue.get()
            if work is None:
                return
            future, fn, args = work
            try:
                result = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - 原样回传线程异常
                exc_to_set = exc
            else:
                exc_to_set = None
            try:
                if exc_to_set is None:
                    future.set_result(result)
                else:
                    future.set_exception(exc_to_set)
            except concurrent.futures.InvalidStateError:
                # 已被 await 侧放弃（取消/超时）：结果无人消费，忽略。
                pass

    def submit(self, fn: Callable[..., Any], *args: Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        if self._closed:
            future.set_exception(RuntimeError("executor closed"))
            return future
        self._queue.put((future, fn, args))
        return future

    def shutdown(self) -> None:
        """停止接收新任务并让线程跑完当前工作后退出（不等待、不强杀）。"""
        self._closed = True
        for _ in self._threads:
            self._queue.put(None)


class AsyncExecutionWorker(QtCore.QObject):
    started = QtCore.Signal(str)
    succeeded = QtCore.Signal(str, object)
    failed = QtCore.Signal(str, str)
    cancelled = QtCore.Signal(str)
    rejected = QtCore.Signal(str)
    step_started = QtCore.Signal(str, str)
    step_succeeded = QtCore.Signal(str, str, object)
    operation_timing = QtCore.Signal(str, float)
    progress = QtCore.Signal(str, object, str)
    # 看门狗：单步超时（疑似卡死）时发出；随后走 failed 路径，busy 自动复位。
    watchdog_timeout = QtCore.Signal(str)

    def __init__(self, parent=None, step_timeout: float = STEP_WATCHDOG_SECONDS) -> None:
        super().__init__(parent)
        self.step_timeout = step_timeout
        self._cancel_event = threading.Event()
        self._busy = False
        self._task: asyncio.Task | None = None
        self._request_id: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timed_out = False
        # max_workers=2：被超时弃置的线程占用一个槽位，留一个备用槽位保证
        # 「弃置后仍可继续运行」。
        self._pool = DaemonThreadPool(max_workers=2, name="async-worker")

    # ------------------------------------------------------------------
    # 运行状态
    # ------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def cancel(self) -> None:
        """请求立即停止：置位协作取消事件 + 兜底取消当前任务。

        线程池线程无法强杀：正在执行的步骤在下一个进度检查点（report）抛出
        ``ExecutionCancelled`` 中断；若卡在原生调用里，则由看门狗超时兜底。
        """
        self._cancel_event.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------

    def submit(self, request_id: str, operation: Callable[[Any], Any]) -> bool:
        """单操作运行（兼容旧 API；生产主路径为 submit_steps）。"""
        return self._submit(request_id, operation, steps_mode=False)

    def submit_steps(
        self,
        request_id: str,
        steps: list[Any],
        operation: Callable[[Any, dict[str, Any], Callable], Any],
    ) -> bool:
        """提交顺序计划：逐节点执行、逐节点同步提交。"""
        return self._submit(request_id, (steps, operation), steps_mode=True)

    def _submit(self, request_id: str, payload: Any, *, steps_mode: bool) -> bool:
        if self._busy:
            self.rejected.emit(request_id)
            return False
        self._busy = True
        self._cancel_event.clear()
        self._timed_out = False
        self._request_id = request_id
        # 惰性捕获循环：生产环境（QtAsyncio.run 内）为运行中的 QAsyncioEventLoop；
        # 测试环境为 conftest 安装的、经 processEvents 泵动的 QAsyncioEventLoop。
        self._loop = self._current_loop()
        self.started.emit(request_id)
        if steps_mode:
            steps, operation = payload
            task = self._loop.create_task(
                self._run_steps(request_id, steps, operation)
            )
        else:
            task = self._loop.create_task(self._run(request_id, payload))
        self._task = task
        task.add_done_callback(self._on_task_done)
        return True

    @staticmethod
    def _current_loop() -> asyncio.AbstractEventLoop:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.get_event_loop()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise ExecutionCancelled()

    def _report(self, request_id: str, fraction: float | None, label: str) -> None:
        self._check_cancelled()
        self.progress.emit(request_id, fraction, label)

    @staticmethod
    def _step_label(step) -> str:
        """步骤的日志标签：节点标题 + 具体类名。"""
        node_class = step[1]
        name = getattr(node_class, "NODE_NAME", None) or getattr(
            node_class, "__name__", str(node_class)
        )
        return (
            f"[{step[0]}] {name}({getattr(node_class, '__name__', type(node_class).__name__)})"
        )

    def _run_with_metadata(self, operation, step, produced, report):
        """线程池内执行（与旧 _Runner 的工作线程语义一致）：execute + 元数据探测。"""
        result = operation(step, produced, report)
        # 传入节点类：元数据展示由节点自身定义（describe_output 继承实现），
        # 无特殊需求时回落默认行为；导出类节点覆写后仅显示关键信息。
        metadata = describe_output(result, step[1])
        return result, metadata

    def _run_single_with_metadata(self, operation, report):
        result = operation(report)
        metadata = describe_output(result)
        return result, metadata

    async def _await_step(self, fut: asyncio.Future) -> tuple[Any, dict[str, Any]]:
        """等待单步结果；看门狗超时与任务取消在此区分。"""
        timeout_handle = self._loop.call_later(
            self.step_timeout, self._on_step_timeout, fut
        )
        try:
            try:
                return await fut
            except asyncio.CancelledError:
                if self._timed_out:
                    raise StepTimedOut() from None
                raise
        finally:
            timeout_handle.cancel()

    def _on_step_timeout(self, fut: asyncio.Future) -> None:
        """GUI 线程（loop.call_later → QTimer → 槽）：标记超时并放弃该步。"""
        if not fut.done():
            self._timed_out = True
            fut.cancel()

    def _on_task_done(self, task: asyncio.Task) -> None:
        """任务完成兜底（GUI 线程，经 loop.call_soon 泵动时执行）。

        协程体正常路径已在 try/finally 内自行发出终态信号并复位 busy
        （此时 ``self._task`` 已置 None，本回调直接返回）。唯一需要兜底的是
        **任务在协程体启动前被取消**（cancel() 发生在首次泵动之前）——
        task.cancel() 直接取消未启动的任务，协程的 finally 不会运行。
        """
        if self._task is not task:
            return  # 协程体已自行处理
        request_id = self._request_id or ""
        if task.cancelled():
            logger.warning("任务在启动前被取消（async，request={}）", request_id)
            self.cancelled.emit(request_id)
        else:
            exc = task.exception()
            if exc is not None:
                logger.exception("任务未捕获异常（async，request={}）：{}", request_id, exc)
                self.failed.emit(request_id, str(exc))
        self._busy = False
        self._task = None

    # ------------------------------------------------------------------
    # 编排协程（GUI 线程）
    # ------------------------------------------------------------------

    async def _run_steps(self, request_id: str, steps: list[Any], operation) -> None:
        produced: dict[str, Any] = {}
        try:
            logger.info(
                "开始执行计划（async，request={}）：共 {} 个节点",
                request_id, len(steps),
            )
            for step in steps:
                self._check_cancelled()
                node_id = step[0]
                label = self._step_label(step)
                self.step_started.emit(request_id, node_id)
                started_at = time.perf_counter()
                logger.info("节点开始执行 {} params={}", label, step[2])

                def report(fraction: float | None, label: str) -> None:
                    self._report(request_id, fraction, label)

                fut = self._loop.run_in_executor(
                    self._pool, self._run_with_metadata, operation, step, produced, report
                )
                result, metadata = await self._await_step(fut)
                self._check_cancelled()  # 提交前最后检查：运行期间已取消则丢弃结果
                produced[node_id] = result
                elapsed = time.perf_counter() - started_at
                logger.info("节点执行完成 {} 耗时={:.3f}s", label, elapsed)
                self.step_succeeded.emit(
                    request_id, node_id, TimedResult(result, elapsed, metadata)
                )
            logger.info("执行计划完成（async，request={}）：{} 个节点全部成功", request_id, len(steps))
            self.succeeded.emit(request_id, produced)
        except ExecutionCancelled:
            logger.warning("执行计划被取消（async，request={}）", request_id)
            self.cancelled.emit(request_id)
        except StepTimedOut:
            logger.warning("看门狗超时（async，request={}）", request_id)
            self.watchdog_timeout.emit(request_id)
            self.failed.emit(
                request_id,
                f"运行超时（疑似卡死，单步超过 {self.step_timeout:g}s）；"
                "logs/faulthandler.log 可定位卡死操作，请重试。",
            )
        except asyncio.CancelledError:
            logger.warning("执行计划被取消（async，request={}）", request_id)
            self.cancelled.emit(request_id)
        except Exception as exc:
            logger.exception("执行计划失败（async，request={}）：{}", request_id, exc)
            self.failed.emit(request_id, str(exc))
        finally:
            self._busy = False
            self._task = None

    async def _run(self, request_id: str, operation) -> None:
        try:
            self._check_cancelled()
            started_at = time.perf_counter()
            logger.info("开始执行（async，request={}）", request_id)

            def report(fraction: float | None, label: str) -> None:
                self._report(request_id, fraction, label)

            fut = self._loop.run_in_executor(
                self._pool, self._run_single_with_metadata, operation, report
            )
            result, metadata = await self._await_step(fut)
            elapsed = time.perf_counter() - started_at
            logger.info("执行完成（async，request={}）耗时={:.3f}s", request_id, elapsed)
            self.operation_timing.emit(request_id, elapsed)
            self.succeeded.emit(request_id, TimedResult(result, elapsed, metadata))
        except ExecutionCancelled:
            logger.warning("执行被取消（async，request={}）", request_id)
            self.cancelled.emit(request_id)
        except StepTimedOut:
            logger.warning("看门狗超时（async，request={}）", request_id)
            self.watchdog_timeout.emit(request_id)
            self.failed.emit(
                request_id,
                f"运行超时（疑似卡死，单步超过 {self.step_timeout:g}s）；"
                "logs/faulthandler.log 可定位卡死操作，请重试。",
            )
        except asyncio.CancelledError:
            logger.warning("执行被取消（async，request={}）", request_id)
            self.cancelled.emit(request_id)
        except Exception as exc:
            logger.exception("执行失败（async，request={}）：{}", request_id, exc)
            self.failed.emit(request_id, str(exc))
        finally:
            self._busy = False
            self._task = None

    def shutdown(self) -> None:
        """停止运行并释放线程池。不等待：被弃置的线程是守护线程，随进程退出。"""
        self._cancel_event.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._pool.shutdown()
