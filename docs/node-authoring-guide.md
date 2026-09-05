# 新增一个节点：作者指南

> 面向「像这次新增 颜色键 / 百分比缩放 一样」的新节点全流程操作指南。
> 规范细节（声明形态 / 面板分派 / 注册机制 / 参数→控件映射）以
> [节点体系约定](node-conventions.md) 为准；本文把 **设计 → 后端 → 节点类 →
> 注册 → 测试 → 文档 → 收尾** 串成可照做的步骤，并记录本项目实际踩过的
> 方法与注意事项。示例对照：`ColorKeyNode`（`nodes/process_nodes.py`）、
> `PercentScaleNode`（`nodes/sequence_nodes.py`），决策记录 #129。

## 0. 设计先行：先理清语义，再动代码

- **同名/同族功能先确认行为模型**：新节点若与其它软件（PR/PS/AE 等）同名或
  同族，先厘清"参考对象到底做什么"。例：颜色键 = PR Color Key 的**同向键出**
  （主色相近 → 透明），而 PS 色相/饱和度是"范围蒙版 + 调色"、不产生透明——
  两者不是一回事，做错方向整节点返工。
- **拿不准先问用户，让用户拍板**：关键参数构成、默认值、度量方式、分组位置
  等由用户拍板后再实现（本次四项特性均经对话澄清后才动手，见 #129）。
- **找"最近等价物"做形式参照**：颜色键 → 超级键（同款色块选色 + 图标）；
  百分比缩放 → 分辨率统一（同款缩放算法下拉），只增差异点，不做全新发明。
- **文档可能滞后于代码**：以 `src/` 代码为事实源；落地的同步义务见第 6 节。

## 1. 改动清单（按序，全部在此）

| 步骤 | 文件 | 做什么 |
|---|---|---|
| 1 后端 | `media/backend_<区段>.py` | 新增纯函数处理 |
| 2 转发 | `media/backend.py` | 对应区段薄转发（决策 #120，API 零改动） |
| 3 节点类 | `nodes/<分类>_nodes.py` | 具体节点类（声明 + execute + docstring） |
| 4 注册 | `nodes/registry.py` | import + 插入 `NODE_CLASSES`（唯一有序注册表） |
| 5 测试 | `tests/test_<节点>.py` | 后端语义 + 节点定义/execute + 注册 |
| 6 文档 | `docs/node-list.md` / `README.md` / `docs/decisions/*` / `docs/decisions/README.md` | 节点行与计数、特性、决策条目、卷目录 |
| 7 收尾 | — | 全量 pytest + 自检 SMOKE_OK + 行尾 CRLF |

> UI 零改动：面板完全由节点声明驱动，新增普通节点不需要碰 `ui/`。

## 2. 后端处理函数（`media/backend_*.py`）

- 纯函数签名约定：`(workspace, progress, artifact/manifest…, **参数)`；
  产物写 `_job_dir(workspace, "<prefix>")`（随机 job 目录，天然参与缓存淘汰），
  逐帧变换走 `_parallel_pil_export(progress, total, output, label, process)`，
  进度用模块版 `_progress(progress, fraction, label)`（决策 #120 拆分约定）。
- 非法参数抛**清晰中文** `ValueError`（本项目测试常按错误文案断言）。
- 逐像素运算一律 **numpy 向量化**（决策 #73 实测：PIL 纯 Python 逐像素对
  1080p 不可接受）；wand 高层接口没有逐像素柔和过渡时不要硬凑。
- **不要改既有函数**：语义不同就新增命名清晰的函数（颜色键 =
  `color_key_tolerance_sequence`，不动超级键的 `color_key_sequence`），旧节点
  行为与测试零回归。
- 返回 `SequenceArtifact(paths, w, h, has_alpha, str(output))`；输出宽高
  （若变化）由本函数算出并在返回值携带（百分比缩放的目标 = `round(原×%/100)`）。

## 3. 节点类（`nodes/<分类>_nodes.py`）

