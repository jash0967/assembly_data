"""Bootstrap sys.path so rag_assembly/* scripts can `import config`,
`import bill_loaders`, `import prompts` from repo root.

Usage: place `import _bootstrap  # noqa: F401` as the first non-docstring
import in any script under rag_assembly/.
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent

# repo root (where config.py lives)
_root = _here
while _root.parent != _root and not (_root / "config.py").exists():
    _root = _root.parent
if (_root / "config.py").exists() and str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
