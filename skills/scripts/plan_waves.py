#!/usr/bin/env python3
"""Compute execution waves from a tasks.md file.

Parses inline task tags of the form::

    - [ ] T3 Title  [id:T3 deps:T1,T2 impl:R2,D-Auth]

and groups tasks by dependency readiness. Tasks with overlapping declared
``Files:`` scopes are placed in separate waves; tasks without a usable scope
run exclusively. Checked tasks are skipped by default and seed the satisfied
dependency set, preserving ordinary resume behavior.

Output (stdout): JSON, e.g.::

    {"waves": [["T1", "T2"], ["T3"]], "tasks": {"T1": {...}, ...}}

Exits non-zero on duplicate task ids, unknown dependencies, or a cycle.

Usage::

    plan_waves.py <tasks.md>
"""

import argparse
import json
import posixpath
import re
import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from fuse_markdown import markdown_fence_line_flags, task_checkbox_row_match
else:
    try:
        from .fuse_markdown import markdown_fence_line_flags, task_checkbox_row_match
    except ImportError:
        from fuse_markdown import markdown_fence_line_flags, task_checkbox_row_match

TASK_TAG = re.compile(r"\[([^\]]*\bid:[^\]]*)\]")
FILES_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?Files:(?:\*\*)?\s*(.*)$", re.IGNORECASE
)
UNUSABLE_SCOPES = {
    "-",
    "...",
    "?",
    "n/a",
    "na",
    "none",
    "not applicable",
    "not specified",
    "null",
    "pending",
    "tbd",
    "todo",
    "unknown",
    "unspecified",
}


class Task(TypedDict):
    """Fields parsed from one task and used by the planner."""

    deps: list[str]
    impl: list[str]
    writes: list[str]
    scope_invalid: bool


def parse_tag(body: str) -> tuple[str | None, list[str], list[str]]:
    """Parse an inline task tag into its id, dependencies, and references."""
    task_id: str | None = None
    deps: list[str] = []
    impl: list[str] = []
    for token in body.split():
        if ":" not in token:
            continue
        key, _, raw = token.partition(":")
        key = key.strip()
        if key == "id":
            task_id = raw.strip()
        elif key == "deps":
            deps = [value for value in raw.split(",") if value]
        elif key == "impl":
            impl = [value for value in raw.split(",") if value]
    return task_id, deps, impl


def normalize_scope(raw_path: str) -> str | None:
    """Normalize one repository-relative scope, or reject an unusable value."""
    candidate = raw_path.strip().replace("\\", "/")
    if not candidate or candidate.casefold() in UNUSABLE_SCOPES:
        return None
    if posixpath.isabs(candidate) or ".." in candidate.split("/"):
        return None
    return posixpath.normpath(candidate).rstrip("/") or "."


def scopes_overlap(first: str, second: str) -> bool:
    """Return whether file, directory, or wildcard scopes may intersect."""
    first_prefix = _static_prefix(first)
    second_prefix = _static_prefix(second)
    if first_prefix == "." or second_prefix == ".":
        return True
    return (
        first_prefix == second_prefix
        or first_prefix.startswith(second_prefix + "/")
        or second_prefix.startswith(first_prefix + "/")
    )


def _static_prefix(scope: str) -> str:
    """Return the literal directory prefix before the first glob component."""
    static_parts: list[str] = []
    for part in scope.split("/"):
        if any(character in part for character in "*?["):
            break
        static_parts.append(part)
    return "/".join(static_parts) or "."


def _split_scope_entries(value: str) -> list[str] | None:
    """Split comma-separated paths outside Markdown inline-code spans."""
    entries: list[str] = []
    start = 0
    code_delimiter_length: int | None = None
    index = 0
    while index < len(value):
        if value[index] == "`":
            end = index
            while end < len(value) and value[end] == "`":
                end += 1
            delimiter_length = end - index
            if code_delimiter_length is None:
                code_delimiter_length = delimiter_length
            elif delimiter_length == code_delimiter_length:
                code_delimiter_length = None
            index = end
            continue
        if value[index] == "," and code_delimiter_length is None:
            entries.append(value[start:index].strip())
            start = index + 1
        index += 1

    if code_delimiter_length is not None:
        return None
    entries.append(value[start:].strip())
    return entries


def _unwrap_inline_code(entry: str) -> str | None:
    """Return an entry's path text and reject unmatched inline-code markers."""
    if not entry.startswith("`"):
        return None if "`" in entry else entry

    delimiter_end = 0
    while delimiter_end < len(entry) and entry[delimiter_end] == "`":
        delimiter_end += 1
    delimiter = "`" * delimiter_end
    if len(entry) <= delimiter_end * 2 or not entry.endswith(delimiter):
        return None
    return entry[delimiter_end:-delimiter_end].strip()


