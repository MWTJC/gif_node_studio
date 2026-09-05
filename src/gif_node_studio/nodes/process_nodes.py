"""画面处理节点（原「一般处理」）：逐帧处理画面内容——颜色调整/二值化/灰度/反相/
键控/旋转/纵横比挤压/翻转/裁剪/缩放/颜色量化。与「序列处理」的分界：每个输出帧
仅由对应输入帧决定（帧数不变、无跨帧关系）。"""

from __future__ import annotations

import math

from PIL import Image
import qtawesome as qta
from ..core.domain import CropSpec, SequenceArtifact
from ..core.options import COLOR_REDUCE_ALGORITHM, CROP_ASPECT, DITHER, FLIP_DIRECTION, HUE_SAT_TARGET, QUANTIZE_COLORSPACE, QUANTIZE_DITHER, RESAMPLE
from ..media.palettes import POSTERIZE_LEVELS, ordered_dither_map_names
from .crop_overlay import make_crop_panel
from .definitions import (
    BoolParam,
    ChoiceParam,
    ColorParam,
    CropOverlayParam,
    FloatParam,
    IntParam,
    NodeCategory,
    NodeDefinition,
    PanelSpec,
    PortDefinition,
    PortType,
)
from .icon_resource import category_icon
from .parameter_panel import ParameterPanel
from .sequence_nodes import SequenceNode

