"""预览控件：Wand 解码的 GIF 逐帧播放器 + 棋盘格透明背景预览标签。"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.color_tokens import DARK
from ..media.image_utils import _wand_rgba_bytes
from ..media.imagemagick import configure_imagemagick, require_wand

class GifPreviewPlayer(QtCore.QObject):
    """Wand 解码的 GIF 逐帧播放器（透明区域与序列预览一致）。

    Qt 的 QMovie 会忽略 GIF 的透明色索引——把透明像素按调色板对应颜色
    **不透明**渲染（实测 transparent.gif：第 2 帧透明区域显示黑色、第 3–4 帧
    显示白色——各帧局部颜色表把索引 0 定义成不同颜色），因此带透明的 GIF
    预览改走本播放器：Wand coalesce 后逐帧直取 RGBA 字节（与格式化解包
    ``_format_animated_image`` 同源），QTimer 按帧延迟驱动换帧。

    仅用于小尺寸 GIF（估算 RGBA 内存 ≤ ``GIF_PREVIEW_MEMORY_BUDGET`` 且帧数
    有限）；大 GIF 仍走 QMovie 流式播放，避免预览卡顿与内存暴涨。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._next)
        self._wand = None
        self._durations: list[int] = []
        self._loop = 1          # 0 = 无限，正数 = 循环次数（调用方已归一化）
        self._count = 0
        self._index = 0
        self._repeats = 0
        self._on_frame = None

    def start(self, path: str, durations: list[int], loop: int, on_frame) -> bool:
        """打开 GIF 并 coalesce；成功后立即回调 ``on_frame(raw_rgba, w, h)`` 显示首帧并启动定时器。

        Wand/ImageMagick 不可用或解码失败时返回 ``False``（调用方回退 QMovie）。
        """
        try:
            require_wand(configure_imagemagick(), "GIF 预览")
            from wand.image import Image as WandImage

            self._wand = WandImage(filename=path)
            self._wand.coalesce()
        except Exception:
            self.stop()
            return False
        self._count = len(self._wand.sequence)
        self._durations = list(durations or [])
        self._loop = loop
        self._index = 0
        self._repeats = 0
        self._on_frame = on_frame
        self._show(0)
        self._schedule()
        return True

    def _show(self, index: int) -> None:
        try:
            raw, width, height = _wand_rgba_bytes(self._wand.sequence[index])
            if self._on_frame is not None:
                self._on_frame(raw, width, height)
        except Exception:
            self._timer.stop()  # 解码失败不再尝试换帧

    def _delay_ms(self) -> int:
        if not self._durations:
            return 100
        return max(10, self._durations[min(self._index, len(self._durations) - 1)])

    def _schedule(self) -> None:
        self._timer.start(self._delay_ms())

    def _next(self) -> None:
        self._index += 1
        if self._index >= self._count:
            self._repeats += 1
            if self._loop and self._repeats >= self._loop:
                self._timer.stop()
                return
            self._index = 0
        self._show(self._index)
        self._schedule()

    def pause(self) -> None:
        """暂停播放：停止帧定时器（当前帧保持显示）。

        窗口最小化/非前台时由面板调用，消除空闲时的换帧解码与重绘开销。
        """
        self._timer.stop()

    def resume(self) -> None:
        """恢复播放：按当前帧延迟重新启动帧定时器。"""
        if self._wand is not None:
            self._schedule()

    def stop(self) -> None:
        self._timer.stop()
        if self._wand is not None:
            try:
                self._wand.close()
            except Exception:
                pass
            self._wand = None




