# Legacy scripts (archived)

These are the original one-off scripts from before the InvoiceDesk redesign.
They are kept for traceability only and are not maintained.

- `invoice_stats.py` / `发票统计.py` — PDF scan, amount/date extraction, Excel + HTML output
- `parse-invoices-pymupdf.py` / `parse-invoices.js` — earlier parsing experiments
- `merge_pdfs.py` — A4 two-up PDF merge by category
- `launcher.py` / `启动统计.py` / `pick_folder.vbs` / `选择文件夹.vbs` — folder picker + launcher chain
- `发票统计.bat` / `安装环境.bat` / `快速安装.bat` — Windows launchers
- `生成Excel.ps1` / `发票统计完整版.ps1` / `选择文件夹.ps1` — PowerShell prototypes

Known issues are documented in `docs/DECISIONS.md` (ADR-004) and the Phase 0 journal
entry. Do not build on these files; new code lives in `app/`.

Hard-coded personal paths in the original files were replaced with placeholders
when archiving.