class BrightnessNode(SequenceNode):
    """亮度调整。

    处理：backend.adjust_color(brightness=amount)
    参数：amount（FloatParam 滑条+数值框，-100..100）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "亮度调整"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "brightness", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "mdi.brightness-6"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(FloatParam("amount", "亮度 %", default=0.0, minimum=-100, maximum=100),),
            ),
            help=(
                "输入图片序列\n"
                "亮度 %：0 = 不变，负值变暗、正值变亮\n"
                "输出图片序列（Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.adjust_color(cls.sequence(inputs), brightness=float(params["amount"]))




class SaturationNode(SequenceNode):
    """饱和度调整。

    处理：backend.adjust_color(saturation=amount)
    参数：amount（FloatParam 滑条+数值框，-100..200）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "饱和度调整"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "saturation", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "ri.contrast-2-fill"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(FloatParam("amount", "饱和度 %", default=0.0, minimum=-100, maximum=200),),
            ),
            help=(
                "输入图片序列\n"
                "饱和度 %：0 = 不变，负值褪色、正值更鲜艳\n"
                "输出图片序列（Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.adjust_color(cls.sequence(inputs), saturation=float(params["amount"]))




def _hex_hue_degrees(hex_color: str) -> float:
    """'#rrggbb' → 色相（0..360 度；RGB→HSV 的 H，自定义(取色器)中心）。"""
    from colorsys import rgb_to_hsv

    r, g, b = (int(hex_color[index:index + 2], 16) for index in (1, 3, 5))
    return float(rgb_to_hsv(r / 255, g / 255, b / 255)[0] * 360.0)


class HueSaturationNode(SequenceNode):
    """色相/饱和度：PS「色相/饱和度」对话框复刻（取代已删除的「色相调整」）。

    处理：backend.hue_sat_range_sequence(center_hue, hue_delta, sat_delta,
          light_delta, range_half, feather_deg)
    参数：target（ChoiceParam 色域下拉，唯一源头 options.HUE_SAT_TARGET）、
          target_color（ColorParam 色块，仅色域=自定义(取色器) 时决定中心色相）、
          hue_delta/sat_delta/light_delta（FloatParam 滑条+数值框）、
          range_half/feather（FloatParam 滑条+数值框，仅非「全图」生效）
    组件：帧滑条（PanelSpec.scrub_frames）

    与「色相调整」关系（决策 #134）：色相调整 = 全图色相旋转（PIL HSV point，
    adjust_color 的 hue 分支）与新节点「全图」作用域完全重合，已删除；旧存档
    hue 节点加载时自动迁移为本节点（色域=全图、色相=原值，见 ui/session.py
    节点类型迁移映射）。「亮度调整/饱和度调整」保留（PIL ImageEnhance 公式，
    与新节点 PS 式明度/饱和度不同）。
    """

    NODE_NAME = "色相/饱和度"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "hue_sat", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "mdi6.palette-swatch"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    ChoiceParam("target", "色域", options=HUE_SAT_TARGET),
                    ColorParam("target_color", "目标颜色", default="#ff0000"),
                    FloatParam("hue_delta", "色相 °", default=0.0, minimum=-180, maximum=180),
                    FloatParam("sat_delta", "饱和度 %", default=0.0, minimum=-100, maximum=100),
                    FloatParam("light_delta", "明度 %", default=0.0, minimum=-100, maximum=100),
                    # 范围/过渡只对选区（非全图）有意义：全图时置灰。
                    FloatParam("range_half", "范围半宽 °", default=15.0, minimum=0, maximum=180,
                               enabled_when=("target", HUE_SAT_TARGET.labels[1:])),
                    FloatParam("feather", "过渡半宽 °", default=30.0, minimum=0, maximum=180,
                               enabled_when=("target", HUE_SAT_TARGET.labels[1:])),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "按 PS「色相/饱和度」调整画面颜色（可只作用于目标色附近，也可全图）：\n"
                "色域：作用范围\n"
                "目标颜色：仅色域=自定义(取色器)时生效\n"
                "色相 °/饱和度 %/明度 %：调整量（0 = 不变）\n"
                "范围半宽 °/过渡半宽 °：色域内全量生效、向外柔和衰减的宽度\n"
                "输出图片序列（Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        mode = HUE_SAT_TARGET.key_of(str(params["target"]))
        if mode == "master":
            center_hue = None  # 全图：蒙版恒 1（PS Master）
        elif mode == "custom":
            center_hue = _hex_hue_degrees(str(params["target_color"]))
        else:
            center_hue = float(HUE_SAT_TARGET.value_for_key(mode))
        return backend.hue_sat_range_sequence(
            cls.sequence(inputs),
            center_hue=center_hue,
            hue_delta=float(params["hue_delta"]),
            sat_delta=float(params["sat_delta"]),
            light_delta=float(params["light_delta"]),
            range_half=float(params["range_half"]),
            feather_deg=float(params["feather"]),
        )




class ColorBalanceNode(SequenceNode):
    """色彩平衡调整。

    处理：PIL 逐帧 RGB 通道缩放（factor=1+值/100，Alpha 保留）+ backend._job_dir 物化
    参数：red/green/blue（FloatParam 滑条+数值框，-100..100）
    组件：无增量（默认预览框）
    """

    NODE_NAME = "色彩平衡调整"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "color_balance", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, ),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    FloatParam("red", "红", default=0.0, minimum=-100, maximum=100),
                    FloatParam("green", "绿", default=0.0, minimum=-100, maximum=100),
                    FloatParam("blue", "蓝", default=0.0, minimum=-100, maximum=100),
                ),
            ),
            help=(
                "输入图片序列\n"
                "分别增减红、绿、蓝通道强度（每个 0 = 不变，负值减弱、正值增强）\n"
                "输出图片序列（Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        artifact = cls.sequence(inputs)
        output = backend._job_dir("balance")
        paths = []
        factors = tuple(max(0, 1 + float(params[name]) / 100) for name in ("red", "green", "blue"))
        for index, source in enumerate(artifact.frames):
            image = Image.open(source).convert("RGBA")
            red, green, blue, alpha = image.split()
            channels = [
                channel.point(lambda value, factor=factor: min(255, round(value * factor)))
                for channel, factor in zip((red, green, blue), factors)
            ]
            target = output / f"frame_{index:06d}.png"
            Image.merge("RGBA", (*channels, alpha)).save(target)
            paths.append(str(target))
        return SequenceArtifact(tuple(paths), artifact.width, artifact.height, True, str(output))




class BinarizeNode(SequenceNode):
    """二值化。

    处理：backend.binarize_sequence(threshold)
    参数：threshold（IntParam 滑条+数值框，0..255）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "二值化"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "binarize", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "ph.square-half-fill"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(IntParam("threshold", "阈值", default=128, minimum=0, maximum=255),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "转灰度后按阈值二值化：像素值大于等于阈值转白、小于阈值转黑\n"
                "输出图片序列（只保留黑/白两色，Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.binarize_sequence(cls.sequence(inputs), threshold=int(params["threshold"]))




class GrayscaleNode(SequenceNode):
    """灰度化。

    处理：backend.grayscale_sequence
    参数：无
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "灰度化"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "grayscale", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "mdi6.texture-box"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "把每帧转为灰度，丢弃颜色信息\n"
                "输出图片序列（Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.grayscale_sequence(cls.sequence(inputs))




class ContrastNode(SequenceNode):
    """对比度调整。

    处理：backend.contrast_sequence(amount)
    参数：amount（FloatParam 滑条+数值框，-100..100）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "对比度调整"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "contrast", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "ri.contrast-2-line"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(FloatParam("amount", "对比度 %", default=0.0, minimum=-100, maximum=100),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "对比度 %：0 = 不变，负值减弱、正值增强\n"
                "输出图片序列（Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.contrast_sequence(cls.sequence(inputs), amount=float(params["amount"]))




class InvertNode(SequenceNode):
    """反相。

    处理：backend.invert_sequence
    参数：无
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "反相"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "invert", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "mdi.invert-colors"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "把每帧颜色反相（黑变白、红变青）\n"
                "输出图片序列（Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.invert_sequence(cls.sequence(inputs))




class SuperKeyNode(SequenceNode):
    """超级键：按用户选取的背景色抠像（PR 颜色键/超级键式），边缘处理强弱可调。

    处理：backend.color_key_sequence(key_color, edge_strength)
    参数：key_color（ColorParam 色块按钮）、edge_strength（FloatParam 滑条+数值框）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "超级键"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "super_key", self.NODE_NAME, NodeCategory.PROCESS,
                # icon=category_icon(NodeCategory.PROCESS, "mdi.opacity"),  # mdi6.square-opacity
                icon=qta.icon(  # 手动规定icon 手动icon 自定义icon
                    "fa6s.square-full", "mdi.opacity",
                    options=[
                        {"color": f"{NodeCategory.PROCESS.color}"},
                        {"color": "#00FF00", "scale_factor": 0.8},
                    ],
                ),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 背景色由 QColorDialog 选取（ColorParam 色块按钮控件）；
                    # 默认绿幕色（与设置「透明背景色」默认一致）。
                    ColorParam("key_color", "背景色", default="#00ff00"),
                    FloatParam("edge_strength", "边缘处理强弱 %", default=50.0, minimum=0, maximum=100),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "选取背景色，把颜色与它相近的像素抠成透明（超级键式）：\n"
                "背景色：要抠成透明的颜色（点击色块选取）\n"
                "边缘处理强弱 %：0% 硬边抠像，100% 大范围柔和过渡\n"
                "输出图片序列（Alpha 只减不增）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        # '#rrggbb' → (r, g, b) 三元组（hex 字符串为预设持久化格式）。
        hex_color = str(params["key_color"])
        key_color = tuple(int(hex_color[index:index + 2], 16) for index in (1, 3, 5))
        return backend.color_key_sequence(
            cls.sequence(inputs),
            key_color=key_color,
            edge_strength=float(params["edge_strength"]),
        )




class ColorKeyNode(SequenceNode):
    """颜色键：按用户选取的背景色抠像，容差/羽化双滑条（PR 颜色键式）。

    处理：backend.color_key_tolerance_sequence(key_color, tolerance, feather)
    参数：key_color（ColorParam 色块按钮）、tolerance（FloatParam 容差 %）、
          feather（FloatParam 羽化 %）
    组件：帧滑条（PanelSpec.scrub_frames）

    与「超级键」的滤色逻辑区别：键控半径由**容差**决定（50% ≡ 归一化距离
    0.25，与超级键默认中心一致），**羽化**自容差边界向外过渡——容差内的
    像素完全键出，不会因羽化而在中心出现半透明（超级键为固定中心 +
    边缘处理强弱单滑条）。距离度量与超级键同一（RGB 欧氏距离归一化）。
    """

    NODE_NAME = "颜色键"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "color_key", self.NODE_NAME, NodeCategory.PROCESS,
                # 与「超级键」同款双层图标（色块底 + 键出色圆点），
                # 形式与超级键类似（图标一致，靠标题区分）。
                icon=category_icon(NodeCategory.PROCESS, "mdi.opacity"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 背景色由 QColorDialog 选取（ColorParam 色块按钮控件）；
                    # 默认绿幕色（与「超级键」一致）。
                    ColorParam("key_color", "背景色", default="#00ff00"),
                    FloatParam("tolerance", "容差 %", default=50.0, minimum=0, maximum=100),
                    FloatParam("feather", "羽化 %", default=50.0, minimum=0, maximum=100),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "颜色键：选取背景色，把颜色与它相近的像素抠成透明\n"
                "容差 %：越大抠得越多（0 = 只抠完全相同的颜色）\n"
                "羽化 %：越大边缘越虚\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        # '#rrggbb' → (r, g, b) 三元组（hex 字符串为预设持久化格式）。
        hex_color = str(params["key_color"])
        key_color = tuple(int(hex_color[index:index + 2], 16) for index in (1, 3, 5))
        return backend.color_key_tolerance_sequence(
            cls.sequence(inputs),
            key_color=key_color,
            tolerance=float(params["tolerance"]),
            feather=float(params["feather"]),
        )




class RotateNode(SequenceNode):
    """旋转：输入序列 → 输出序列，按角度旋转每帧，空隙用全透明像素填充。

    处理：PIL rotate(BICUBIC, expand=True) 逐帧物化（外接/内接画布）
    参数：angle（FloatParam 滑条+数值框）、inscribe（BoolParam 勾选框）
    组件：帧滑条（PanelSpec.scrub_frames）

    边界策略：
    - 不勾选「内接最大矩形裁剪」（默认）：按最小外接矩形确定画布，
      旋转造成的三角空隙为全透明像素；
    - 勾选：把旋转结果居中裁剪为旋转后内容的最大轴对齐内接矩形，
      画布更小、内容完整无空隙。
    """

    NODE_NAME = "旋转"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "rotate", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "fa6s.rotate"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    FloatParam("angle", "角度 °", default=0.0, minimum=-180.0, maximum=180.0),
                    BoolParam("inscribe", "内接最大矩形裁剪", default=False),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "按角度旋转每帧，旋转空隙用透明像素填充：\n"
                "角度 °：逆时针为正（0 = 不变）\n"
                "内接最大矩形裁剪：勾选后把旋转结果裁成内容完整的最小矩形（画布更小、无空隙）\n"
                "输出图片序列"
            ),
        )

    @staticmethod
    def _rotated_size(width: int, height: int, angle: float) -> tuple[int, int]:
        """最小外接矩形画布尺寸——与 PIL ``rotate(expand=True)`` 完全一致。

        PIL 用「绕中心旋转后四角坐标的 ceil(max)−floor(min)」计算画布，
        比纯几何外接矩形（W|c|+H|s| 四舍五入）在亚像素上可能大 1px；
        必须复刻同一算法，输出画布尺寸才与实际帧一致。
        """
        w, h = float(width), float(height)
        a = -math.radians(angle % 360.0)
        ca = round(math.cos(a), 15)
        sa = round(math.sin(a), 15)
        cx, cy = w / 2.0, h / 2.0
        # 绕中心旋转的平移项（与 PIL matrix[2]/[5] 相同）
        tx = ca * -cx + sa * -cy + cx
        ty = -sa * -cx + ca * -cy + cy
        xx, yy = [], []
        for x, y in ((0.0, 0.0), (w, 0.0), (w, h), (0.0, h)):
            xx.append(ca * x + sa * y + tx)
            yy.append(-sa * x + ca * y + ty)
        return math.ceil(max(xx)) - math.floor(min(xx)), math.ceil(max(yy)) - math.floor(min(yy))

    @staticmethod
    def _inscribed_size(width: int, height: int, angle: float) -> tuple[int, int]:
        """最大轴对齐内接矩形尺寸（向下取整，保证内容完整不被裁到）。"""
        theta = math.radians(angle)
        c, s = abs(math.cos(theta)), abs(math.sin(theta))
        if s < 1e-9:  # 0°/±180°：无旋转效果
            return width, height
        if c < 1e-9:  # ±90°/±270°：旋转后无空隙
            return height, width
        denom = c * c - s * s
        if abs(denom) > 1e-9:
            w = (width * c - height * s) / denom
            h = (height * c - width * s) / denom
            if w > 0 and h > 0:
                return max(1, math.floor(w)), max(1, math.floor(h))
        candidates: list[tuple[float, float]] = []
        # 约束 w·s + h·c ≤ H 单独收紧（旋转后细边主导）
        w, h = height / (2 * s), height / (2 * c)
        if w * c + h * s <= width + 1e-9:
            candidates.append((w, h))
        # 约束 w·c + h·s ≤ W 单独收紧
        w, h = width / (2 * c), width / (2 * s)
        if w * s + h * c <= height + 1e-9:
            candidates.append((w, h))
        if candidates:
            w, h = max(candidates, key=lambda pair: pair[0] * pair[1])
            return max(1, math.floor(w)), max(1, math.floor(h))
        # 兜底（理论不可达）：退回外接矩形
        return RotateNode._rotated_size(width, height, angle)

    @classmethod
    def execute(cls, inputs, params, backend):
        artifact = cls.sequence(inputs)
        angle = float(params["angle"])
        inscribe = bool(params.get("inscribe", False))
        if inscribe:
            canvas_w, canvas_h = cls._inscribed_size(artifact.width, artifact.height, angle)
        else:
            canvas_w = canvas_h = None
        output = backend._job_dir("rotate")
        paths = []
        for index, source in enumerate(artifact.frames):
            image = Image.open(source).convert("RGBA")
            rotated = image.rotate(
                angle,
                resample=Image.Resampling.BICUBIC,
                expand=True,
                fillcolor=(0, 0, 0, 0),
            )
            if not inscribe and canvas_w is None:
                # 外接模式画布以实际旋转帧为准（与 PIL expand=True 一致，
                # 避免亚像素取整差异导致 SequenceArtifact 尺寸与帧不符）。
                canvas_w, canvas_h = rotated.size
            if inscribe:
                left = max(0, (rotated.width - canvas_w) // 2)
                top = max(0, (rotated.height - canvas_h) // 2)
                rotated = rotated.crop((left, top, left + canvas_w, top + canvas_h))
            target = output / f"frame_{index:06d}.png"
            rotated.save(target)
            paths.append(str(target))
        return SequenceArtifact(tuple(paths), canvas_w, canvas_h, True, str(output))



class AspectRatioNode(SequenceNode):
    """纵横比挤压：输入序列 → 输出序列，按因子非等比缩放帧宽控制纵横比。

    处理：backend.squeeze_aspect_sequence(factor)
    参数：factor（FloatParam 滑条+数值框，0.2..5.0）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "纵横比挤压"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "aspect_squeeze", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "mdi.aspect-ratio"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(FloatParam("factor", "挤压程度", default=1.0, minimum=0.2, maximum=5.0),),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "非等比缩放帧宽（高度不变）控制纵横比：\n"
                "挤压程度：1.0 = 不变，小于 1 横向压扁，大于 1 横向拉宽\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.squeeze_aspect_sequence(
            cls.sequence(inputs), factor=float(params["factor"])
        )



