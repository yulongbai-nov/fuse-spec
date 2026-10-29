"""Regression coverage for the spec traceability checker."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "skills" / "scripts" / "sync_check.py"


def run_checker(spec_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(spec_dir)],
        capture_output=True,
        check=False,
        text=True,
    )


def write_valid_spec(
    spec_dir: Path, *, requirements: str = "### R1 Requirement\n"
) -> None:
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "requirements.md").write_text(requirements, encoding="utf-8")
    (spec_dir / "design.md").write_text(
        "### D-Model Model\nsatisfies: R1\n", encoding="utf-8"
    )
    (spec_dir / "tasks.md").write_text(
        "- [ ] T1 Implement [id:T1 deps: impl:R1,D-Model]\n",
        encoding="utf-8",
    )


def test_missing_spec_inputs_fail_clearly(tmp_path: Path) -> None:
    result = run_checker(tmp_path)

    assert result.returncode == 1
    assert "missing requirements input" in result.stdout.lower()


def test_empty_requirements_input_fails_clearly(tmp_path: Path) -> None:
    write_valid_spec(tmp_path, requirements="  \n")

    result = run_checker(tmp_path)

    assert result.returncode == 1
    assert "requirements input is empty" in result.stdout.lower()


def test_requirements_without_ids_fail_clearly(tmp_path: Path) -> None:
    write_valid_spec(tmp_path, requirements="# Requirements\n\nPurpose only.\n")

    result = run_checker(tmp_path)

    assert result.returncode == 1
    assert "no requirement ids" in result.stdout.lower()


def test_duplicate_task_ids_fail_clearly(tmp_path: Path) -> None:
    write_valid_spec(tmp_path)
    (tmp_path / "tasks.md").write_text(
        """\
- [ ] T1 First [id:T1 deps: impl:R1,D-Model]
- [ ] T1 Second [id:T1 deps: impl:R1,D-Model]
""",
        encoding="utf-8",
    )

    result = run_checker(tmp_path)

    assert result.returncode == 1
    assert "duplicate task id t1" in result.stdout.lower()


def test_valid_spec_passes(tmp_path: Path) -> None:
    write_valid_spec(tmp_path)

    result = run_checker(tmp_path)

    assert result.returncode == 0
    assert "OK: traceability is consistent" in result.stdout
