"""Move a legacy ``InvoiceDeskData`` folder's pool/backups/logs back home.

    python tools/split_data_dir.py --source "E:\\账\\2026年10月\\发票\\InvoiceDeskData" --dry-run
    python tools/split_data_dir.py --source "E:\\账\\2026年10月\\发票\\InvoiceDeskData"

Older builds kept the pool, backups, exports, and logs beside the invoices.
Newer builds keep the pool in the app home and only write generated files to
``报销发票整理`` next to the invoices. This tool copies the pool (verified through
SQLite's backup API), backups, and logs into ``%LOCALAPPDATA%\\InvoiceDesk``,
copies the old ``out`` files into ``报销发票整理``, and updates ``config.json``.
The app must be closed. The old folder is never deleted here: deleting it is a
separate decision for the user.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import datamove


def default_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return Path.home() / ".invoicedesk"
    return Path(base) / "InvoiceDesk"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="legacy data folder to empty")
    parser.add_argument(
        "--home",
        default=str(default_home()),
        help="app home that receives the pool (default: %%LOCALAPPDATA%%\\InvoiceDesk)",
    )
    parser.add_argument(
        "--export",
        default=None,
        help="export folder (default: <invoice folder>\\报销发票整理)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="settings file to update (default: <home>\\config.json)",
    )
    parser.add_argument("--dry-run", action="store_true", help="read and verify only")
    args = parser.parse_args()
    try:
        result = datamove.split_data_dir(
            args.source,
            args.home,
            export_dir=args.export,
            config_file=args.config,
            dry_run=args.dry_run,
        )
    except datamove.DataMoveError as exc:
        print(f"error: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    if not args.dry_run:
        print("old folder kept: verify the app, then delete it yourself")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
