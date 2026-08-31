"""gifski 运行时探测与定位（「GIF 合成(gifski)」CLI 后端，决策 #124）。

gifski（Kornel，AGPL-3.0-or-later，见[调研存档](../../docs/research/gif-ecosystem-evaluation.md)）
官方主分发即 CLI（Windows zip，gif.ski 下载）；无官方维护的 Python 绑定
（PyPI 的 pygifsicle 是 gifsicle 的封装，与 gifski 无关），libgifski C API
需 Rust 工具链自编译——CLI 形态与项目 gifsicle 先例完全同构，故直接调用
随包 gifski.exe：
- 运行时来源唯一：``runtime/gifski/``（与 gifsicle/ImageMagick 运行时同模式，
  由 prepare 脚本放置，app_root_dir() 定位，缺失时明确报错）；
- 本模块只负责：定位可执行文件、探测版本、构造命令行参数、可用性守卫；
  实际子进程调用在 ``MediaBackend.export_gif_gifski``（帧序列 → GIF，一次
  有界调用）。

gifski 设计特性（节点选项按此设计）：quality（量化质量）/ motion-quality
（时域一致性/跨帧颜色重用）/ lossy-quality（有损 LZW）三轴独立控制——
区别于项目其它 GIF 合成节点的「单一质量」概念；另有时域动效（bounce）、
循环次数、固定色/合成底色、尺寸限制（官方：对体积影响最大）。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..core.paths import app_root_dir

# 单次 gifski 调用超时（秒）：每帧 pngquant 量化 + 时域优化较慢，给足余量；
# 超时以清晰中文报错抛出（子进程被杀，不会留下半成品——输出走临时文件）。
GIFSKI_TIMEOUT_S = 600

# Windows：无控制台的 GUI 父进程（Nuitka attach 双击启动）每次拉起控制台
# 子系统子进程（gifski.exe）时，Windows 都会为子进程新建一个控制台窗口
# ——屏幕上闪黑框。CREATE_NO_WINDOW 禁止该窗口（仅 Windows 有该标志，
# getattr 兜底使代码可跨平台导入）。子进程输出本就走 capture_output 管道，
# 不需要控制台。
CREATE_NO_WINDOW_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 速度-质量档位键 → gifski 参数；"normal" 不传（默认）。
FAST_MODES = ("normal", "fast", "extra")


def _runtime_dir() -> Path:
    """gifski 运行时根目录（**唯一来源**）：随包 runtime/gifski/。

    以 app_root_dir() 为基准：打包后（Nuitka/PyInstaller）= exe 旁
    runtime/gifski/；开发态指向 src/gif_node_studio/runtime/gifski/。
    """
    return app_root_dir() / "runtime" / "gifski"


@dataclass(frozen=True)
class GifskiRuntime:
    """gifski 运行时探测结果。

    - ``exe`` —— 找到的 gifski 可执行文件路径；None 表示未找到；
    - ``available`` —— 是否可用（exe 存在且能响应 ``--version``）；
    - ``version`` —— 解析出的版本号（如 "1.34.0"）；
    - ``version_line`` —— ``--version`` 输出的首行（如 "gifski 1.34.0"）。
    """

    exe: Path | None
    available: bool
    version: str | None
    version_line: str | None


def _probe_version(exe: Path) -> tuple[str | None, str | None]:
    """运行 ``gifski --version``，解析 (版本号, 首行)；失败返回 (None, None)。

    ``stdin=subprocess.DEVNULL``：Nuitka 产物以 ``--windows-console-mode=attach``
    双击启动（无控制台可附加）时标准句柄为无效值，subprocess 在
    ``_get_handles`` 中处理 stdin 继承会抛 ``OSError: [WinError 6] 句柄无效``
    （见 Nuitka issue #3030）；gifski 不读 stdin，显式 DEVNULL 彻底绕开
    无效句柄（探测与编码两处调用一致）。
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
        match = re.search(r"gifski\s+(\d+\.\d+(?:\.\d+)?)", first, re.IGNORECASE)
        if match:
            version = match.group(1)
    return version, first


def configure_gifski(candidates: Iterable[Path] | None = None) -> GifskiRuntime:
    """定位 gifski 并探测版本；找不到时返回 available=False（不抛错）。

    运行时来源唯一：``runtime/gifski/``（显式传入 candidates 供测试覆盖）。
    幂等：进程内只探测一次（与 ``configure_gifsicle`` 同模式）。
    调用方（backend）在真正需要时给出明确报错。
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
        for name in ("gifski.exe", "gifski"):
            candidate = directory / name
            if candidate.is_file():
                exe = candidate
                break
        if exe is not None:
            break
    if exe is None:
        _RUNTIME_CACHE = GifskiRuntime(None, False, None, None)
        return _RUNTIME_CACHE
    version, version_line = _probe_version(exe)
    _RUNTIME_CACHE = GifskiRuntime(exe, True, version, version_line)
    return _RUNTIME_CACHE


def require_gifski(runtime: GifskiRuntime, operation: str) -> None:
    """gifski 编码前的可用性守卫，报错信息包含排查方向。"""
    if not runtime.available or runtime.exe is None:
        raise RuntimeError(
            f"{operation}需要 gifski 命令行工具（当前不可用）。"
            "请确认随包运行时 runtime/gifski/gifski.exe 存在（scripts/prepare_gifski_runtime.py）。"
        )


def _color_arg(value: str | None) -> str | None:
    """ColorParam 值（'#rrggbb'）→ gifski RGBHEX（'rrggbb'，去 #）；None/空返回 None。"""
    if not value:
        return None
    return value.lstrip("#")


