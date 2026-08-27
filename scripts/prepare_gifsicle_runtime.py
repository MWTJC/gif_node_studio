"""把 gifsicle 运行时复制到应用 runtime 目录。
自行编译
用法：改顶部 GS_DIR（或设环境变量 GIFSICLE_SOURCE_DIR）后
     python scripts/prepare_gifsicle_runtime.py
"""
import os
import shutil

from pathlib import Path

# 手动指定的ImageMagick文件夹路径
GS_DIR = r""

#: 本机 ImageMagick 安装根目录（或 portable 解压目录）；环境变量可覆盖
SOURCE_DIR = os.environ.get("GIFSICLE_SOURCE_DIR") or GS_DIR

#: 目标目录：开发态 app_root_dir() 所指（Nuitka 打包也从这里携带）
DEST_DIR = (Path(__file__).resolve().parents[1] / "src" / "gif_node_studio"
            / "runtime" / "gifsicle")

# 目标文件列表
WHITELIST = [
    "gifsicle.exe",
    "README.md",
]
def main() -> None:
    src = Path(SOURCE_DIR)
    if not (src / "src" / "gifsicle.exe").is_file():
        raise SystemExit(f"[error] SOURCE_DIR 不是 gifsicle 运行时根目录: {src}")
    if DEST_DIR.exists():
        shutil.rmtree(DEST_DIR)
    DEST_DIR.mkdir(parents=True)
    for paths in src.rglob("*"):
        if paths.name in WHITELIST:
            shutil.copy2(paths, DEST_DIR / paths.name)
    print(f"[ok] 已复制到 {DEST_DIR}")


if __name__ == "__main__":
    main()
