"""ImageMagick 运行时探测与定位（Wand 专用，不依赖 magick.exe CLI）。

Wand 通过 ctypes 直接绑定 ImageMagick 动态库（MagickWand/MagickCore DLL），
因此本模块只负责：定位随包运行时目录、注入环境变量、验证 Wand 可用性。
发行打包时需随程序携带 ImageMagick 运行时文件（DLL + modules + 配置 XML），
具体清单见 docs/packaging.md。
"""

from __future__ import annotations

import os, sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..core.paths import app_root_dir, im_data_dir

# 事先移除外部IM以控制变量
os.environ.pop('MAGICK_HOME', None)
os.environ['PATH'] = os.pathsep.join(
    p for p in os.environ.get('PATH','').split(os.pathsep)
    if p and 'ImageMagick' not in p)
sys.path.insert(0, 'src')



def _runtime_dir() -> Path:
    """ImageMagick 运行时根目录（**唯一来源**）：随包 runtime/imagemagick/。

    以 app_root_dir() 为基准：打包后（Nuitka/PyInstaller）= exe 旁
    runtime/imagemagick/；开发态 app_root_dir() 指向包目录（见 paths.py），
    故解析为 src/gif_node_studio/runtime/imagemagick/——由
    scripts/prepare_imagemagick_runtime.py（minimal 模式）生成。
    不探测 MAGICK_HOME / PATH / 注册表，保证任何环境下行为一致
    （提高发行版在其他机器的可靠性）。
    """
    return app_root_dir() / "runtime" / "imagemagick"


@dataclass(frozen=True)
class ImageMagickRuntime:
    home: Path | None  # 找到的 ImageMagick 根目录（含 DLL 与配置）；None 表示由系统解析
    wand_available: bool  # Wand 是否可实际创建图像
    version: str | None  # ImageMagick 版本字符串
    library_path: str | None  # 实际加载的 MagickWand 动态库文件路径（关于页/诊断用）


def _loaded_library_path() -> str | None:
    """反查当前 Wand 实际加载的 MagickWand 动态库文件路径。

    Wand 通过 ctypes 按 DLL 名加载（Windows 上通常是 ``CORE_RL_MagickWand_.dll``），
    这里用 GetModuleFileNameW 从已加载句柄取得真实文件路径——它才是 ImageMagick
    依赖的**实际来源**（随包 runtime/imagemagick/ 或系统安装目录，两者可能不同）。
    非 Windows 平台或查询失败时返回 None。
    """
    try:
        import ctypes
        import wand.api as wand_api

        handle = getattr(wand_api.library, "_handle", None)
        if not handle:
            return None
        if not hasattr(ctypes, "windll"):  # GetModuleFileNameW 仅 Windows
            return None
        buffer = ctypes.create_unicode_buffer(4096)
        size = ctypes.windll.kernel32.GetModuleFileNameW(
            ctypes.c_void_p(handle), buffer, len(buffer)
        )
        if size == 0 or size >= len(buffer):
            return None
        return buffer.value or None
    except Exception:
        return None


def _has_magick_library(directory: Path) -> bool:
    """目录内是否包含 MagickWand 动态库（Wand 的绑定目标）。"""
    if not directory.is_dir():
        return False
    return any(directory.glob("CORE_RL_MagickWand*.dll")) or any(
        directory.glob("MagickWand*.dll")
    )


def configure_imagemagick(candidates: Iterable[Path] | None = None) -> ImageMagickRuntime:
    """定位 ImageMagick 并验证 Wand 可用；仅使用 Wand（不再需要 magick.exe）。

    运行时来源唯一：``runtime/imagemagick/``（显式传入 candidates 供测试覆盖）。
    找到后注入环境变量，使 Wand 的 ctypes 加载器与 MagickCore 的配置/模块
    查找都指向该目录；未找到时交由 wand 的系统解析（注册表/PATH，仍可能
    可用，但不作为设计依赖）。
    返回的 runtime 恒不抛错——Wand 不可用时由调用方（backend）在真正需要
    GIF 能力时给出明确报错。

    MAGICK_CONFIGURE_PATH 恒把应用自带的 ``data/`` 目录放在最前：该目录包含
    自定义 ``thresholds.xml``（diag5x5 等官方配置没有的阈值图）。ImageMagick
    会遍历配置路径中的全部 thresholds.xml，因此不会遮蔽安装目录的内建映射。

    幂等：每个进程只执行一次。重复调用会再次改写环境变量（PATH 重复前置、
    MAGICK_* 重新指向），实测会导致已加载的 wand/ImageMagick 出现随机性
    原生崩溃（access violation / illegal instruction）；而应用会为每个节点、
    每次运行创建新的 MediaBackend，必须保证这里只配置一次。
    """
    global _RUNTIME_CACHE
    if _RUNTIME_CACHE is not None:
        return _RUNTIME_CACHE
    if candidates is None:
        candidates = (_runtime_dir(),)
    home = next((Path(path) for path in candidates if _has_magick_library(Path(path))), None)
    # 应用自带配置目录：自定义 thresholds.xml（diag5x5 阈值图）。
    data_dir = im_data_dir()
    configure_path = str(data_dir)
    if home is not None:
        os.environ["MAGICK_HOME"] = str(home)
        os.environ["MAGICK_MODULE_PATH"] = str(home / "modules")
        # 关键：MagickCore 的 GetMagickModulePath 只认
        # MAGICK_CODER_MODULE_PATH / MAGICK_FILTER_MODULE_PATH（**不是**
        # MAGICK_MODULE_PATH），且 Windows 分支不自动拼 coders/ 子目录——
        # 值必须直接指向含 coder/filter DLL 的目录。缺失时 MagickCore 会
        # 依次回退 MAGICK_HOME 根目录、exe 目录、注册表 CoderModulesPath
        # （指向系统 IM 安装路径，改名/换机即失效）——这就是「完全独立
        # runtime」失败的根源（2026-08 实测：随包 coder 在
        # modules/coders/ 下，而搜索链从不访问该子目录）。
        os.environ["MAGICK_CODER_MODULE_PATH"] = str(home / "modules" / "coders")
        os.environ["MAGICK_FILTER_MODULE_PATH"] = str(home / "modules" / "filters")
        os.environ["PATH"] = str(home) + os.pathsep + os.environ.get("PATH", "")
        configure_path += os.pathsep + str(home)
    os.environ["MAGICK_CONFIGURE_PATH"] = configure_path
    try:
        from wand.image import Image as WandImage

        with WandImage(width=1, height=1):
            pass
        version: str | None = None
        try:
            import wand.version as wand_version

            version = getattr(wand_version, "MAGICK_VERSION", None)
        except Exception:
            pass
        _RUNTIME_CACHE = ImageMagickRuntime(
            home, True, version, _loaded_library_path()
        )
    except (ImportError, OSError):
        _RUNTIME_CACHE = ImageMagickRuntime(home, False, None, None)
    return _RUNTIME_CACHE


_RUNTIME_CACHE: ImageMagickRuntime | None = None


def require_wand(runtime: ImageMagickRuntime, operation: str) -> None:
    """GIF 相关操作前的 Wand 可用性守卫，报错信息包含排查方向。"""
    if not runtime.wand_available:
        raise RuntimeError(
            f"{operation}需要 Wand 与 ImageMagick 动态库（当前不可用）。"
        )
