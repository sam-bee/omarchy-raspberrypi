#!/usr/bin/python3
"""Import compatibility wrapper for the hyphenated updater helper."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_path = Path(__file__).with_name("update-lib.py")
_spec = spec_from_file_location("omarchy_pi_update_lib_impl", _path)
if _spec is None or _spec.loader is None:
    raise ImportError(f"cannot load {_path}")
_module = module_from_spec(_spec)
_spec.loader.exec_module(_module)
for _name in dir(_module):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_module, _name)
