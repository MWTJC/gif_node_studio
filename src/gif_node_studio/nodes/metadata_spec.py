"""输出元数据字段声明：空态骨架的唯一源头（决策 #142）。

- ``MetaField`` —— 一个基础字段（显示标签 + 空态占位符）；
- ``MetadataSpec`` —— 有序字段集 + ``extras`` 语义（运行后是否允许追加动态字段）；
- 常量 ``SEQUENCE_META`` / ``MANIFEST_META`` / ``ANALYSIS_META`` /
  ``FILE_LIST_META`` / ``EXPORT_FILE_META`` / ``GIF_EXPORT_META`` —— 按输出值类型
  固定的一份字段集。

声明层规则：
- **纯常量**：不在构造期探测文件/媒体，声明期只描述「基础字段」——目录构建
  （实例化 60 个节点 ≈57ms）不允许被 I/O 拖慢；
- 只声明**稳定核心**：GIF 探测项、多输出端口前缀、分析节点自定义指标等动态
  字段由 ``describe_output`` 运行时追加（``extras=True`` 表示「运行后还会多」）；
- 标签与 ``media/media_info.default_describe_output`` 的键由契约测试保持一致
  （``tests/test_metadata_spec_contract.py``），避免两处漂移。

节点用法（默认继承，不需要写任何代码）：``SequenceNode`` / ``ManifestNode``
族基类各自挂上对应规范；输出形态与族默认不一致或元数据仅关键信息的节点，
覆写类属性即可（如 ``FormatNode`` → ``SEQUENCE_META``、导出终端 → 
``EXPORT_FILE_META``）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetaField:
    """元数据基础字段：``label`` 同时是运行时的键名，``placeholder`` 为空态占位。"""

    label: str
    placeholder: str = "—"


@dataclass(frozen=True)
class MetadataSpec:
    """节点输出元数据的**基础字段清单**（空态骨架）与动态字段开关。

    - ``fields`` —— 有序基础字段（严格按此顺序展示，声明即文档）；
    - ``extras`` —— 运行后是否追加动态字段（如「文件名 : 文件大小」之后的 GIF
      探测项、分析指标）；为空态提示文案使用，不影响运行时字典。
    """

    fields: tuple[MetaField, ...] = ()
    extras: bool = False

    @property
    def labels(self) -> tuple[str, ...]:
        """基础字段的显示标签（= ``describe_output`` 的键名，契约测试据此核对）。"""
        return tuple(field.label for field in self.fields)

    def skeleton_text(self) -> str:
        """空态骨架：每字段一行「标签：占位」；``extras`` 追加一行提示。

        无字段声明时返回空串，由调用方回落到通用占位文案（不运行、不探测）。
        """
        if not self.fields:
            return ""
        lines = [f"{field.label}：{field.placeholder}" for field in self.fields]
        if self.extras:
            lines.append("（运行后追加详细信息）")
        return "\n".join(lines)


# 序列图片产物（SequenceArtifact）：恒 3 项，不含帧率（决策 #36）。
SEQUENCE_META = MetadataSpec(
    (MetaField("帧数"), MetaField("尺寸"), MetaField("包含透明通道")),
)

# 格式化清单（MediaManifest）：恒 2 项基础字段；GIF/视频/图片探测项运行时追加。
MANIFEST_META = MetadataSpec(
    (MetaField("媒体类型"), MetaField("源文件数")),
    extras=True,
)

# 分析节点（AnalysisResult）：预览文件信息（Path 探测）为稳定核心，指标运行时追加。
ANALYSIS_META = MetadataSpec(
    (MetaField("文件名"), MetaField("文件大小"), MetaField("尺寸")),
    extras=True,
)

# 文件元组/列表（PNG 序列导出）：恒 2 项。
FILE_LIST_META = MetadataSpec((MetaField("输出文件数"), MetaField("输出总大小")))

# 导出终端节点：关键信息（实测输出尺寸 + 可读文件大小）；个别节点追加编码说明。
EXPORT_FILE_META = MetadataSpec((MetaField("尺寸"), MetaField("文件大小")), extras=True)

# GIF 导出终端：在尺寸/文件大小之外再保证播放帧速（实测自产物帧延迟，
# 与 gif优化分析「全局帧率」同口径，决策 #143）。
GIF_EXPORT_META = MetadataSpec(
    (MetaField("尺寸"), MetaField("帧速"), MetaField("文件大小")),
)
