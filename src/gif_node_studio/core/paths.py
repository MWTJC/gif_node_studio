from __future__ import annotations

import sys
from pathlib import Path

from PySide6 import QtCore


def _is_compiled() -> bool:
    """当前是否运行于打包/编译产物（exe 环境）。

    - PyInstaller：设置 ``sys.frozen``；
    - Nuitka：**不设置** ``sys.frozen``（官方文档明确说明，见
      https://nuitka.net/user-documentation/tips.html#detecting-nuitka-compilation-at-runtime ），
      而是向每个编译模块注入 ``__compiled__`` 模块属性。

    两种打包器的探测都要做，否则 Nuitka 产物会误入开发分支。
    """
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def app_root_dir() -> Path:
    """软件根目录：只读程序文件（可执行文件、runtime/、data/）的基准目录。

    - 打包（Nuitka standalone / PyInstaller）后为**可执行文件所在目录**；
    - 开发时为**包目录**（``src/gif_node_studio``，即本模块所在目录
      的上两级，非项目根）。

    只读资源（``runtime/imagemagick/``、``runtime/gifsicle/``、``data/``）
    位于该目录之下；**可写数据（logs / cache / settings.ini）不在此处**，
    统一走 :func:`user_data_dir`——打包后 Program Files 对非管理员只读，
    数据若写在 exe 旁会导致普通用户启动即崩溃（见关键决策 #84）。
    """
    if _is_compiled():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# 组织/应用名（QStandardPaths 用户数据目录的路径组成部分；app.py 的
# setOrganizationName / setApplicationName 从这里引用，保持单一源头）。
ORG_NAME = "mwtjc"
APP_NAME = "GIF Node Studio"


def user_data_dir() -> Path:
    """用户数据目录：可写数据（logs/、cache/、settings.ini）的基准目录。

    - 打包态：``%LOCALAPPDATA%\\Ghooost\\GIF Node Studio``，用 Qt
      ``QStandardPaths`` 定位（Windows 上 = SHGetKnownFolderPath(
      FOLDERID_LocalAppData)，不读环境变量）；安装到 ``C:\\Program Files``
      后普通用户同样可写（见关键决策 #84）；
    - 开发态：与 :func:`app_root_dir` 一致（包目录），保持开发时数据就近、
      不污染 AppData。

    QStandardPaths 在 Windows 上会把 organizationName / applicationName 拼进
    AppLocalDataLocation 返回路径；名称未设置时退化为进程名（python.exe 等），
    故先**无条件**锁定与 app.py 一致的组织/应用名再查询——静态 setter 不会
    创建 QCoreApplication 实例，导入期调用安全；app.py 之后设置相同值，幂等。
    """
    if not _is_compiled():
        return app_root_dir()
    QtCore.QCoreApplication.setOrganizationName(ORG_NAME)
    QtCore.QCoreApplication.setApplicationName(APP_NAME)
    base = QtCore.QStandardPaths.writableLocation(
        QtCore.QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base)


def node_presets_dir() -> Path:
    """项目预设目录： ``node_presets/``（存放 ``*.json`` 节点方案预设）。
    对程序而言，虽然可由用户增删，但从定义而言，这算程序附带资源文件，程序仅从其中读取预设，不需要可写
    """
    return app_root_dir() / "node_presets"

def im_data_dir() -> Path:
    """im的自定义图样与固定调色板位置
    """
    return app_root_dir() / "data"
