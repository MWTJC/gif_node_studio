from __future__ import annotations

import asyncio
import platform
import sys
import tempfile
import time
from pathlib import Path

from loguru import logger

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6 import QtAsyncio, QtGui, QtCore
from .core.paths import APP_NAME, ORG_NAME, user_data_dir
from .core.logging_setup import (
    install_exception_hooks,
    install_hang_diagnostics,
    setup_logging,
)
from .version_info import __version__ as APP_VERSION


# 生产启动引导的退出码：0 = 正常；运行时不可用/加载失败 = 1。QtAsyncio.run
# 在 keep_running=True（默认）下不会返回协程结果（事件循环常驻到关窗），
# 失败退出码经此模块级变量传回 main（见 main 底部注释与 _startup_error）。
_startup_exit_code = 0


def _load_heavy_and_probe():
    """重量级库导入 + 双运行时探测，在**后台线程**执行（asyncio.to_thread）。

    这是「splash 异步化」的核心：主线程在导入期间保持事件循环空闲，QMovie
    启动动画**流畅推进**（不再依赖逐段 processEvents 泵动，也不会因同步
    导入阻塞而冻结）。模块级 import 线程安全（GIL + 导入锁，实测无模块级
    Qt 实例化）；本函数只做「导入 + 探测」，Qt 控件的构造仍在主线程
    （_bootstrap 协程内，await 返回后）。
    """
    from .ui.theme import apply_theme  # noqa: F401（主线程调用，模块已缓存）
    from .ui.ui import MainWindow  # noqa: F401
    from .media.imagemagick import configure_imagemagick
    from .media.gifsicle import configure_gifsicle

    return configure_imagemagick(), configure_gifsicle()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # 自检模式（--test）：与生产启动走完全相同的初始化路径（日志、卡死诊断、
    # QApplication、主题、MainWindow 全构造——节点注册表、全部 UI 构建、设置
    # 恢复、ImageMagick/gifsicle 运行时探测），初始化完毕后打印 SMOKE_OK 并
    # 以 0 退出；任何未捕获异常向上抛出，进程以非 0 退出。自检用临时工作区
    # 与设置文件，不触碰真实 cache/ 与 settings.ini。这是验证「程序可运行」
    # 的唯一自动化手段（tests/test_selftest.py 子进程调用）。
    selftest = "--test" in argv
    if selftest:
        argv = [arg for arg in argv if arg != "--test"]
    # 日志先行：节点执行日志写入用户数据目录 logs/app.log（1 MB 覆盖）。
    setup_logging()
    # 全局异常钩子：Qt 槽/后台线程未捕获异常也写入日志（否则只有弹窗/stderr）。
    install_exception_hooks()
    # 卡死诊断（日志先行后安装）：进程挂起 30s 后自动把全部线程的
    # Python 栈转储到 logs/faulthandler.log，用于定位节点运行莫名卡死。
    install_hang_diagnostics()
    # 生命周期事件：启动（版本/平台/数据目录，排障第一手信息）。
    logger.info(
        "应用启动：版本 {} | Python {} | {} | 数据目录 {} | 模式 {}",
        APP_VERSION,
        platform.python_version(),
        platform.platform(),
        user_data_dir(),
        "自检" if selftest else "生产",
    )
    _t0 = time.monotonic()
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)

    if selftest:
        # 自检：同步路径（无启动画面，离屏无意义且拖慢 CI），初始化路径与
        # 生产一致（主题 → 双运行时探测 → MainWindow 全构造）。
        from .ui.settings_manager import SettingsManager
        from .ui.theme import apply_theme
        from .ui.ui import MainWindow
        from .media.imagemagick import configure_imagemagick
        from .media.gifsicle import configure_gifsicle

        apply_theme(app)
        runtime_im = configure_imagemagick()
        runtime_sicle = configure_gifsicle()
        if not runtime_im.wand_available or not runtime_sicle.available:
            print(
                "RUNTIME_UNAVAILABLE: ImageMagick/gifsicle runtime not found",
                file=sys.stderr,
                flush=True,
            )
            return 1
        tmp = Path(tempfile.mkdtemp(prefix="agif_selftest_"))
        window = MainWindow(
            workspace=str(tmp / "cache"),
            settings=SettingsManager(str(tmp / "settings.ini")),
        )
        window.show()
        window.worker.shutdown()
        logger.info("自检完成：SMOKE_OK（{:.1f}s）", time.monotonic() - _t0)
        print("SMOKE_OK: initialization complete", flush=True)
        return 0

    # ---- 生产：异步启动引导（splash 异步化）----
    # 启动画面必须在**其它库加载之前**就开始显示——本函数顶部的 import 只含
    # PySide6 与轻量模块（core.logging_setup / core.paths）；重量级库
    # （NodeGraphQt / PyAV / wand / PIL / numpy / qtawesome 等）的导入与双
    # 运行时探测全部放进后台线程（_load_heavy_and_probe），主线程事件循环
    # 保持空闲：QMovie 动画在**整个初始化期间**流畅推进（不再逐段
    # processEvents 泵动）。自检模式已在上方返回，不走此路径。
    from .splash import show_startup_splash

    splash = show_startup_splash(app)

    async def _bootstrap() -> None:
        """异步启动引导：后台线程导入/探测 → 主线程构建 UI → 常驻事件循环。"""
        try:
            runtime_im, runtime_sicle = await asyncio.to_thread(_load_heavy_and_probe)
        except Exception:
            logger.exception("启动加载失败")
            _startup_error(
                splash,
                "启动失败",
                "启动时加载组件失败，请查看日志 logs/app.log。\n程序即将退出。",
            )
            return
        if not runtime_im.wand_available or not runtime_sicle.available:
            _startup_error(
                splash,
                "Runtime不可用",
                "Runtime文件缺失："
                + "ImageMagick缺失，" if not runtime_im.wand_available else ""
                + "gifsicle缺失，" if not runtime_sicle.available else ""
                + "\n程序即将退出。",
            )
            return
        try:
            from .ui.theme import apply_theme

            apply_theme(app)
            from .ui.ui import MainWindow

            window = MainWindow()
            icon = QtGui.QIcon()
            icon.addFile(u":/ico/app_icon.ico", QtCore.QSize(), QtGui.QIcon.Mode.Normal, QtGui.QIcon.State.Off)
            window.setWindowIcon(icon)
            window.show()
            # 启动画面随主窗口首帧显示后关闭（finish 等待主窗口真正上屏，过渡无闪断）。
            if splash is not None:
                splash.finish(window)
        except Exception:
            logger.exception("启动引导失败")
            _startup_error(
                splash,
                "启动失败",
                "启动引导出现异常，请查看日志 logs/app.log。\n程序即将退出。",
            )

    # QtAsyncio：asyncio 循环骑在 Qt 事件循环上（生产执行路径由
    # AsyncExecutionWorker 使用）。keep_running=True（默认）：_bootstrap
    # 完成后事件循环**继续运行**（协程结果不返回值），直到关窗
    # （quitOnLastWindowClosed）才退出；启动失败由 _startup_error 显式
    # QApplication.quit() 停止循环，退出码经 _startup_exit_code 传回。
    QtAsyncio.run(_bootstrap())
    # 生命周期事件：退出（退出码 + 运行时长；覆盖正常关窗与启动失败两条路径）。
    logger.info(
        "应用退出：退出码 {}，运行时长 {:.1f}s",
        _startup_exit_code,
        time.monotonic() - _t0,
    )
    return _startup_exit_code


def _startup_error(splash, title: str, text: str) -> None:
    """启动失败收尾：关启动画面 → 弹窗 → 退出（置非 0 退出码）。"""
    global _startup_exit_code
    _startup_exit_code = 1
    if splash is not None:
        splash.close()
    QMessageBox.critical(None, title, text)
    QApplication.quit()
