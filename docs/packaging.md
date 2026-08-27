# 发行打包：需随程序携带的运行时（ImageMagick + gifsicle）

## ImageMagick 运行时（Wand 后端）

Wand 通过 ctypes 绑定 MagickWand/MagickCore 动态库，因此**发行包必须包含 ImageMagick 运行时文件**（`magick.exe` CLI 不需要）。运行时目录唯一约定为 `app_root_dir()/runtime/imagemagick/`：打包后（Nuitka standalone / PyInstaller）`app_root_dir()` = exe 所在目录，即 exe 旁 `runtime/imagemagick/`；开发态 `app_root_dir()` 返回**包目录** `src/anime_gif_node_studio`（见 `core/paths.py`），项目根 `runtime/imagemagick/` 不会被探测（见下「运行时发现与回退」）。运行时由 `scripts/prepare_imagemagick_runtime.py` 生成（约 10 MB；源目录 = 脚本顶部 `SOURCE_DIR` 变量，可用环境变量 `IMAGEMAGICK_SOURCE_DIR` 覆盖），清单见下表：

| 类别 | 文件（minimal 实测集 = 13 DLL + 2 coder，合计约 10 MB） | 说明 |
|---|---|---|
| 核心库 | `CORE_RL_MagickCore_.dll`、`CORE_RL_MagickWand_.dll` | Wand 的 ctypes 绑定目标 |
| MagickCore 导入表闭包 | `CORE_RL_bzip2_`、`CORE_RL_freetype_`、`CORE_RL_lcms_`、`CORE_RL_lqr_`、`CORE_RL_raqm_`、`CORE_RL_xml_`、`CORE_RL_zlib_`、`CORE_RL_glib_`、`CORE_RL_fribidi_`、`CORE_RL_harfbuzz_` | 缺任一则 MagickCore 无法加载（glib/fribidi/harfbuzz 为 lqr/raqm 传递依赖） |
| PNG delegate | `CORE_RL_png_.dll`（依赖 zlib） | 由 PNG coder 运行时加载 |
| coder 模块 | `modules/coders/IM_MOD_RL_png_.dll`、`IM_MOD_RL_gif_.dll` | GIF 零额外依赖（LZW 内置于 coder） |
| 合规 | `License.txt`、`NOTICE.txt` | 脚本强制保留 |

**不需要**：配置 XML（官方有序仿色阈值图 o8x8 等编译进 MagickCore；自定义图 diag5x5 等由
app `data/` 提供，`MAGICK_CONFIGURE_PATH` 恒带 `data/`）、VC 运行库（目标机系统提供
VC++ Redistributable，Nuitka 打包另在 exe 目录携带——随包只会造成 dist 内重复 DLL 如
vcruntime140.dll）、`mfc140u.dll` / 滤镜模块 / `Magick++` / CLI / 安装器残留。

minimal 集经六阶段精简 + 真 app 端到端验证（GIF 导出
MagickQuantizeImages（原样合成，决策 #77）、颜色深度含自定义 diag5x5 阈值图、
web-safe remap、GIF 解码）。需要完整 coder 集合排障时，直接整目录复制 `SOURCE_DIR`
并剔除安装器残留即可（约 50 MB）。

## 运行时发现与回退（实测语义，勿信旧文档「MAGICK_HOME → runtime → PATH」顺序）

