# 界面配色设计调研（决策 #117 前置）

> 结论先行：旧版配色"不尽如意"的根因不是"分类色没选好"，而是**彩色只存在于
> 色条/图标/端口，而节点壳（体/画布/边框）与连线这两个最大视觉面用库默认色**，
> 且色板感知距离不足。落地为决策 #117：壳色（方案 A）+ OKLCH 等距环分类色
> （方案 B-1）。本文记录调研过程与数据来源，供后续微调参考。

## 1. 现状盘点（代码实读，2026-08）

| 元素 | 来源 | 色值 | 备注 |
|---|---|---|---|
| 应用窗体/面板/对话框 | Qt Fusion 深色默认 | #353535/#2A2A2A 系 | 决策 #90 固定深色，无定制 |
| 画布背景 | NodeGraphQt `ViewerEnum.BACKGROUND_COLOR` | (35,35,35)=#232323 | 项目从未调用 set_background_color |
| 网格线 | `GRID_COLOR` | (45,45,45)=#2D2D2D | 对比 1.14:1，几乎不可见 |
| 节点体 | NodeGraphQt `NodeModel.color` 默认 | (35,35,35)=#232323 | **与画布同色，对比 1.00:1** |
| 节点边框 | `NodeModel.border_color` 默认 | (85,100,100) | 0.8px 细线 |
| 标题栏 | 节点体上叠 (0,0,0,80) | 略暗于节点体 | 库默认机制 |
| 选中描边 | `NodeEnum.SELECTED_BORDER_COLOR` | (254,207,42)=#FECF2A | 与分析黄撞色（ΔEOK≈0.01） |
| 分类色 | `NodeCategory` → Material 2014（决策 #114） | 400 系为主 | 感知最近邻对 ΔEOK≈0.07 |
| 端口色 | `node_base.PORT_COLORS`（硬编码，未收编） | MANIFEST 橙 / SEQUENCE 蓝 | 与分类色系统并列 |
| 连线 | `PipeItem` 默认 `PipeEnum.COLOR` | (175,95,30) 橙 | **与端口色不一致**（像素采样证实） |
| 预览/裁剪/剃刀/手柄 | `DARK` Palette（决策 #114） | bg #14161A 等 | 已收编，唯一写死 hex 处 |

**架构缺口**：`PORT_COLORS` 仍硬编码在 node_base.py，未进 color_tokens.py
（违反 #114 唯一颜色层精神）——这是方案 C（连线/端口统一）的前置清理项。

## 2. 量化诊断（scripts/color_palette_analysis.py，coloraide）

- 节点体 vs 画布：**1.00:1**（同色）；画布 vs 网格 1.14:1；选中黄 vs 分析黄 **1.03:1**
- 分类色最近邻对（ΔEOK，黑↔白=1.0 标尺）：
  - 预格式化(青)↔格式化(青绿) **0.07**；通道(粉)↔输出(红) **0.07**
  - 格式化↔背景 0.08；动效(橙)↔分析(黄) 0.08；输入(绿)↔格式化 0.09
  - 参考系：Okabe-Ito 最近邻 0.16；Tol Muted 最近邻 0.12
- 明度方差：当前色板 L 范围 0.576~0.862（std 0.077），亮度不齐加重辨识负担

## 3. 外部参考（联网查证）

- **Bforartists**（bforartists.de）：核心哲学「颜色=导航 / 形状=识别」+
  背景近中性 + 对比度 ≥160/255 灰阶；彩色图标而非单色
- **ComfyUI 官方深色主题 JSON**（docs.comfy.org/interface/appearance）：
  画布 #222 → 节点体 #353535 → 边框 #666 **三级亮度阶梯**；链路按类型着色
  （IMAGE #64B5F6、MASK #81C784、LATENT #FF9CF9…）
- **Blender/Nuke/Fusion 惯例**：深画布 + 节点体亮一档，类型色集中于标题/色条
- **色盲安全分类色板**：Okabe-Ito（8 色，Nature Methods）、Paul Tol Muted（10 色）
- **Qt 深色主题包**：PyQtDarkTheme（PySide6，Python<3.12 与本项目 3.11 兼容）、
  Qt-Material、QDarkStyleSheet —— 调研结论：可作参考，但不引入（现有
  Fusion+QPalette 已定，换整包 QSS 牵连 combobox-popup 等已修回归，风险>收益）

## 4. 方案评估

| 方案 | 内容 | 工作量 | 风险 | 结论 |
|---|---|---|---|---|
| A 壳色统一 | 画布/网格/节点体/边框/选中进 Palette + 应用 | 小 | 低 | ✅ 已落地（决策 #117） |
| B-1 OKLCH 等距环 | 分类色换感知均匀色板 | 中 | 中 | ✅ 已落地（决策 #117） |
| B-2 Tol Muted | 色盲安全替代 | 小 | 低 | 备用（10 色现成） |
| C 连线/端口统一 | 端口色收编 + 连线跟随端口 + 网格换挡修复 + 背景框收编 | 中 | 中 | ✅ 已落地（决策 #118） |

**B-1 映射与穷举优化**（scripts/optimize_category_mapping.py）：10 个等距点
（H=12+36k）中 9 个业务分类语义锚定，避免敏感对（粉↔红、绿↔青绿↔青）落在
相邻点；输入族取色相渐变（156°→192°→228°）保流程感。已知局限：相邻点
ΔEOK≈0.09（36° 数学极限），青/青绿、绿/青绿深底上仍偏近，靠图标形状
（#111 双通道）辅助；如需再拉开可微调单点 H/C。

## 5. 产物

- `docs/research/color_theme_before.png` / `color_theme_after.png` — 离屏渲染对比
- `docs/research/_pipe_color_probe.png` + `scripts/probe_pipe_color.py` — 连线/端口色验证
- `scripts/render_color_baseline.py` — 对比图复现脚本（改色后重跑）
- `scripts/color_palette_analysis.py` — 色板量化分析
- `scripts/palette_proposals.py` / `scripts/optimize_category_mapping.py` — 色板生成与映射优化
- 决策：`docs/decisions/111-118.md` #117