骨架（对照 [node-conventions.md](node-conventions.md)「空白节点基线」）：

```python
class 某Node(SequenceNode):   # 族基类只提供 require_input/sequence 校验
    NODE_NAME = "显示标题"

    def __init__(self):
        super().__init__(
            definition=NodeDefinition(
                "kind_机器键", self.NODE_NAME, NodeCategory.PROCESS,
                icon=category_icon(NodeCategory.PROCESS, "mdi.<glyph>"),  # 或 qta.icon 多层
                inputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                outputs=(PortDefinition("序列图片", PortType.SEQUENCE),),
                params=(FloatParam(...), ChoiceParam(...)),
                panel=PanelSpec(scrub_frames=True),
            ),
            help=("输入图片序列\n" "...\n" "输出图片序列"),
        )

    @classmethod
    def execute(cls, inputs, params, backend):
        return backend.<fn>(cls.sequence(inputs), ...)
```

- 分类模块：输入 `input_nodes` / 预格式化+格式化 `manifest_nodes` /
  序列处理 `sequence_nodes` / 画面处理（原一般处理）`process_nodes` / 动效 `motion_nodes` /
  通道 `channel_nodes` / 输出 `export_nodes` / 分析 `analysis_nodes`。
- 端口类型 `PortType.MANIFEST`（橙色清单）/ `SEQUENCE`（蓝色序列）；多输入 /
  多输出端口 `PortDefinition(..., show_name=True)`；端口类型不符的连线会被
  拒绝（`_connection_type_mismatch`），不要提供跨类型直连能力。
- `ChoiceParam` 的选项：整组选项跨节点共享时收进 `core/options.py`（单一源头，
  `RESAMPLE` / `SCALE_STRATEGY` 等）；节点 execute 用
  `RESAMPLE.key_of(params["resample"])` **标签 → 机器键**传给后端——后端只认
  机器键，显示标签改名不漂移。
- 参数标签尽量带上单位风格（「容差 %」「羽化 %」「缩放百分比 %」）。
- 类 docstring 必须按三行自述（node-conventions.md「docstring 自述约定」）：
  处理 / 参数 / 组件，复杂语义补在后方。

### help 文案：面向使用者，别背文档（本次用户强制约定）

- **对用户只说"输入什么 → 输出什么" + 每个参数一句直观效果**，例如
  「羽化 %：越大边缘越虚」「容差 %：越大抠得越多」；
- **不写算法细节、不列下拉菜单内容**（不说 0.25/0.5、过渡带方向、归一化距离，
  也不枚举"最近邻/双线性/双三次/Lanczos"），
  这些细节归属 `docs/`（node-list.md / 决策记录）；
- 其它软件也有的通用名词（容差、羽化、缩放算法）默认用户认识，
  不要展开教学。

## 4. 注册（唯一有序注册表）

- `nodes/registry.py`：`NODE_CLASSES` 里 import 与 tuple 均按分类就近插入
  （本次：超级键后插 颜色键、分辨率统一后插 百分比缩放）。
- 节点库按钮总数 = `len(NODE_CLASSES) + 1`（背景框），由测试自动派生
  （`test_node_library_panel.py`），**不需要手工改测试**。
- 禁止平行注册表 / 平行 kind→class 映射 / 平行缓存文件名常量（#51/#53/#124）。

## 5. 测试（`tests/test_<节点>.py`）

按既有测试风格（`tests/test_flip_node.py` 等）：

```python
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 模块顶部，先于 Qt
```

- **后端语义**：用 `tmp_path` 造小图 → `MediaBackend(tmp_path)` → 调用后端，
  打开产物逐像素断言（颜色键的 2×2 各色像素 alpha 期望值、百分比缩放尺寸与
  最近邻复制语义）；**和"最近等价物"对照断言差异点**（颜色键容差 30% 保留灰
  像素而超级键键出，直接证明"滤色逻辑不同"）。
