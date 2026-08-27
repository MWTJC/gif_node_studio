# 数据模型

> 领域模型集中在 `core/domain.py`，均为不可变描述符。

## `MediaManifest`（不可变 dataclass）

```python
MediaManifest(
    kind: MediaKind,            # VIDEO / STATIC_SEQUENCE / ANIMATED_IMAGE
    sources: tuple[str, ...],   # 源路径（序列为有序帧路径）
    start: float|int|None,      # 截取起点（time: 秒；frame: 帧号）
    end: float|int|None,        # 截取终点（0/None 表示开放）
    crop: CropSpec,             # 归一化裁剪（默认恒等 (0,0,1,1)）
    scale_percent: int,         # 解码分辨率百分比（仅格式化节点设置）
    range_mode: str,            # "time" / "frame"
    preview: str|None,          # 代表性预览图路径（首帧，输入节点物化）
)
```

校验：`end <= start` 抛错（截取串行合成后若窗口异常，会在 `replace` 时得到清晰报错）。

## `SequenceArtifact`（格式化解码产物）

`frames: tuple[str,...]`、`width`、`height`、`has_alpha`、`cache_dir`。
**不携带帧率/帧速信息**（用户需求，见[关键决策 #36](decisions/31-40.md#d36)）：需要帧率工作的节点
（GIF 合成、抽帧）均由用户把帧速作为参数输入。

## `CropSpec.compose(inner)`

两个归一化裁剪合成（先应用 `self`，再在其区域内应用 `inner`）为一个等效规格：

```
left   = self.left   + inner.left   * (self.right - self.left)
top    = self.top    + inner.top    * (self.bottom - self.top)
right  = self.left   + inner.right  * (self.right - self.left)
bottom = self.top    + inner.bottom * (self.bottom - self.top)
```
