"""日志（loguru，用户数据目录 logs/）与卡死诊断（线程栈转储）。

- ``setup_logging``：loguru 文件 sink，1 MB 轮转保留最新一份；
- ``install_hang_diagnostics``：启动**Python 守护线程**，每 30 秒把全部线程的
  Python 栈转储到 ``logs/faulthandler.log``（文件名沿用 faulthandler 惯例，
  与既有崩溃排查工作流一致）。

为什么不用 ``faulthandler.dump_traceback_later``：实测 Windows 上它要求文件
对象提供 ``fileno()`` 并**绕过 Python 直接写 fd**——无法在 Python 层加时间戳、
也无法挂不稳定检测回调。自建转储用 ``sys._current_frames()`` +
``traceback.format_stack`` 读取各线程最后执行的 Python 帧（与 faulthandler
输出等价；卡死在原生调用内部时显示进入该调用的最后一帧），输出完全可控：
每行带 loguru 风格时间戳，转储后检查 GUI 心跳并通知观察者。

注意：这是**无条件周期转储**（主线程完全正常也会每 30 秒写一次，供崩溃/
卡死后的栈历史分析），真正的「疑似卡死」判定由心跳检查完成。
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from loguru import logger

from .paths import user_data_dir

_configured = False

# ---------------------------------------------------------------------------
# 不稳定检测：GUI 心跳 + 转储观察者
# ---------------------------------------------------------------------------
_fh_observers: list[Callable[[str], None]] = []
_last_heartbeat = time.monotonic()
_heartbeat_lock = threading.Lock()

# 主线程心跳过期阈值（秒）：超过该时长未泵动事件循环视为疑似卡死。
HEARTBEAT_STALE_SECONDS = 30.0

# 转储间隔（秒）与转储线程状态（install 幂等）。
DUMP_INTERVAL_SECONDS = 30.0
# 转储文件大小上限（字节）：超过即轮转为 faulthandler.log.1 并重开新文件
# （只保留最新一份，与 app.log 的 1 MB 轮转约定一致；30s 周期转储在长时间
# 运行时约 12 KB/min ≈ 720 KB/h，1 MB 上限约覆盖 80 分钟栈历史）。
FAULTHANDLER_ROTATE_BYTES = 1024 * 1024
_fh_file: TextIO | None = None
_fh_stop = threading.Event()
_fh_thread: threading.Thread | None = None


def heartbeat() -> None:
    """GUI 线程心跳：事件循环存活即定期调用（MainWindow 的 5s QTimer）。"""
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.monotonic()


def heartbeat_stale(threshold: float = HEARTBEAT_STALE_SECONDS) -> bool:
    """主线程心跳是否过期（= 疑似卡死）。"""
    with _heartbeat_lock:
        return time.monotonic() - _last_heartbeat > threshold


def add_faulthandler_observer(callback: Callable[[str], None]) -> None:
    """注册不稳定通知回调：检测到疑似卡死时调用（参数 = 标记行文本）。

    回调在转储线程执行——UI 侧应发 Qt 信号（自动排队到 GUI 线程）而不是
    直接操作控件。
    """
    _fh_observers.append(callback)


def _now_stamp() -> str:
    """loguru 风格时间戳：YYYY-MM-DD HH:MM:SS.mmm。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _dump_thread_stacks(file: TextIO) -> None:
    """把全部线程的 Python 栈转储到 ``file``，每行前加时间戳。"""
    stamp = _now_stamp()
    file.write(f"{stamp} | Timeout ({_fmt_interval()})! — 线程栈转储（Python watchdog）\n")
    for ident, frame in sorted(sys._current_frames().items()):  # noqa: SLF001 - 标准转储手段
        file.write(f"{stamp} | Thread 0x{ident:08x} (most recent call first):\n")
        for line in "".join(traceback.format_stack(frame)).splitlines():
            file.write(f"{stamp} |   {line}\n")
        file.write("\n")
    file.flush()


def _fmt_interval() -> str:
    minutes, seconds = divmod(int(DUMP_INTERVAL_SECONDS), 60)
    return f"0:00:{seconds:02d}"


def _rotate_if_needed() -> None:
    """转储文件超过大小上限时轮转：faulthandler.log → .1（覆盖旧备份），重开新文件。

    与 app.log 的「1 MB 轮转、只保留最新一份」约定一致，避免长时间运行下
    转储文件无限增长（30s 周期转储 ≈ 12 KB/min）。
    """
    global _fh_file
    if _fh_file is None:
        return
    try:
        if _fh_file.tell() < FAULTHANDLER_ROTATE_BYTES:
            return
        _fh_file.close()
        path = logs_dir() / "faulthandler.log"
        backup = path.with_name("faulthandler.log.1")
        try:
            backup.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            path.rename(backup)
        except OSError:
            pass  # 轮转失败不中断：重开新文件继续写
        _fh_file = path.open("a", encoding="utf-8", buffering=1)
        _fh_file.write(f"{_now_stamp()} | ===== 转储文件轮转（旧内容 → faulthandler.log.1） =====\n")
        _fh_file.flush()
    except Exception:
        logger.exception("faulthandler 转储文件轮转失败（不影响主流程）")


