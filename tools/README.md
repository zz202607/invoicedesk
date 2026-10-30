# Tools

## ai_reviewer.py

Multi-agent PR review powered by DeepSeek, adapted from
`English-Lens/tools/ai_reviewer.py` (same owner, reused with project context and
skip paths changed).

- Triggered only by `.github/workflows/ai-review.yml` on pull requests.
- Reviews the PR diff through the GitHub API; it never reads invoice data.
- Required repository secret: `DEEPSEEK_API_KEY`.
- Optional repository variables: `DEEPSEEK_MODEL`, `REVIEW_AGENTS`.
- Local smoke test: `python tools/ai_reviewer.py --diff-file changes.diff --dry-run`.

The reviewer checks out the PR's **base** commit, so a pull request cannot modify
the reviewer that runs with the secret. Fork PRs are skipped.

## build_app.py

Builds `dist/InvoiceDesk` with PyInstaller (`InvoiceDesk.spec`), then runs the
frozen EXE with `--smoke` in a temporary working directory:

```bash
python -m pip install -r requirements-build.txt
python tools/build_app.py            # build + frozen smoke
python tools/build_app.py --skip-smoke
python tools/build_app.py --skip-build   # re-smoke the existing dist/
```

## migrate_data_dir.py

Copies a packaged app home (database, backups, exports, logs) to a new data
folder, verifies the copied database, and rewrites `config.json` so the next
start uses it. Close the app first; the old copy is left in place.

```bash
python tools/migrate_data_dir.py --dry-run --target "E:\\账\\2026年10月\\发票\\InvoiceDeskData"
python tools/migrate_data_dir.py --target "E:\\账\\2026年10月\\发票\\InvoiceDeskData"
```

## make_version.py

Writes `build/version_info.txt` (a PyInstaller `VSVersionInfo` literal) from the
version in `pyproject.toml`, so the packaged EXE's Properties page shows the
same release as the app. `build_app.py` calls it before every build.

```bash
python tools/make_version.py            # build/version_info.txt
python tools/make_version.py --out X    # write somewhere else
```

## make_icon.py

Draws the multi-size `build/icon.ico` with the standard library only; no Pillow.
`--preview build/icon.png` also writes a 256 px PNG for README/screenshots.
