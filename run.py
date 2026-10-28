"""Launch the desktop workbench from a source checkout.

Works with the embedded interpreter in ``python_env`` (whose ``_pth`` file
hides the working directory) as well as with a normal virtualenv:

    python run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import main  # noqa: E402 - path bootstrap must run first

if __name__ == "__main__":
    raise SystemExit(main())