def _watchdog_tick() -> None:
    """单次转储迭代：写转储 → 心跳过期则写标记行并通知观察者。

    独立成函数便于测试（循环本身只是按间隔调用它）。
    """
    if _fh_file is None:
        return
    _dump_thread_stacks(_fh_file)
    if heartbeat_stale():
        stamp = _now_stamp()
        marker = f"{stamp} | !!! 疑似卡死/不稳定：主线程超过 {HEARTBEAT_STALE_SECONDS:.0f}s 未响应，请立即保存 !!!\n"
        try:
            _fh_file.write(marker)
            _fh_file.flush()
        except Exception:
            logger.exception("faulthandler 标记行写入失败")
        for callback in list(_fh_observers):
            try:
                callback(marker)
            except Exception:
                logger.exception("faulthandler 观察者回调异常")


def _watchdog_loop() -> None:
    while not _fh_stop.wait(DUMP_INTERVAL_SECONDS):
        try:
            _rotate_if_needed()
            _watchdog_tick()
        except Exception:
            logger.exception("faulthandler 转储失败（不影响主流程）")


def logs_dir() -> Path:
    """日志目录：用户数据目录下的 ``logs``（开发=包目录，打包=LOCALAPPDATA）。"""
    path = user_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def install_exception_hooks() -> None:
    """全局异常钩子：所有未捕获异常（Qt 槽、后台线程、回调）记录到 loguru。

    - ``sys.excepthook``：主线程/Qt 槽未捕获异常（PySide6 会把槽异常路由到
      它，默认只打印 stderr——用户看到弹窗/闪退却查不到日志）；
    - ``threading.excepthook``：后台线程（asyncio.to_thread 的 executor 线程、
      守护线程等）未捕获异常。

    先写 loguru（含 traceback）再交给默认实现（stderr 打印 / 异常退出），
    进程内幂等：重复调用不重复包装。
    """
    global _exception_hooks_installed
    if _exception_hooks_installed:
        return

    def _log_and_delegate(exc_type, exc, tb) -> None:
        logger.opt(exception=(exc_type, exc, tb)).critical(
            "未捕获异常（{}）", exc_type.__name__
        )
        return sys.__excepthook__(exc_type, exc, tb)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).critical(
            "后台线程未捕获异常（{}）", args.exc_type.__name__
        )
        return threading.__excepthook__(args)

    sys.excepthook = _log_and_delegate
    threading.excepthook = _thread_hook
    _exception_hooks_installed = True
    logger.success("异常钩子已安装（sys.excepthook / threading.excepthook）")


_exception_hooks_installed = False


def setup_logging() -> None:
    """配置 loguru 文件日志（进程内幂等，可重复调用）。

    - 位置：用户数据目录 ``logs/app.log``（打包后为
      ``%LOCALAPPDATA%\\Ghooost\\GIF Node Studio\\logs``，见
      ``paths.user_data_dir``，关键决策 #84）；
    - 轮转：单文件达到 **1 MB** 即轮转并**只保留最新一份**（旧日志被覆盖，
      磁盘占用 ≈ 1 MB）；默认 stderr sink 保留：开发时 `uv run` 终端
      同样可见日志；
    - **降级**：日志目录不可写（数据目录异常）时文件日志禁用、仅 stderr，
      绝不因日志失败阻塞启动（原实现 ``logs_dir().mkdir`` 直接抛
      PermissionError 导致普通用户无法启动）。
    """
    global _configured
    if _configured:
        return
    try:
        logger.add(
            logs_dir() / "app.log",
            rotation="1 MB",
            retention=1,
            encoding="utf-8",
        )
        _configured = True
        logger.success("日志：{}", logs_dir() / "app.log")
    except OSError as exc:
        _configured = True
        logger.warning("日志文件不可用，文件日志已禁用（仅终端输出）：{}", exc)


def install_hang_diagnostics() -> None:
    """卡死诊断：守护线程每 30 秒把全部线程的 Python 栈转储到日志（带时间戳）。

    - 转储用 ``sys._current_frames()`` + ``traceback.format_stack``（与
      faulthandler 输出等价），守护线程不依赖 Qt 事件循环，卡死在
      Wand/ImageMagick 原生调用内部时显示进入该调用的最后一帧；
    - 每行带 ``YYYY-MM-DD HH:MM:SS.mmm | `` 时间戳，崩溃后可按时间回看
      栈演变（栈变化 = 仍在推进或极慢；栈纹丝不动 = 真正挂死）；
    - **疑似卡死判定**：每次转储后检查 GUI 心跳（``heartbeat``），主线程
      超过 30s 未泵动事件循环 → 写 ``!!! 疑似卡死/不稳定 !!!`` 标记行并
      通知观察者（见 ``add_faulthandler_observer``）。

    进程内幂等：重复调用直接返回。
    """
    global _fh_file, _fh_thread
    if _fh_thread is not None:
        return
    try:
        _fh_file = (logs_dir() / "faulthandler.log").open(
            "a", encoding="utf-8", buffering=1
        )
        _fh_stop.clear()
        _fh_thread = threading.Thread(
            target=_watchdog_loop, name="faulthandler-watchdog", daemon=True
        )
        _fh_thread.start()
        logger.success(
            "faulthandler： {}s → logs/faulthandler.log",
            DUMP_INTERVAL_SECONDS,
        )
    except Exception:
        logger.exception("faulthandler 诊断安装失败（不影响启动）")
