# 源侧伪影调查与候选新节点评估：视频录屏转出的 GIF（motion.gif 场景）

> 研究存档（2026-09）。结论先行：
> ① **「简易处理 → 重新导出」不可改善这类 GIF**——伪影（振铃白边、3 px 软边、抖动杂色）全部已在源文件里，
> 颜色域操作（调色板/仿色/降色/换编码器）无法触及，只能以「色带」换「噪点」，实测最差的一档（256 色 + FS 仿色）体积与斑点都变差。
> ② 真正对症的是**空间域滤波**：实测 **kuwahara 2（保边平滑）** 同时把振铃过冲 6.38→1.10、过冲边缘 50.3%→8.3%、
> 平坦区噪声 1.22→0.02，且**边缘过渡宽度 3.0→2.0 px（变锐）**；**形态学 open** 把过冲压到 ≈0。
> ③ **锐化类一律恶化**（过冲 +121%~+131%），必须"先去斑再锐化"或做"过冲钳制"，不能裸锐化。
> ④ 本机 PyAV 内含 **468 个 libavfilter 滤镜**（含 deblock / deband / median / removegrain / bilateral / bilateral_cuda /
> unsharp / cas / morpho / xbr / epx），**全部可零新依赖进程内调用**——新节点不需要引入任何第三方库。
> ⑤ 用户已排除的路线：**矢量化**（适用范围有限且与「PNG 序列」的根子相矛盾）、**神经模型**（waifu2x / Real-ESRGAN / gifski 之外的 AI 超分）；
> **Anime4K 一族经核实亦不划算**（详见 §6.4）。本页仅评估，未实施。

---

## 1. 场景与复现

用户场景：手动优化 GIF —— 输入 `samples/gif_sample/motion.gif`（简易处理，通常是「颜色量化 → GIF 合成」或直通「GIF 合成」），
重新导出 GIF，结果**并不比原 GIF 好**。内容观感上是纯色块线稿，但实际源自视频录屏。

复现方式：**调用 app 自己的后端函数**（不经 GUI），链路与节点执行完全一致：

| 环节 | 调用 |
|---|---|
| GIF 解码 | `backend_format._format_animated_image`（Wand coalesce → 逐帧 RGBA → PNG） |
| 颜色量化 | `backend_quantize.color_quantize_sequence` |
| GIF 合成 | `backend_export.export_gif`（wand 原样合成）/ `export_gif_ffmpeg` / `export_gif_gifski` |
| GIF 优化 | `backend_export.optimize_gif`（gifsicle CLI） |

源文件结构（`probe_gif` + `gifsicle --info`）：722×720、**39 帧 / 20 fps / 1950 ms**、10.86 MiB（11,387,218 B）、
全局色表 256 + **每帧局部色表**、每帧用满 **254 色**、帧全幅存储（`disposal asis`，无帧优化）。

## 2. 源文件实测画像：三类伪影全部已在源里

| # | 伪影 | 实测证据 |
|---|---|---|
| 1 | **抖动/量化杂色吃光调色板** | 全片并集唯一色 **254**（=色板容量）；**最大单色占比仅 3.1%**（真正纯色块内容应有 30%+）；近白区（luma>225，占画面 **31.8%**）8×8 块亮度 std 中位 **2.08**；最干净的 5% 块 std 仅 1.22 → 噪声集中在近白/中灰大片区域与边缘附近 |
| 2 | **白边 = 亮侧过冲（振铃）** | 沿强边（\|Δluma\|>60）：**约 50% 强边存在 ≥8 级亮侧过冲**，平均过冲 **6.38 级**。扫描线原样证据（y=438）：平台 ≈236 → 紧贴暗线处 `205.1, 247.3, 239.1, 235.4`（+12~+19 级过冲后回落）。三通道同向跳变 → **亮度振铃，非色度串色** |
| 3 | **马赛克模糊 = 边缘被抹软** | 强边 10%–90% 过渡宽度中位 **3.0 px**（另一测法 4–5 px）；对 722×720 平色线稿偏软（纯数字线稿通常 1–2 px）。外推检验排除整数倍点放大：`\|Δ1px\|=10.9 ≫ 0`、0.5 cyc/px 谱能量仅 **0.11%**、降 2× 再放大 PSNR 仅 30.02 dB |

补充：块结构检验（4/8/16/24/32 px 网格边界跳变 vs 非边界）仅 1.02–1.18× → **源里没有明显的编解码块效应**，
这解释了后文 `FFmpeg deblock` 无效。

