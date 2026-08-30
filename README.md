# GIF Node Studio

- 我没准备正儿八经维护这个项目，喜欢的拿去即可

节点式 GIF 制作器：在节点画布上把视频 / 图片序列编排成 GIF、ICO 或 PNG 序列。

![logo.gif](build_src/ico/logo.gif)

主要使用下面的技术：
- PySide6
- NodeGraphQt
- PyAV（含进程内 FFmpeg 滤镜图）
- Wand（ImageMagick）
- Gifsicle
- Pillow

## 例子
### 风格化

<img alt="风格化1.gif" height="200" src="docs/imgs/%E9%A3%8E%E6%A0%BC%E5%8C%961.gif"/>

<img alt="风格化2.gif" height="200" src="docs/imgs/%E9%A3%8E%E6%A0%BC%E5%8C%962.gif"/>

### 录屏优化
<center>
    <img src="docs/imgs/%E5%BD%95%E5%B1%8F1.gif" alt="录屏1.gif">
    <br>
    <div>849*522 1.95MB</div>
</center>

## 特性

- **节点式工作流**：输入 → 截取 → 格式化 → 处理（含画面裁剪等）→ 输出，按端口颜色连线（橙色 = 格式化清单，蓝色 = 序列图片）
- **多种输入**：视频（PyAV 流式解码）、连续编号图片序列、GIF、空白 / 渐变序列
- **可视化裁剪**：序列级画面裁剪节点，在 1:1 预览图（框 = 素材像素尺寸，跟随图片）上直接拖拽裁剪框，支持纵横比锁定，可放在处理链任意位置
- **丰富处理节点**：时间 / 帧位截取、序列往复 / 抽帧 / 长度统一、序列相加 / 序列叠加（层叠合成，含「不缩放」策略）、帧冻结（首/末帧定格延长）、**序列剃刀**（胶片条拖拽剃刀线切割，实时预览切割处两侧帧）、亮度 / 饱和度 / 色相 / 对比度 / 二值化 / 灰度化 / 反相、旋转、纵横比挤压、颜色量化（ImageMagick 原生）、帧差静止保持（录屏 → GIF 时域去噪）、通道分离 / 合并、超级键抠像、平移滚动动效
- **多种输出**：GIF 合成（wand 原样合成）、GIF 合成(FFmpeg)（PyAV 进程内 palettegen/paletteuse，编码时直接做帧优化）、GIF 优化（gifsicle）、WebP / APNG 动画导出（Pillow 内建）、ICO 合成、PNG 序列导出，均带「导出…」按钮直接保存
- **分析节点**：GIF 调色板、1:1 分辨率、GIF 优化形态、ICO 分辨率查看
- **自动模式**：修改参数或连线后自动重跑受影响链路；后台线程串行执行，可随时停止

完整节点清单与参数说明见 [docs/node-list.md](docs/node-list.md)。

## 环境要求

- Windows
- Python 3.11 + [uv](https://docs.astral.sh/uv/) （NodeGraphQt在更高版本python有import问题）
- ImageMagick 7 运行时（Wand 动态库绑定，不需要 `magick.exe` CLI；使用随包 `runtime/imagemagick/` 目录）
- gifsicle（1.96，请自行编译）（「GIF 优化」节点使用；随包 `runtime/gifsicle/` 目录）

## 开发运行
```bash
git clone
cd 进入
```
将imagemagick与gifsicle（1.96）分别复制到  
`src\gif_node_studio\runtime\gifsicle`   
与  
`src\gif_node_studio\runtime\imagemagick`  
（或者参照`scripts\prepare_imagemagick_runtime.py`与`scripts\prepare_gifsicle_runtime.py`）

```bash
# 安装环境
uv sync
# 生成资源包文件
uv run scripts/pack_rc.py
# 启动
$env:PYTHONPATH="src"
uv run src/gif_node_studio/__main__.py
```

## 基本使用

1. 从左侧节点库创建节点（按输入 / 预格式化 / 格式化 / 序列处理 / 一般处理 / 通道处理 / 动效处理 / 输出 / 分析分类）。
2. 按端口颜色连线：橙色为「格式化清单」，蓝色为「序列图片」。
3. 单击节点，在右侧「节点参数」面板编辑参数。
4. 点击「运行至此节点」执行，或开启「自动模式」让修改自动触发重跑。
5. 输出节点带「导出…」按钮，直接保存结果文件。

节点方案可通过「保存预设 / 读取预设」存档与恢复（NodeGraphQt JSON），菜单栏「文件 → 导入预设」可增量导入 `node_presets/` 目录中的全部预设。

## 文档

- [docs/ 文档中心](docs/README.md)：架构、数据模型、节点约定、决策记录、测试与打包
- [节点清单](docs/node-list.md)：全部节点与参数说明

## 测试

```bash
uv run pytest -q
```

测试覆盖自检冒烟（`--test` 走与生产完全相同的初始化路径后自动退出）、预览 DPI 回归、启动画面探针等，详见 [docs/testing.md](docs/testing.md)。
