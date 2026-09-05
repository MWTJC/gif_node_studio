# GIF Node Studio — 文档中心

面向开发与维护的完整技术文档：架构、数据模型、节点约定、决策记录、测试与打包说明。本文档即**总目录**。

- 项目入口（运行 / 基本操作 / 特性）：[项目根 README](../README.md)

## 目录

### 1. 项目概览与架构

| 页面 | 内容 |
|---|---|
| [项目概览](overview.md) | 用途、技术栈、模块职责一览 |
| [架构分层](architecture.md) | 分层职责与设计原则 |
| [数据模型](data-model.md) | `MediaManifest` / `SequenceArtifact` / `CropSpec` |
| [执行链路](execution-pipeline.md) | 执行计划、工作线程、脏传播、失败与缓存清理 |

### 2. 机制与设计要点

| 页面 | 内容 |
|---|---|
| [节点体系约定](node-conventions.md) | 构造注入声明、注册表、族基类 |
| [新增节点：作者指南](node-authoring-guide.md) | 从设计到收尾的完整操作流程（后端/注册/测试/文档/注意事项） |
| [预览机制](preview.md) | 清单携带预览图、链式预览、帧滑条、1:1 查看 |
| [规格合成语义](spec-composition.md) | 裁剪/截取链式「合成」而非「替换」 |
| [NodeGraphQt 显示层修复](nodegraphqt-fixes.md) | `StudioNodeItem` 标题撑宽 / widget 居中 |
| [元数据展示规则](metadata.md) | `StudioNode.describe_output`（节点自身定义，默认接管）探测与格式化 |

### 3. 决策记录（按时间线分卷）

关键决策按时间线整理，每条一个锚点小节，可被其它页面直接链接：

- [决策记录总览](decisions/README.md)
  - [决策 #1–#10](decisions/01-10.md)
  - [决策 #11–#20](decisions/11-20.md)
  - [决策 #21–#30](decisions/21-30.md)
  - [决策 #31–#40](decisions/31-40.md)
  - [决策 #41–#50](decisions/41-50.md)
  - [决策 #51–#60](decisions/51-60.md)
  - [决策 #61–#70](decisions/61-70.md)
  - [决策 #71–#80](decisions/71-80.md)
  - [决策 #81–#90](decisions/81-90.md)
  - [决策 #91–#100](decisions/91-100.md)
  - [决策 #101–#110](decisions/101-110.md)
  - [决策 #111–#120](decisions/111-120.md)
  - [决策 #121–#130](decisions/121-130.md)
  - [决策 #131–#140](decisions/131-140.md)

### 4. 质量与发布

| 页面 | 内容 |
|---|---|
| [已知限制与改进方向](limitations.md) | 边界、遗留问题、候选改进 |
| [验证方式](testing.md) | 测试清单、回归项、离屏注意点 |
| [发行打包](packaging.md) | ImageMagick / gifsicle 运行时清单、Nuitka standalone |

### 5. 参考

| 页面 | 内容 |
|---|---|
| [节点清单](node-list.md) | 53 个节点（`NODE_CLASSES` 顺序） |
| [运存缓存可行性评估](research/memory-cache-evaluation.md) | 中间产物磁盘缓存改运存的收益/风险/分档方案/工作量 |
| [缓存管理增强可行性评估](research/cache-management-evaluation.md) | 缓存总大小限制 + 可调缓存路径 |
| [PS 颜色深度对齐研究存档](research/ps-color-reduction.md) | 已回退实验；2026-08 黑盒差分结论 |
| [gifsicle/pygifsicle 补全性调研存档](research/gifsicle-evaluation.md) | wand 四领域缺口 vs gifsicle 补全；落地为「GIF 优化」节点 |
| [GIF 生态补全性调研存档](research/gif-ecosystem-evaluation.md) | IM/gifsicle 之外可引入的库与 CLI 全景；落地为「GIF 合成(FFmpeg)」与 WebP/APNG 导出 |

---

## 文档约定

- **交叉引用**：全部使用相对链接（含锚点）；决策条目链接为
  `decisions/<区间>.md#<条目号>-<标题>`。
- **专有名词**：`代码标识` 用反引号；节点名 / 参数名与代码一致（单一源头
  原则，见[关键决策 #51](decisions/51-60.md#d51)）。
- **更新历史**：git commit 与决策记录（decisions/）即完整更新历史。
- **面向 VitePress**：本目录结构（每主题一页、`README.md` 作目录页）可直接作为
  VitePress 的 `docs/` 源目录使用；相对链接与锚点在 VitePress 下自动生效。