## 3. 复现实测：12 个方案的度量（同一份源、同一链路口径）

| 方案 | 体积 MiB | 每帧色数 | 最平5%块std | 孤立斑点% | 振铃过冲均值 | 有过冲边缘% | 边缘过渡 px |
|---|---|---|---|---|---|---|---|
| 源 motion.gif | 10.86 | 254 | 1.22 | 0.123 | 6.38 | 50.3 | 3.0 |
| A 直通重导出（无处理） | 11.15 | 225 | 1.1 | 0.126 | 6.57 | 50.1 | 3.0 |
| B 颜色量化 256 + FS 仿色 | **12.62** | 243 | 1.2 | **0.164** | 6.77 | 51.0 | 3.0 |
| C 颜色量化 256 + 无仿色 | 12.14 | 254 | 1.22 | 0.123 | 6.38 | 50.3 | 3.0 |
| D 颜色量化 64 + 无仿色 | 6.82 | 60 | **0.17** | 0.871 | 6.68 | 48.0 | 3.0 |
| E 颜色量化 32 + 无仿色 | 4.44 | 31 | **0.0** | 2.209 | 4.96 | 45.0 | 3.0 |
| F 直通 + gifsicle -O3 | 10.10 | 225 | 1.1 | 0.126 | 6.57 | 50.1 | 3.0 |
| G gifsicle 降 64 色（无仿色） | 6.10 | 62 | 0.9 | 0.577 | **4.10** | **44.9** | 3.0 |
| H FFmpeg 合成（无仿色） | 10.86 | 254 | 1.22 | 0.123 | 6.38 | 50.3 | 3.0 |
| I gifski（每帧色表） | 6.15 | 303 | **1.32** | 0.188 | 5.45 | 45.0 | 3.0 |
| J 条件中值去斑 + 量化 64 | 6.83 | 61 | 0.16 | 0.822 | 6.54 | 47.2 | 3.0 |
| K 去斑 + 平坦区均值化 + 量化 32 | 4.25 | 32 | **0.0** | 1.069 | 5.57 | 51.5 | 3.0 |

读数（全部为实测，非推断）：

- **A 直通重导出 ≈ 原样品**，体积还大 **2.7%**，四项伪影度量与源逐项相同 → 「重新导出不如原 gif」定量复现。
- **B（256+FS）最差**：体积最大、孤立斑点 0.123%→**0.164%**、过冲边缘比例上升 → 对已量化的 GIF 再做误差扩散量化 = 纯二代损失。
- **H（FFmpeg 无仿色）≈ 无损重编码**：源本身 ≤256 色，palettegen 可精确重建色板 → 换更好的编码器改善不了烧进像素的伪影。
- **I gifski 把噪声如实保留**（每帧 303 色、平坦区 std 1.32 > 源 1.22）。
- **F gifsicle -O3 只省体积（−7%）、画质零变化**；**G（降 64 色 + 无仿色）是文件级唯一显著压低振铃的档**（过冲 6.4→4.10）。
- **降色是双刃剑**：32 色时平坦区噪声归零，但孤立斑点涨到 1.07~2.21%（抖动噪点换成背景**色阶断层**）。
- **全部方案「边缘过渡宽度」恒为 3.0 px** → 颜色域操作完全无法改善软边。

## 4. 归因：三层来源

| 层 | 产生者 | 表现 | 颜色域能否处理 |
|---|---|---|---|
| 1 源侧 | 有损视频编码（振铃/蚊噪）+ 非整数倍重采样（软边、过冲）、多次转码 | 振铃白边（过冲 6~19 级）、3 px 软边 | ❌ |
| 2 GIF 量化侧 | 误差扩散（FS）仿色在大片极端色区域累积误差 | 白点/杂色斑点、色板被吃光、平坦区 ±2~3 级噪声 | ⚠️ 只能"避免"，不能"修复" |
| 3 二次处理侧 | 任何再量化都是近似「已经近似过的像素」 | 二代损失；降色以 banding 换 noise | ❌ |

## 5. 空间域算子实测：候选节点的设计依据（**本节是本次调查的核心产出**）

对同一条源序列逐帧施加候选算子，在 7 个采样帧（0,6,…,36）上度量（口径与 §3 的「振铃过冲均值 / 有过冲边缘% / 边缘过渡宽度 / 最平5%块std」一致）：

