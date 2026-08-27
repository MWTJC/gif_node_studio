"""Nuitka standalone build shim for PyAV.

PyAV's Python modules are compiled Cython extensions (av/*.pyd).  The compiled
extensions import their cimport partner modules at RUNTIME (e.g.
av/stream.pyd executes ``import av.utils`` to resolve cimported symbols).
Those modules have NO static importer anywhere in the source graph, so
Nuitka's standalone import analysis (a closed world: only statically seen
modules are registered) never includes them, and the frozen exe dies with::

    ModuleNotFoundError: No module named 'av.utils'

The imports below make every one of those runtime-only modules statically
visible to Nuitka, so it registers and bundles them (as extension modules,
copied from the venv).  The set was computed by diffing the runtime
``sys.modules`` after ``import av`` against the package's static import
statements (see ``nuitka-qt-repro/av_dynamic.py``).

Notes:
- ``--include-package=av`` is NOT usable as an alternative: it force-includes
  modules that are already statically imported (e.g. av.codec.context) and
  crashes Nuitka 4.1.3 with ``AssertionError: av.codec.context``
  (ModuleNodes.py avoid_duplicates).
- If a future PyAV version adds more runtime-cimport modules, re-run the
  diff and extend this list.
"""
# flake8: noqa: F401  (imported for its side effect on the Nuitka module set)
import av.audio  # noqa: F401
import av.audio.plane  # noqa: F401
import av.buffer  # noqa: F401
import av.dictionary  # noqa: F401
import av.filter  # noqa: F401
import av.filter.context  # noqa: F401
import av.filter.link  # noqa: F401
import av.frame  # noqa: F401
import av.index  # noqa: F401
import av.opaque  # noqa: F401
import av.plane  # noqa: F401
import av.sidedata  # noqa: F401
import av.stream  # noqa: F401
import av.utils  # noqa: F401
import av.video  # noqa: F401
import av.video.plane  # noqa: F401
import av.video.reformatter  # noqa: F401
