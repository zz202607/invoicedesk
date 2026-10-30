"""Copy the packaged app home to a new data folder and repoint config.json.

    python tools/migrate_data_dir.py --target "E:\\账\\2026年10月\\发票\\InvoiceDeskData"
    python tools/migrate_data_dir.py --source-home "C:\\Users\\me\\AppData\\Local\\InvoiceDesk" \
        --target "E:\\账\\2026年10月\\发票\\InvoiceDeskData" --dry-run

The app must be closed. The source home is copied and verified; it is never
deleted here. Deleting the old copy is a separate decision for the user.
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

from app.core import appconfig, datamove


def default_source_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        return Path.home() / ".invoicedesk"
    return Path(base) / "InvoiceDesk"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="new data folder")
    parser.add_argument(
        "--source-home",
        default=str(default_source_home()),
        help="packaged app home (default: %%LOCALAPPDATA%%\\InvoiceDesk)",
    )
    parser.add_argument("--dry-run", action="store_true", help="read and verify only")
    args = parser.parse_args()

    source_db = Path(args.source_home).expanduser() / appconfig.DB_FILENAME
    try:
        summary = datamove.summarize(source_db)
    except datamove.DataMoveError as exc:
        print(f"error: {exc}")
        return 1
    print("source: " + json.dumps(
        {"path": str(source_db), "summary": summary.__dict__}, ensure_ascii=True
    ))
    if args.dry_run:
        print("dry run: nothing copied")
        return 0
    try:
        result = datamove.copy_data_dir(args.source_home, args.target)
    except datamove.DataMoveError as exc:
        print(f"error: {exc}")
        return 1
    print("moved: " + json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
