#!/usr/bin/env python3
"""Validate traceability across a spec's requirements, design, and tasks.

Given a spec directory, this checks that every cross-reference resolves and
reports orphaned declarations.

Reference model
--------------
requirements.md (or bugfix.md): requirement headings such as ``### R1 ...``
design.md: design anchors such as ``### D-Auth ...`` and ``satisfies: R1``
tasks.md: inline tags such as ``[id:T3 deps:T1 impl:R2,D-Auth]``

Errors exit 1; warnings exit 0. A missing or empty requirements input is an
error because traceability cannot be checked without at least one requirement.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

HEADING_R = re.compile(r"^#{1,6}\s*(R\d+)\b")
HEADING_D = re.compile(r"^#{1,6}\s*(D-[\w-]+)\b")
SATISFIES = re.compile(r"satisfies:\s*([^\n]+)", re.IGNORECASE)
TASK_TAG = re.compile(r"\[([^\]]*\bid:[^\]]*)\]")
R_REF = re.compile(r"\bR\d+\b")
D_REF = re.compile(r"\bD-[\w-]+\b")


def read(path: Path) -> str:
    """Read an existing UTF-8 file, returning an empty string if absent."""
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def strip_fences(text: str) -> str:
    """Drop fenced code blocks so documentation examples are not parsed."""
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return "\n".join(lines)


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate spec traceability.")
    parser.add_argument("spec_dir", help="path to .specs/NNN-<slug>/")
    args = parser.parse_args(argv)

    spec = Path(args.spec_dir)
    if not spec.is_dir():
        print(f"error: not a directory: {spec}", file=sys.stderr)
        return 2

    req_path = spec / "requirements.md"
    if not req_path.is_file():
        req_path = spec / "bugfix.md"
    if not req_path.is_file():
        print(
            "ERROR: missing requirements input: expected requirements.md or bugfix.md"
        )
        return 1

    req_text = strip_fences(read(req_path))
    if not req_text.strip():
        print(f"ERROR: requirements input is empty: {req_path.name}")
        return 1

    req_ids = {
        match.group(1)
        for line in req_text.splitlines()
        if (match := HEADING_R.match(line))
    }
    if not req_ids:
        print(f"ERROR: no requirement IDs found in {req_path.name}")
        return 1

    design_text = strip_fences(read(spec / "design.md"))
    tasks_text = strip_fences(read(spec / "tasks.md"))

    design_anchors = {
        match.group(1)
        for line in design_text.splitlines()
        if (match := HEADING_D.match(line))
    }

    design_satisfies: set[str] = set()
    design_section_count = sum(
        1 for line in design_text.splitlines() if HEADING_D.match(line)
    )
    for match in SATISFIES.finditer(design_text):
        design_satisfies.update(R_REF.findall(match.group(1)))
    satisfies_line_count = len(SATISFIES.findall(design_text))
    missing_satisfies_count = max(0, design_section_count - satisfies_line_count)

    task_ids: set[str] = set()
    task_deps: dict[str, list[str]] = {}
    task_impl: dict[str, list[str]] = {}
    duplicate_task_ids: set[str] = set()
    tasks_without_impl: list[str] = []
    for match in TASK_TAG.finditer(tasks_text):
        task_id, deps, impl = parse_tag(match.group(1))
        if not task_id:
            continue
        if task_id in task_ids:
            duplicate_task_ids.add(task_id)
        task_ids.add(task_id)
        task_deps[task_id] = deps
        task_impl[task_id] = impl
        if not impl:
            tasks_without_impl.append(task_id)

    errors: list[str] = [
        f"duplicate task id {task_id}" for task_id in sorted(duplicate_task_ids)
    ]
    warnings: list[str] = []

    # Design -> requirements
    for reference in sorted(design_satisfies):
        if reference not in req_ids:
            errors.append(
                f"design.md satisfies cites {reference} but it is not defined "
                f"in {req_path.name}"
            )

    # Task traceability and dependency declarations
    impl_r_refs: set[str] = set()
    impl_d_refs: set[str] = set()
    for task_id in sorted(task_ids):
        for reference in task_impl.get(task_id, []):
            if R_REF.fullmatch(reference):
                impl_r_refs.add(reference)
                if reference not in req_ids:
                    errors.append(
                        f"task {task_id} impl cites {reference} but it is not "
                        f"defined in {req_path.name}"
                    )
            elif D_REF.fullmatch(reference):
                impl_d_refs.add(reference)
                if reference not in design_anchors:
                    errors.append(
                        f"task {task_id} impl cites {reference} but it is not "
                        "defined in design.md"
                    )
            else:
                warnings.append(
                    f"task {task_id} impl has unrecognized reference "
                    f"'{reference}' (expected R<n> or D-<name>)"
                )
        for dependency in task_deps.get(task_id, []):
            if dependency not in task_ids:
                errors.append(f"task {task_id} deps cites unknown task {dependency}")

    # WARN: requirement satisfied by nothing (neither design nor any task)
    for requirement_id in sorted(req_ids):
        if requirement_id not in design_satisfies and requirement_id not in impl_r_refs:
            warnings.append(
                f"requirement {requirement_id} is satisfied by no design section "
                "and implemented by no task"
            )

    # WARN: design anchor referenced by no task
    for anchor in sorted(design_anchors):
        if anchor not in impl_d_refs:
            warnings.append(f"design anchor {anchor} is referenced by no task")

    if missing_satisfies_count:
        warnings.append(
            f"{missing_satisfies_count} design section(s) appear to have no "
            "'satisfies:' line"
        )
    for task_id in tasks_without_impl:
        warnings.append(
            f"task {task_id} has an empty impl list (no requirement/design reference)"
        )

    for warning in warnings:
        print(f"WARN: {warning}")
    for error in errors:
        print(f"ERROR: {error}")

    if not errors and not warnings:
        print("OK: traceability is consistent")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
