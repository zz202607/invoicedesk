"""Make the repository root importable for tests.

The legacy embedded interpreter used for local checks has an isolated
``python311._pth`` and ignores PYTHONPATH, so make the root explicit.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