class PercentScaleNode(SequenceNode):
    """百分比缩放：输入序列 → 输出序列，按用户百分比等比缩放每一帧。

    处理：backend.scale_percent_sequence(percent, resample)
    参数：percent（IntParam 缩放百分比 %，1..1000，默认 100 = 原尺寸）、
          resample（ChoiceParam 缩放算法，与「分辨率统一」同一来源）
    组件：帧滑条（PanelSpec.scrub_frames）

    画面处理（原序列处理分类，随分类重划迁入）：单序列输入、逐帧等比缩放、
    帧数不变——目标尺寸由本节点自己的百分比决定（新宽高 =
    round(原尺寸 × percent / 100)），不需要第二路基准序列。
    """

    NODE_NAME = "百分比缩放"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "percent_scale", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "mdi.resize"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    IntParam("percent", "缩放百分比 %", default=100, minimum=1, maximum=1000),
                    # 与「序列相加」「分辨率统一」同一缩放算法来源（单一源头 options.py）。
                    ChoiceParam("resample", "缩放算法", options=RESAMPLE),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "按设置的百分比等比缩放画面（宽高同比例）：\n"
                "缩放百分比 %：100 = 不变，小于 100 缩小，大于 100 放大\n"
                "缩放算法：放大/缩小时的画面重采样方式\n"
                "输出图片序列（帧数不变）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.scale_percent_sequence(
            cls.sequence(inputs),
            percent=int(params["percent"]),
            resample=RESAMPLE.key_of(params["resample"]),
        )



