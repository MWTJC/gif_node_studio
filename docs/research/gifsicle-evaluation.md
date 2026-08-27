# gifsicle/pygifsicle 补全性调研存档（2026-08）

> 调研任务：在 GIF 处理（颜色深度、仿色、调色板取色、GIF 压缩优化）方面，
> 当前 wand 是否有不足可由 pygifsicle 补全。本文为结论存档；后续按结论
> 另行实现独立节点（见[关键决策 #76](../decisions/71-80.md#d76)）。
> 最后更新：2026-08-18。

> ✅ **已实施（2026-08，[关键决策 #78](../decisions/71-80.md#d78)）**：
> 按本文 §3 集成建议落地「GIF 优化」节点（`gif_optimize`，输入=格式化清单，
> 输出=优化后 GIF 清单）：优化级别 -O1/-O2/-O3、有损度 --lossy、GIF 级降色
> （--colors+--color-method+--dither+--use-colormap 固定色板）、--careful；
> gifsicle 1.96 已本机编译（见 §4 风险表的 Windows 版本缺口——自编译补上
> oklab/atkinson/use-exact-colormap）并随包放于 `runtime/gifsicle/`；
> **未采用 pygifsicle**——它本质是 subprocess 包装（零额外价值），且其
> Windows 二进制仍停留在 eternallybored 1.95；设置「关于」页新增 gifsicle
> 版本探测。验证结论见[关键决策 #78](../decisions/71-80.md#d78)。

---

## 0. 调研对象与事实速览

**项目现状**（读源码确认）：GIF 链路全走 Wand（`media/backend_export.py`）——解码
`coalesce()`、组装 `_assemble_gif`（共享 `MagickQuantizeImages` 量化 +
Python 侧内容包围盒裁剪替代 IM 的 optimize_layers/transparency，[决策 #75]）、
颜色深度节点 `color_reduce_sequence`（octree sRGB / Lab 感知量化 + 固定色板
remap）。GIF 合成节点（`GifExportNode`）不暴露颜色数/仿色参数（导出固定
256 色 FloydSteinberg），降色只能靠上游「颜色深度」节点在 PNG 序列阶段做，
**没有 GIF 文件级压缩优化环节**。

**pygifsicle 事实**（PyPI JSON + GitHub 源码核实）：
- 最新 **1.1.0**（2024-07-06），MIT，零依赖，**纯 subprocess 包装**
  （`gifsicle()` / `optimize()`，`options` 参数透传任意 CLI 参数），
  **不捆绑二进制**——Windows 需自装 gifsicle.exe（官方不支持 Windows，
  [eternallybored.org](https://eternallybored.org/misc/gifsicle/) 提供
  **1.95** win32/win64/winarm64 移植版）。
- 底层 gifsicle（Eddie Kohler，C 程序，1997 至今）最新 **1.96**
  （2025-02-26）；`--lossy`（Kornel Lipiński 贡献）、`--gamma=srgb|oklab`、
  `--dither=atkinson`、`--use-exact-colormap` 等；生产使用广泛（ezgif.com、
  imagemin gifsicle-bin 周下载 51 万、Tumblr 曾赞助缩放改进）。**GPL v2**。

**Python 生态定位**：GIF 优化领域 gifsicle 是事实标准——"There are not many
good options in Python for gif optimization. There is one excellent CLI tool
that does just that called Gifsicle"（2025-01 实测文）；Pillow 的 GIF 编码至今
还是 1999 年的非标准伪 LZW（[Pillow #5278](https://github.com/python-pillow/Pillow/issues/5278)），
不适合做 GIF 编码。**没有等价纯 Python 库替代 gifsicle**。

---

## 1. 分项对比：wand 的不足 vs gifsicle 能补什么

### 1.1 GIF 压缩优化（差距最大、补全价值最高）✅

| 能力 | wand / ImageMagick | gifsicle / pygifsicle |
|---|---|---|
| 有损压缩 | **无**（GIF 编码器只有标准 LZW，无 lossy 概念） | `--lossy[=0..200]`（默认 20）：在 LZW 层面改写像素颜色换取体积，gamma 感知（1.96 起按所选色彩空间算色差，可 `--gamma=oklab`） |
| 无损帧优化 | `-layers Optimize` **有正确性 bug**（[#3520 输出破损](https://github.com/ImageMagick/ImageMagick/issues/3520)、[#838 帧重叠](https://github.com/ImageMagick/ImageMagick/issues/838)；项目自身 [决策 #75](../decisions/71-80.md#d75) 证实会破坏透明底，被迫手写 bbox 裁剪） | `-O1/O2/O3` 20+ 年打磨的专用优化器：只存变化区域、透明优化、**-O3 多策略择优**、色表重排、删除未用色、`-Okeep-empty` |
| 效果实测 | — | 3.7MB → **613KB**（`-O3 --colors 48 --lossy`，[Simon Willison](https://til.simonwillison.net/imagemagick/compress-animated-gif)）；`-O3 --colors 256 --lossy=80` **体积减半以上**（[IM discussion #2693](https://github.com/ImageMagick/ImageMagick/discussions/2693)） |

**关键点**：这是 wand **完全没有**的能力——保留现有全部 wand 链路，在
`_assemble_gif` 之后加一步 gifsicle 后处理，就能获得"体积减半"级别的优化；
IM 自己的文档都承认 LZW 优化层面 gifsicle 更优、建议极端压缩时用多工具对比
（[IM anim_opt](https://usage.imagemagick.org/anim_opt)）。现有手写
`_crop_frame_to_content`（决策 #75 替代品）可被 `-O3` 整体替换或作为补充。

### 1.2 颜色深度 ✅（部分补全）

- IM **只有一个量化算法**（octree/adaptive spatial subdivision，[官方文档自认](https://usage.imagemagick.org/quantize)）；
  pngquant 作者 Kornel 还批评 IM 量化"不必要地 posterize、不支持 alpha、较慢、
  开 dithering 时可能产出 malformed 图"（[IM 论坛 t=23340](https://jqmagick.imagemagick.org/discourse-server/viewtopic.php?t=23340)）。
- gifsicle `--colors 2–256` + `--color-method`：
  - **diversity**（默认，xv 算法）：**从现有颜色取严格子集**——**不产生原图
    不存在的中间色**。这正好打在项目对 octree 的核心痛点上（[PS 颜色深度对齐研究](ps-color-reduction.md#33-本质差异im-octree-vs-ps-adaptive)：
    octree 叶均值产出 (33,42,74) 等原图不存在的颜色，而 PS Adaptive 选原图真实颜色）；
  - **blend-diversity**（对色群取混合色）、**median-cut**（Heckbert 经典算法——
    项目曾自实现过 Lab 中位切割后回退）；
  - `--gamma=srgb|oklab`（1.96）做感知校正选色。
- 但**边界**：gifsicle 只读 GIF（输入已 ≤256 色），只能做"GIF 级再降色"，
  不能吃 truecolor/PNG 序列。

### 1.3 仿色 ✅（补全：新算法 + 抗闪烁有序仿色）

- IM 量化路径只有 floyd_steinberg / riemersma / no；有序仿色靠
  `ordered_dither` 阈值图（项目已用于 pattern）。
- gifsicle `--dither[=method]`：**floyd-steinberg / atkinson**（1.96 新增，
  IM 量化路径没有；局部化图案，适合大片纯色）/ **ro64 / o3 / o4 / o8 /
  ordered**（有序模式，**专为避免动画帧间闪烁伪影设计**——1.77 起）/
  halftone / squarehalftone / diagonal（特殊效果）。1.77 起选色与仿色都做
  gamma 校正。
- 同样仅作用于 GIF 级降色路径。

### 1.4 调色板取色 ✅（补全：原生固定色板 + 色板文件）

- 项目现状：固定色板（web 216 / Windows / MacOS / 黑白）手写 PNG 色板 blob +
  `remap`（`_websafe_map_blob` / `system_palette_blob` / `_palette_png_blob`）。
- gifsicle 原生支持：
  - **`--use-colormap web|gray|bw|文件`**：web = 216 色 Web-safe 色板（内建！）；
    文件可为**文本色板**（每行 `r g b` 或 `#rrggbb`）或 **GIF 文件的全局色表**；
    配合 `--colors N` 取色板子集；
  - **`--use-exact-colormap`**（1.96）：原样使用色板、不重排不丢色；
  - `--colors` 文档明确可"eliminate any local color tables"，对整个动画选
    **全局调色板**（与项目共享调色板语义一致）；透明 GIF 会自动预留 +1 色。
- 项目 `data/palettes/*.png`（256×1 RGB）换成文本色板文件即可直接给 gifsicle
  用，可完全替代手写 remap 路径的这部分。

---

## 2. 诚实评估：pygifsicle **不能**补全的

1. **PS「存储为 Web 所用格式」家族级对齐**（[PS 对齐研究](ps-color-reduction.md)的核心目标）：
   gifsicle 的 diversity/blend-diversity/median-cut 不是"频次 + 感知加权"选色
   （PS Adaptive/Perceptual 私有算法）；**kmeans 家族结论不变**（adaptive→
   IM `-kmeans`、perceptual→Lab kmeans）。不过 diversity 的"严格子集"性质
   值得用项目的 PS 测试集黑盒对比一次（零代码，纯 CLI，成本极低）——若命中率
   显著提升，可作为一个低成本替代候选。
2. **逐帧 PNG 序列阶段**（颜色深度节点 `DitherNode` 的主体）：gifsicle 只能
   处理 GIF 文件，PNG 中间序列仍走 wand，该节点不换。
3. **GIF 解码/预览/coalesce**：gifsicle 不提供 RGBA 帧解码，预览链
   （QMovie / `GifPreviewPlayer`）不动。它只有 `--explode`（拆成单帧 GIF），
   不是像素解码 API。
4. **大调色板下的共享量化质量**：项目实验已证大 N 时 octree 共享量化优于自实现
   中位切割；gifsicle 的 median-cut 是"另一种启发式"，对项目素材效果未知，
   **不能默认更好**，需实测。
5. **Alpha/半透明**：GIF 格式本身只有 1-bit 透明，gifsicle 不解决任何 PNG
   中间阶段问题。

---

## 3. 集成建议（与项目架构的契合点）

**推荐形态：GIF 合成（wand 现状，不动）→ 新增「GIF 优化」节点（gifsicle 后处理）**

```
PNG 序列 → [颜色量化 wand] → [GIF 合成 wand] → preview.gif → [GIF 优化 gifsicle] → 优化后 GIF + 清单
```
- 输入取「格式化清单」（与现有 `gif优化分析` 节点同输入类型、天然衔接），输出
  更新后的清单，`ui/main_window_preview.py` 按 `EXPORT_KIND` 分派导出不变；
- **gif优化分析 节点的「存储帧」视图可直接目检优化效果**（bbox 裁剪、透明化、
  色表变化一目了然）——项目已有现成检视工具；
- 参数建议：优化级别（-O1/O2/O3）、有损度（lossy 0–200）、颜色数/取色方法
  （可选，GIF 级再降色）、仿色方法、固定色板（web/自定义文本色板）、
  `--careful` 开关；
- **不推荐**用 gifsicle 完全接管组装（PNG→单帧 GIF→merge）：每帧独立色表会
  破坏项目引以为傲的**共享调色板**（帧间闪烁/偏色，正是 `MagickQuantizeImages`
  解决的问题）。wand 负责"像素质量"，gifsicle 负责"文件体积"，职责清晰。

**实现层**：pygifsicle 只是 subprocess 包装（options 透传），直接用 gifsicle.exe
或引 pygifsicle（MIT、5KB）都行；gifsicle.exe 随包放 `runtime/gifsicle/`，
与现有 ImageMagick 运行时同模式（`scripts/prepare_*` 脚本下载、
`app_root_dir()` 定位、缺失时明确报错）。

---

## 4. 风险与注意事项（需用户拍板）

| 风险 | 说明 |
|---|---|
| **子进程依赖** | 项目决策刻意去掉了 magick.exe CLI（改纯 ctypes Wand，[limitations](../limitations.md)）。gifsicle **只有 CLI、无官方库/ctypes 绑定**，pygifsicle 本质就是 subprocess——引入一次有界子进程调用（文件进/文件出）。Python 生态无等价替代（Pillow GIF 编码不合格）。 |
| **GPL v2 许可** | gifsicle 是 GPL v2。项目此前因 **libimagequant GPLv3+ 许可放弃引入**（[PS 研究 §4.7](ps-color-reduction.md#4-关键结论可复用)）——gifsicle 有同类义务：随发行包分发 gifsicle.exe 需附其源码与 LICENSE（GPL 覆盖仅限 gifsicle 本身，不感染宿主应用，实践即"附源码+许可文件"，与现有 IM 运行时的 License.txt/NOTICE.txt 模式可并存）。需接受该义务才能随包分发。 |
| **Windows 二进制版本** | eternallybored 目前只提供 **1.95**：有 `--lossy`（1.92 起）、但**没有** 1.96 的 `--gamma=oklab` / `--dither=atkinson` / `--use-exact-colormap`；1.96 需自行编译。 |
| **无损保证** | `-O` 不保证缩小（罕见情况变大）；`--lossy`/`--colors` 不可逆，重复优化累积伪影。 |
| **播放器兼容** | 极小化 GIF 在个别实现（老 Java/IE）显示异常→ `--careful`；项目内 QMovie/自研播放器一般无问题，导出外部使用需留意。 |

---

## 5. 结论一句话

> **有，且补全价值很高——但补的是"GIF 文件级"的能力**：wand 完全没有的
> **`--lossy` 有损压缩**（体积可减半以上）、比 IM `-layers Optimize` 更可靠
> 更小的 **`-O3` 无损帧优化**（替代决策 #75 的手写 bbox 裁剪）、**3 种额外
> 取色方法 + 内建 web 色板/文本色板 + oklab 感知校正**、以及
> **atkinson/ro64/ordered 等仿色方法**。它**补不了**的是 PS 家族对齐（kmeans
> 结论不变）、PNG 序列阶段处理、GIF 解码预览。落地形态 = 「GIF 优化」后处理
> 节点，前置条件 = 接受一次 CLI 子进程依赖 + GPL v2 随包义务。

**低成本验证计划**（零代码即可先行）：① 用现有产物跑
`gifsicle -O3 / -O3 --lossy=30..80 / --colors 128` 对比体积与观感（配合
gif优化分析 存储帧视图）；② 用 `ps-color-reduction-research` 的取色_001.jpg
测试集跑 `--colors 4 --color-method diversity/blend-diversity/median-cut`
黑盒对比 PS 家族命中率（验证 diversity 严格子集假设）；③ 透明 GIF 回归（验证
-O3 不破坏透明，对照决策 #75 场景）。

---

## 6. 参考来源

- gifsicle 官网/手册：<https://www.lcdf.org/gifsicle/man.html>（--lossy、
  --optimize、--color-method、--dither、--use-colormap、--gamma、--careful、
  --conserve-memory 等全部选项语义）
- gifsicle 变更历史：<https://www.lcdf.org/gifsicle/changes.html>（1.96：
  lossy 色差算法重写 + --gamma=oklab + --dither=atkinson + --use-exact-colormap）
- pygifsicle：<https://github.com/LucaCappelletti94/pygifsicle>（1.1.0，
  2024-07-06，MIT，纯 subprocess 包装，Windows 需自装二进制）
- Windows 移植版：<https://eternallybored.org/misc/gifsicle/>（1.95 win32/win64/winarm64）
- IM 官方量化页（只承认 octree 一种算法）：<https://usage.imagemagick.org/quantize>
- IM 动画优化页（承认 gifsicle LZW 优化更强）：<https://usage.imagemagick.org/anim_opt>
- IM -layers optimize 破损：<https://github.com/ImageMagick/ImageMagick/issues/3520>、
  <https://github.com/ImageMagick/ImageMagick/issues/838>
- 实测体积对比：<https://til.simonwillison.net/imagemagick/compress-animated-gif>、
  <https://github.com/ImageMagick/ImageMagick/discussions/2693>
- pngquant 作者对 IM 量化的批评：<https://jqmagick.imagemagick.org/discourse-server/viewtopic.php?t=23340>
- Pillow GIF 伪 LZW：<https://github.com/python-pillow/Pillow/issues/5278>
