"""启动画面（QSplashScreen + QMovie 动画）。

**设计约束（用户需求）**：启动画面必须在**其它库加载之前**就开始显示——
因此本模块只允许 import PySide6 与轻量模块（``img_resource_rc`` 只含
``PySide6.QtCore`` 资源注册），**禁止** import NodeGraphQt / PyAV / wand /
PIL / numpy / qtawesome 等重量级库；重量级库的加载由 ``app.main()`` 推迟到
启动画面显示之后（见 app.py 注释）。

动画源：``:/ico/logo.gif``（``build_src/img_resource.qrc`` 编译进
``img_resource_rc.py``：128×128、60 帧、20ms、无限循环）。QSplashScreen
默认不透明，logo 带透明通道——必须设 ``WA_TranslucentBackground``，否则
GIF 的透明区域显示为不透明底色。

**异步化（决策 #88 增补）**：启动期间重量级库（NodeGraphQt / PyAV / wand /
PIL / numpy / qtawesome）的导入与运行时探测由 ``app.main()`` 放进**后台
线程**（``asyncio.to_thread``），主线程事件循环保持空闲——QMovie 帧定时器
随事件循环正常触发，动画在整个导入期间**流畅推进**（同步导入会阻塞主线程，
动画只能停在当前帧）。Qt 控件（MainWindow 等）仍在主线程构建（协程 await
返回之后）。

**构造期泵动（决策 #103 增补）**：MainWindow 控件构建段仍是主线程同步块
（实测真实显示 ~1.3s，其中节点定义缓存实例化 46 个节点控件占 ~0.6s），期间
事件循环被占满、动画会冻结。修复：MainWindow 构造阶段边界泵动
``processEvents(ExcludeUserInputEvents)``，且节点定义缓存构建热点内部每 4 个
节点泵动一次（``registry.node_definitions(pump=...)``，见 ui.py
``_pump_startup_events``）——动画在构造全程持续推进，不再冻结。
"""

from __future__ import annotations

from PySide6.QtGui import QPixmap, QMovie
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplashScreen

# 资源注册（qInitResources）：随后经 :/ico/logo.gif 读取 logo 动画。
# 该模块只 import PySide6.QtCore，属于轻量模块，不违反上述约束。
from . import img_resource_rc  # noqa: F401

# logo 动画资源路径（build_src/img_resource.qrc 中 ico/logo.gif）。
LOGO_RESOURCE = ":/ico/logo.gif"


class StartupSplash(QSplashScreen):
    """带 GIF 动画的启动画面。

    动画驱动：QMovie 帧切换 → ``setPixmap`` → 显式 ``repaint()``
    （QSplashScreen 不会自动重绘，只更新 pixmap 不会触发绘制）。
    """

    def __init__(self, movie_path: str = LOGO_RESOURCE) -> None:
        super().__init__(QPixmap(movie_path))
        # logo 带透明通道：不设 WA_TranslucentBackground 时透明区域呈黑/白底。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self._movie = QMovie(movie_path)
        self._movie.frameChanged.connect(self._on_frame_changed)
        self._movie.start()

    # --- C++ 虚函数/槽（类体定义，勿移入 mixin，见 ui.py 类体注释约定） ---

    def _on_frame_changed(self, _frame: int) -> None:
        self.setPixmap(self._movie.currentPixmap())
        self.repaint()

    def is_ready(self) -> bool:
        """logo 资源是否成功加载；资源缺失时返回 False（调用方跳过启动画面，不阻塞启动）。"""
        return not self.pixmap().isNull()

    def stop_animation(self) -> None:
        self._movie.stop()


def show_startup_splash(app) -> StartupSplash | None:
    """创建并显示启动画面；logo 资源缺失时返回 None（启动画面为增强体验，不硬性依赖）。

    显示后立即 ``processEvents`` 泵动一次，保证首帧在重量级库加载前上屏。
    """
    splash = StartupSplash()
    if not splash.is_ready():
        return None
    splash.show()
    splash.raise_()
    app.processEvents()
    return splash