| 算子 | 振铃过冲均值 | 有过冲边缘% | 边缘过渡 px | 最平5%块std | 39 帧耗时 |
|---|---|---|---|---|---|
| 源（未处理） | 6.38 | 50.3 | 3.0 | 1.22 | — |
| **IM kuwahara 2（保边平滑）** | **1.10** | **8.3** | **2.0** ↓ | **0.02** | 9.4 s |
| IM kuwahara 3 | 2.24 | 8.2 | 2.0 ↓ | 0.02 | 10.0 s |
| IM 形态学 open D1（去亮点） | **-0.02** | 11.5 | 3.0 | 0.39 | 7.0 s |
| IM 形态学 open+close D1（LULU） | 1.72 | 11.5 | 3.0 | **0.02** | 8.2 s |
| IM 形态学 close D1（去暗点） | **8.93** ↑ | 46.6 | 3.0 | 0.33 | 7.2 s |
| IM 中值 5×5 | 1.73 | **8.8** | 3.0 | 0.13 | 11.4 s |
| IM 中值 3×3 | 1.34 | 18.7 | 3.0 | 0.44 | 9.8 s |
| IM despeckle | **0.58** | 20.7 | 3.0 | 0.37 | 10.7 s |
| IM 双边 0x3 / 0x6 | 6.72 / 6.30 | 49.8 / 47.1 | 3.0 | 0.46 / 0.37 | 6.7 / 9.4 s |
| IM 高斯模糊 0x0.6（对照） | 2.14 | 24.3 | 3.0 | 0.27 | 10.1 s |
| IM 锐化 0x1 | **14.73** ↑↑ | **76.1** | 3.0 | 2.26 | 6.3 s |
| IM 自适应锐化 0x1 | **14.79** ↑↑ | **76.9** | 3.0 | 2.25 | 7.4 s |
| FFmpeg median(r=1) | 1.60 | 18.8 | 3.0 | 0.51 | **1.5 s** |
| FFmpeg removegrain(19) | 1.64 | **14.5** | **4.0** ↑软 | 0.44 | **1.4 s** |
| FFmpeg bilateral | 2.87 | 30.9 | 3.0 | 0.38 | 1.3 s |
| FFmpeg deblock(strong/weak,8) | 5.89 / 6.00 | 48.0 / 48.2 | 3.0 | 1.11 / 1.10 | 1.5 s |
| FFmpeg atadenoise（默认/轻） | 6.18 / 6.74 | 47.6 / 48.7 | 3.0 | 1.32 | 1.4 s |
| FFmpeg deband / gradfun | 6.44 / 6.40 | 48.4 | 3.0 | 1.33 / 1.29 | 1.5 s |
| FFmpeg unsharp(0.8) | **14.11** ↑↑ | **76.4** | 3.0 | 2.15 | 1.3 s |
| FFmpeg cas(0.6) | **8.93** ↑ | 58.0 | 3.0 | 2.08 | 1.4 s |

结论：

1. **保边平滑是唯一同时改善三项指标且不软化边缘的一类**：kuwahara 2 把边缘过渡从 3.0 压到 **2.0 px**（Kuwahara 的固有特性：局部按方差选最平坦象限做均值，等价于"去噪 + 边缘锐化"），与「白边 + 软边」两个症状完全对应。
2. **形态学 open（先腐蚀后膨胀）对亮侧过冲最狠**（过冲均值降到 ≈0），因为它直接抹掉"细而亮的凸起带"——白边正是这种结构。**close 反向加重（6.38→8.93）**：闭运算会扩张亮区，把白边做实。→ 节点参数必须能区分 open / close / LULU。
3. **中值/去斑类稳定有效**（中值 5×5、despeckle、FF median/removegrain），代价是 removegrain(19) 会把边缘从 3.0 软化到 **4.0 px**。
4. **锐化类一律恶化**（IM sharpen/adaptive_sharpen ≈ +131%、FF unsharp +121%、cas +40%）：**源已有过冲，锐化把它放大**。
   与 Real-ESRGAN 论文的结论同源（朴素放大/锐化会放大输入中的振铃），也与社区实践"先清理再放大"一致。
   → 若要做「锐化」节点，必须配「先做去斑/保边平滑」的流程提示，或实现"过冲钳制型锐化"。
