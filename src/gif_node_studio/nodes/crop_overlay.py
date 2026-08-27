"""画面裁剪可视化：源图上绘制红色裁剪线，拖拽实时调整（百分比参数接管）。

``CropOverlayWidget`` 是裁剪交互画布：源图上绘制四条红色裁剪线，支持鼠标
拖动调整裁剪范围（归一化值，与 ``CropSpec`` 一致）。

``CropOverlayPanel`` 是接管面板（决策 #109）：容器内嵌画布 + 结果缩略图 +
数值只读，并联动纵横比下拉（``CropOverlayParam.linked``），作为接管控件
直接在节点 params 中声明使用（替代旧面板 ``crop_overlay=True`` 构造标志）。
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.color_tokens import DARK
from .definitions import ChoiceParam

class CropOverlayWidget(QtWidgets.QWidget):
    """可视化裁剪控件：在源图上绘制四条红色裁剪线，支持鼠标拖动调整裁剪范围。

    - 坐标约定：内部状态为归一化值（0~1），与 ``CropSpec`` 一致；
    - 显示模式：
      * ``1:1``（默认，``set_1to1(True)``）：预览框 = 素材物理像素 ÷ 当前
        DPR（跟随图片，不缩放不裁剪），内容按设备像素 1:1 绘制（关平滑）——
        与 ``CheckerPreviewLabel`` 的 1:1 终版语义一致（见预览 DPI 决策 #86），
        裁剪交互直接面对原始像素；跨屏后由 MainWindow 按窗口实时 DPR 调
        ``refresh_dpr(dpr)`` 重建框尺寸；
      * 适配（``set_1to1(False)``）：图像等比适配到固定 200×200 控件内
        （可放大也可缩小，居中），旧裁剪节点的交互约定；
    - 交互：拖动四条边/四个角调整裁剪区；在框内拖动整体平移；框外压暗即
      「被裁掉的部分」，结果由上层缩略图实时展示；
    - 纵横比锁定：``set_aspect_ratio()`` 传入宽/高比值（None = 自由）后，
      拖边/拖角/平移均维持该比例（角跟随鼠标主方向、边按比例推导、
      越界时按比例收缩并整体平移回界内），切换比例时当前框保持中心与面积
      重投影到新比例；
    - 信号：拖拽期间实时发 ``values_changed``（归一化字典）；手势开始/结束
      发 ``gesture_begin`` / ``gesture_end``，供上层把整个手势折叠为一条
      撤销记录（见 ``MainWindow._param_gesture_*``）；预览框尺寸变化发
      ``geometry_changed``（1:1 框跟随图片，节点据此重排）。
    """

    values_changed = QtCore.Signal(dict)
    gesture_begin = QtCore.Signal()
    gesture_end = QtCore.Signal()
    geometry_changed = QtCore.Signal()

    SIZE = 200  # 空载/适配模式固定边长（与 ParameterPanel.PREVIEW_SIZE 一致）
    EDGE_TOLERANCE = 6.0   # 命中边线的像素容差
    MIN_EDGE_PX = 2.0      # 裁剪区最小边长（屏幕像素），防止拖成空区
    HANDLE_SIZE = 7.0      # 四角手柄边长

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self._pixmap: QtGui.QPixmap | None = None
        self._values: dict[str, float] = {"left": 0.0, "top": 0.0, "right": 1.0, "bottom": 1.0}
        self._active: str | None = None          # 当前拖拽目标（边/角/move）
        self._press_norm: tuple[float, float] | None = None
        self._press_values: dict[str, float] | None = None
        self._aspect_ratio: float | None = None  # 锁定纵横比（宽/高）；None = 自由
        self._aspect_label: str | None = None    # 纵横比显示文本（如 "16:9"），None 时按数值生成
        # 1:1 模式（默认）：预览框 = 素材物理像素 ÷ 当前 DPR，跟随图片；
        # 适配模式：固定 200×200，图像等比适配（可放大可缩小）。
        self._mode_1to1 = True
        # 显式 DPR 覆盖（跨屏重建用）：None = 取控件自身 devicePixelRatioF；
        # 窗口跨屏后该值可能不更新（实测），由 MainWindow 以窗口句柄实时
        # DPR 显式设置（与 CheckerPreviewLabel 同约定）。粘性：设置后持续生效。
        self._dpr_override: float | None = None

    # ------------------------------------------------------------------
    # 坐标映射（纯函数，便于无 Qt 事件测试）
    # ------------------------------------------------------------------

    @staticmethod
    def fitted_rect(widget_size: QtCore.QSize, image_size: QtCore.QSize) -> QtCore.QRectF:
        """图像等比适配到控件内的绘制区域（KeepAspectRatio、居中）。"""
        if widget_size.isEmpty() or image_size.isEmpty():
            return QtCore.QRectF()
        scale = min(
            widget_size.width() / image_size.width(),
            widget_size.height() / image_size.height(),
        )
        width = image_size.width() * scale
        height = image_size.height() * scale
        x = (widget_size.width() - width) / 2.0
        y = (widget_size.height() - height) / 2.0
        return QtCore.QRectF(x, y, width, height)

    @staticmethod
    def widget_to_normalized(pos: QtCore.QPointF, fitted: QtCore.QRectF) -> tuple[float, float]:
        """控件坐标 → 归一化图像坐标（越界钳制到 0~1）。"""
        if fitted.isEmpty():
            return 0.0, 0.0
        nx = (pos.x() - fitted.x()) / fitted.width()
        ny = (pos.y() - fitted.y()) / fitted.height()
        return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))

    @staticmethod
    def normalized_to_widget(nx: float, ny: float, fitted: QtCore.QRectF) -> QtCore.QPointF:
        if fitted.isEmpty():
            return QtCore.QPointF()
        return QtCore.QPointF(fitted.x() + nx * fitted.width(), fitted.y() + ny * fitted.height())

    @staticmethod
    def crop_rect(values: dict, image_size: QtCore.QSize) -> QtCore.QRect:
        """归一化裁剪值 → 像素矩形（钳制到图像范围，至少 1px）。"""
        width = max(1, image_size.width())
        height = max(1, image_size.height())
        left = max(0.0, min(1.0, float(values.get("left", 0.0))))
        top = max(0.0, min(1.0, float(values.get("top", 0.0))))
        right = max(0.0, min(1.0, float(values.get("right", 1.0))))
        bottom = max(0.0, min(1.0, float(values.get("bottom", 1.0))))
        if right <= left:
            right = min(1.0, left + 1.0 / width)
        if bottom <= top:
            bottom = min(1.0, top + 1.0 / height)
        return QtCore.QRect(
            round(left * width),
            round(top * height),
            max(1, round((right - left) * width)),
            max(1, round((bottom - top) * height)),
        )

    # ------------------------------------------------------------------
    # 状态读写
    # ------------------------------------------------------------------

    def set_image(self, path: str | Path | None) -> None:
        """设置源图（None 清空）。保存全分辨率位图，供结果缩略图实时裁剪。"""
        if not path:
            self._pixmap = None
        else:
            pixmap = QtGui.QPixmap(str(path))
            self._pixmap = pixmap if not pixmap.isNull() else None
        # 换图后归一化空间的目标宽高比换算变化（r_norm = aspect × h/w）：
        # 纵横比锁定时把当前框重投影，保证像素空间的裁剪比例不随源图尺寸漂移。
        if self._aspect_ratio is not None and self._pixmap is not None:
            self._values = self._project_to_aspect(self._values)
        self._update_size()
        self.update()

    # ------------------------------------------------------------------
    # 1:1 模式与跨屏 DPR 重建（与 CheckerPreviewLabel 同约定，见预览 DPI 决策 #86）
    # ------------------------------------------------------------------

    def set_1to1(self, enabled: bool) -> None:
        """切换显示模式：True = 1:1（框 = 素材物理像素 ÷ DPR，跟随图片）；
        False = 适配（固定 200×200，图像等比适配）。"""
        enabled = bool(enabled)
        if enabled != self._mode_1to1:
            self._mode_1to1 = enabled
            self._update_size()
            self.update()

    def _current_dpr(self) -> float:
        """当前生效 DPR：跨屏重建显式传入的窗口 DPR 优先，否则控件自身值。"""
        if self._dpr_override is not None:
            return self._dpr_override
        return self.devicePixelRatioF()

    def refresh_dpr(self, dpr: float) -> None:
        """窗口跨屏/系统缩放变化后重建 1:1 预览：按显式 DPR 重算框尺寸并重绘。

        显式传窗口句柄的实时 DPR——嵌入代理的控件 devicePixelRatioF 跨屏后
        实测不更新，必须用外部实时值（与 CheckerPreviewLabel 同约定）。
        """
        self._dpr_override = float(dpr)
        self._update_size()
        self.update()

    def _content_rect(self) -> QtCore.QRectF:
        """内容绘制/坐标映射矩形（逻辑单位）：

        - 1:1 模式：素材物理像素 ÷ 当前 DPR 的精确矩形（``(0, 0, w/dpr,
          h/dpr)``），与绘制一致；控件尺寸为取整后的同一矩形，映射与绘制
          不因取整错位；
        - 适配模式：``fitted_rect``（等比适配、居中）。
        """
        if self._mode_1to1 and self._pixmap is not None:
            dpr = self._current_dpr()
            return QtCore.QRectF(
                0.0, 0.0,
                self._pixmap.width() / dpr,
                self._pixmap.height() / dpr,
            )
        return self.fitted_rect(self.size(), self._image_size())

    def _update_size(self) -> None:
        """按当前模式/内容/DPR 重算固定尺寸；变化时发 geometry_changed。

        1:1 = 素材物理像素 ÷ 当前 DPR（框跟随图片与所在屏幕缩放）；无内容
        或适配模式维持固定 200×200。仅尺寸变化才 setFixedSize + 发信号
        （避免节点重排风暴）。
        """
        if self._mode_1to1 and self._pixmap is not None:
            dpr = self._current_dpr()
            new_size = QtCore.QSize(
                int(self._pixmap.width() / dpr + 0.5),
                int(self._pixmap.height() / dpr + 0.5),
            )
            if new_size != self.size():
                self.setFixedSize(new_size)
                self.geometry_changed.emit()
        elif self.size() != QtCore.QSize(self.SIZE, self.SIZE):
            self.setFixedSize(self.SIZE, self.SIZE)

    # ------------------------------------------------------------------
    # 纵横比锁定
    # ------------------------------------------------------------------

    def set_aspect_ratio(self, ratio: float | None, label: str | None = None) -> None:
        """设置裁剪框纵横比锁定（``ratio`` = 宽/高 比值，None 解除锁定）。

        - 传入 ``label`` 作为裁剪框内角标的显示文本（如 "16:9"）；
        - 锁定时把当前裁剪框按「保持中心与面积」重投影到目标比例，
          未锁定（自由）时保持原框不动。
        """
        ratio = None if ratio is None else float(ratio)
        if ratio == self._aspect_ratio and label == self._aspect_label:
            return
        self._aspect_ratio = ratio
        self._aspect_label = label
        if ratio is not None:
            self._values = self._project_to_aspect(self._values)
        self.update()

    def aspect_ratio(self) -> float | None:
        """当前锁定纵横比（None = 自由）。"""
        return self._aspect_ratio

    def _aspect_norm(self) -> float | None:
        """归一化空间的目标宽高比（把像素比例换算到 0~1 坐标）。

        像素宽高比 = (w_norm·img_w) / (h_norm·img_h) = (w_norm/h_norm)·(img_w/img_h)，
        令其等于锁定比例 → w_norm/h_norm = ratio·img_h/img_w。
        """
        if self._aspect_ratio is None:
            return None
        size = self._image_size()
        if size.isEmpty() or size.width() <= 0:
            return None
        return self._aspect_ratio * size.height() / size.width()

    def _project_to_aspect(self, values: dict) -> dict:
        """把裁剪框重投影到当前锁定纵横比（保持中心与面积；钳制边界与最小跨度）。

        无锁定（自由）时退化为普通钳制（``_clamped_values``）。
        """
        r = self._aspect_norm()
        if r is None:
            return self._clamped_values(values)
        min_w, min_h = self._min_span()
        area = max(
            (values["right"] - values["left"]) * (values["bottom"] - values["top"]),
            min_w * min_h,
        )
        # 目标尺寸：w·h = area 且 w = r·h；同时满足两个方向的最小跨度与 0~1 边界。
        w = math.sqrt(area * r)
        h = w / r
        max_w = min(1.0, r)  # w ≤ 1 且 h = w/r ≤ 1
        if w > max_w:
            w = max_w
            h = w / r
        if w < min_w or h < min_h:
            w = max(min_w, min_h * r)
            h = w / r
        cx = (values["left"] + values["right"]) / 2.0
        cy = (values["top"] + values["bottom"]) / 2.0
        min_cx, max_cx = w / 2.0, 1.0 - w / 2.0
        min_cy, max_cy = h / 2.0, 1.0 - h / 2.0
        if min_cx > max_cx:  # 防御：理论不可达（w 接近 1 时）
            min_cx = max_cx = 0.5
        if min_cy > max_cy:
            min_cy = max_cy = 0.5
        cx = max(min_cx, min(max_cx, cx))
        cy = max(min_cy, min(max_cy, cy))
        return {
            "left": cx - w / 2.0,
            "top": cy - h / 2.0,
            "right": cx + w / 2.0,
            "bottom": cy + h / 2.0,
        }

    @staticmethod
    def _aspect_step(
        values: dict, active: str, nx: float, ny: float, r: float, min_w: float, min_h: float,
        press_norm: tuple[float, float] | None = None,
    ) -> dict:
        """纵横比锁定下的一次拖拽步进（纯函数，便于无 Qt 事件测试）。

        - move：平移（尺寸不变，比例自然保持），位移 = 鼠标相对按下点的偏移；
        - 边：对边锚定，尺寸按比例推导；越界时按比例收缩并整体平移回界内；
        - 角：以对角为锚点，框始终包住鼠标（w = max(|dx|, |dy|·r)），
          越界时整体平移回界内（尺寸不变，比例保持）。
        """
        if active == "move":
            span_w = values["right"] - values["left"]
            span_h = values["bottom"] - values["top"]
            press_x, press_y = press_norm if press_norm is not None else (values["left"], values["top"])
            dx = nx - press_x
            dy = ny - press_y
            left = max(0.0, min(1.0 - span_w, values["left"] + dx))
            top = max(0.0, min(1.0 - span_h, values["top"] + dy))
            return {"left": left, "top": top, "right": left + span_w, "bottom": top + span_h}

        if active in ("left", "right"):
            h = values["bottom"] - values["top"]
            w = h * r
            max_w = min(1.0, r)  # 收缩上限：w ≤ 1 且 h = w/r ≤ 1
            if active == "left":
                w = min(w, max_w, values["right"])  # left ≥ 0
                left = values["right"] - w
                right = values["right"]
            else:
                w = min(w, max_w, 1.0 - values["left"])
                left = values["left"]
                right = left + w
            h = w / r  # 宽度受限时高度同步收缩（比例保持）
            cy = (values["top"] + values["bottom"]) / 2.0
            top = cy - h / 2.0
            bottom = cy + h / 2.0
            if top < 0.0:
                bottom -= top
                top = 0.0
            if bottom > 1.0:
                top -= bottom - 1.0
                bottom = 1.0
            return {"left": left, "top": top, "right": right, "bottom": bottom}

        if active in ("top", "bottom"):
            w = values["right"] - values["left"]
            h = w / r
            max_h = min(1.0, 1.0 / r)  # 收缩上限：h ≤ 1 且 w = h·r ≤ 1
            if active == "top":
                h = min(h, max_h, values["bottom"])  # top ≥ 0
                top = values["bottom"] - h
                bottom = values["bottom"]
            else:
                h = min(h, max_h, 1.0 - values["top"])
                top = values["top"]
                bottom = top + h
            w = h * r  # 高度受限时宽度同步收缩（比例保持）
            cx = (values["left"] + values["right"]) / 2.0
            left = cx - w / 2.0
            right = cx + w / 2.0
            if left < 0.0:
                right -= left
                left = 0.0
            if right > 1.0:
                left -= right - 1.0
                right = 1.0
            return {"left": left, "top": top, "right": right, "bottom": bottom}

        # 角：锚定对角，框包住鼠标（保持比例）
        anchor_x = values["right"] if active in ("tl", "bl") else values["left"]
        anchor_y = values["bottom"] if active in ("tl", "tr") else values["top"]
        dx = nx - anchor_x
        dy = ny - anchor_y
        max_w = min(1.0, r)
        w = min(max(abs(dx), abs(dy) * r), max_w)
        h = w / r
        if active in ("tl", "tr"):
            top = anchor_y - h
            bottom = anchor_y
        else:
            top = anchor_y
            bottom = anchor_y + h
        if active in ("tl", "bl"):
            left = anchor_x - w
            right = anchor_x
        else:
            left = anchor_x
            right = anchor_x + w
        # 越界时整体平移回界内（尺寸与比例不变；被 clamp 的边精确置 0/1，
        # 避免浮点误差残留微负值）
        if left < 0.0:
            shift = -left
            left = 0.0
            right += shift
        elif right > 1.0:
            shift = right - 1.0
            left -= shift
            right = 1.0
        if top < 0.0:
            shift = -top
            top = 0.0
            bottom += shift
        elif bottom > 1.0:
            shift = bottom - 1.0
            top -= shift
            bottom = 1.0
        # 最终数值清洗：平移修正可能让对侧边残留微负/微越界（浮点误差），
        # 统一钳制到精确边界（误差 ~1e-16，不改变比例）。
        return {
            "left": max(0.0, left),
            "top": max(0.0, top),
            "right": min(1.0, right),
            "bottom": min(1.0, bottom),
        }

    def source_pixmap(self) -> QtGui.QPixmap | None:
        return self._pixmap

    def has_image(self) -> bool:
        return self._pixmap is not None

    def values(self) -> dict[str, float]:
        return dict(self._values)

    def set_values(self, values: dict) -> None:
        """程序化设置归一化裁剪值（不触发 values_changed/手势信号）。"""
        clamped = self._clamped_values(values)
        if clamped != self._values:
            self._values = clamped
            self.update()

    # ------------------------------------------------------------------
    # 内部：钳制与拖拽
    # ------------------------------------------------------------------

    def _image_size(self) -> QtCore.QSize:
        if self._pixmap is not None:
            return self._pixmap.size()
        return QtCore.QSize(1, 1)

    def _min_span(self) -> tuple[float, float]:
        """归一化最小跨度（对应屏幕上约 2px，防拖成空区）。"""
        fitted = self._content_rect()
        min_w = self.MIN_EDGE_PX / fitted.width() if fitted.width() > 0 else 0.5
        min_h = self.MIN_EDGE_PX / fitted.height() if fitted.height() > 0 else 0.5
        return min(0.49, min_w), min(0.49, min_h)

    def _clamped_values(self, values: dict) -> dict[str, float]:
        """把任意输入钳制为合法裁剪（0~1、左<右、上<下）。"""
        min_w, min_h = self._min_span()
        left = max(0.0, min(1.0, float(values.get("left", self._values["left"]))))
        top = max(0.0, min(1.0, float(values.get("top", self._values["top"]))))
        right = max(0.0, min(1.0, float(values.get("right", self._values["right"]))))
        bottom = max(0.0, min(1.0, float(values.get("bottom", self._values["bottom"]))))
        right = max(right, left + min_w)
        bottom = max(bottom, top + min_h)
        left = min(left, right - min_w)
        top = min(top, bottom - min_h)
        return {"left": left, "top": top, "right": right, "bottom": bottom}

    def _hit_test(self, pos: QtCore.QPointF) -> str | None:
        """按按下位置返回拖拽目标：'left'/'top'/'right'/'bottom'、角 'tl'/'tr'/'bl'/'br'、
        'move'（框内平移）；未命中返回 None。"""
        if not self.has_image():
            return None
        fitted = self._content_rect()
        if fitted.isEmpty():
            return None
        left_pt = self.normalized_to_widget(self._values["left"], self._values["top"], fitted)
        right_pt = self.normalized_to_widget(self._values["right"], self._values["bottom"], fitted)
        left, top = left_pt.x(), left_pt.y()
        right, bottom = right_pt.x(), right_pt.y()
        tol = self.EDGE_TOLERANCE
        near_left = abs(pos.x() - left) <= tol
        near_right = abs(pos.x() - right) <= tol
        near_top = abs(pos.y() - top) <= tol
        near_bottom = abs(pos.y() - bottom) <= tol
        inside = left <= pos.x() <= right and top <= pos.y() <= bottom
        if (near_left or near_right) and (near_top or near_bottom):
            horizontal = "l" if near_left else "r"
            vertical = "t" if near_top else "b"
            return vertical + horizontal  # "tl"/"tr"/"bl"/"br"
        if near_left:
            return "left"
        if near_right:
            return "right"
        if near_top:
            return "top"
        if near_bottom:
            return "bottom"
        if inside:
            return "move"
        return None

    @staticmethod
    def _free_step(
        values: dict, active: str, nx: float, ny: float, min_w: float, min_h: float,
        press_norm: tuple[float, float] | None = None,
    ) -> dict:
        """自由（无纵横比锁定）拖拽步进（纯函数）：与旧行为一致。

        不修改入参：``values`` 通常是手势首帧 ``_press_values``，若原地修改
        会污染首帧状态，导致后续步进把偏移重复叠加（红框不跟随鼠标）。
        """
        values = dict(values)
        if active == "move":
            span_w = values["right"] - values["left"]
            span_h = values["bottom"] - values["top"]
            press_x, press_y = press_norm if press_norm is not None else (values["left"], values["top"])
            dx = nx - press_x
            dy = ny - press_y
            left = max(0.0, min(1.0 - span_w, values["left"] + dx))
            top = max(0.0, min(1.0 - span_h, values["top"] + dy))
            values["left"], values["top"] = left, top
            values["right"], values["bottom"] = left + span_w, top + span_h
        elif active in ("left", "right"):
            if active == "left":
                values["left"] = max(0.0, min(nx, values["right"] - min_w))
            else:
                values["right"] = max(values["left"] + min_w, min(1.0, nx))
        elif active in ("top", "bottom"):
            if active == "top":
                values["top"] = max(0.0, min(ny, values["bottom"] - min_h))
            else:
                values["bottom"] = max(values["top"] + min_h, min(1.0, ny))
        elif active in ("tl", "tr", "bl", "br"):
            if active in ("tl", "bl"):
                values["left"] = max(0.0, min(nx, values["right"] - min_w))
            else:
                values["right"] = max(values["left"] + min_w, min(1.0, nx))
            if active in ("tl", "tr"):
                values["top"] = max(0.0, min(ny, values["bottom"] - min_h))
            else:
                values["bottom"] = max(values["top"] + min_h, min(1.0, ny))
        else:
            return values
        return values

    def _apply_gesture(self, pos: QtCore.QPointF) -> None:
        """按当前拖拽目标更新归一化值；变化时发 values_changed。"""
        fitted = self._content_rect()
        if fitted.isEmpty() or self._press_values is None or self._press_norm is None:
            return
        nx, ny = self.widget_to_normalized(pos, fitted)
        min_w, min_h = self._min_span()
        r = self._aspect_norm()
        if r is None:
            values = self._free_step(self._press_values, self._active, nx, ny, min_w, min_h, self._press_norm)
        else:
            # 纵横比锁定：先把手势首帧投影到锁定比例（程序化设置的旧值可能
            # 不符合比例），再按比例约束步进，保证拖拽期间比例恒成立。
            start = self._project_to_aspect(self._press_values)
            values = self._aspect_step(start, self._active, nx, ny, r, min_w, min_h, self._press_norm)
        if values != self._values:
            self._values = values
            self.update()
            self.values_changed.emit(dict(values))

    # ------------------------------------------------------------------
    # 鼠标与绘制
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        active = self._hit_test(event.position())
        if active is None:
            self._active = None
            return
        self._active = active
        self._press_values = dict(self._values)
        fitted = self._content_rect()
        self._press_norm = self.widget_to_normalized(event.position(), fitted)
        self.gesture_begin.emit()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._active is not None and self._press_values is not None:
            self._apply_gesture(event.position())
            event.accept()
            return
        # 悬停：按命中目标切换光标形状
        active = self._hit_test(event.position())
        if active == "move":
            self.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        elif active in ("left", "right"):
            self.setCursor(QtCore.Qt.CursorShape.SizeHorCursor)
        elif active in ("top", "bottom"):
            self.setCursor(QtCore.Qt.CursorShape.SizeVerCursor)
        elif active in ("tl", "br"):
            self.setCursor(QtCore.Qt.CursorShape.SizeFDiagCursor)
        elif active in ("tr", "bl"):
            self.setCursor(QtCore.Qt.CursorShape.SizeBDiagCursor)
        else:
            self.setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def mouseReleaseEvent(self, event) -> None:
        if self._active is None:
            return
        self._active = None
        self._press_values = None
        self._press_norm = None
        self.gesture_end.emit()
        event.accept()

    def event(self, event) -> bool:
        if event.type() == QtCore.QEvent.Type.DevicePixelRatioChange:
            # 窗口跨屏/系统缩放变更：内容按新 DPR 重画（框尺寸由
            # MainWindow 的 refresh_dpr 显式重建，与 CheckerPreviewLabel 同约定）。
            self.update()
        return super().event(event)

    def paintEvent(self, _event) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QtGui.QColor(DARK.bg))
        fitted = self._content_rect()
        if not self.has_image() or fitted.isEmpty():
            painter.setPen(QtGui.QColor(DARK.muted))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, "无预览")
            return
        # 1:1 模式关平滑（每个源像素恰好对应一个设备像素，与 100% 屏渲染
        # 逐位相同）；适配模式保持平滑缩放。
        painter.setRenderHint(
            QtGui.QPainter.RenderHint.SmoothPixmapTransform,
            not self._mode_1to1,
        )
        painter.drawPixmap(fitted, self._pixmap, QtCore.QRectF(self._pixmap.rect()))
        left_pt = self.normalized_to_widget(self._values["left"], self._values["top"], fitted)
        right_pt = self.normalized_to_widget(self._values["right"], self._values["bottom"], fitted)
        left, top = left_pt.x(), left_pt.y()
        right, bottom = right_pt.x(), right_pt.y()
        crop_rect = QtCore.QRectF(left, top, right - left, bottom - top)
        # 框外压暗（即「被裁掉的部分」）
        outer = QtGui.QPainterPath()
        outer.addRect(fitted)
        inner = QtGui.QPainterPath()
        inner.addRect(crop_rect)
        painter.fillPath(outer.subtracted(inner), QtGui.QColor(0, 0, 0, 130))
        # 三分构图辅助线（区域足够大时才画）
        if crop_rect.width() >= 40 and crop_rect.height() >= 40:
            guide = QtGui.QPen(
                QtGui.QColor(255, 255, 255, 70),
                1,
                QtCore.Qt.PenStyle.DotLine,
            )
            painter.setPen(guide)
            for fraction in (1.0 / 3.0, 2.0 / 3.0):
                x = left + crop_rect.width() * fraction
                y = top + crop_rect.height() * fraction
                painter.drawLine(QtCore.QPointF(x, top), QtCore.QPointF(x, bottom))
                painter.drawLine(QtCore.QPointF(left, y), QtCore.QPointF(right, y))
        # 红色裁剪边线
        red = QtGui.QPen(QtGui.QColor(DARK.danger), 2)
        painter.setPen(red)
        painter.drawRect(crop_rect)
        # 四角手柄
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(DARK.danger))
        half = self.HANDLE_SIZE / 2.0
        for cx, cy in ((left, top), (right, top), (left, bottom), (right, bottom)):
            painter.drawRect(QtCore.QRectF(cx - half, cy - half, self.HANDLE_SIZE, self.HANDLE_SIZE))
        # 纵横比锁定角标（裁剪框内左上角小字，提示当前锁定比例）
        if self._aspect_ratio is not None:
            label = self._aspect_label or f"{self._aspect_ratio:.3g}"
            painter.setPen(QtGui.QColor(DARK.danger_soft))
            font = painter.font()
            font.setPointSizeF(max(6.0, font.pointSizeF() - 1.0))
            painter.setFont(font)
            painter.drawText(
                QtCore.QRectF(left + 4.0, top + 4.0, crop_rect.width() - 8.0, 16.0),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop,
                label,
            )


class CropOverlayPanel(QtWidgets.QWidget):
    """可视化裁剪接管面板（复合控件，决策 #109）：画布 + 结果缩略图 + 数值只读。

    作为 ``CropOverlayParam`` 的接管控件（``widget_factory=make_crop_panel``）
    在节点 params 中声明即排版，替代旧面板 ``crop_overlay=True`` 构造标志：
    接管 ``owned``（left/top/right/bottom）参数的值读写，保留 ``linked``
    （纵横比）参数常规行并联动锁定裁剪框比例。

    接管控件契约：
    - ``changed`` —— 拖拽/纵横比联动后发出（面板据此发 ``changed(values())``）；
    - ``values()`` / ``set_values(dict)`` —— 按参数名读写**百分比**（面板侧
      值域，内部归一化换算由本控件承担）；
    - ``gesture_begin`` / ``gesture_end`` —— 拖拽手势（上层折叠为一条撤销记录）；
    - ``set_image`` —— 喂入上游源图（``data_source="first_frame"``）；
    - ``release_content`` —— 清空源图、结果缩略图与只读；
    - ``refresh_dpr`` —— 跨屏重建 1:1 画布（与 CheckerPreviewLabel 同约定）。
    """

    changed = QtCore.Signal()
    gesture_begin = QtCore.Signal()
    gesture_end = QtCore.Signal()
    geometry_changed = QtCore.Signal()

    RESULT_PREVIEW_SIZE = 96  # 结果缩略图边长（随拖拽实时更新，不依赖运行）

    def __init__(self, panel=None, param=None):
        super().__init__(None)
        self._panel = panel
        self._param = param
        self.canvas = CropOverlayWidget()
        # 1:1 显示模式由节点面板声明（PanelSpec.preview_1to1）派生；
        # 适配模式退回画布固定 200×200。
        if panel is not None:
            self.canvas.set_1to1(panel.definition.panel.preview_1to1)
        self.canvas.values_changed.connect(self._on_values_changed)
        self.canvas.gesture_begin.connect(self.gesture_begin)
        self.canvas.gesture_end.connect(self.gesture_end)
        # 1:1 框跟随图片尺寸变化 → 面板几何变化 → 节点重排（与预览标签同约定）。
        self.canvas.geometry_changed.connect(self.geometry_changed.emit)
        self.result_preview = QtWidgets.QLabel("—")
        self.result_preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.result_preview.setFixedSize(self.RESULT_PREVIEW_SIZE, self.RESULT_PREVIEW_SIZE)
        self.result_preview.setStyleSheet(
            f"background:{DARK.bg};border:1px solid {DARK.border};color:{DARK.muted};"
        )
        self.readout = QtWidgets.QLabel("左 0% · 上 0% · 右 100% · 下 100%")
        self.readout.setWordWrap(True)
        self.readout.setStyleSheet(f"color:{DARK.danger_soft};")
        result_row = QtWidgets.QHBoxLayout()
        result_row.setContentsMargins(0, 0, 0, 0)
        result_row.addWidget(QtWidgets.QLabel("结果"), 1)
        result_row.addWidget(self.result_preview)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas)
        layout.addLayout(result_row)
        layout.addWidget(self.readout)
        self._link_aspect(panel, param)

    # --- 纵横比联动（linked 参数常规行 → 画布锁定） ---

    def _link_aspect(self, panel, param) -> None:
        """连接纵横比下拉（``linked`` 参数，常规行）→ ``set_aspect_ratio``。

        控件在面板常规行之后构造（linked 行已存在），故此处可直接取控件。
        """
        self._aspect_def = None
        if panel is None or param is None or not param.linked:
            return
        aspect_name = param.linked[0]
        for p in panel.definition.params:
            if p.name == aspect_name and isinstance(p, ChoiceParam):
                self._aspect_def = p
                break
        widget = panel.widgets.get(aspect_name)
        if widget is not None and hasattr(widget, "currentTextChanged"):
            widget.currentTextChanged.connect(self._on_aspect_changed)

    def _on_aspect_changed(self, label: str) -> None:
        """纵横比下拉变化：锁定画布并重投影裁剪框，按面板惯例发 changed。"""
        ratio = (
            self._aspect_def.options.value_of(label)
            if self._aspect_def is not None and self._aspect_def.options is not None
            else None
        )
        self.canvas.set_aspect_ratio(ratio, label=label)
        self._refresh()
        self.changed.emit()

    # --- 接管契约 ---

    def set_image(self, path) -> None:
        """喂入上游源图（None 清空）；随后刷新结果缩略图与只读。"""
        self.canvas.set_image(path)
        self._refresh()

    def values(self) -> dict:
        """当前裁剪值（百分比，面板侧值域；内部归一化由画布承担）。"""
        return {k: round(v * 100.0, 2) for k, v in self.canvas.values().items()}

    def set_values(self, values: dict) -> None:
        normalized = {k: float(v) / 100.0 for k, v in values.items()}
        self.canvas.set_values(normalized)
        self._refresh()

    def release_content(self) -> None:
        """清空源图、结果缩略图与只读（不触发 changed）。"""
        self.canvas.set_image(None)
        self.result_preview.setPixmap(QtGui.QPixmap())
        self.result_preview.setText("—")
        self.readout.setText("左 0% · 上 0% · 右 100% · 下 100%")

    def refresh_dpr(self, dpr: float) -> None:
        """窗口跨屏/系统缩放变化后重建 1:1 画布（与预览标签同约定）。"""
        self.canvas.refresh_dpr(dpr)

    # --- 内部 ---

    def _on_values_changed(self, values: dict) -> None:
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        """刷新结果缩略图与数值只读（拖拽/程序化/纵横比联动共用，不触发 changed）。"""
        values = self.canvas.values()
        self.readout.setText(
            f"左 {values['left'] * 100:.1f}% · 上 {values['top'] * 100:.1f}% · "
            f"右 {values['right'] * 100:.1f}% · 下 {values['bottom'] * 100:.1f}%"
        )
        pixmap = self.canvas.source_pixmap()
        if pixmap is None or pixmap.isNull():
            self.result_preview.setPixmap(QtGui.QPixmap())
            self.result_preview.setText("—")
            return
        rect = CropOverlayWidget.crop_rect(values, pixmap.size())
        cropped = pixmap.copy(rect)
        self.result_preview.setPixmap(cropped.scaled(
            self.RESULT_PREVIEW_SIZE,
            self.RESULT_PREVIEW_SIZE,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        ))


def make_crop_panel(panel, param):
    """``CropOverlayParam`` 的控件工厂（供 ``TakeoverParam.widget_factory`` 引用）。"""
    return CropOverlayPanel(panel, param)
