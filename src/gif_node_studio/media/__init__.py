"""media：媒体后端（mediaclass）。

按职责分文件（2026-08 代码整理，见关键决策 #82）：

- ``backend.py`` —— MediaBackend 组合类（实例状态 + mixin 汇总）。
- ``backend_format.py`` / ``backend_color.py`` / ``backend_sequence.py`` /
  ``backend_export.py`` / ``backend_quantize.py`` / ``backend_analysis.py`` /
  ``backend_cache.py`` —— MediaBackend 各职责 mixin。
- ``palettes.py`` —— 调色板/阈值图/系统色板辅助。
- ``image_utils.py`` —— wand/PIL 像素与字节转换、缓存 PNG 压缩常量。
- ``media_info.py`` —— 源探测与输出元数据描述。
- ``imagemagick.py`` —— ImageMagick 运行时探测与环境变量注入。
- ``sequence.py`` —— 文件名连续数字序列发现。
"""
