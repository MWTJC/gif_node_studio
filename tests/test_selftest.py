"""自检冒烟：程序加 --test 启动，全部初始化完毕后自动退出并返回 pass。

按最初设想，测试代码只验证「程序可运行」；各功能与 UI 的细节设计由
docs/ 文档与源码 docstring/注释承载（见 docs/testing.md「验证方式」）。

--test 与生产启动走完全相同的初始化路径（日志、卡死诊断、QApplication、主题、
MainWindow 全构造：节点注册表、全部 UI、设置恢复、ImageMagick 探测），
初始化完毕后打印 SMOKE_OK 并以 0 退出；任何异常 → 非 0 退出码。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def _run_selftest() -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # 显式覆盖 PYTHONPATH（避开 Hermes 注入的 hermes-agent venv）与离屏平台。
    env["PYTHONPATH"] = str(PROJECT / "src")
    env["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run(
        [sys.executable, "-m", "gif_node_studio", "--test"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(PROJECT),
    )


def test_selftest_initializes_and_exits_pass() -> None:
    proc = _run_selftest()
    assert proc.returncode == 0, (
        f"自检失败（退出码 {proc.returncode}）：\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "SMOKE_OK" in proc.stdout, f"缺少 SMOKE_OK 标记：{proc.stdout!r}"
