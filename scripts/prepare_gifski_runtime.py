"""把 gifski 运行时解压到应用 runtime 目录（决策 #124）。

gifski 官方主分发即 CLI（Windows zip，https://gif.ski/gifski-<ver>.zip，
AGPL-3.0-or-later——LICENSE 随包以满足源码可获取义务）。本脚本从 zip
解压 `win/gifski.exe` + `LICENSE` 到 `src/gif_node_studio/runtime/gifski/`。

用法：
    python scripts/prepare_gifski_runtime.py            # 用环境变量 GIFSKI_ZIP
    GIFSKI_ZIP=path/to/gifski-1.34.0.zip python scripts/prepare_gifski_runtime.py

（与 prepare_gifsicle_runtime.py 同模式；gifsicle 是手动编译目录复制，
gifski 官方直接给 zip，故本脚本为 zip 解压。）
"""
import os
import shutil
import sys
import zipfile

from pathlib import Path

# 官方 zip 路径：环境变量覆盖；默认找工作目录/Downloads 下的 gifski-*.zip
GIFSKI_ZIP = os.environ.get("GIFSKI_ZIP") or ""

#: 目标目录：开发态 app_root_dir() 所指（Nuitka 打包也从这里携带）
DEST_DIR = (Path(__file__).resolve().parents[1] / "src" / "gif_node_studio"
            / "runtime" / "gifski")

# 目标文件列表（zip 内相对路径 → DEST 文件名）
WHITELIST = {
    "win/gifski.exe": "gifski.exe",
    "LICENSE": "LICENSE",
}


def _find_zip() -> Path:
    if GIFSKI_ZIP:
        path = Path(GIFSKI_ZIP)
        if path.is_file():
            return path
        raise SystemExit(f"[error] GIFSKI_ZIP 不是有效文件: {path}")
    # 默认候选：当前目录与用户下载目录的 gifski-*.zip
    candidates = [Path.cwd()]
    home_downloads = Path.home() / "Downloads"
    if home_downloads.is_dir():
        candidates.append(home_downloads)
    for directory in candidates:
        matches = sorted(directory.glob("gifski-*.zip"))
        if matches:
            return matches[-1]
    raise SystemExit(
        "[error] 未找到 gifski-*.zip。请从 https://gif.ski/ 下载官方 zip 后\n"
        "        设环境变量 GIFSKI_ZIP 指向它再运行本脚本。"
    )


def main() -> None:
    zip_path = _find_zip()
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        missing = [name for name in WHITELIST if name not in names]
        if missing:
            raise SystemExit(f"[error] zip 缺少所需文件: {missing}（{zip_path}）")
        if DEST_DIR.exists():
            shutil.rmtree(DEST_DIR)
        DEST_DIR.mkdir(parents=True)
        for source, dest_name in WHITELIST.items():
            with archive.open(source) as src, (DEST_DIR / dest_name).open("wb") as dst:
                shutil.copyfileobj(src, dst)
    print(f"[ok] 已解压到 {DEST_DIR}（来源 {zip_path.name}）")


if __name__ == "__main__":
    sys.exit(main())
