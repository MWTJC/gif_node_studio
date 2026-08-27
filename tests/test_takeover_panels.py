"""接管型参数（TakeoverParam，决策 #109）面板专项测试。

覆盖：
- 剃刀（RazorCutParam → RazorStripPanel）：values/set_values 往返、程序化
  set_values 不触发 changed、胶片条拖拽（cut_changed）触发 changed 并携带
  cut、手势信号转发、feed_sequence_frames 后取切割处帧；
- 裁剪（CropOverlayParam → CropOverlayPanel）：values 百分比、set_values
  百分比→画布归一化、纵横比下拉联动画布锁定、set_image 后结果缩略图更新、
  set_values 不触发 changed；
- default_params：无值接管声明（裁剪）不进入参数字典，有值接管声明（剃刀
  cut）照常参与；
- takeover_data_sources：按声明暴露外部数据源需求（替代 ui 的 KIND 特判）。

QApplication 必须先于任何节点定义实例化（offscreen）。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from gif_node_studio.nodes.process_nodes import SequenceCropNode
from gif_node_studio.nodes.sequence_nodes import SequenceRazorNode, SequenceTrimNode


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture()
def razor(qt_app):
    return SequenceRazorNode()


@pytest.fixture()
def trim(qt_app):
    return SequenceTrimNode()


@pytest.fixture()
def crop(qt_app):
    return SequenceCropNode()


# --- 剃刀（RazorCutParam → RazorStripPanel） ---


def test_razor_default_params(qt_app):
    """接管声明有值（cut）时照常进入参数字典（执行/存档依赖）。"""
    node = SequenceRazorNode()
    assert node.params == {"cut": 1}
    assert node.panel.values() == {"cut": 1}


def test_razor_values_set_values_roundtrip(razor):
    """values/set_values 往返：程序化恢复不触发 changed（存档恢复语义）。"""
    spy = QSignalSpy(razor.panel.changed)
    razor.panel.set_values({"cut": 5})
    assert razor.panel.values() == {"cut": 5}
    assert spy.count() == 0  # set_values 程序化，不产生用户手势信号
    # 存档恢复后拖拽仍生效（值从接管声明名读写，不生成数值行）
    razor.panel.set_values({"cut": 3})
    assert razor.panel.values() == {"cut": 3}


def test_razor_drag_emits_changed(razor):
    """胶片条拖拽（cut_changed）→ 面板 changed（携带 cut），手势信号转发。"""
    panel = razor.panel
    strip = panel._takeover_widgets["cut"].strip  # 容器内的胶片条画布
    changed_spy = QSignalSpy(panel.changed)
    begin_spy = QSignalSpy(panel.gesture_begin)
    end_spy = QSignalSpy(panel.gesture_end)
    # 喂入帧后模拟拖拽：胶片条先更新内部切割状态再发 cut_changed
    # （与 RazorStripWidget.mouseMoveEvent 一致），容器转发到面板 changed。
    panel.feed_sequence_frames(["a.png", "b.png", "c.png", "d.png"])
    strip.gesture_begin.emit()
    strip.set_cut(2)
    strip.cut_changed.emit(2)
    strip.gesture_end.emit()
    assert panel.values()["cut"] == 2
    assert changed_spy.count() == 1
    assert changed_spy.at(0)[0] == {"cut": 2}
    assert begin_spy.count() == 1 and end_spy.count() == 1


def test_razor_feed_sequence_frames(razor, tmp_path):
    """feed_sequence_frames 后胶片条有帧、切割处预览/只读刷新。"""
    panel = razor.panel
    widget = panel._takeover_widgets["cut"]
    # 造两张 4×4 图（PIL 可用；不可用时跳过本项）
    pytest.importorskip("PIL")
    from PIL import Image

    paths = []
    for i in range(4):
        p = tmp_path / f"f{i}.png"
        Image.new("RGB", (4, 4), (i * 60, 0, 0)).save(p)
        paths.append(str(p))
    panel.feed_sequence_frames(paths)
    assert widget.strip.frame_count() == 4
    panel.feed_sequence_frames([])
    assert widget.strip.frame_count() == 0


def test_razor_side_previews_stay_fixed_size(razor, tmp_path):
    """切割处两侧预览框固定 96×96（适配模式），高分帧不把框撑大。"""
    pytest.importorskip("PIL")
    from PIL import Image

    panel = razor.panel
    widget = panel._takeover_widgets["cut"]
    paths = []
    for i in range(4):
        p = tmp_path / f"big{i}.png"
        Image.new("RGB", (400, 300), (i * 60, 0, 0)).save(p)
        paths.append(str(p))
    panel.feed_sequence_frames(paths)
    # 400×300 帧下框仍为固定适配框边长（1:1 跟随模式的旧行为会撑到 ~400×300）
    expected = QtCore.QSize(widget.SIDE_PREVIEW_SIZE, widget.SIDE_PREVIEW_SIZE)
    assert widget.preview_a.size() == expected
    assert widget.preview_b.size() == expected
    # 内容确实喂入（适配 contain，不放大）
    assert widget.preview_a._content is not None
    panel.release_preview()
    assert widget.preview_a.size() == expected


def test_razor_show_preview_none_keeps_side_previews(razor, tmp_path):
    """运行后刷新（show_preview(None)）不清空切割处两侧帧预览（与一般节点
    运行后显示结果一致）；release_preview（节点删除）仍显式清空。"""
    pytest.importorskip("PIL")
    from PIL import Image

    panel = razor.panel
    widget = panel._takeover_widgets["cut"]
    paths = []
    for i in range(4):
        p = tmp_path / f"f{i}.png"
        Image.new("RGB", (40, 30), (i * 60, 0, 0)).save(p)
        paths.append(str(p))
    panel.feed_sequence_frames(paths)
    assert widget.preview_a._content is not None
    assert widget.preview_b._content is not None
    # 运行完成路径：preview_path_for_node 先喂帧，随后 show_preview(None)
    # （胶片条类接管的预览路径恒为 None）——不得把刚喂入的预览清空。
    panel.show_preview(None)
    assert widget.preview_a._content is not None
    assert widget.preview_b._content is not None
    assert widget.readout.text().startswith("切割位置：第")
    # 节点删除/移除路径仍显式清空。
    panel.release_preview()
    assert widget.preview_a._content is None
    assert widget.preview_b._content is None


def test_razor_data_sources(razor):
    """接管数据源声明：剃刀需要上游序列全帧。"""
    assert razor.panel.takeover_data_sources() == frozenset({"sequence_frames"})


# --- 序列截取（TrimRangeParam → TrimStripPanel，决策 #115） ---


def test_trim_default_params(qt_app):
    """无值接管声明（trim_range 纯声明）不进入参数字典；start/end 照常参与。"""
    node = SequenceTrimNode()
    assert node.params == {"start": 0, "end": 1}
    assert "trim_range" not in node.params
    assert node.panel.values() == {"start": 0, "end": 1}


def test_trim_values_set_values_roundtrip(trim):
    """values/set_values 往返：程序化恢复不触发 changed（存档恢复语义）。"""
    spy = QSignalSpy(trim.panel.changed)
    trim.panel.set_values({"start": 2, "end": 5})
    assert trim.panel.values() == {"start": 2, "end": 5}
    assert spy.count() == 0  # set_values 程序化，不产生用户手势信号
    # 单键恢复（旧存档可能只更新一边）
    trim.panel.set_values({"end": 3})
    assert trim.panel.values() == {"start": 2, "end": 3}


def test_trim_drag_emits_changed(trim):
    """胶片条拖拽（range_changed）→ 面板 changed（携带 start/end），手势信号转发。"""
    panel = trim.panel
    strip = panel._takeover_widgets["trim_range"].strip  # 容器内的胶片条画布
    changed_spy = QSignalSpy(panel.changed)
    begin_spy = QSignalSpy(panel.gesture_begin)
    end_spy = QSignalSpy(panel.gesture_end)
    # 喂入帧后模拟拖拽：胶片条先更新内部区间再发 range_changed
    # （与 TrimStripWidget.mouseMoveEvent 一致），容器转发到面板 changed。
    panel.feed_sequence_frames(["a.png", "b.png", "c.png", "d.png"])
    strip.gesture_begin.emit()
    strip.set_range(1, 3)
    strip.range_changed.emit(1, 3)
    strip.gesture_end.emit()
    assert panel.values() == {"start": 1, "end": 3}
    assert changed_spy.count() == 1
    assert changed_spy.at(0)[0] == {"start": 1, "end": 3}
    assert begin_spy.count() == 1 and end_spy.count() == 1


def test_trim_feed_sequence_frames(trim, tmp_path):
    """feed_sequence_frames 后胶片条有帧；空列表清空。"""
    pytest.importorskip("PIL")
    from PIL import Image

    panel = trim.panel
    widget = panel._takeover_widgets["trim_range"]
    paths = []
    for i in range(4):
        p = tmp_path / f"f{i}.png"
        Image.new("RGB", (4, 4), (i * 60, 0, 0)).save(p)
        paths.append(str(p))
    panel.feed_sequence_frames(paths)
    assert widget.strip.frame_count() == 4
    panel.feed_sequence_frames([])
    assert widget.strip.frame_count() == 0


def test_trim_range_clamping(trim, tmp_path):
    """set_range 越界按帧数钳制（喂帧后）；无帧时保留原值（执行时后端校验）。"""
    pytest.importorskip("PIL")
    from PIL import Image

    panel = trim.panel
    widget = panel._takeover_widgets["trim_range"]
    # 无帧：原始值保留（不静默改写，越界由 backend.trim_sequence 报清晰错误）
    widget.set_range(-5, 1000000)
    assert widget.range() == (0, 1000000)
    # 喂帧后：钳制到合法区间且 start < end 恒成立
    paths = []
    for i in range(4):
        p = tmp_path / f"f{i}.png"
        Image.new("RGB", (4, 4), (i * 60, 0, 0)).save(p)
        paths.append(str(p))
    panel.feed_sequence_frames(paths)
    widget.set_range(-5, 1000000)
    assert widget.range() == (0, 4)    # end 钳制到帧数
    widget.set_range(3, 1)
    assert widget.range() == (3, 4)    # end 钳制到 start+1（区间恒非空）
    widget.set_range(99, 99)
    assert widget.range() == (3, 4)    # start 钳制到帧数-1


def test_trim_side_previews_stay_fixed_size(trim, tmp_path):
    """区间起止两侧预览框固定 200×200（适配模式），高分帧不把框撑大。"""
    pytest.importorskip("PIL")
    from PIL import Image

    panel = trim.panel
    widget = panel._takeover_widgets["trim_range"]
    paths = []
    for i in range(4):
        p = tmp_path / f"big{i}.png"
        Image.new("RGB", (400, 300), (i * 60, 0, 0)).save(p)
        paths.append(str(p))
    panel.feed_sequence_frames(paths)
    # 400×300 帧下框仍为固定适配框边长（1:1 跟随模式的旧行为会撑到 ~400×300）
    expected = QtCore.QSize(widget.SIDE_PREVIEW_SIZE, widget.SIDE_PREVIEW_SIZE)
    assert widget.preview_a.size() == expected
    assert widget.preview_b.size() == expected
    # 内容确实喂入（适配 contain，不放大）
    assert widget.preview_a._content is not None
    panel.release_preview()
    assert widget.preview_a.size() == expected


def test_trim_show_preview_none_keeps_previews(trim, tmp_path):
    """运行后刷新（show_preview(None)）不清空区间起止帧预览；release_preview 清空。"""
    pytest.importorskip("PIL")
    from PIL import Image

    panel = trim.panel
    widget = panel._takeover_widgets["trim_range"]
    paths = []
    for i in range(4):
        p = tmp_path / f"f{i}.png"
        Image.new("RGB", (40, 30), (i * 60, 0, 0)).save(p)
        paths.append(str(p))
    panel.feed_sequence_frames(paths)
    assert widget.preview_a._content is not None
    assert widget.preview_b._content is not None
    # 运行完成路径：preview_path_for_node 先喂帧，随后 show_preview(None)
    # （胶片条类接管的预览路径恒为 None）——不得把刚喂入的预览清空。
    panel.show_preview(None)
    assert widget.preview_a._content is not None
    assert widget.preview_b._content is not None
    assert widget.readout.text().startswith("截取范围：第")
    # 节点删除/移除路径仍显式清空。
    panel.release_preview()
    assert widget.preview_a._content is None
    assert widget.preview_b._content is None


def test_trim_data_sources(trim):
    """接管数据源声明：截取需要上游序列全帧。"""
    assert trim.panel.takeover_data_sources() == frozenset({"sequence_frames"})


# --- 裁剪（CropOverlayParam → CropOverlayPanel） ---


def test_crop_default_params(qt_app):
    """无值接管声明（裁剪）不进入参数字典/模型属性；四角与纵横比保留。"""
    node = SequenceCropNode()
    assert "crop" not in node.params
    assert node.params["left"] == 0.0 and node.params["right"] == 100.0
    assert node.params["aspect"]  # 保留常规参数行（联动）
    values = node.panel.values()
    assert set(values) == {"aspect", "left", "top", "right", "bottom"}
    assert values["left"] == 0.0 and values["right"] == 100.0


def test_crop_values_percent(crop):
    """面板侧值为百分比；set_values 百分比 → 画布归一化。"""
    panel = crop.panel
    widget = panel._takeover_widgets["crop"]
    panel.set_values({"left": 12.5, "top": 25.0, "right": 87.5, "bottom": 75.0})
    # 画布内部为归一化
    assert widget.canvas.values() == {
        "left": 0.125, "top": 0.25, "right": 0.875, "bottom": 0.75,
    }
    # 面板侧仍为百分比
    assert panel.values()["left"] == 12.5
    assert panel.values()["bottom"] == 75.0


def test_crop_set_values_no_changed(crop):
    """set_values 程序化恢复不触发 changed（存档恢复语义）。"""
    spy = QSignalSpy(crop.panel.changed)
    crop.panel.set_values({"left": 10.0, "top": 10.0, "right": 90.0, "bottom": 90.0})
    assert spy.count() == 0


def test_crop_aspect_link(crop):
    """纵横比下拉（linked 常规行）→ 画布锁定；联动发出 changed。"""
    panel = crop.panel
    aspect = panel.widgets["aspect"]
    canvas = panel._takeover_widgets["crop"].canvas
    assert canvas.aspect_ratio() is None  # 默认「自由」
    spy = QSignalSpy(panel.changed)
    aspect.setCurrentText("16:9")
    assert canvas.aspect_ratio() == pytest.approx(16.0 / 9.0)
    assert spy.count() >= 1


def test_crop_set_image_updates_result(crop, tmp_path):
    """set_image 后结果缩略图按当前参数实时裁剪。"""
    pytest.importorskip("PIL")
    from PIL import Image

    panel = crop.panel
    widget = panel._takeover_widgets["crop"]
    p = tmp_path / "src.png"
    Image.new("RGB", (40, 20), (200, 50, 50)).save(p)
    widget.set_image(str(p))
    assert widget.canvas.has_image()
    assert not widget.result_preview.pixmap().isNull()
    panel.release_preview()
    assert not widget.canvas.has_image()


def test_crop_data_sources(crop):
    """接管数据源声明：裁剪需要上游首帧/清单预览。"""
    assert crop.panel.takeover_data_sources() == frozenset({"first_frame"})