- `configure_imagemagick()` 探测来源**唯一**：`app_root_dir()/runtime/imagemagick/`（glob `CORE_RL_MagickWand*.dll`，判据与 `imagemagick._has_magick_library` 一致）。**不读任何环境变量**——MAGICK_HOME / PATH / 注册表都不参与本项目探测。
- 命中时设置：`MAGICK_HOME`、**`MAGICK_CODER_MODULE_PATH`（= `runtime/imagemagick/modules/coders`，关键，见下）**、`MAGICK_MODULE_PATH`（modules/）、把运行时目录前置到 `PATH`（VC 运行库与依赖 DLL 解析）；`MAGICK_CONFIGURE_PATH` **恒设置**（应用自带 `data/` 目录置于最前，含自定义 `thresholds.xml`）。
- ⚠ **coder 模块搜索（2026-08 实测根因）**：MagickCore 的 `GetMagickModulePath` 对 coder 模块**只认 `MAGICK_CODER_MODULE_PATH` 环境变量**（`MAGICK_MODULE_PATH` 不参与），且 Windows 分支**不自动拼 `modules/coders/` 子目录**——值必须直接指向含 coder DLL 的目录。缺失时搜索链依次回退：`MAGICK_HOME` 根目录 → exe 目录 → 注册表 `HKLM\SOFTWARE\ImageMagick\<ver>\Q:16\CoderModulesPath`（指向系统 IM 安装路径）→ 用户目录。**若随包 coder 只在 `modules/coders/` 下而漏设该变量，运行时实际加载的是注册表指向的系统 IM coder**——系统 IM 被改名/换机后即报 `MissingDelegateError: NoDecodeDelegateForThisImageFormat`（「完全独立 runtime」失效的实测案例）。修复 = 设置 `MAGICK_CODER_MODULE_PATH` 指向随包 `modules/coders/`；`prepare_imagemagick_runtime.py` 另把 coder 双份复制到运行时根目录兜底（环境变量被覆盖时 `MAGICK_HOME` 分支仍可命中）。
- 未命中 → **静默回退** wand 的系统解析（`wand_available=True` 但 `home=None`）。wand 0.7.2 在 Windows 的解析顺序：① `MAGICK_HOME`（以此为根穷举 `CORE_RL_wand_*` / `CORE_RL_MagickWand_*` / `libMagickWand*` DLL）；② 仅当 ① 未设时读注册表 `HKLM\SOFTWARE\ImageMagick\Current` 的 `LibPath`（并追加到 PATH）；③ 无条件 `ctypes.util.find_library`（Windows 实现 = 纯遍历 PATH）。
- ⚠ 开发态 `app_root_dir()` = 包目录（不是项目根）→ 运行时目录解析为 `src/anime_gif_node_studio/runtime/imagemagick/`；`scripts/prepare_imagemagick_runtime.py` 默认即写入该处，Nuitka 打包经 `__main__.py` 的 `--include-data-dir={MAIN_DIRECTORY}/runtime/` 随包携带（exe 旁即为运行时目录）。判断 app 实际用了哪个 IM，以「关于」页 `ImageMagickRuntime.library_path` 为准（`GetModuleFileNameW` 反查实际加载的 DLL）。
- wand 加载器相关变量：只认 `MAGICK_HOME` 与 `WAND_MAGICK_LIBRARY_SUFFIX`；`WAND_MAGICK_LIBRARY_PATH` 为无效变量。⚠ **删/错设 `MAGICK_HOME` 模拟不了后端缺失**——wand 仍会回退 find_library 沿 PATH 加载成功；要模拟缺失须把 ImageMagick 目录从 PATH 剔除（新进程、`import wand` 之前）。

## CI（GitHub Actions）获取 portable 运行时

官方 Windows portable 包发布在 GitHub Releases（`.7z` 格式；旧的 `imagemagick.org/archive/binaries/` 已下线）。Windows runner 预装 7-Zip，钉版本下载解压后交给准备脚本生成 minimal 运行时：

```bash
curl -sL -o im.7z https://github.com/ImageMagick/ImageMagick/releases/download/7.1.2-29/ImageMagick-7.1.2-29-portable-Q16-HDRI-x64.7z
7z x -y -oIM_SRC im.7z    # 解压出 IM_SRC/ImageMagick-*/ 版本目录
IM_DIR=$(find IM_SRC -maxdepth 2 -name 'CORE_RL_MagickWand*.dll' -printf '%h\n' | head -1)
IMAGEMAGICK_SOURCE_DIR="$IM_DIR" python scripts/prepare_imagemagick_runtime.py
```

> 判据与 `imagemagick._has_magick_library` 一致（找到含 `CORE_RL_MagickWand*.dll` 的目录）。
> 脚本默认写入 `src/anime_gif_node_studio/runtime/imagemagick/`（开发态 `app_root_dir()` 所指）；
> 发行版探测的是 exe 旁 `runtime/imagemagick/`——`__main__.py` 的
> `--include-data-dir={MAIN_DIRECTORY}/runtime/=./runtime/` 把它随包携带。

## gifsicle 运行时（GIF 优化后端，2026-08 新增）

