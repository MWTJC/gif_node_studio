# 已知限制与改进方向

1. **~~跨模式截取混链不可合成~~（已修复，见[规格合成语义](spec-composition.md#修复原则)/[关键决策 #28](decisions/21-30.md#d28)）**：截取节点改为
   串行合成（`compose_trim`），后级基于前级窗口进一步截取，跨模式（time↔frame）
   同样叠加。跨模式合成所需的帧率用「总帧数 / 总时长」估算，对 VFR（可变帧率）
   视频为近似值；帧窗口换算的边界取整亦为近似（精确到帧号取整）。
2. **重命名不重排布局**：NodeGraphQt 对 `name` 属性不触发 `draw_node`，节点尺寸创建时定型。
   如需即时适配，可在重命名路径主动调用 `node.view.draw_node()`（未实现）。
3. **目录构建需 QApplication**：`_definitions_by_class()` 实例化全部节点类（约 57 ms），
   只能在 QApplication 存在时调用；无 GUI 的脚本场景需先建 Qt 应用。
4. **旧预设数值级迁移**：`TrimNode`/`kind="trim"` 已移除；且截取节点参数已从秒/帧号 → 百分比（0–100%）
   又改为「起点 % + 持续秒数/帧数」（`start`/`duration`），旧预设中的 `start`/`end` 数值
   会被重新解释或丢失，需重建（无迁移实现；如需兼容，可注册 `TrimNode` 别名类并映射到 `time_trim`）。
   自[决策 #80](decisions/71-80.md#d80)起，旧存档**可以读取**：已删除的
   参数/节点类型会被丢弃并弹窗提示（不再抛 `NodePropertyError` 崩溃），缺失参数回落默认值；
   但旧数值不会被换算为新的参数语义。
5. **`runner/engine.py` 未使用**：`ExecutionGraph`/`RuntimeNode` 无任何引用（历史遗留）。
   建议：删除，或作为框架无关引擎恢复用于无 GUI 测试/批处理。
6. **视频首帧提取成本**：`extract_first_frame` 用 PyAV 解码至首个关键帧（首个 GOP），
   成本随 GOP 长度增长；对预览而言可接受，极端长 GOP 视频可考虑 seek 到首个关键帧。
7. **GIF 后端**：仅使用 Wand（不再需要 `magick.exe` CLI）。若部署环境连 ImageMagick 动态库都没有，GIF 解包/导出会报错（视频链路不受影响）；发行时需随程序携带 ImageMagick 运行时（[发行打包](packaging.md)）。
8. **进度粒度**：移除 `MagickSetImageProgressMonitor` 后（[关键决策 #12](decisions/11-20.md#d12)），GIF 的
   coalesce/量化/优化等长操作期间进度条为不确定态（逐帧/逐阶段仍有显式进度）。
9. **测试现状**：共 219 项（offscreen，`QT_QPA_PLATFORM=offscreen`），覆盖目录/构造/预设
   往返/预览/合成语义/颜色深度/序列处理/设置/导出并行/通道合并/右键菜单/空闲暂停等；
   详细清单与注意点见
   [验证方式](testing.md)。
10. **可视化裁剪 overlay 为静态首帧（1:1）**：`CropOverlayWidget` 显示的是上游输出
    序列的首帧（或清单预览图），红框作用于全部帧；对 GIF 动画预览可考虑在 overlay
    上叠加 `GifPreviewPlayer` 播放（未实现）。overlay 预览框默认 **1:1**（框 = 素材
    物理像素 ÷ 当前 DPR，跟随图片，交互直面原始像素，与「图片1:1分辨率查看」一致——
    超大素材会把预览框撑大）；`set_1to1(False)` 可退回旧「固定 200×200 等比适配
    （可放大也可缩小）」交互区（见 `CropOverlayWidget` 类注释）。
11. **GIF 预览解码后端固定为 Wand（P2，2026-08 生态调查建议）**：`GifPreviewPlayer`
    解码与格式化解包同源（Wand coalesce + `_wand_rgba_bytes`）；若未来想减少对
    ImageMagick 的依赖，可做**解码后端可插拔**（Wand/Pillow 二选一）。Pillow 逐帧
    `seek` + `convert("RGBA")` 也能正确处理透明索引，但有已知坑（Pillow #4644 丢
    透明索引 / #4650 alpha_composite 丢透明），且照样要自写播放器框架——属可选
    优化，非缺陷。
12. **颜色深度算法升级候选（P3，2026-08 黑盒结论）**：项目黑盒差分
    （`ps-color-reduction-research` 独立仓库 `REPORT.md`）结论：与 PS 的
    家族级对齐最优路径是 **kmeans 家族**——adaptive→IM `-kmeans`（内建、零新依赖；
    4K·256 色 >120s 需降采样）、perceptual→Lab 空间 kmeans（自实现或 sklearn）；
    libimagequant 已实测排除（语义系统性偏离）。⚠️ **「颜色深度」节点已过时**
    （[决策 #76](decisions/71-80.md#d76)），
    本条目作为 PS 对齐研究方向仍有效；新工作流用 IM 原生「颜色量化」节点
    （`color_quantize`，含 `-quantize` 色彩空间/`-treedepth`/`-dither`/
    `-ordered-dither`/`-posterize`），详见
    [PS 颜色深度对齐研究存档](research/ps-color-reduction.md#4-关键结论可复用)。
    gifsicle/pygifsicle 补全性调研见 [research/gifsicle-evaluation.md](research/gifsicle-evaluation.md)。
13. **空闲 CPU（已实现，见[关键决策 #63](decisions/61-70.md#d63)）**：
    非前台（最小化/隐藏/失焦）时自动暂停 GIF 预览播放（QMovie/自定义播放器）并冻结
    画布重绘，恢复前台后继续——实测 800×450 1:1 GIF 播放 ≈10% CPU → 暂停后 0.00%。
    剩余空转开销：5s 心跳定时器（faulthandler 卡死检测，<0.1%）与 Qt 事件循环
    本身，无法再降（检测机制的有意取舍）。
14. **GIF 优化节点依赖 gifsicle CLI 子进程（2026-08 已实现，见[关键决策 #78](decisions/71-80.md#d78)）**：
    「GIF 优化」节点通过 subprocess 调用随包 `runtime/gifsicle/gifsicle.exe`
    （GPL v2——随包分发需附其源码与 LICENSE，不感染宿主应用）。这与第 7 条的
    「纯 ctypes Wand、不派生 magick.exe」决策不同：gifsicle 无官方库/ctypes
    绑定、Python 生态无等价替代，只能做文件进/文件出的单次有界子进程调用。
    运行时缺失时该节点报清晰中文错误；设置「关于」页显示 gifsicle 版本探测
    结果。优化语义边界：`-O` 不保证缩小（罕见变大）；`--lossy/--colors`
    不可逆，重复优化累积伪影。
15. **GIF 合成(FFmpeg) 节点 = PyAV 进程内滤镜图（2026-08 已实现，见[关键决策 #100](decisions/91-100.md#d100)）**：
    「GIF 合成(FFmpeg)」用 `av.filter.Graph` 进程内跑 palettegen/paletteuse 业界事实标准
    管线（零新依赖），`diff_mode=rectangle` 编码时直接产出局部帧（gifsicle -O2 级）。
    与 wand「GIF 合成」（原样合成）平行：wand 管共享调色板精确控制（录屏冻结等
    确定性调色板场景），FFmpeg 管线管「调色板 + 编码时帧优化」一体。已知边界：
    ① paletteuse 在调色板到达前会 FIFO 缓冲全部帧（内存 ≈ 序列总像素，与 wand
    `MagickQuantizeImages` 全量装配同量级，非流式）；② paletteuse 的
    Floyd-Steinberg 仿色对相同帧输出不同图案（决策 #96 教训同样适用，录屏工作流
    配无仿色或 Bayer 有序仿色）；③ FFmpeg 编码器对非整除帧速有 ±1 厘秒延迟取整。
16. **WebP/APNG 动画导出 = Pillow 内建（2026-08 已实现，见[关键决策 #101](decisions/101-110.md#d101)）**：
    `WebpExportNode`/`ApngExportNode` 用 Pillow `save_all` 直接写动画（零新依赖）。
    ⚠️ **Pillow WebP 动画有损路径丢失 alpha**（透明区域写为不透明黑；单帧有损/动画
    无损均正常，Pillow #8101 同类缺陷）——序列含透明时后端自动强制无损编码，
    节点元数据如实报告。APNG 为无损格式无此问题。
