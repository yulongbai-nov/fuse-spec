"""Regression coverage for the legacy dependency-wave planner."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).parents[1] / "skills" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import plan_waves  # noqa: E402

SCRIPT = Path(__file__).parents[1] / "skills" / "scripts" / "plan_waves.py"


def run_planner(
    tmp_path: Path, text: str, *args: str
) -> subprocess.CompletedProcess[str]:
    tasks_file = tmp_path / "tasks.md"
    tasks_file.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(tasks_file), *args],
        capture_output=True,
        check=False,
        text=True,
    )


def test_checked_tasks_seed_ordinary_resume(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [x] T1 Complete [id:T1 deps: impl:R1]
- [ ] T2 Dependent [id:T2 deps:T1 impl:R1]
""",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["waves"] == [["T2"]]


def test_include_done_replays_checked_tasks(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [x] T1 Complete [id:T1 deps: impl:R1]
- [ ] T2 Dependent [id:T2 deps:T1 impl:R1]
""",
        "--include-done",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["waves"] == [["T1"], ["T2"]]


def test_duplicate_task_ids_fail_clearly(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [ ] T1 First [id:T1 deps:T99 impl:R1]
- [ ] T1 Second [id:T1 deps: impl:R1]
""",
    )

    assert result.returncode == 1
    assert "duplicate task id t1" in result.stderr.lower()


def test_unknown_dependency_fails_clearly(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        "- [ ] T1 Dependent [id:T1 deps:T99 impl:R1]\n",
    )

    assert result.returncode == 1
    assert "depends on unknown task T99" in result.stderr


def test_dependency_cycle_fails_clearly(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [ ] T1 First [id:T1 deps:T2 impl:R1]
- [ ] T2 Second [id:T2 deps:T1 impl:R1]
""",
    )

    assert result.returncode == 1
    assert "dependency cycle" in result.stderr


def test_overlapping_write_scopes_are_serialized(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [ ] T1 First [id:T1 deps: impl:R1]
    - **Files:** `src/shared.ts`
- [ ] T2 Second [id:T2 deps: impl:R1]
    - **Files:** `src/shared.ts`
""",
    )

    assert result.returncode == 0, result.stderr
    waves = json.loads(result.stdout)["waves"]
    assert waves == [["T1"], ["T2"]]


def test_public_task_parser_reuses_markdown_files_syntax() -> None:
    parsed, duplicates, done = plan_waves.parse_task_declarations(
        """\
- [ ] T7 Example [id:T7 deps:T5 impl:R1]
    - **Files:** `tests/test_pr_provider.py`, src/other.py
- [ ] T8 Example [id:T8 deps:T5 impl:R1]
    - **Files:** `tests/`
"""
    )

    assert parsed == {
        "T7": {
            "deps": ["T5"],
            "impl": ["R1"],
            "writes": ["tests/test_pr_provider.py", "src/other.py"],
            "scope_invalid": False,
        },
        "T8": {
            "deps": ["T5"],
            "impl": ["R1"],
            "writes": ["tests"],
            "scope_invalid": False,
        },
    }
    assert duplicates == set()
    assert done == set()


def test_disjoint_write_scopes_remain_parallel(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [ ] T1 First [id:T1 deps: impl:R1]
    - **Files:** `src/one.ts`
- [ ] T2 Second [id:T2 deps: impl:R1]
    - **Files:** `src/two.ts`
""",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["waves"] == [["T1", "T2"]]


@pytest.mark.parametrize("scope", ["src", "src/shared.ts"])
def test_parent_directory_write_scope_conflicts(tmp_path: Path, scope: str) -> None:
    result = run_planner(
        tmp_path,
        f"""\
- [ ] T1 Parent [id:T1 deps: impl:R1]
    - **Files:** `{scope}`
- [ ] T2 Child [id:T2 deps: impl:R1]
    - **Files:** `src/shared.ts`
""",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["waves"] == [["T1"], ["T2"]]


def test_mixed_inline_and_plain_paths_are_both_preserved(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [ ] T1 First [id:T1 deps: impl:R1]
    - **Files:** `src/one.ts`, src/shared.ts
- [ ] T2 Second [id:T2 deps: impl:R1]
    - Files: src/shared.ts
""",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["waves"] == [["T1"], ["T2"]]


def test_wildcard_scope_overlaps_descendants(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [ ] T1 Parent [id:T1 deps: impl:R1]
    - **Files:** `src/**`
- [ ] T2 Child [id:T2 deps: impl:R1]
    - **Files:** `src/components/button.ts`
""",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["waves"] == [["T1"], ["T2"]]


def test_two_space_nested_bullet_is_a_write_scope(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [ ] T1 First [id:T1 deps: impl:R1]
  - **Files:** `src/shared.ts`
- [ ] T2 Second [id:T2 deps: impl:R1]
    - **Files:** `src/shared.ts`
""",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["waves"] == [["T1"], ["T2"]]


@pytest.mark.parametrize(
    "files_line",
    [
        None,
        "    - **Files:**",
        "    - **Files:** TBD",
        "    - **Files:** `src/partial.ts`,",
        "    - **Files:** `src/partial.ts",
    ],
)
def test_missing_empty_or_unusable_scope_is_exclusive(
    tmp_path: Path, files_line: str | None
) -> None:
    first_task = "- [ ] T1 Unscoped [id:T1 deps: impl:R1]\n"
    if files_line is not None:
        first_task += f"{files_line}\n"
    result = run_planner(
        tmp_path,
        first_task
        + "- [ ] T2 Scoped [id:T2 deps: impl:R1]\n"
        + "    - **Files:** `src/other.ts`\n",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["waves"] == [["T1"], ["T2"]]


def test_two_unscoped_tasks_are_exclusive_with_each_other(tmp_path: Path) -> None:
    result = run_planner(
        tmp_path,
        """\
- [ ] T1 First [id:T1 deps: impl:R1]
- [ ] T2 Second [id:T2 deps: impl:R1]
""",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["waves"] == [["T1"], ["T2"]]