5. **FFmpeg 的 deblock / deband / gradfun / atadenoise 对本场景无效**（源不含块效应与色带；时域降噪对静态伪影无力）→ **不必为此建节点**，避免无价值节点。
6. **IM 与 FFmpeg 两条实现路线的取舍**：IM（Wand）算子质量最好（kuwahara 全场最佳）但慢（≈10 s/39 帧）；
   FFmpeg 进程内快 6~7 倍（median 1.5 s / removegrain 1.4 s）且**零新依赖**，适合做"快速档"。
   ⚠️ 注意 wand 的 `sharpen/adaptive_sharpen` 只暴露 `radius/sigma`（无 amount），可调强度不足 → **锐化类建议走 FFmpeg `unsharp`/`cas`**。
   ⚠️ wand `morphology` 的 method 必须**小写**（`open`/`close`/`erode`/`dilate`；大写会抛 `ValueError: tuple.index(x): x not in tuple`）。

## 6. 网络侧解决思路汇总（含已排除路线的核实）

### 6.1 回到源头重做（本场景**不适用**：用户已确认无源视频）
- FFmpeg GIF 管线作者 pkh.me：误差扩散在帧间相同区域会 swarming、毁掉压缩；有序 Bayer 可预测；可以 `dither=none`。
- 录屏专门建议：**颗粒感来自 dither 而不是分辨率**；平色 UI 用 `paletteuse=dither=none` 更清晰更小；`max_colors=64` 通常不可见；`stats_mode=diff` + `diff_mode=rectangle`。
- gifsicle 官方：`--dither` 默认关闭（误差扩散更大且产生动画伪影），1.77 起提供 ordered/halftone 抖动专避动画伪影。

### 6.2 空间域修复（**对症，见 §5 实测**）
- 社区标准链（动漫平色素材，含 slow.pics 前后对比）：`dehalo_alpha` 去晕 → `hqdering` 去振铃 → `KNLMeansCL` 轻降噪 → 再放大。核心原则：**先清理再放大**。

### 6.3 GIF 域
- **ImageMagick 官方「E-Dither Pixel Speckling」**：误差扩散在极端色区域累积误差，每隔一段输出一个 "highly contrasting white pixel" 纠错 → 白点斑点。官方三条修法：换无色表限制格式 / 改有序仿色（局部化误差）/ 给大色团补 "picket fence" 外围色。
- **gifsicle 文件级降色 + `--dither=none`**（实测 G 档唯一有效）。
- **undither**（kornelski；gifski/pngquant 作者）：按调色板判断"相邻两像素之间是否还存在色板色"来区分边缘与抖动，只平滑抖动区。

### 6.4 Anime4K 一族核实（用户问"体积/硬件是否够弱"）——**结论：本项目不建议**
| 形态 | 事实 | 可用性 |
|---|---|---|
| 经典 Anime4K（bloc97，MIT，21.3k★） | **GLSL 着色器集**，需要 libplacebo/mpv 这类 GPU shader 宿主 | ❌ 本进程内无法直接调用（不是"库"） |
| Anime4KCPP v3.2.0（TianZerL，2.0k★） | **预编译 CLI（x64 MSVC）53.30 MiB**；核心里程 v3 已是 **CNN 模型**（ACNet/ARNet 多尺寸，小模型 ACNet_F8B4 低端 CPU 可实时）；支持 **CPU（SSE/AVX2/AVX512）**/OpenCL/CUDA；**带 video 模块 = GPLv3，不带 = MIT**（需自编译）；另有 DS/AVS/VS 插件包 9.4 MiB（需宿主播放器） | ⚠️ 体积 53 MiB 不放轻、预编译包含 video 模块为 GPLv3、且本质是神经模型（与"排除神经模型"的取舍冲突） |

→ 若仍想要"非神经"的清晰度提升，本机**零新依赖**可做的是 FFmpeg `unsharp` / `cas`（对比度自适应锐化）与 `xbr`/`epx`（整数倍像素放大），
但 §5 已实测：**裸锐化会把白边放大**，必须与 §5 的保边平滑串联使用。

### 6.5 学术侧
- **GIF2Video**（arXiv 1901.02840）：明确指出 GIF 固有 artifacts = flat color regions / false contours / color shift / dotted patterns，给出「颜色反量化（去量化 + 去抖动）+ 时域插值」的学习式路线。本项目按用户决策不采用学习式方案。

## 7. 候选新节点清单（按症状对症程度排序）