- **边界与报错**：非法百分比 / 非法缩放算法 → `pytest.raises(ValueError, match=...)`。
- **节点级**：`qt_app` fixture（module scope）后实例化节点断言
  `definition.kind` / `NODE_NAME` / 默认 `params` / `node_class_by_kind(kind)`
  解析 / `ColorKeyNode in NODE_CLASSES`；再直接调 `Node.execute(inputs, params, backend)`
  验证 标签→机器键→后端 整条链路。
- 需要真实 MainWindow + NodeGraphQt 的集成场景（启动/存档/面板绑定），参考
  `tests/test_razor_chain_preview.py` / `tests/test_autosave.py` 的 fixture
  模式：`MainWindow(workspace=tmp, settings=临时SettingsManager)`，teardown
  调 `worker.shutdown()`；弹窗用 monkeypatch 放行。

## 6. 文档同步（四件套，缺一不可）

1. `docs/node-list.md`：加一行（kind/类/类别/输入→输出/一句话+直观参数效果），
   并同步更新**头部计数**与引言计数（本次 51 → 53 两处）；
2. 根 `README.md`：特性列表补上节点名（一行内加词，别展开）；
3. 决策记录：新条目（编号连续；每卷 10 条、满卷开新卷如 `131-140.md`；
   条目标题前 `<a id="dN"></a>` 短锚点）——写清 背景/改动/验证，引用测试
   文件与项数；配套更新 `docs/decisions/README.md` 的**分卷目录**（新卷行）
   与**纯计数目录**（新条目标题）；
4. 涉及文档中心目录引用（新增指南页、节点计数等）时同步 `docs/README.md`。

## 7. 收尾验证

- 全量：`PYTHONPATH=src <venv>/python -m pytest -q`——基线已知唯一失败
  `test_render_colorbar_and_icon`（离屏图标白形渲染环境问题，决策 #121/#128
  已记录，改动前即失败），其余必须全绿；
- 生产初始化同路径自检：`PYTHONPATH=src QT_QPA_PLATFORM=offscreen python -m
  gif_node_studio --test` 应打印 `SMOKE_OK`；
- **行尾 CRLF**：工作区约定（决策 #82，git 索引 LF、`core.autocrlf=true`）——
  patch/write 产生 LF 的新文件最后统一转 CRLF（新增测试与决策卷最容易漏）。

## 注意事项速查（本次实际踩过 / 项目沉淀）

- **kind 稳定、显示名可改**：存档按 kind + 参数键持久化；kind 与参数键是
  兼容边界（决策 #55 先例），title/标签可自由改。
- **新增参数不影响旧存档读取**：缺失参数走模型默认值；删除/改名参数会被
  旧存档兼容清洗（`sanitize_session_data`）丢弃并提示（决策 #80）——清洗的
  合法键集合由节点模型派生，**新参数自动兼容**，无需额外注册。
- **图标 glyph 先探测**：`qta.icon("未知.glyph")` 会抛错；不确定时先跑一次
  `qta.icon(name)` 探活；「形式类似」可复用同款图标（颜色键与超级键同款），
  不必追求视觉独创。
- **enabled_when 置灰**：参数依赖另一 Bool/Choice 取值时声明
  `enabled_when=(依赖参数, 允许取值集)`（例：gifski 的 fixed_color 先例），
  面板自动禁用，不用手写 UI。
- **ColorParam 持久化格式是 `#rrggbb`**：execute 里自行 hex → (r,g,b)
  （超级键/颜色键同款），不要存 tuple。
- **导出终端节点**才需要 `EXPORT_KIND` + `CACHE_FILENAME` 类属性（ui 按类
  派生导出按钮与固定缓存保留）；普通处理节点不需要。
- **逐帧 PNG 物化统一命名** `frame_{index:06d}.png`（gifski glob、序列产物
  解析等依赖字典序 = 帧序）。
- **帮助文本别用 emoji/特殊符号**（会拖慢带该文本控件的软件初始化），状态栏
  文案同理（决策 #130）。
