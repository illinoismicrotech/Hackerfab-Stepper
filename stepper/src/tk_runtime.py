"""Use the optional font-enabled Tk build only inside this project/process."""
import importlib.util
import os
from pathlib import Path
import sys


def enable_font_support():
    if sys.platform != 'linux' or '_tkinter' in sys.modules:
        return False
    prefix = Path(__file__).resolve().parents[1] / '.runtime' / 'tk-9.0.4'
    tk_library = prefix / 'lib' / 'libtcl9tk9.0.so'
    if not tk_library.exists():
        return False
    spec = importlib.util.find_spec('_tkinter')
    # Never load a Tcl 9 runtime into an extension built for Tcl 8.
    if spec is None or not spec.origin or b'libtcl9tk9.0.so' not in Path(spec.origin).read_bytes():
        return False
    import ctypes
    ctypes.CDLL(str(prefix / 'lib' / 'libtcl9.0.so'), mode=ctypes.RTLD_GLOBAL)
    ctypes.CDLL(str(tk_library), mode=ctypes.RTLD_GLOBAL)
    os.environ['TCL_LIBRARY'] = str(prefix / 'lib' / 'tcl9.0')
    os.environ['TK_LIBRARY'] = str(prefix / 'lib' / 'tk9.0')
    return True
