"""media：媒体后端。

- ``backend.py`` —— MediaBackend：实例状态核心（``__init__`` / ``for_node`` /
  ``_progress`` / ``_job_dir``）+ 七区段方法的薄转发（决策 #120；外部调用方
  按 ``backend.<fn>(...)`` 调用，API 零改动）。
- ``backend_format.py`` / ``backend_color.py`` / ``backend_sequence.py`` /
  ``backend_export.py`` / ``backend_quantize.py`` / ``backend_analysis.py`` /
  ``backend_cache.py`` —— 七职责区段纯函数模块（决策 #120 由 MediaBackend 拆出；
  无实例状态，工作区/进度等依赖由调用方显式注入）。
- ``palettes.py`` —— 调色板/阈值图/系统色板辅助。
- ``image_utils.py`` —— wand/PIL 像素与字节转换、缓存 PNG 压缩常量。
- ``media_info.py`` —— 源探测与输出元数据描述。
- ``imagemagick.py`` —— ImageMagick 运行时探测与环境变量注入。
- ``sequence.py`` —— 文件名连续数字序列发现。
"""
