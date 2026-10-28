"""Guard the documentation budget and the no-financial-data rule."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_DOCS = [
    "PROGRESS.md",
    "ROADMAP.md",
    "CLAUDE.md",
    "README.md",
    "docs/README.md",
    "docs/ARCHITECTURE.md",
    "docs/PARSER.md",
    "docs/DECISIONS.md",
    "docs/METRICS.md",
    "docs/IDEAS.md",
    "docs/journal/2026-10.md",
]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "python_env",
    "legacy",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
}




def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _should_skip(relative: Path) -> bool:
    if relative.parts[:2] == ("tests", "local") or relative.parts[:1] == ("out",):
        return True
    if relative.suffix == ".json" and len(relative.parts) == 1 and (
        relative.name == "out.json"
        or relative.name.startswith("parsed")
        or relative.name.startswith("_test_")
    ):
        return True
    return any(part in SKIP_DIRS for part in relative.parts)


def test_required_docs_exist() -> None:
    missing = [name for name in REQUIRED_DOCS if not (ROOT / name).is_file()]
    assert not missing, f"missing docs: {missing}"


def test_hot_docs_stay_within_budget() -> None:
    assert _line_count(ROOT / "PROGRESS.md") <= 120
    assert _line_count(ROOT / "CLAUDE.md") <= 150


def test_single_docs_stay_within_budget() -> None:
    for path in (ROOT / "docs").glob("*.md"):
        assert _line_count(path) <= 300, f"{path.name} exceeds 300 lines"


def test_decisions_are_numbered() -> None:
    text = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    for number in range(1, 8):
        assert f"## ADR-{number:03d}" in text


def test_roadmap_covers_all_phases() -> None:
    text = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    for phase in range(5):
        assert f"Phase {phase}" in text


def test_skip_is_limited_to_tests_local() -> None:
    assert _should_skip(Path("tests/local/truth.json")) is True
    assert _should_skip(Path("out/parsed.json")) is True
    assert _should_skip(Path("parsed-run.json")) is True
    assert _should_skip(Path("_test_force.json")) is True
    assert _should_skip(Path("docs/parsed_rules.json")) is False
    assert _should_skip(Path("docs/local/note.md")) is False
    assert _should_skip(Path("legacy/old.py")) is True


def _git_ignores(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def test_financial_data_ignore_rules_are_narrow() -> None:
    if shutil.which("git") is None:  # pragma: no cover - CI and dev machines have git
        return
    assert _git_ignores("data/invoicedesk.db") is True
    assert _git_ignores("data/invoicedesk.db-wal") is True
    assert _git_ignores("tests/fixtures/sample.db") is True
    assert _git_ignores("out/parsed.json") is True
    assert _git_ignores("docs/notes.md") is False
