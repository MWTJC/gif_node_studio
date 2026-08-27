# GIF 生态补全性调研存档：现有 IM/gifsicle 之外可引入的库与 CLI（2026-08）

> 调研任务：项目针对 GIF 已使用 ImageMagick（Wand，像素处理/合成/量化）与
> gifsicle（GIF 文件级优化 CLI），为充分利用节点式结构的优势，调查还有哪些
> **已得到广泛应用**的库或 CLI 可能引入。
> 最后更新：2026-08-23。
> 相关存档：[gifsicle/pygifsicle 补全性调研](gifsicle-evaluation.md)（决策 #76/#78 已落地）、
> [PS 颜色深度对齐研究](ps-color-reduction.md)（libimagequant 已排除）。
> 本调查的可行性验证（PyAV 进程内 FFmpeg 滤镜图）另存档于技能
> `node-based-media-processing-apps` → `references/pyav-inprocess-filter-graphs.md`。

## 0. 现状快照（读源码确认）

| 环节 | 当前实现 | 技术 |
|---|---|---|
| 视频输入 | PyAV 流式解码 | `av` 18.0.0（已依赖） |
| GIF 解码 | Wand `coalesce`（ctypes 绑 IM 7.1.2，无 CLI 子进程） | `media/backend.py` |
| GIF 合成 | Wand `MagickQuantizeImages` 共享调色板 + **原样合成**（决策 #77） | `_assemble_gif` |
| 颜色量化 | Wand `-quantize/-colors/-treedepth/-dither/-ordered-dither/-posterize`（决策 #76） | `color_quantize_sequence` |
| 像素处理 | PIL + numpy 自实现（43 个节点） | — |
| GIF 文件级优化 | **gifsicle 1.96** CLI 子进程（决策 #78） | `runtime/gifsicle/` |

## 1. 候选全景（按价值排序，许可/版本 2026-08 联网核实）

| 候选 | 类型 | 许可 | 广泛度证据 | 引入形态 |
|---|---|---|---|---|
| **FFmpeg palettegen/paletteuse** | 滤镜，**PyAV 进程内可用（零新依赖）** | LGPL（PyAV wheel） | 全球 ffmpeg GIF 教程/工具（gifify、Mux 等）事实标准 | 「GIF 合成(FFmpeg)」平行节点，合成+编码时帧优化一体 |
| **gifski** | Rust CLI | **AGPL-3.0-or-later** ⚠️ | 5.6k★、gif.ski/Squoosh/ImageOptim 生态 | 「GIF 合成(gifski)」平行节点（需拍板许可与本地色表哲学） |
| **waifu2x-ncnn-vulkan** | CLI（Vulkan GPU） | MIT | 动漫放大事实标准，nihui 便携包 | 「动漫放大」序列节点 |
| **Real-ESRGAN-ncnn-vulkan** | CLI（Vulkan GPU） | MIT（ncnn 版） | 2.2k★、含 **realesr-animevideov3 动漫模型** | 「动漫超分」序列节点 |
| **RIFE / rife-ncnn-vulkan** | CLI（Vulkan GPU） | MIT | 1.1k★、帧插值 SOTA、VapourSynth 生态常用 | 「帧插值」序列节点 |
| **WebP 动画** | Pillow **内建（实测可写）** | BSD | WhatsApp/Telegram/浏览器贴图事实格式 | 「WebP 动画导出」输出节点 |
| **APNG** | Pillow **内建（实测可写）** | BSD | 浏览器原生支持 | 「APNG 导出」输出节点 |
| **FFmpeg 滤镜池**（minterpolate/unsharp/gblur…） | 滤镜 | LGPL | ffmpeg 内置数百成熟滤镜 | 泛化「FFmpeg 滤镜」节点 |
| Anime4K / Anime4KCPP | 着色器/C++ CLI | Anime4K **MIT** 21.3k★；Anime4KCPP CLI 带 video 模块时 **GPLv3** | 动漫实时放大知名方案 | 「动漫放大(轻量)」节点 |
| MP4/WebM 导出 | PyAV 编码器 | LGPL | 现代「GIF 替代」 | 「视频导出」输出节点 |
| scipy/OpenCV/colour-science | Python 库 | BSD | 通用 | 低优先级（成本/收益比差） |
| libimagequant / pygifsicle / Pillow GIF 编码 / moviepy | — | GPLv3+ / — | — | **已存档排除**（见 §5） |

## 2. FFmpeg palettegen/paletteuse（P0：零新依赖、已实测跑通）

**背景**：`palettegen`（两遍法生成整段序列的 256 色优化调色板）+ `paletteuse`
（按调色板映射，带仿色/帧优化）是 ffmpeg 官方钦定的高质量 GIF 管线，
2015 年由 Clément Bœsch 引入，全世界的 mp4→GIF 教程与 gifify/Mux 等工具
都基于它。

