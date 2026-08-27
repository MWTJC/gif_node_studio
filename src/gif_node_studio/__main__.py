# nuitka-project: --mode=standalone
# nuitka-project: --windows-console-mode=attach
# nuitka-project: --output-dir=dist
# nuitka-project: --enable-plugin=pyside6,upx
# nuitka-project: --include-module=PySide6.QtSvg
# nuitka-project: --include-data-dir={MAIN_DIRECTORY}/data/=./data/
# nuitka-project: --include-data-dir={MAIN_DIRECTORY}/node_presets/=./node_presets/
# nuitka-project: --include-raw-dir={MAIN_DIRECTORY}/runtime/=./runtime/
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/img_resource_rc.py=./
# nuitka-project: --windows-icon-from-ico={MAIN_DIRECTORY}/../../build_src/ico/app_icon.ico

from gif_node_studio import main

# Nuitka: register PyAV's runtime-cimport modules (see _nuitka_av_shim.py).
from gif_node_studio import _nuitka_av_shim  # noqa: F401

raise SystemExit(main())
