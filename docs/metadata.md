# 元数据展示规则（`StudioNode.describe_output` / `media/media_info.py`）

## 架构：节点自身定义展示，默认行为接管

- 输出元数据由**产出节点自身**定义：基类 `StudioNode.describe_output(output)`
  为**默认行为**——按输出值类型给出通用摘要（委托 `media_info.default_describe_output`）。
  具体节点无特殊需求时无需覆写；需要自定义展示时覆写本方法
  （可用 `super().describe_output(output)` 回落默认行为）。
- `media/media_info.describe_output(output, node=None)` 为统一出口：优先委托给
  `node.describe_output(output)`（`node` 为产出节点的类或实例；工作线程只持有
  节点类 `step[1]`，UI 兜底分支持有实例，二者均经类方法分派），无节点
  （单操作运行等）时直接走默认行为。
- 新增节点的元数据规则：无特殊需求 → 不覆写；有特殊需求 → 在节点类内覆写
  `describe_output`，把「这节点显示什么」留在节点自身，不再进中央函数。

## 默认行为（`media_info.default_describe_output`，与产出节点无关）

- `MediaManifest`：`媒体类型`、`源文件数`；源探测按后缀分派：
  - `.gif` → `probe_gif`（帧数、总时长、帧时间、循环、**颜色板颜色数**等）：优先走 `_gif_parse` 轻量解析器
    （直接扫描 GIF 块结构：GCE 延迟、图像描述符计数、NETSCAPE 循环、全局/局部颜色表大小，**不解码像素**，
    大 GIF 也不卡顿）；解析失败回退 PIL 逐帧迭代。
    - 颜色板颜色数 = 全局颜色表条目数（无全局表时取各帧局部颜色表最大值），如实反映文件中声明的调色板大小；
    - 帧时间不恒定时，按**从小到大列举（去重）**显示，例如 `40 ms、80 ms、120 ms`；恒定时仍为单值。
  - 视频后缀（mp4/mkv/mov/webm/avi/...）→ `probe_video`：**总时长**（`h:mm:ss` / `m:ss` / `s`）与**帧率**（PyAV `average_rate`/`base_rate`）；
  - 其它 → 文件名 + 文件大小。
