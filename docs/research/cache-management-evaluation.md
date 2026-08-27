# 缓存管理增强可行性评估：总大小限制 + 可调缓存路径

> 研究存档（2026-08）。背景：已确定**保留磁盘缓存、不做运存化**
> （见[运存缓存可行性评估](memory-cache-evaluation.md)）。磁盘缓存会长期存在，
> 因此需要补上「用量限制 + 路径可调」两项管理能力。结论先行：**两项均可行、
> 风险低，合计约 3 天工作量**（含测试与文档）；建议先做路径、再做大小限制。
> **已按此顺序实施完成**（见[关键决策 #53](../decisions/51-60.md#d53)），
> 本页保留评估原文；实施与评估的差异见文末「实施记录」。

---

## 1. 现状盘点（缓存如何被创建/统计/清理）

| 环节 | 现状 |
|---|---|
| 缓存根 | `MainWindow.__init__`：`MediaBackend(workspace or app_root_dir() / "cache")`
  （`MainWindow.__init__`）。默认 = **软件根目录 `cache/`**（开发=项目根，打包=exe 目录，
  见[关键决策 #30](../decisions/21-30.md#d30)） |
| 目录结构 | `cache/nodes/<node_id>/<job>_<uuid>/frame_*.png`；导出型节点另有顶层
  固定缓存 `preview.gif`（GIF 合成）/ `preview_frames/`（PNG 输出） |
| 统计 | 仅单节点 `backend.for_node(id).cache_size()`（`rglob` 求和），**无总量统计** |
| 清理 | ① 启动 `clear_workspace()` 全清；② 节点重跑成功删旧 job
  （`snapshot_workspace` + `clear_previous_run`，[关键决策 #16](../decisions/11-20.md#d16)）；
  ③ 工具栏「清缓存」全清；④ 删节点/读预设清对应节点工作区 |
| 设置层 | `SettingsManager`（QSettings INI）+ `SettingGroup`（枚举类选项唯一源头，
  见[关键决策 #51](../decisions/51-60.md#d51)）；
  现有字符串类设置（连线样式/背景网格）走独立 getter/setter 模式（`pipe_style`） |

## 2. 需求拆解

- **A. 缓存文件总大小限制**（默认 4 GB）：超限时自动淘汰旧缓存，而不是无限累积；
- **B. 可调缓存路径**（默认暂定项目根目录）：把 `cache/` 移到用户指定位置
  （如数据盘），解决「程序装在 C 盘、缓存想放 D 盘」以及打包后 exe 目录
  只读/无权限的问题。

## 3. 可行性：A. 缓存总大小限制

**可行性：高。** 基础设施齐全：缓存天然按「job 目录」组织、`_remove_path`
（重试容忍 Windows 句柄竞争）可复用、`rglob` 统计已有先例（`cache_size`）。

### 设计决策点

1. **统计口径**：`cache/` 下全部文件求和（`root_workspace.rglob`）。
   量级评估：job 目录数 = 节点数 × 历史运行次数；几千个文件级别 rglob 为
   毫秒级，可放在执行后检查点，不阻塞 UI（执行本就在工作线程）。
2. **淘汰粒度与保留规则**（最关键）：
   - 最小淘汰单元 = **job 目录**（一个 `xxx_<uuid>/`）；
   - **必须保留**：每个节点工作区**最新**的 job（预览 `preview_path_for_node`
     与帧滑条读它）+ 顶层固定缓存 `preview.gif` / `preview_frames/`
     （导出按钮复制它）；
   - 其余按 `mtime` 从旧到新删除，直到总量 ≤ 限制 × 回退系数（如 0.8，
     避免临界抖动）。
3. **触发时机**：**每次节点执行成功后**（工作线程内，最及时，rglob 毫秒级
   开销可忽略）——在 `_execute_step` 的 `clear_previous_run` 之后调用。
   **不需要启动时触发**：`MainWindow.__init__` 本就 `clear_workspace()` 全清
   （`MainWindow.__init__`），启动即空，无残余可淘汰。也不做后台定时器（复杂度不值）。
4. **超限行为**：**自动淘汰**（静默 + 状态栏提示「缓存超限，已清理 N MB」），
   不阻塞运行——与「自动模式报错不弹窗」的用户体验取向一致
   （[关键决策 #50](../decisions/41-50.md#d50)）。
5. **与失败保留语义的协同**：现有「失败保留上次成功产物」靠旧 job 仍在；
   淘汰只删**非最新** job，失败节点的旧产物属于「最新」范畴（该节点未产生
   更新的 job），天然安全。
6. **4 GB 默认值**：1080p RGBA PNG（level=1）约 9.3 MB/30 帧 → 4 GB ≈
   430 帧/1080p 的中间产物量；长链节点会更快触及。作为默认合理，用户可调。

### 改动清单（A）

- `media/backend_cache.py`：`total_cache_size()`（root 级统计）+ `enforce_cache_limit(
  limit_bytes)`（淘汰，复用 `_remove_path`）；保护规则做成纯函数便于测试；
- `ui/main_window_execution.py`：`_execute_step`（工作线程）在 `clear_previous_run` 后调用
  `enforce_cache_limit`；状态栏提示经现有信号通道（可选）；
- `settings_manager.py`：大小限制设置项（见 §5）。

## 4. 可行性：B. 可调缓存路径

**可行性：高。** `backend` 已按 `root_workspace` 组织（`for_node` →
`root/nodes/<id>`），**换根即整体迁移**，`nodes/` 子目录自动跟随。

### 设计决策点

1. **存储**：字符串类设置（非枚举），走 `pipe_style` 的独立 getter/setter
   模式，键如 `cache/dir`；默认值 = `app_root_dir() / "cache"`（保持现状
   语义）。*注*：用户原话「默认暂定项目根目录」按「默认保持现状
   （项目根下 `cache/`）」理解；若确指「缓存直接放项目根本身」，仅改默认值
   一处即可，实施前确认。
2. **UI**：设置对话框「设置」页加两行——「缓存目录」（只读 `QLineEdit` +
   「浏览…」`QFileDialog.getExistingDirectory`）、「缓存大小限制」
   （数值框 + 单位下拉 MB/GB，或直接 GB 小数）。设置对话框为固定尺寸，
   需微调高度。
3. **生效时机**：推荐**下次启动生效**。原因：运行中切换会导致——正在执行/
   已完成的节点产物路径全部失效（预览、帧滑条、导出全断）；立即重建需
   「清空全部节点缓存 + 全部重跑」，副作用大且易出错。下次启动生效只需
   `MainWindow.__init__` 读设置创建 backend，改动最小、语义清晰。
   对话框内提示「更改将于下次启动生效」。
4. **校验**：路径非空、可创建（`mkdir(parents=True)`）、可写（探测写入）；
   非法回退默认并提示。相对路径按绝对化处理。
5. **旧路径残留**：切换后旧 `cache/` **不自动删除**（用户可手动清理）。
   注意「清缓存」清的是**当前**路径——旧路径残留需在对话框提示
   （如「旧缓存目录：…」），避免用户以为已清空。
6. **打包影响**：`app_root_dir()` 打包后 = exe 目录，可调路径同时解决
   「exe 所在目录不可写」的发行场景。

### 改动清单（B）

- `settings_manager.py`：`cache_dir()` / `set_cache_dir()`（含校验/回退）；
- `settings_manager.py`：`SettingsDialog` 加路径行 + 浏览按钮 + 提示；
- `ui/ui.py`：`MainWindow.__init__` 的 backend 创建改读设置
  （`MediaBackend(settings.cache_dir())`），状态栏照常显示当前工作区；
  测试注入的 `workspace` 参数保持优先（测试隔离不破坏）。

## 5. 设置项形态建议（A+B 共用）

`SettingGroup` 只适合**枚举类**选项（主题/透明背景色）；大小与路径是
**自由值**（整数/字符串），沿用 `pipe_style` 的独立 getter/setter 模式：

- `cache/limit_mb`（int，默认 4096，范围如 256–102400，防呆钳制）；
- `cache/dir`（str，默认 `app_root_dir()/cache`，非法回退默认）。
- `reset()` 一并恢复这两项。

## 6. 工作量汇总

| 项 | 改动 | 工作量 |
|---|---|---|
| 设置项（大小 + 路径，含校验/回退/reset） | `settings_manager.py` | 0.5 天 |
| 设置对话框 UI（路径浏览行 + 大小输入行） | `settings_manager.py` | 0.5 天 |
| MainWindow 接线（backend 读设置创建） | `ui.py` | 0.25 天 |
| 总量统计 + 淘汰方法（保护规则纯函数） | `backend.py` | 0.5–1 天 |
| 触发点（步骤成功后） | `ui.py` | 0.25 天 |
| 测试（设置往返/对话框/淘汰行为/路径校验） | `tests/test_settings.py` 等 | 0.5–1 天 |
| 文档（决策 #53 + 文档集更新） | `docs/` | 0.25 天 |
| **合计** | | **≈ 3 天**（一人，含测试与文档） |

> 淘汰行为测试用**小限额注入**（如 1 MB + 假缓存）或参数化限额，不真写 4 GB。

## 7. 建议落地顺序

1. **先做 B（路径）**：改动最小、独立、无淘汰语义风险，先解「缓存位置」痛点；
2. **再做 A（大小限制）**：注意保留规则（最新 job + 固定缓存）优先于删除；
3. 两项均不动运存化（已定保留磁盘缓存）。

> 本页为评估存档；实施记录见[关键决策 #53](../decisions/51-60.md#d53)。

## 8. 实施记录（与评估的差异）

2026-08 按评估方案实施（先 B 后 A），与评估一致的部分从略，差异如下：

- **大小限制钳制范围**：评估未定；实施为 `cache/limit_mb` 钳制 [64, 102400] MB
  （下限 64 MB，防止误设过小导致每轮运行都大扫除）；
- **淘汰识别**：评估用「job 目录」概念；实施用命名正则
  `_JOB_DIR_RE = ^.+_[0-9a-f]{10}$` 区分 job 目录与固定缓存
  （`preview.gif` / `preview_frames` 不含该后缀，天然保留）；
- **触发点**：评估说「步骤成功后 + 启动时」；实施核对发现启动本就
  `clear_workspace()` 全清（`MainWindow.__init__`），故只保留「每次节点执行成功后」；
- **跨线程安全**：限额由 `run_to`（UI 线程）读入 `_active_cache_limit_bytes`，
  工作线程 `_execute_step` 只读该值，不触碰 QSettings；
- **状态栏提示**：`_cache_eviction_note` 由工作线程写入、UI 线程在
  `_run_succeeded` 显示并清空（经排队信号保证顺序）；
- 新增 11 项测试（设置 5 项 + 淘汰/端到端 6 项）；全量 171 项通过。