**关键发现**：项目已依赖 PyAV 18.0.0，其 `av.filter.Graph` **在进程内**暴露
全部 FFmpeg 滤镜（libavfilter 11.14）。本机实测完整链路跑通：

- 双 `add_buffer` 源（主视频 + palettegen）→ `palettegen(max_colors=256,
  stats_mode=full)` → `paletteuse` → `buffersink` → `av` gif 编码器（pal8）；
- 8 帧 RGB → GIF 编码成功、回读 8 帧；`gifsicle --info` 验证
  **`diff_mode=rectangle` 在编码时直接产出局部帧**（后续帧 = `1x1 at 7,7`、
  `3x3 at 5,0` 最小变化矩形 + 透明索引 255）——gifsicle -O2 级帧优化，
  无需后处理；
- `minterpolate` 滤镜同样可用（帧插值，零依赖低配版）。

**与项目现状的契合点**：

- `stats_mode=full` = 整段序列共享调色板，与项目「MagickQuantizeImages
  共享调色板防闪烁」哲学**一致**（区别于 gifski 的每帧本地色表）；
- 补「编码时帧优化」：现有「GIF 合成」按决策 #77 全幅存储，帧优化要等
  gifsicle 后处理；FFmpeg 管线一条链直接产出「共享调色板 + 局部帧」；
- 参数与现有「颜色量化」节点天然对接（max_colors/dither/bayer_scale 与
  IM `-colors/-dither/-ordered-dither` 语义平行）。

**⚠️ 与既有结论的冲突点**：paletteuse `dither=floyd_steinberg` 同样是误差
扩散——决策 #96 实测「FS 仿色对相同帧输出不同图案（变化敏感）」的教训
**同样适用**：录屏冻结工作流应配 `dither=none` 或确定性有序仿色 `bayer`
（`bayer_scale` 调粒度）。

**落地形态**（已实现，见关键决策 #100）：「GIF 合成(FFmpeg)」作现有
`gif_export` 的平行输出节点（EXPORT_KIND="gif"、CACHE_FILENAME="preview.gif"、
MANIFEST 端口同构），参数 max_colors/stats_mode/dither（bayer/heckbert/
floyd_steinberg/sierra2_4a/atkinson/none）/bayer_scale/diff_mode（帧优化
开关）。不替换 wand 合成（共享调色板精确控制场景保留）。

## 3. gifski（P1：质量天花板，但许可与哲学需拍板）

- Kornel（pngquant/libimagequant 作者）出品，**GIF 编码质量天花板**：每帧
  本地色表（数千色/帧）+ 时域仿色抗闪烁 + 有损 LZW；
- 1.34.0（2025）、Windows CLI zip、5.6k★；gif.ski/Squoosh/ImageOptim 生态；
- CLI 选项（已核实）：`--quality 1-100`、`--motion-quality`、`--lossy-quality`、
  `--width/--height`（缩放）、`--fps`、`--fast/--extra`、glob 输入 PNG 帧序列
  ——**输入恰好是项目序列产物的 PNG 序列，调用模式与 gifsicle 完全一致**；

**两个必须拍板的点**：

1. **许可 = AGPL-3.0-or-later**（GitHub README 明确；比 gifsicle 的 GPLv2
   更严——网络服务使用也触发 copyleft）。初判 MIT/Apache 是**错的**；
2. **设计哲学冲突**：gifski 核心 = 每帧本地色表（所以能「数千色/帧」），
   项目引以为傲的是**全局共享调色板**（防帧间闪烁/偏色，决策 #76/#78 反复
   强调）。两者是平行路线而非替代：共享调色板=稳定可控，gifski=单帧质量
   最大。落地应为独立节点让用户选，且 gifski 输出仍可接 gifsicle 再优化
   （业界标准串联）。

## 4. 动漫工作流候选（P1/P2：与「anime」项目定位高契合）

| 工具 | 版本/许可 | 模型 | 说明 |
|---|---|---|---|
| waifu2x-ncnn-vulkan | 20220728 / MIT | waifu2x 系列（动漫专用） | nihui 便携包（含模型），Vulkan GPU，`-n 降噪 -s 放大` |
| Real-ESRGAN-ncnn-vulkan | v0.2.0 / MIT | **realesr-animevideov3** | 2.2k★，放大+修复一体 |
| Anime4K / Anime4KCPP | MIT（原版 21.3k★）；Anime4KCPP CLI 带 video 模块时 GPLv3 | 实时轻量 | 锐化/放大/降噪，CPU 也能跑 |
| RIFE / rife-ncnn-vulkan | 20221029 / MIT | **rife-anime** | 1.1k★，帧插值；`-n` 目标帧数；低配替代 = FFmpeg `minterpolate`（零依赖） |