> 落地成本口径按`references/features-129-131`的标准配方：后端纯函数 → `backend.py` 薄转发 → 节点类（类体 execute + 三行 docstring）
> → `nodes/registry.py` → docs 计数（`node-list.md` 表头 + 首段两处、README）→ tests → 决策记录。**每新增 1 个节点：`NODE_CLASSES` 计数 +1。**

| # | 建议节点（kind / 类名 / 分类） | 端口 | 核心实现（零新依赖） | 参数（Param 类型 → 控件） | 实测依据 | 成本 |
|---|---|---|---|---|---|---|
| **N1** | `edge_smooth` / `EdgeSmoothNode` / 画面处理 | 序列→序列 | IM `-kuwahara R`（Wand `kuwahara`）**主**；可选 FFmpeg `bilateral` 快速档 | `radius` IntParam 1–6（滑条+数值框，默认 2）、`backend` ChoiceParam（IM 精修 / FFmpeg 快速） | **kuwahara 2：过冲 6.38→1.10、过冲边缘 50.3%→8.3%、边缘 3.0→2.0 px、平坦噪声 1.22→0.02** —— 本次唯一"去噪 + 锐边"双赢的算子 | 低（1 后端函数 + 1 节点） |
| **N2** | `median_filter` / `MedianFilterNode` / 画面处理 | 序列→序列 | IM `-statistic median NxN`；或 FFmpeg `median`/`removegrain`（快 6×） | `size` ChoiceParam（3×3/5×5/7×7，默认 3）、`backend` ChoiceParam | 中值 5×5：过冲 1.73、过冲边缘 8.8%、平坦 0.13；中值 3×3：1.34/18.7% | 低 |
| **N3** | `morph_clean` / `MorphCleanNode` / 画面处理 | 序列→序列 | IM `-morphology open/close/… Diamond:R`（Wand `morphology(method=小写,…)`） | `mode` ChoiceParam（开 open / 闭 close / 先开后闭 LULU，默认 LULU）、`radius` IntParam 1–3 | **open：过冲 →-0.02**；**close：过冲 6.38→8.93（反向，须标注）**；LULU：过冲 1.72 / 平坦 0.02 | 低 |
| **N4** | `artifact_metrics` / `ArtifactMetricsNode` / **分析** | 清单/序列→（无输出） | 本次探针产品化：读帧做亮度/梯度统计（numpy，零依赖） | `samples` IntParam（采样帧数，默认 8，全帧=0）、可选 `heatmap` BoolParam（输出伪影热力图预览） | 本次全部结论都靠它得到；直接解决"改了看不出好没好"的原始痛点 | 中（指标定义 + 元数据表 + 可选热力图） |
| **N5** | `sharpen_adaptive` / `SharpenNode` / 画面处理（**低优先**） | 序列→序列 | FFmpeg `unsharp`（可控 amount）/ `cas`；可选"过冲钳制"后处理 | `amount` FloatParam 0–2、`radius`/`size`、`clamp` BoolParam（钳制邻域过冲） | ⚠️ 裸锐化实测恶化（过冲 +121%~+131%）；只有配 N1/N2 前置或钳制才有意义 | 中（含钳制算法） |
| **N6** | `upscale_pixel` / `PixelUpscaleNode` / 序列处理（可选） | 序列→序列 | FFmpeg `xbr`/`epx`（整数倍，像素风放大） | `factor` ChoiceParam（2×/3×/4×）、`filter` ChoiceParam（xbr/epx/neighbor） | 无修复能力，仅放大；与「分辨率统一最近邻」功能部分重叠 | 低 |
| **N7** | `deblock_deband` / 不建节点（记录决策即可） | — | FFmpeg `deblock`/`deband`/`gradfun`/`atadenoise` | — | **实测对本场景无效**（源无块效应/无带；时域降噪无力），建了也不会用 | — |

