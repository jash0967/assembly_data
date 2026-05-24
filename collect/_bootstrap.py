"""Bootstrap sys.path so collect/* scripts can `import config` (at repo root)
and `from age_utils import ...` (collect/ siblings) regardless of where the
script lives — collect/, collect/migrations/, or collect/_legacy/.

Usage: place `import _bootstrap  # noqa: F401` as the first non-docstring
import in any script under collect/.
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent

# 1) repo root (where config.py lives)
_root = _here
while _root.parent != _root and not (_root / "config.py").exists():
    _root = _root.parent
if (_root / "config.py").exists() and str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# 2) collect/ directory (siblings: age_utils, api_client, progress, etc.)
_collect = _here
while _collect.parent != _collect and _collect.name != "collect":
    _collect = _collect.parent
if _collect.name == "collect" and str(_collect) not in sys.path:
    sys.path.insert(0, str(_collect))