三者都是「序列阶段放大 → 再量化 → GIF」的经典动漫工作流（放大后再转 GIF
能显著提升观感，gifski 作者都建议 GIF 前先放大）。CLI 子进程模式与 gifsicle
相同；前置条件：用户机器有 Vulkan 能力的 GPU（或 Anime4K 的 CPU 路径）。

## 5. 已存档排除（无需重新评估）

- **libimagequant**：GPLv3+；PS「存储为 Web 所用格式」对齐黑盒失败
  （`ps-color-reduction.md` §4.7：4 色家族命中 0–2/4、64 色 10–12/64，
  选色逻辑与 PS 频次语义系统性偏离）；Pillow 官方 wheel 未启用
  `Quantize.LIBIMAGEQUANT`；
- **pygifsicle**：纯 subprocess 包装（零额外价值），Windows 二进制停留在
  1.95（缺 1.96 的 oklab/atkinson/use-exact-colormap）——决策 #78 已落地
  直接调用随包 gifsicle.exe；
- **Pillow GIF 编码**：1999 年伪 LZW（Pillow #5278），不用于 GIF 编码；
  WebP/APNG 写入则成熟可用；
- **moviepy/imageio**：GIF 写内核即 Pillow，质量不合格；moviepy 是高层
  API 非节点契合。

## 6. WebP/APNG 动画导出（P0：Pillow 内建，已实测）

Pillow 12.3 实测：`save(save_all=True, append_images=..., duration=, loop=)`
的 **WebP 动画**与 **APNG**（acTL chunk 存在）均直接可写——**零新依赖**。
作为输出节点（与 `png_export` 同构），WebP 动画体积通常远小于 GIF、APNG
无损，是「节点式」白送的两个输出维度。（已实现，见关键决策 #101。）

## 7. 落地优先级与结论

| 优先级 | 候选 | 理由 |
|---|---|---|
| **P0** | FFmpeg palettegen/paletteuse GIF 合成节点 | 零新依赖（PyAV 已有）、实测跑通、补「编码时帧优化」、调色板哲学与现状一致 |
| **P0** | WebP/APNG 动画导出 | Pillow 内建、实测 OK、落地成本低 |
| **P1** | gifski 合成节点 | 质量天花板，但 AGPL + 本地色表哲学需拍板 |
| **P1** | waifu2x / Real-ESRGAN 动漫放大节点 | anime 定位高契合，MIT；需 Vulkan GPU 前提 |
| **P2** | RIFE 帧插值 / FFmpeg minterpolate | 补帧场景；前者需 Vulkan，后者零依赖 |
| **P2** | MP4/WebM 视频导出 | PyAV 编码器可做，工作量大于 GIF 输出 |
| 低 | scipy/OpenCV/colour-science | 通用能力，成本/收益比差，不优先 |

**结论一句话**：最值得引入的不是新 CLI，而是**已被 PyAV 包在项目里的
FFmpeg GIF 管线**（palettegen/paletteuse，业界最广泛、零新依赖、编码时
直接做帧优化）；其次是 gifski（质量天花板，但 AGPL + 本地色表哲学需拍板）
与 waifu2x/Real-ESRGAN 动漫放大（与「anime」定位契合、MIT）；WebP/APNG
导出是 Pillow 白送。

## 8. 参考来源

- ffmpeg palettegen/paletteuse 官方文档：<https://ffmpeg.org/ffmpeg-filters.html>
  （§11.191 palettegen / §11.192 paletteuse；max_colors/stats_mode/
  reserve_transparent/dither/bayer_scale/diff_mode 全部选项语义）
- 高质量 GIF 管线教程：<https://blog.pkh.me/p/21-high-quality-gif-with-ffmpeg.html>
- gifski：<https://github.com/ImageOptim/gifski>（README 许可 = AGPL-3.0-or-later）、
  <https://gif.ski>（CLI 下载/用法）、Releases（1.34.0）
- waifu2x-ncnn-vulkan：<https://github.com/nihui/waifu2x-ncnn-vulkan>（MIT，20220728）
- Real-ESRGAN-ncnn-vulkan：<https://github.com/xinntao/Real-ESRGAN-ncnn-vulkan>
  （MIT，v0.2.0，realesr-animevideov3）
- rife-ncnn-vulkan：<https://github.com/nihui/rife-ncnn-vulkan>（MIT，20221029）
- Anime4K：<https://github.com/bloc97/Anime4K>（MIT，21.3k★）；
  Anime4KCPP：<https://github.com/TianZerL/Anime4KCPP>（视频模块 GPLv3，其余 MIT）
- Pillow WebP/APNG 动画保存：本机 Pillow 12.3 实测（`save_all` 直接可写）