def _frame_args(frames: Iterable[str | Path]) -> tuple[list[str], bool]:
    """帧输入参数构造：同目录且字典序 = 帧序 → 单个 glob（决策 #125）。

    旧实现把每帧绝对路径逐个传给 gifski：1024 帧 × ~120 字符 ≈ 122 KiB，
    超 Windows CreateProcess 32 KiB 命令行上限 → 报「文件名或扩展名太长」
    （ERROR_FILENAME_EXCED_RANGE）。项目序列物化命名统一零填充
    ``frame_{index:06d}.png``（backend_sequence / backend_format 全部路径），
    同目录下字典序 = 帧序——此时传单个 glob（把首帧名中的数字段替换为
    ``*``，如 ``frame_000000.png`` → ``frame_*.png``）由 gifski 内部展开并
    按默认排序（不传 ``--no-sort``），命令行长度恒定；跨目录、帧序非字典序
    或帧名无数字段时回退显式列表 + ``--no-sort``（此类帧数少，不构成超限
    风险）。

    返回 ``(参数列表, 是否需要 --no-sort)``。
    """
    paths = [Path(frame) for frame in frames]
    parents = {path.parent for path in paths}
    if len(parents) == 1 and sorted(paths) == paths:
        first = paths[0].name
        stem, _, suffix = first.rpartition(".")
        if re.search(r"\d", stem):
            star_stem = re.sub(r"\d+", "*", stem)
            pattern = f"{star_stem}.{suffix}" if suffix else star_stem
            return [str(parents.pop() / pattern)], False
    return [str(path) for path in paths], True


def build_gifski_args(
    frames: Iterable[str | Path],
    output_path: str | Path,
    *,
    fps: float = 12.0,
    quality: int = 90,
    motion_quality: int = 90,
    lossy_quality: int = 90,
    width: int = 0,
    height: int = 0,
    fast_mode: str = "normal",
    repeat: int = 0,
    bounce: bool = False,
    fixed_color: str | None = None,
    matte: str | None = None,
) -> list[str]:
    """构造 gifski 命令行参数（纯函数，便于测试参数顺序与非法值校验）。

    节点参数映射 gifski CLI 选项 1:1，机器键在此集中校验（非法键构造即报错，
    不等到子进程）。gifski 设计特性（决策 #124）：

    - ``quality`` 1–100（-Q）：量化质量（pngquant）；``motion_quality``
      1–100（--motion-quality）：时域一致性/跨帧颜色重用；``lossy_quality``
      1–100（--lossy-quality）：有损 LZW 压缩——三轴独立，与 gifski CLI
      语义一致（默认均为 quality 值）；
    - ``width``/``height``（-W/-H）：>0 时传（输出尺寸限制，gifski 官方：
      对体积影响最大）；0 = 不传（用 gifski 默认 ~800×600 限制）；
    - ``fast_mode``：normal/fast/extra → 不传 / ``--fast``（快 50% 质量
      略降）/ ``--extra``（慢 50% 质量略升）；
    - ``repeat``：-1 = 不循环、0 = 无限、N = 重复 N 次（``--repeat``）；
    - ``bounce``：``--bounce`` 正放+倒放（gifski 时域动效）；
    - ``fixed_color``/``matte``：'#rrggbb' 或 None → ``--fixed-color``/
      ``--matte``（去 # 的 RGBHEX）。

    返回参数列表（不含可执行文件本身）；帧输入（glob 或显式列表）与
    ``--output`` 放在末尾。帧输入构造见 ``_frame_args``（决策 #125）：
    同目录且字典序 = 帧序时传单个 glob（命令行恒定短，规避 Windows
    CreateProcess 32 KiB 命令行上限的「文件名或扩展名太长」），否则显式
    列表 + ``--no-sort`` 保持顺序。
    """
    if not 1 <= quality <= 100:
        raise ValueError(f"quality 必须在 1–100 之间（当前 {quality}）")
    if not 1 <= motion_quality <= 100:
        raise ValueError(f"motion_quality 必须在 1–100 之间（当前 {motion_quality}）")
    if not 1 <= lossy_quality <= 100:
        raise ValueError(f"lossy_quality 必须在 1–100 之间（当前 {lossy_quality}）")
    if fast_mode not in FAST_MODES:
        raise ValueError(f"未知速度档键：{fast_mode!r}（可选：{FAST_MODES}）")
    frame_args, needs_no_sort = _frame_args(frames)
    args: list[str] = ["--quiet"]
    if needs_no_sort:
        args.append("--no-sort")
    args.extend([
        f"--fps={fps:g}",
        f"-Q{quality}",
        f"--motion-quality={motion_quality}",
        f"--lossy-quality={lossy_quality}",
    ])
    if width > 0:
        args.append(f"--width={width}")
    if height > 0:
        args.append(f"--height={height}")
    if fast_mode == "fast":
        args.append("--fast")
    elif fast_mode == "extra":
        args.append("--extra")
    args.append(f"--repeat={repeat}")
    if bounce:
        args.append("--bounce")
    fixed = _color_arg(fixed_color)
    if fixed is not None:
        args.append(f"--fixed-color={fixed}")
    matte_value = _color_arg(matte)
    if matte_value is not None:
        args.append(f"--matte={matte_value}")
    args.extend(frame_args)
    args.extend(["--output", str(output_path)])
    return args


_RUNTIME_CACHE: GifskiRuntime | None = None
