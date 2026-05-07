"""Streamlit app loader - bypasses corrupted source, uses preserved bytecode."""
import os, marshal, sys

# Monkey-patch pandas to fix pd.NA -> float(nan) in Series.replace calls
import pandas as pd
_orig_replace = pd.Series.replace
def _patched_replace(self, to_replace=None, value=None, *args, **kwargs):
    if value is pd.NA:
        value = float("nan")
    return _orig_replace(self, to_replace, value, *args, **kwargs)
pd.Series.replace = _patched_replace

# Load and execute preserved bytecode
_pyc = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__pycache__", "streamlit_app_original.cpython-311.pyc")
with open(_pyc, "rb") as _f:
    _f.read(16)  # skip 16-byte header
    _code = marshal.loads(_f.read())

_globals = {"__name__": "__main__", "__file__": _pyc, "__builtins__": __builtins__}
exec(_code, _globals)