def _paths_from_files_line(value: str) -> list[str]:
    """Parse every listed path, returning no scope if any entry is unusable."""
    entries = _split_scope_entries(value)
    if not entries or any(not entry for entry in entries):
        return []

    paths: list[str] = []
    for entry in entries:
        path_text = _unwrap_inline_code(entry)
        if path_text is None:
            return []
        path = normalize_scope(path_text.strip(" \"'"))
        if path is None:
            return []
        paths.append(path)
    return paths


def parse_task_declarations(
    tasks_markdown: str,
) -> tuple[dict[str, Task], set[str], set[str]]:
    """Parse task metadata, write scopes, duplicate IDs, and checked task IDs."""
    all_tasks: dict[str, Task] = {}
    duplicates: set[str] = set()
    done_ids: set[str] = set()
    invalid: set[str] = set()
    current_task_id: str | None = None
    lines = tasks_markdown.splitlines()
    for line, in_fence in zip(lines, markdown_fence_line_flags(lines), strict=True):
        if in_fence:
            continue
        checkbox_row = task_checkbox_row_match(line)
        match = TASK_TAG.search(line) if checkbox_row is not None else None
        if match:
            task_id, dependencies, implementation = parse_tag(match.group(1))
            current_task_id = task_id
            if not task_id:
                continue
            if task_id in all_tasks:
                duplicates.add(task_id)
                continue
            all_tasks[task_id] = {
                "deps": dependencies,
                "impl": implementation,
                "writes": [],
                "scope_invalid": False,
            }
            if checkbox_row is not None and checkbox_row.group("status").lower() == "x":
                done_ids.add(task_id)
            continue
        if current_task_id is None:
            continue
        indentation = len(line) - len(line.lstrip())
        files_match = FILES_RE.match(line) if indentation >= 2 else None
        if files_match:
            task_scopes = _paths_from_files_line(files_match.group(1))
            if not task_scopes:
                all_tasks[current_task_id]["writes"] = []
                all_tasks[current_task_id]["scope_invalid"] = True
                invalid.add(current_task_id)
            elif current_task_id not in invalid:
                all_tasks[current_task_id]["writes"].extend(task_scopes)
    return all_tasks, duplicates, done_ids


def compute_waves(
    tasks: Mapping[str, Task], already_done: Collection[str] | None = None
) -> list[list[str]]:
    """Group dependency-ready tasks into deterministic, non-conflicting waves.

    ``already_done`` seeds the satisfied set so tasks depending on completed
    tasks become schedulable without those completed tasks reappearing. Tasks
    with no usable write scope are placed in a wave by themselves.
    """
    remaining = set(tasks)
    done = set(already_done or ())
    waves: list[list[str]] = []
    while remaining:
        ready = sorted(
            task_id
            for task_id in remaining
            if all(dependency in done for dependency in tasks[task_id]["deps"])
        )
        if not ready:
            cycle = sorted(remaining)
            print(
                f"error: dependency cycle or unresolved deps among {cycle}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        wave: list[str] = []
        wave_scopes: list[str] = []
        exclusive_task_in_wave = False
        for task_id in ready:
            task_scopes = tasks[task_id]["writes"]
            if not task_scopes:
                if wave:
                    continue
                wave.append(task_id)
                exclusive_task_in_wave = True
                continue
            if exclusive_task_in_wave:
                continue
            if any(
                scopes_overlap(scope, existing_scope)
                for scope in task_scopes
                for existing_scope in wave_scopes
            ):
                continue
            wave.append(task_id)
            wave_scopes.extend(task_scopes)

        waves.append(wave)
        done.update(wave)
        remaining.difference_update(wave)
    return waves


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute execution waves from tasks.md."
    )
    parser.add_argument("tasks_file", help="path to tasks.md")
    parser.add_argument(
        "--include-done",
        action="store_true",
        help="include already-checked tasks (default: skip them)",
    )
    args = parser.parse_args(argv)

    path = Path(args.tasks_file)
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2

    all_tasks, duplicates, done_ids = parse_task_declarations(
        path.read_text(encoding="utf-8")
    )

    if duplicates:
        for task_id in sorted(duplicates):
            print(f"error: duplicate task id {task_id}", file=sys.stderr)
        return 1

    # Validate every declaration, including checked tasks skipped for resume.
    unknown: list[tuple[str, str]] = []
    all_ids = set(all_tasks)
    for task_id, task in all_tasks.items():
        for dependency in task["deps"]:
            if dependency not in all_ids:
                unknown.append((task_id, dependency))
    if unknown:
        for task_id, dependency in unknown:
            print(
                f"error: task {task_id} depends on unknown task {dependency}",
                file=sys.stderr,
            )
        return 1

    tasks = {
        task_id: task
        for task_id, task in all_tasks.items()
        if args.include_done or task_id not in done_ids
    }
    seed = set() if args.include_done else (done_ids - set(tasks))
    waves = compute_waves(tasks, already_done=seed)
    output_tasks = {
        task_id: {"deps": task["deps"], "impl": task["impl"]}
        for task_id, task in tasks.items()
    }
    print(json.dumps({"waves": waves, "tasks": output_tasks}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
