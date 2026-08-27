"""gifsicle 运行时探测与定位（GIF 优化 CLI 后端）。

gifsicle（Eddie Kohler，GPL v2）只有 CLI、没有官方库/ctypes 绑定，
pygifsicle 本质就是 subprocess 包装（无任何额外价值），因此本项目直接
调用随包 gifsicle.exe（见 [gifsicle 调研存档](../../docs/research/gifsicle-evaluation.md)）：
- 运行时来源唯一：``runtime/gifsicle/``（与 ImageMagick 运行时同模式，
  由 prepare 脚本/手工放置，app_root_dir() 定位，缺失时明确报错）；
- 本模块只负责：定位可执行文件、探测版本、构造命令行参数、可用性守卫；
  实际子进程调用在 ``MediaBackend.optimize_gif``（文件进/文件出，一次
  有界调用）。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..core.paths import app_root_dir

# 单次 gifsicle 调用超时（秒）：大 GIF + -O3 可能较慢，给足余量；
# 超时以清晰中文报错抛出（子进程被杀，不会留下半成品——输出走临时文件）。
GIFSICLE_TIMEOUT_S = 600

# Windows：无控制台的 GUI 父进程（Nuitka attach 双击启动）每次拉起控制台
# 子系统子进程（gifsicle.exe）时，Windows 都会为子进程新建一个控制台窗口
# ——屏幕上闪黑框。CREATE_NO_WINDOW 禁止该窗口（仅 Windows 有该标志，
# getattr 兜底使代码可跨平台导入）。子进程输出本就走 capture_output 管道，
# 不需要控制台。
CREATE_NO_WINDOW_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 优化级别键 → gifsicle 参数（-O1/-O2/-O3）；"none" 表示不传 -O。
OPTIMIZE_LEVELS = ("none", "o1", "o2", "o3")
# 取色方法键（--color-method）：diversity 从现有颜色取严格子集（默认），
# blend-diversity 对色群取混合色，median-cut 为 Heckbert 经典算法。
COLOR_METHODS = ("diversity", "blend-diversity", "median-cut")
# 内建固定色板键（--use-colormap）：web=216 色 Web-safe；gray/bw=灰度/黑白；
# file=自定义文本色板或 GIF 文件的全局色表（路径由参数提供）。
BUILTIN_COLORMAPS = ("web", "gray", "bw")


def _runtime_dir() -> Path:
    """gifsicle 运行时根目录（**唯一来源**）：随包 runtime/gifsicle/。

    以 app_root_dir() 为基准：打包后（Nuitka/PyInstaller）= exe 旁
    runtime/gifsicle/；开发态指向 src/gif_node_studio/runtime/gifsicle/。
    """
    return app_root_dir() / "runtime" / "gifsicle"


@dataclass(frozen=True)
class GifsicleRuntime:
    """gifsicle 运行时探测结果。

    - ``exe`` —— 找到的 gifsicle 可执行文件路径；None 表示未找到；
    - ``available`` —— 是否可用（exe 存在且能响应 ``--version``）；
    - ``version`` —— 解析出的版本号（如 "1.96"）；
    - ``version_line`` —— ``--version`` 输出的首行（如 "LCDF Gifsicle 1.96 (Windows)"）。
    """

    exe: Path | None
    available: bool
    version: str | None
    version_line: str | None


def _probe_version(exe: Path) -> tuple[str | None, str | None]:
    """运行 ``gifsicle --version``，解析 (版本号, 首行)；失败返回 (None, None)。

    ``stdin=subprocess.DEVNULL``：Nuitka 产物以 ``--windows-console-mode=attach``
    双击启动（无控制台可附加）时标准句柄为无效值，subprocess 在
    ``_get_handles`` 中处理 stdin 继承会抛 ``OSError: [WinError 6] 句柄无效``
    （见 Nuitka issue #3030）；gifsicle 只读输入文件、不读 stdin，
    显式 DEVNULL 彻底绕开无效句柄（探测与优化两处调用一致）。
    ``creationflags=CREATE_NO_WINDOW_FLAG``：无控制台父进程启动控制台子进程
    会闪黑框，禁止新建控制台窗口。
    """
    try:
        result = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW_FLAG,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if result.returncode != 0:
        return None, None
    first = (result.stdout or "").strip().splitlines()[0] if (result.stdout or "").strip() else None
    version: str | None = None
    if first:
        match = re.search(r"Gifsicle\s+(\d+\.\d+(?:\.\d+)?)", first)
        if match:
            version = match.group(1)
    return version, first


def configure_gifsicle(candidates: Iterable[Path] | None = None) -> GifsicleRuntime:
    """定位 gifsicle 并探测版本；找不到时返回 available=False（不抛错）。

    运行时来源唯一：``runtime/gifsicle/``（显式传入 candidates 供测试覆盖）。
    幂等：进程内只探测一次（与 ``configure_imagemagick`` 同模式）。
    调用方（backend / 关于页）在真正需要时给出明确报错。
    """
    global _RUNTIME_CACHE
    if _RUNTIME_CACHE is not None:
        return _RUNTIME_CACHE
    if candidates is None:
        candidates = (_runtime_dir(),)
    exe: Path | None = None
    for directory in candidates:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for name in ("gifsicle.exe", "gifsicle"):
            candidate = directory / name
            if candidate.is_file():
                exe = candidate
                break
        if exe is not None:
            break
    if exe is None:
        _RUNTIME_CACHE = GifsicleRuntime(None, False, None, None)
        return _RUNTIME_CACHE
    version, version_line = _probe_version(exe)
    _RUNTIME_CACHE = GifsicleRuntime(exe, True, version, version_line)
    return _RUNTIME_CACHE


def require_gifsicle(runtime: GifsicleRuntime, operation: str) -> None:
    """GIF 优化前的 gifsicle 可用性守卫，报错信息包含排查方向。"""
    if not runtime.available or runtime.exe is None:
        raise RuntimeError(
            f"{operation}需要 gifsicle 命令行工具（当前不可用）。"
            "请确认随包运行时 runtime/gifsicle/gifsicle.exe 存在。"
        )


def build_gifsicle_args(
    input_path: str | Path,
    output_path: str | Path,
    *,
    optimize: str = "o3",
    lossy: int = 0,
    recolor: bool = False,
    colors: int = 128,
    color_method: str = "diversity",
    dither: str = "floyd-steinberg",
    colormap: str = "none",
    colormap_file: str | None = None,
    careful: bool = False,
) -> list[str]:
    """构造 gifsicle 命令行参数（纯函数，便于测试参数顺序与非法值校验）。

    与 ImageMagick 节点同模式：节点参数映射 gifsicle CLI 选项 1:1，
    机器键在此集中校验（非法键构造即报错，不等到子进程）。

    - ``optimize``：``none``/``o1``/``o2``/``o3`` → ``-O1``/``-O2``/``-O3``；
    - ``lossy``：0–200，0 = 不启用有损压缩（不传 ``--lossy``）；
    - ``recolor``：GIF 级再降色总开关——开启后传 ``--colors``/
      ``--color-method``；``dither != "none"`` 时再传 ``--dither=方法``；
      ``colormap`` 非 none 时传 ``--use-colormap=web|gray|bw|<文件路径>``；
    - ``careful``：``--careful``（极小化 GIF 兼容老 Java/IE 播放器）。

    返回参数列表（不含可执行文件本身），输入文件与 ``--output`` 放在末尾。
    """
    if optimize not in OPTIMIZE_LEVELS:
        raise ValueError(f"未知优化级别键：{optimize!r}（可选：{OPTIMIZE_LEVELS}）")
    if not 0 <= lossy <= 200:
        raise ValueError(f"有损度必须在 0–200 之间（当前 {lossy}）")
    args: list[str] = []
    if optimize != "none":
        args.append(f"-O{optimize[1]}")  # "o1" → "-O1"
    if lossy > 0:
        args.append(f"--lossy={lossy}")
    if recolor:
        args.append(f"--colors={max(2, min(256, colors))}")
        if color_method not in COLOR_METHODS:
            raise ValueError(f"未知取色方法键：{color_method!r}（可选：{COLOR_METHODS}）")
        args.append(f"--color-method={color_method}")
        if dither != "none":
            args.append(f"--dither={dither}")
        if colormap != "none":
            if colormap in BUILTIN_COLORMAPS:
                args.append(f"--use-colormap={colormap}")
            elif colormap == "file":
                if not colormap_file:
                    raise ValueError("GIF 优化：固定色板选择「自定义文件」但未提供色板文件")
                args.append(f"--use-colormap={colormap_file}")
            else:
                raise ValueError(f"未知固定色板键：{colormap!r}")
    if careful:
        args.append("--careful")
    args.extend([str(input_path), "--output", str(output_path)])
    return args


_RUNTIME_CACHE: GifsicleRuntime | None = None
