"""把 ImageMagick minimal 运行时复制到应用 runtime 目录。

minimal 集 = 13 个 CORE_RL DLL + PNG/GIF 两个 coder + config/thresholds.xml
（2026-08 实测：MagickCore 导入表闭包 + coder 依赖 + 阈值图配置。官方有序
仿色阈值图 o8x8/o2x2/h4x4a 等**不编译进 MagickCore**，由 MagickCore 运行时
从 ``thresholds.xml`` 加载——漏带则「颜色量化」节点勾选有序仿色选这些图会报
``InvalidArgument``（threshold.c）；app 自定义图 diag5x5 等由
data/thresholds.xml 提供，两者经 MAGICK_CONFIGURE_PATH 合并）。

coder 布局注意（2026-08 实测，见 docs/packaging.md「运行时发现与回退」）：
MagickCore 的 GetMagickModulePath（Windows 分支）**不自动拼
modules/coders/ 子目录**——它依次查 MAGICK_CODER_MODULE_PATH 环境变量
（值须直接指向含 coder DLL 的目录）→ MAGICK_HOME 根目录 → exe 目录 →
注册表 CoderModulesPath。因此本脚本把 coder **双份**复制：标准
modules/coders/ 布局（configure_imagemagick 的 MAGICK_CODER_MODULE_PATH
指向此处，主修复）+ 运行时根目录副本（兜底：即使环境变量被外部覆盖，
MAGICK_HOME 分支仍能找到）。

用法：改顶部 IM_DIR（或设环境变量 IMAGEMAGICK_SOURCE_DIR）后
     python scripts/prepare_imagemagick_runtime.py
"""
import os
import shutil

from pathlib import Path

# 手动指定的ImageMagick文件夹路径
IM_DIR = r"E:\PROGRAMS\ImageMagick-7.1.2-Q16-HDRI-folder"

#: 本机 ImageMagick 安装根目录（或 portable 解压目录）；环境变量可覆盖
SOURCE_DIR = os.environ.get("IMAGEMAGICK_SOURCE_DIR") or IM_DIR

#: 目标目录：开发态 app_root_dir() 所指（Nuitka 打包也从这里携带）
DEST_DIR = (Path(__file__).resolve().parents[1] / "src" / "gif_node_studio"
            / "runtime" / "imagemagick")

#: 实测最小集：核心 + MagickCore 导入表闭包 + PNG delegate
CORE_FILES = (
    "CORE_RL_MagickCore_.dll",
    "CORE_RL_MagickWand_.dll",
    "CORE_RL_bzip2_.dll",
    "CORE_RL_freetype_.dll",
    "CORE_RL_lcms_.dll",
    "CORE_RL_lqr_.dll",
    "CORE_RL_raqm_.dll",
    "CORE_RL_xml_.dll",
    "CORE_RL_zlib_.dll",
    "CORE_RL_glib_.dll",
    "CORE_RL_fribidi_.dll",
    "CORE_RL_harfbuzz_.dll",
    "CORE_RL_png_.dll",
)

#: coder 模块（modules/coders/ 下）
CODER_FILES = ("IM_MOD_RL_png_.dll", "IM_MOD_RL_gif_.dll")

#: 配置 XML（运行时根目录）：官方 ordered-dither 阈值图（o8x8 等）不编译进
#: MagickCore，随 MAGICK_CONFIGURE_PATH 的 runtime/imagemagick 目录加载。
#: 缺失则有序仿色选内建图报 InvalidArgument。
CONFIG_FILES = ("thresholds.xml",)


def main() -> None:
    src = Path(SOURCE_DIR)
    if not (src / "CORE_RL_MagickWand_.dll").is_file():
        raise SystemExit(f"[error] SOURCE_DIR 不是 ImageMagick 运行时根目录: {src}")
    if DEST_DIR.exists():
        shutil.rmtree(DEST_DIR)
    coders = DEST_DIR / "modules" / "coders"
    coders.mkdir(parents=True)
    for name in CORE_FILES:
        shutil.copy2(src / name, DEST_DIR / name)
    for name in CODER_FILES:
        shutil.copy2(src / "modules" / "coders" / name, coders / name)
    # 配置 XML：官方 ordered-dither 阈值图（o8x8 等）不编译进核心，随运行时携带。
    for name in CONFIG_FILES:
        shutil.copy2(src / name, DEST_DIR / name)
    # 兜底：coder 同时复制到运行时根目录。MagickCore 的 Windows 模块搜索
    # 链（GetMagickModulePath）不自动拼 modules/coders/ 子目录——即使
    # configure_imagemagick 已设置 MAGICK_CODER_MODULE_PATH（主修复），
    # 根目录副本保证环境变量被外部覆盖时 MAGICK_HOME 分支仍能找到 coder。
    for name in CODER_FILES:
        shutil.copy2(src / "modules" / "coders" / name, DEST_DIR / name)
    print(f"[ok] {len(CORE_FILES) + len(CODER_FILES) * 2 + len(CONFIG_FILES)} 个文件已复制到 {DEST_DIR}")


if __name__ == "__main__":
    main()