「GIF 优化」节点（[关键决策 #78](decisions/71-80.md#d78)）
通过 CLI 子进程调用 gifsicle（GPL v2）。运行时唯一约定为
`app_root_dir()/runtime/gifsicle/`（exe 旁或开发态包目录下），当前为
**本机编译的 1.96**（`gifsicle.exe` + `gifdiff.exe`，PE32+ x64，
`--version` = "LCDF Gifsicle 1.96 (Windows)"；编译步骤与 Makefile.w32 的
`kcolor.obj` 修复见 `media/gifsicle.py` 注释引用的调研存档/技能）。
eternallybored 移植版只有 1.95（缺 1.96 的 `--gamma=oklab` /
`--dither=atkinson` / `--use-exact-colormap`），本项目不使用。

- 探测：`media/gifsicle.py` 的 `configure_gifsicle()` 只认
  `runtime/gifsicle/`（glob `gifsicle.exe`），幂等缓存；缺失时「GIF 优化」
  节点报清晰错误，设置「关于」页显示未找到。
- 随包：与 ImageMagick 运行时同一 `--include-data-dir={MAIN_DIRECTORY}/runtime/`
  规则，整目录进 dist（约 0.7 MB）；**无额外 DLL 依赖**（静态链接的
  PE32+，VC 运行库由系统提供）。
- 合规：gifsicle 为 **GPL v2**——随发行包分发 gifsicle.exe 需附其源码与
  LICENSE（不感染宿主应用；与 ImageMagick 运行时的 License.txt/NOTICE.txt
  模式并存）。

## Nuitka standalone 打包（2026-08 修正：exe 启动报错排查）

构建命令（配置写在 `src/anime_gif_node_studio/__main__.py` 的 `# nuitka-project:` 指令里）：

```bash
uv run nuitka .\src\anime_gif_node_studio
```

即 `--mode=standalone --output-dir=dist --enable-plugin=pyside6,upx`，外加两个关键修正：

1. **`--include-module=PySide6.QtSvg`**：NodeGraphQt 经 `Qt.py`（qt_py shim）使用 QtSvg，
   Qt.py 用 `__import__("PySide6.QtSvg")` **动态导入**，Nuitka 静态分析看不到 → standalone 包缺
   `PySide6/QtSvg.pyd`（但 qt6svg.dll 与 qsvg 插件都在）→ 启动报
   `ImportError: cannot import name 'QtSvg' from 'Qt' (…dist\Qt.py)`（来自
   `NodeGraphQt/qgraphics/node_svg.py` 第 4 行 `from Qt import QtCore, QtGui, QtSvg, QtWidgets`）。
   注意：Nuitka standalone 的导入是**封闭世界**——编译期未注册的模块运行时不可导入，
   把 QtSvg.pyd 事后拷进 dist **无效**，必须在编译期注册。

2. **`_nuitka_av_shim.py`**（`__main__.py` 中 import）：PyAV 的 Python 模块是 Cython 编译的
   `.pyd`，它们在**运行时**才 `import av.utils` 等 cimport 伙伴模块（如 av/stream.pyd 解析
   cimport 符号），而这些模块没有任何静态导入者 → Nuitka 不注册 → 启动报
   `ModuleNotFoundError: No module named 'av.utils'`。shim 把 17 个运行时模块显式静态导入
   （集合由差分 `import av` 后 `sys.modules` 与包内静态导入语句算出）。**不能用 `--include-package=av` 替代**：
   它会重复包含已静态导入的 `av.codec.context`，在 Nuitka 4.1.3 触发
   `AssertionError: av.codec.context`（ModuleNodes.py avoid_duplicates，Nuitka 自身的 bug）。
   升级 PyAV 后若出现新的 `No module named 'av.xxx'`，重跑差分脚本扩充 shim 即可。

验证：`dist/anime_gif_node_studio.dist\anime_gif_node_studio.exe` 应能启动
（`QT_QPA_PLATFORM=offscreen` 下存活超过数秒即通过导入阶段；修复前 1 秒内退出并打印 traceback）。

## 用户数据目录（logs / settings.ini / cache 不随程序目录）

> 关键决策 [#84](decisions/81-90.md#d84)。

发行版经 Inno 安装到 `C:\Program Files` 后，exe 目录对**非管理员只读**——若
`settings.ini` / `logs/` / `cache/` 仍写在 exe 旁，普通用户启动即崩溃
（`setup_logging` 里 `logs_dir().mkdir` 抛 `PermissionError`；且 64 位进程
不享受 UAC VirtualStore 重定向）。

- **只读程序文件**（`app_root_dir()` = exe 目录）：exe/DLL、`runtime/`、
  `data/` —— 保持现状；
- **可写用户数据**（`user_data_dir()` =
  `%LOCALAPPDATA%\Ghooost\GIF Node Studio`，Qt
  `QStandardPaths.AppLocalDataLocation` 定位，不读环境变量）：
  - `settings.ini`（QSettings 默认路径；设置对话框「关于」页展示）；
  - `logs/app.log` 与 `logs/faulthandler.log`（1 MB 轮转）；
  - `cache/`（默认缓存目录；设置对话框仍可改路径，写探测校验见决策 #53）。
- 开发态 `user_data_dir()` = `app_root_dir()`（包目录），行为不变；
- **降级**：日志目录不可写时文件日志禁用、仅终端输出，绝不阻塞启动
  （`logging_setup.setup_logging` 兜底）；
- Inno 脚本（`scripts/innoSetup.iss`）：无需给 `{app}` 下任何目录授权；
  卸载**不删除** `%LOCALAPPDATA%` 用户数据（升级/重装保留设置与缓存）。