- `SequenceArtifact`：帧数、尺寸、是否含透明通道（**不含帧率**，见[关键决策 #36](decisions/31-40.md#d36)）。
- `AnalysisResult`：合并预览文件信息（`Path` 探测）与 `metadata` 附加信息。
- `MultiOutput`：按端口名逐通道摘要（`SequenceArtifact` → 帧数/尺寸；`MediaManifest` → 首个源文件探测），
  之后合并 `metadata`。
- `Path`：按后缀分派（gif → `probe_gif`；图片 → 文件名/大小/尺寸；其它 → 文件名/大小）。
- 文件元组/列表：`输出文件数`/`输出总大小`（PNG 序列导出）。
- 元数据探测（含大 GIF 的 `probe_gif`）在**工作线程**内完成（`worker._run`/`_run_steps`），
  不阻塞 UI，且计入该步骤「上次运行耗时」。

## 节点自定义展示

- **输出类节点**（GIF 合成/优化、WebP/APNG/ico 导出，`nodes/export_nodes.py`）：
  覆写 `describe_output`——execute 把可读摘要（实测尺寸、文件大小等）附到
  `MultiOutput.metadata`，节点直接透出，不再逐端口展开/深度探测（大 GIF 的
  probe_gif 九字段、序列帧数/尺寸等对导出节点是噪音）。PNG 序列导出返回文件元组，
  不覆写，走默认行为（输出文件数/输出总大小，显示时可读化）。

### 导出终端元数据：尺寸 / 帧速（决策 #143）

- 导出终端的尺寸与帧速一律**读回产物**（不回显参数）：
  - GIF（wand / FFmpeg / gifski）：`media_info.gif_export_summary` → 逻辑屏幕尺寸 +
    帧速（帧延迟均值换算，与 gif优化分析「全局帧率」同口径）+ 文件大小；
  - WebP / APNG：`media_info.image_dimensions`（Pillow 读产物头）+ 文件大小；
  - ICO：`icon_compose` 产物的**最大档**尺寸 + 文件大小（容器含多分辨率，详见
    ICO 分辨率查看节点）。
- 为什么读回：`尺寸 %`、gifski 宽高限制（0 = ~800×600 默认）、GIF 帧延迟 10ms 量化、
  gifski「往复」（帧数翻倍）都会让实际值 ≠ 参数值；实测才是「跑出来的是多大、多快」。
- **字段保证**：GIF 导出终端的 `METADATA_SPEC` 恒为 `尺寸 / 帧速 / 文件大小` 三项，
  解析失败时值回落 `—` 但键始终存在（`gif_export_summary` 不抛错）。
- **例外**：GIF 优化（gifsicle）是清单→清单的后处理而非合成本身，保持
  `优化前大小 / 优化后大小 / 压缩率` 三项（文件大小与「优化后大小」重复）。
- `gif优化分析` 节点的元数据**不含逐帧明细**（仅画布/帧数/循环/解码方式/帧优化/帧优化占比/
  透明优化统计/**时长诊断**；逐帧信息由滑条逐帧查看承担）。其中：
  - **帧优化**（局部帧数）**按文件结构计算**（图像描述符声明的存储区域）——存储帧与
    合成帧两种解码方式都如实反映文件的帧优化情况（合成帧模式下 coalesce 只抹掉显示层）；
  - **帧优化占比**以最短帧时间为基准：相对**等时长全最短帧序列**，优化后的 GIF 节省的
    帧数百分比（``总时长 ÷ 最短帧时间`` 为基准帧数，占比 = 1 − 实际帧数 ÷ 基准帧数，
    如「节省 42.9%（等时长全最短帧 7 帧 → 实际 4 帧）」；帧时间缺失或含 0 时显示
    「—（无法以最短帧为基准）」）；同样与解码方式无关；
  - **时长诊断**（决策 #121）：`总时长`（帧延迟求和）、`全局帧率`（**平均**帧时间
    换算 1000 ÷ 平均 ms——帧时间不恒定时中位数会误判）、`帧时间`（恒定单值/非恒定
    列举去重）。用于直接暴露「实际播放帧速 ≠ 合成参数帧速」的漂移：如 30fps 参数
    的 GIF 若帧时间均匀 30ms → 实际 33.333 fps（总时长比真实短 11%）；若 30/40ms
    交替 → 实际 30 fps（漂移补偿，时长准确）。
- `gif调色板查看` 节点：色板图为**固定 16×16 色块**（RGBA，颜色不足 256 时缺色格透明），
  预览框按原始像素 1:1 显示；元数据为颜色数/含透明。

## 基础字段声明与空态骨架（决策 #142）

- **问题**：未运行的节点，元数据面板只有一句通用占位文案，用户看不到「这节点会给出
  哪些信息」；而「显示什么」此前只存在于 `default_describe_output` 的 if 分派里，
  是运行后才可见的隐式约定。
- **声明**：节点类属性 `METADATA_SPEC`（`nodes/metadata_spec.py`，纯常量、无 Qt）——
  `MetadataSpec(fields=(MetaField(label, placeholder), ...), extras=bool)`；
  `extras=True` 表示运行后还会追加动态字段（探测项/分析指标），空态附一行提示。
- **继承默认**：`SequenceNode` → `SEQUENCE_META`（帧数/尺寸/包含透明通道）、
  `ManifestNode` → `MANIFEST_META`（媒体类型/源文件数）；其余按输出形态覆写，
  完整清单见 [节点体系约定](node-conventions.md#metadata-spec)。
- **职责边界**：`METADATA_SPEC` 只声明**基础字段与顺序**（空态骨架），
  **不参与取值**；运行时的键值仍由 `describe_output` / `default_describe_output` 产生
  （动态字段不受声明限制）。两者一致性由 `tests/test_metadata_spec_contract.py` 守。
- **展示规则**：`output_metadata` 为空 → 骨架（`帧数：—` 逐行）；有值 → 实际结果。
  参数变更（dirty）不清空结果，因此骨架只出现在「从未运行」或「载入/清缓存后重置」。
- **UI 落点**：`HelpWidget._metadata_text`（`ui/widgets.py`）——无声明字段的节点仍回落
  「运行节点后将在此显示输出元数据。」。