class CheckerPreviewLabel(QtWidgets.QLabel):
    """预览框：自绘背景/棋盘格/边框/内容；1:1 内容按设备像素绘制（决策 #86 终版）。

    高 DPI 问题的根源：QLabel 内建 ``setPixmap`` 把 pixmap 当「UI 元素」按
    **逻辑单位**绘制——256px 的图在 175% 屏幕被放大到 448 设备像素
    （1.75× 插值）→ 边缘模糊。

    最终策略（经用户反复权衡确定）：

    - **优先保证图片内容像素精准**：1:1 内容按设备像素 1:1 绘制（目标 =
      物理÷当前 DPR 的逻辑矩形，关平滑 → 每个源像素恰好对应一个设备像素，
      与 100% 屏渲染逐位相同）；**预览框跟随图片**（框 = 物理÷当前 DPR 的
      逻辑尺寸，图贴满框），不追求跨缩放屏幕的框尺寸一致。
    - **跨屏重建**：拖动窗口发现显示器放大倍率变化后（鼠标松开后），
      MainWindow 按**窗口句柄的实时 DPR** 调 ``refresh_dpr(dpr)`` 重建所有
      1:1 预览——嵌入代理的标签 ``devicePixelRatioF()`` 跨屏后实测不更新
      （重跑/refresh/update 均无效），必须用外部实时值显式传入。
    - **棋盘格/背景/边框维持旧行为**：棋盘格瓦片**不设** devicePixelRatio，
      ``drawTiledPixmap`` 按逻辑单位平铺（随 UI 缩放，用户实测任何缩放下
      都清晰，非本问题）；背景默认深色、边框 1 设备像素，均自绘（样式表
      背景在自绘 paintEvent 下不会参与绘制）。
    - **空载尺寸与原方案一致**：无内容时预览框固定 200×200（1:1 与适配
      模式同）。
    """

    CHECKER_CELL_PX = 8   # 单格边长（逻辑像素）；图样固定逻辑尺寸
    CHECKER_COLORS = DARK.checker  # 暗黑棋盘格
    # CHECKER_COLORS = ("#FFFFFF", "#C8C8C8")  # PS 默认白/浅灰
    DEFAULT_BG = DARK.bg
    DEFAULT_BORDER = DARK.border
    PLACEHOLDER_COLOR = DARK.muted

    # 预览框内容/尺寸变化时发出（节点侧据此重排嵌入式面板）。
    geometry_changed = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._content: QtGui.QPixmap | None = None
        self._mode_1to1 = True
        self._fit_box = QtCore.QSize(200, 200)
        self._fit_upscale = False
        self._bg_color: str | None = None     # None = 默认深色背景
        self._checker_enabled = False
        self._checker_tile: QtGui.QPixmap | None = None
        # 显式 DPR 覆盖（跨屏重建用）：None = 取标签自身 devicePixelRatioF；
        # 窗口跨屏后该值可能不更新（实测），由 MainWindow 以窗口句柄实时
        # DPR 显式设置。粘性：设置后持续生效，直到下次 refresh_dpr 更新。
        self._dpr_override: float | None = None

    # ------------------------------------------------------------------
    # 模式/内容 API（面板统一只喂原始 QPixmap）
    # ------------------------------------------------------------------

    def set_1to1(self, enabled: bool) -> None:
        """1:1 模式：预览框 = 素材物理像素 ÷ 当前 DPR（跟随图片），不缩放不裁剪。"""
        enabled = bool(enabled)
        if enabled != self._mode_1to1:
            self._mode_1to1 = enabled
            self._update_size()
            self.update()

    def set_fit_box(self, w: int, h: int) -> None:
        """适配模式的目标框（逻辑单位）；无内容时预览框固定为该尺寸。"""
        box = QtCore.QSize(int(w), int(h))
        if box != self._fit_box:
            self._fit_box = box
            self.update()
        # 空内容时固定为框尺寸（含 1:1 模式：新建即 200×200，与原方案一致；
        # 不能只在 box 变化时设置——1:1 面板的默认 _fit_box 已是 200×200，
        # 早退会漏掉 setFixedSize，布局按文本 sizeHint 收缩成很小的框）。
        if self._content is None and self.size() != box:
            self.setFixedSize(box)

    def set_fit_upscale(self, enabled: bool) -> None:
        """适配模式下是否允许把小于框的内容放大填满（默认 False=只缩不放）。"""
        enabled = bool(enabled)
        if enabled != self._fit_upscale:
            self._fit_upscale = enabled
            self.update()

    def set_content(self, pixmap: QtGui.QPixmap) -> None:
        """喂入原始 QPixmap（物理像素，dpr=1.0）；尺寸/DPR/绘制全在标签内处理。"""
        self._content = pixmap
        self._update_size()
        self.update()

    def clear_content(self) -> None:
        """清空内容；1:1 模式恢复默认框尺寸（与 release_preview 行为一致）。"""
        had = self._content is not None
        self._content = None
        if self._mode_1to1 and had:
            # 静默复位（不触发 geometry_changed：释放预览时节点多半正在被删除）
            self.setFixedSize(self._fit_box)
        self.update()

    # ------------------------------------------------------------------
    # DPR 取值与跨屏重建
    # ------------------------------------------------------------------

    def _current_dpr(self) -> float:
        """当前生效 DPR：跨屏重建显式传入的窗口 DPR 优先，否则标签自身值。"""
        if self._dpr_override is not None:
            return self._dpr_override
        return self.devicePixelRatioF()

    def refresh_dpr(self, dpr: float) -> None:
        """窗口跨屏/系统缩放变化后重建：按显式 DPR 重算 1:1 框尺寸并重绘。

        MainWindow 检测到显示器放大倍率变化（鼠标松开后）对全部 1:1 面板
        调用。显式传窗口句柄的实时 DPR——嵌入代理的标签 devicePixelRatioF
        跨屏后实测不更新（重跑/refresh/update 均无效），必须用外部实时值。
        """
        self._dpr_override = float(dpr)
        self._update_size()
        self.update()

    def _update_size(self) -> None:
        """按当前模式/内容/DPR 重算固定尺寸；变化时发 geometry_changed。

        1:1 = 素材物理像素 ÷ 当前 DPR（框跟随图片与所在屏幕缩放）；
        仅尺寸变化才 setFixedSize + 发 geometry_changed（GIF 帧尺寸恒定，
        逐帧触发会把每帧变成一次节点重排，空转数分钟有几率 0xC0000005）。
        """
        if self._mode_1to1 and self._content is not None:
            dpr = self._current_dpr()
            new_size = QtCore.QSize(
                int(self._content.width() / dpr + 0.5),
                int(self._content.height() / dpr + 0.5),
            )
            if new_size != self.size():
                self.setFixedSize(new_size)
                self.geometry_changed.emit()
        elif not self._mode_1to1 and self._content is None:
            if self._fit_box != self.size():
                self.setFixedSize(self._fit_box)

    # ------------------------------------------------------------------
    # 棋盘格开关与瓦片（逻辑平铺，随 UI 缩放；不设 DPR——旧行为，用户实测无问题）
    # ------------------------------------------------------------------

    def set_checker_enabled(self, enabled: bool) -> None:
        """开启/关闭棋盘格底纹（只触发重绘，不触碰样式表/内容）。"""
        enabled = bool(enabled)
        if enabled != self._checker_enabled:
            self._checker_enabled = enabled
            self.update()

    def checker_enabled(self) -> bool:
        return self._checker_enabled

    def set_bg_color(self, color: str | None) -> None:
        """纯色背景（CSS 色值）；None = 默认深色。棋盘格开启时优先。"""
        if color != self._bg_color:
            self._bg_color = color
            self.update()

    def _tile(self) -> QtGui.QPixmap:
        """棋盘格瓦片（2×2 格，dpr=1.0 逻辑平铺，惰性生成一次）。"""
        if self._checker_tile is None:
            cell = self.CHECKER_CELL_PX
            tile = QtGui.QPixmap(cell * 2, cell * 2)
            tile.fill(QtGui.QColor(self.CHECKER_COLORS[0]))
            painter = QtGui.QPainter(tile)
            painter.fillRect(0, 0, cell, cell, QtGui.QColor(self.CHECKER_COLORS[1]))
            painter.fillRect(cell, cell, cell, cell, QtGui.QColor(self.CHECKER_COLORS[1]))
            painter.end()
            self._checker_tile = tile
        return self._checker_tile

    # ------------------------------------------------------------------
    # 跨屏 DPR 变化（框 DPR 无关，只需重绘让内容按当前 DPR 重画）
    # ------------------------------------------------------------------

    def event(self, event) -> bool:
        if event.type() == QtCore.QEvent.Type.DevicePixelRatioChange:
            # 窗口跨屏/系统缩放变更：内容按新 DPR 重画（1:1 目标 = 物理÷当前
            # DPR）；框尺寸不变，无需重算几何。
            self.update()
        return super().event(event)

    # ------------------------------------------------------------------
    # 自绘
    # ------------------------------------------------------------------

    def _fit_target(self) -> QtCore.QRectF:
        """适配模式的目标矩形：固定框内保持纵横比 contain（逻辑单位）。"""
        box = self._fit_box
        if self._content is None:
            return QtCore.QRectF(0.0, 0.0, box.width(), box.height())
        cw, ch = self._content.width(), self._content.height()
        if cw <= 0 or ch <= 0 or box.isEmpty():
            return QtCore.QRectF(0.0, 0.0, box.width(), box.height())
        scale = min(box.width() / cw, box.height() / ch)
        if not self._fit_upscale:
            scale = min(scale, 1.0)
        w, h = cw * scale, ch * scale
        x, y = (box.width() - w) / 2.0, (box.height() - h) / 2.0
        return QtCore.QRectF(x, y, w, h)

    def paintEvent(self, event) -> None:
        dpr = self._current_dpr()
        painter = QtGui.QPainter(self)
        # 背景：棋盘格 / 纯色 / 默认深色
        if self._checker_enabled:
            painter.drawTiledPixmap(self.rect(), self._tile())
        else:
            painter.fillRect(self.rect(), QtGui.QColor(self._bg_color or self.DEFAULT_BG))
        # 内容
        if self._content is not None and not self._content.isNull():
            if self._mode_1to1:
                # 1:1：目标 = 物理 ÷ 当前 DPR 的逻辑矩形（图贴满框，框已按同
                # 一 DPR 定尺寸），关闭平滑 → 每个源像素恰好对应一个设备像素
                # （与 100% 屏渲染逐位相同）。
                w = self._content.width() / dpr
                h = self._content.height() / dpr
                painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, False)
                painter.drawPixmap(
                    QtCore.QRectF(0.0, 0.0, w, h),
                    self._content,
                    QtCore.QRectF(self._content.rect()),
                )
            else:
                painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.drawPixmap(
                    self._fit_target(),
                    self._content,
                    QtCore.QRectF(self._content.rect()),
                )
        else:
            # 占位文本（无内容时；QLabel 自带文本绘制不再走 super，避免
            # 旧 pixmap/movie 残留路径与 QSS 背景参与绘制）。
            painter.setPen(QtGui.QColor(self.PLACEHOLDER_COLOR))
            painter.drawText(self.rect(), int(QtCore.Qt.AlignmentFlag.AlignCenter), self.text())
        # 边框：1 设备像素（1.0/dpr 逻辑），直接 drawRect（不内缩；用户实测
        # 边框与本现象无关，仅维持旧版视觉）。
        pen = QtGui.QPen(QtGui.QColor(self.DEFAULT_BORDER))
        pen.setWidthF(1.0 / dpr)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(QtCore.QRectF(0.0, 0.0, self.width(), self.height()))
        painter.end()