class FlipNode(SequenceNode):
    """画面翻转：输入序列 → 输出序列，按所选方向水平/垂直翻转每一帧。

    处理：backend.flip_sequence(direction)
    参数：direction（ChoiceParam 下拉，FLIP_DIRECTION 水平/垂直）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "画面翻转"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "flip", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "mdi.flip-horizontal"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 选项唯一源头：options.FLIP_DIRECTION（水平/垂直）。
                    ChoiceParam("direction", "翻转方向", options=FLIP_DIRECTION),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "按所选方向翻转每一帧（水平 = 左右镜像，垂直 = 上下翻转）\n"
                "输出图片序列（尺寸不变，Alpha 随像素翻转）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.flip_sequence(
            cls.sequence(inputs),
            direction=FLIP_DIRECTION.key_of(params["direction"]),
        )



class SequenceCropNode(SequenceNode):
    """画面裁剪（序列 → 序列）：在 1:1 预览图上拖拽红色裁剪线裁剪每一帧。

    处理：backend.crop_sequence(CropSpec)
    参数：aspect（ChoiceParam 下拉）、crop（CropOverlayParam 接管 left/top/right/bottom）
    组件：1:1 预览（PanelSpec.preview_1to1）+ 可视化裁剪画布（CropOverlayPanel）

    替代旧「清单级裁剪」节点（``kind="crop"``，仅能在格式化解码前生效）：
    本节点直接裁剪已格式化的图片序列，可放在处理链任意位置（旋转/缩放/
    叠加等前后均可）。参数仍是纵横比 + 左/上/右/下四个百分比（overlay 以
    归一化值接管）；裁剪框换算与后端 ``crop_sequence`` 完全一致
    （``CropOverlayWidget.crop_rect`` 同款取整与最小 1px 防呆）。
    """

    NODE_NAME = "画面裁剪"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "sequence_crop", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "mdi.crop"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    ChoiceParam("aspect", "纵横比", options=CROP_ASPECT),
                    FloatParam("left", "左 %", default=0.0, minimum=0, maximum=99),
                    FloatParam("top", "上 %", default=0.0, minimum=0, maximum=99),
                    FloatParam("right", "右 %", default=100.0, minimum=1, maximum=100),
                    FloatParam("bottom", "下 %", default=100.0, minimum=1, maximum=100),
                    # 接管型参数（决策 #109）：面板用 CropOverlayPanel 接管
                    # left/top/right/bottom 四参数（1:1 源图拖拽），纵横比
                    # 下拉保留常规行并联动锁定裁剪框比例。
                    CropOverlayParam(
                        "crop", "可视化裁剪",
                        owned=("left", "top", "right", "bottom"),
                        linked=("aspect",),
                        data_source="first_frame",
                        widget_factory=make_crop_panel,
                    ),
                ),
                panel=PanelSpec(preview_1to1=True),
            ),
            help=(
                "输入图片序列\n"
                "在原始像素预览上拖拽红色裁剪线，裁剪每一帧：\n"
                "纵横比：锁定裁剪框比例（可选自由或常用比例）\n"
                "输出图片序列（输出尺寸 = 裁剪框）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        artifact = cls.sequence(inputs)
        own = CropSpec(*(float(params[name]) / 100 for name in ("left", "top", "right", "bottom")))
        return backend.crop_sequence(artifact, own)



class DitherNode(SequenceNode):
    """颜色深度节点：序列→序列（PS「存储为 Web 所用格式」式降低颜色深度 + 仿色）。

    处理：backend.color_reduce_sequence(algorithm, colors, dither, map_name, levels)
    参数：algorithm/dither/map/levels（ChoiceParam 下拉，部分 enabled_when 置灰）、
          colors（IntParam 滑条+数值框）
    组件：帧滑条（PanelSpec.scrub_frames）
    """

    NODE_NAME = "颜色深度（已过时）"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "dither", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 选项唯一源头：options.COLOR_REDUCE_ALGORITHM / options.DITHER；
                    # enabled_when 的允许取值同样由选项组派生（labels_of_keys），
                    # 互斥规则不再手写标签串（防止与选项组漂移）。
                    ChoiceParam("algorithm", "降低颜色深度算法", options=COLOR_REDUCE_ALGORITHM),
                    IntParam("colors", "颜色数", default=256, minimum=2, maximum=256,
                             enabled_when=("algorithm", COLOR_REDUCE_ALGORITHM.labels_of_keys("adaptive", "perceptual", "grayscale"))),
                    ChoiceParam("dither", "仿色算法", options=DITHER),
                    ChoiceParam("map", "图案阈值图", default="o8x8", choices=ordered_dither_map_names(),
                                enabled_when=("dither", DITHER.labels_of_keys("pattern"))),
                    ChoiceParam("levels", "均匀色阶", default="13", choices=POSTERIZE_LEVELS,
                                enabled_when=("dither", DITHER.labels_of_keys("pattern"))),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "已过时：请改用「颜色量化（IM）」节点（本节点按旧 PS 语义实现、效果欠佳）\n"
                "降低颜色深度并仿色（只作用于颜色，Alpha 保留）\n"
                "输出图片序列"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.color_reduce_sequence(
            cls.sequence(inputs),
            # 标签 → 机器键（后端按机器键分支，稳定不随显示名漂移）。
            algorithm=COLOR_REDUCE_ALGORITHM.key_of(params["algorithm"]),
            colors=int(params["colors"]),
            dither=DITHER.key_of(params["dither"]),
            map_name=params["map"],
            levels=params["levels"],
        )




class ColorQuantizeNode(SequenceNode):
    """颜色量化节点：IM 原生颜色减少（序列→序列）。

    处理：backend.color_quantize_sequence(colorspace, colors, treedepth, dither,
          use_ordered, ordered_map, levels, posterize_levels)
    参数：colorspace/dither/ordered_map/levels（ChoiceParam 下拉）、
          colors/treedepth（IntParam 滑条+数值框）、use_ordered（BoolParam 勾选框）、
          posterize_levels（IntParam spin 数值框）
    组件：帧滑条（PanelSpec.scrub_frames）

    参数直接映射 ImageMagick 操作符（见[关键决策 #76]）：``-quantize`` 量化
    色彩空间、``-colors`` 颜色数、``-treedepth`` 树深度、``-dither`` 量化仿色、
    ``-ordered-dither`` 有序仿色预处理、``-posterize`` 海报化。不做 PS「存储为
    Web 所用格式」语义对齐，不携带旧「颜色深度」节点的固定色板/取色补丁。
    """

    NODE_NAME = "颜色量化（IM）"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "color_quantize", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "fa6s.wand-magic-sparkles"),
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(
                    # 选项唯一源头：options.QUANTIZE_COLORSPACE / QUANTIZE_DITHER。
                    ChoiceParam("colorspace", "量化色彩空间", options=QUANTIZE_COLORSPACE),
                    IntParam("colors", "颜色数", default=256, minimum=2, maximum=256),
                    IntParam("treedepth", "树深度", default=0, minimum=0, maximum=8),
                    # 量化仿色与有序仿色互斥：勾选「有序仿色」后置灰（IM CLI 中
                    # -ordered-dither 与 -dither 是两个独立操作，叠加会双重仿色）。
                    ChoiceParam("dither", "仿色", options=QUANTIZE_DITHER,
                                enabled_when=("use_ordered", (False,))),
                    BoolParam("use_ordered", "有序仿色", default=False),
                    ChoiceParam("ordered_map", "阈值图", default="o8x8",
                                choices=ordered_dither_map_names(),
                                enabled_when=("use_ordered", (True,))),
                    ChoiceParam("levels", "均匀色阶", default="13", choices=POSTERIZE_LEVELS,
                                enabled_when=("use_ordered", (True,))),
                    IntParam("posterize_levels", "海报化色阶(0=关)", default=0,
                             minimum=0, maximum=256, widget="spin"),
                ),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=(
                "输入图片序列\n"
                "把整条序列的颜色统一降到一张调色板（GIF 减色前处理）：\n"
                "量化色彩空间：颜色比较与分桶使用的色彩空间\n"
                "颜色数：调色板最大颜色数（2–256）\n"
                "树深度：0 = 自动\n"
                "仿色：降低颜色数时的像素抖动方式\n"
                "有序仿色：勾选后改用阈值图做图案仿色（仿色选项同时置灰）\n"
                "海报化色阶(0=关)：每通道再均匀分级，0 = 关闭\n"
                "输出图片序列（Alpha 保留）"
            ),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.color_quantize_sequence(
            cls.sequence(inputs),
            # 标签 → 机器键（后端按机器键分支，稳定不随显示名漂移）。
            colorspace=QUANTIZE_COLORSPACE.key_of(params["colorspace"]),
            colors=int(params["colors"]),
            treedepth=int(params["treedepth"]),
            dither=QUANTIZE_DITHER.key_of(params["dither"]),
            use_ordered=bool(params["use_ordered"]),
            ordered_map=params["ordered_map"],
            levels=params["levels"],
            posterize_levels=int(params["posterize_levels"]),
        )
