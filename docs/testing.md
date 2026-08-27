# 验证方式

测试代码验证「程序可运行」：程序加 `--test` 启动后，在全部初始化完毕后自动退出并返回 pass。各功能与 UI 的细节设计沉淀在 docs/ 文档与源码 docstring/注释中，不重复固化于测试断言。

## 快速验证

```bash
uv run pytest -q        # 14 项：自检冒烟、预览 DPI 回归、启动画面探针、智能布局纯计算等
```

或直接手工自检（不经 pytest）：

```bash
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run python -m anime_gif_node_studio --test
# 期望输出 SMOKE_OK: initialization complete 且退出码 0
```

- `--test` 与生产启动走**完全相同**的初始化路径：日志 → 卡死诊断 → QApplication → 主题 → `MainWindow` 全构造（节点注册表、全部 UI 构建、设置恢复、ImageMagick 运行时探测）。初始化完毕后打印 `SMOKE_OK: initialization complete` 并以 0 退出；任何未捕获异常 → 非 0 退出码。
- 自检使用临时工作区与设置文件（`tempfile.mkdtemp`），不触碰真实 `cache/` 与 `settings.ini`。
- pytest 侧（`tests/test_selftest.py`）以子进程运行 `python -m anime_gif_node_studio --test`（offscreen），断言退出码 0 与 `SMOKE_OK` 标记；子进程环境显式覆盖 `PYTHONPATH=src`。

## 开发约定

- 功能验证的结论（覆盖点、项数、结果）写入决策记录 / 本文档，成为长期可查的设计沉淀；验证脚本本身是**一次性**的——验证完毕即删除，结论留在文档中。
- 仅保留需要反复使用的冒烟脚本（如 `prototypes/` 无头端到端冒烟）。

## 行为细节的去向（不再有测试断言的覆盖点）

- 全部节点行为与参数：docs/node-list.md + 源码节点 class 的 docstring/HELP。
- 设计决策：docs/decisions/。
- 预览/合成语义：docs/preview.md；执行管线：docs/execution-pipeline.md；已知限制：docs/limitations.md；打包：docs/packaging.md。
- 手动无头端到端冒烟（真实小图 GIF 输入 → 处理 → 导出）：`prototypes/async_smoke.py` / `prototypes/async_app_smoke.py`，成功打印 `SMOKE_OK:` 并以 0 退出，可手工运行或接入 CI。

## 手动冒烟

```bash
PYTHONPATH=src uv run python -m anime_gif_node_studio        # 真实窗口
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run python prototypes/async_smoke.py [gif路径]   # 无头端到端
PYTHONPATH=src QT_QPA_PLATFORM=offscreen uv run python prototypes/async_app_smoke.py
```

## 注意事项

- offscreen 字体库**无中文字体**：qtawesome 首次 `icon()` 注册应用字体后中文度量坍缩为 0（只影响 offscreen 下的文字宽度测量，真实 Windows 有中文字体不受影响）。
- 视频链路在开发期用 PyAV 现场生成 mp4 验证过（未固化为测试文件）；GIF 链路依赖 ImageMagick 运行时（`--test` 初始化会探测，Wand 不可用不阻塞启动，仅在真正 GIF 操作时报错）。
- 运行前确认 `PYTHONPATH` 未被外部环境注入其它包路径（可能遮蔽本项目依赖，导致 wand/PIL 异常）。
