"""core：核心基础设施与领域模型（无 Qt / 媒体后端依赖）。

- domain.py        —— 不可变清单/产物描述符（MediaManifest / SequenceArtifact / CropSpec…）
- options.py       —— 参数选项唯一源头（ChoiceOption / ChoiceGroup）
- paths.py         —— 路径解析（app_root_dir 只读程序文件 / user_data_dir 可写用户数据）
- logging_setup.py —— loguru 文件日志与卡死诊断（faulthandler）
"""
