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
  覆写 `describe_output`——execute 把可读形式的文件大小等附到 `MultiOutput.metadata`，
  节点直接透出，不再逐端口展开/深度探测（大 GIF 的 probe_gif 九字段、序列帧数/尺寸
  等对导出节点是噪音）。PNG 序列导出返回文件元组，不覆写，走默认行为
  （输出文件数/输出总大小，显示时可读化）。
- `gif优化分析` 节点的元数据**不含逐帧明细**（仅画布/帧数/循环/解码方式/帧优化/帧优化占比/
  透明优化统计；逐帧信息由滑条逐帧查看承担）。其中：
  - **帧优化**（局部帧数）**按文件结构计算**（图像描述符声明的存储区域）——存储帧与
    合成帧两种解码方式都如实反映文件的帧优化情况（合成帧模式下 coalesce 只抹掉显示层）；
  - **帧优化占比**以最短帧时间为基准：相对**等时长全最短帧序列**，优化后的 GIF 节省的
    帧数百分比（``总时长 ÷ 最短帧时间`` 为基准帧数，占比 = 1 − 实际帧数 ÷ 基准帧数，
    如「节省 42.9%（等时长全最短帧 7 帧 → 实际 4 帧）」；帧时间缺失或含 0 时显示
    「—（无法以最短帧为基准）」）；同样与解码方式无关。
- `gif调色板查看` 节点：色板图为**固定 16×16 色块**（RGBA，颜色不足 256 时缺色格透明），
  预览框按原始像素 1:1 显示；元数据为颜色数/含透明。