不建，但值得改文案的两处（非新节点）：
- `gif_optimize`（GIF 优化）：帮助文本补一句「平色/录屏素材建议无仿色或 ordered」（与[决策 #96/#97](decisions/91-100.md#d96)一致）；
- `color_quantize`（颜色量化）：仿色默认值考虑从 FS 改为无仿色，或按素材类型提示（B 档实测数字可直接写进帮助文本）。

## 8. 建议落地顺序

1. **N4 伪影诊断**（分析类）——先让"改完好不好"可判；工作量小、无风险、对后续所有节点是量尺。
2. **N1 保边平滑（kuwahara）**——本次唯一"去噪 + 锐边"双赢；直接对治两个症状。
3. **N2 中值滤波**（可选与 N1 合并为「空间降噪」单节点，算法下拉 = 中值 / kuwahara / 双边 / 形态学——**符合 `color_quantize` 一节点多算子的既有风格**）。
4. **N3 形态学开闭**（可并入 N2 的算法下拉）。
5. 若仍需要"更清晰"：**N5 锐化（带钳制）**，且必须在文档中写明"先 N1/N2 再锐化"。
6. N6 / N7 视后续需求再议；N7 建议直接以"已实测否决"记入限制或决策。

**推荐的最小闭环工作流（节点视角）**：
`GIF 输入` → **`保边平滑/去噪（新）`** → `颜色量化（64 色 + 无仿色）` → `GIF 合成`(或 `GIF 合成(FFmpeg)` 无仿色) → `GIF 优化`（gifsicle 降色 + 无仿色）
→ **`伪影诊断（新）`** 对照原 GIF。

## 9. 探针与可复现命令

探针在仓库外的 `D:\Ai Playground\gif-artifact-probe\`（未污染 git）：

```bash
cd "D:/Ai Playground/gif-artifact-probe"
PY=<repo>/.venv/Scripts/python.exe            # 经 app 自带的 ImageMagick 运行时；PYTHONPATH= 清空 Hermes 注入

# 1) 源文件画像
PYTHONPATH= $PY probe_motion.py <repo>/samples/gif_sample/motion.gif out/src_frames src
PYTHONPATH= $PY probe_profile.py <repo>/samples/gif_sample/motion.gif out/src_frames src
PYTHONPATH= $PY probe_scale.py                 # 放大/软边假设检验
# 2) 12 方案复现 + 度量表 + 对比图
PYTHONPATH= $PY experiment.py                  # A–K 全跑（约 6 分钟）
PYTHONPATH= $PY summarize.py                   # → out/summary.md / summary.json
PYTHONPATH= $PY visual.py ; PYTHONPATH= $PY compact.py
# 3) 候选算子实测（§5）
PYTHONPATH= $PY candops.py all                 # → out/candops.json
```

产物：`out/summary.md`（方案表）、`out/candops.json`（算子表）、`out/*.gif`（12 个方案）、`out/zoom_*.png`（4× 放大对比图）。

## 10. 参考来源

- FFmpeg 高质量 GIF 管线（palettegen/paletteuse）：<https://blog.pkh.me/p/21-high-quality-gif-with-ffmpeg.html>
- 录屏 → GIF 的平色素材建议（`dither=none`、`max_colors=64`、`stats_mode=diff`、`diff_mode=rectangle`）：<https://www.ffmpeg-micro.com/blog/how-to-turn-a-screen-recording-into-a-gif-for-your-readme>
- gifsicle CHANGES（ordered/halftone 抖动；`--dither` 默认关）：<https://www.lcdf.org/gifsicle/changes.html> · man：<https://manpages.ubuntu.com/manpages/jammy/man1/gifsicle.1.html>
- ImageMagick 官方「Color Quantization and Dithering」E-Dither Pixel Speckling 小节：<https://usage.imagemagick.org/quantize>
- 动漫平色素材压缩伪影修复实战（dehalo_alpha / hqdering / KNLMeansCL，含前后对比）：<https://forum.videohelp.com/threads/407747-fixing-standard-noise-(as-well-as-MOSQUITO-noise)-issues-with-anime>
- DPIR（Plug-and-Play Image Restoration，deblock/denoise/SISR）：<https://arxiv.org/abs/2008.13751> · <https://github.com/cszn/DPIR>
- GIF2Video（GIF 反量化 + 时域插值；GIF 固有 artifacts 分类）：<https://arxiv.org/html/1901.02840v2>
- undither（按调色板判定边缘/抖动的去抖动滤镜）：<https://github.com/kornelski/undither>
- Anime4KCPP（v3.2.0 预编译体积/许可/处理器，GitHub Releases API 实测）：<https://github.com/TianZerL/Anime4KCPP> · 53.30 MiB / 带 video 模块 GPLv3
- 本文所有数字均由 `§9` 探针在本机实测（PyAV 18.0.0 / libavfilter 468 滤镜、ImageMagick 7.1.2-29 Q16-HDRI、gifsicle 1.96、gifski 1.34.0）。
