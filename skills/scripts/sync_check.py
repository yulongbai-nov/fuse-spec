#!/usr/bin/env python3
"""Validate traceability across a spec's three files.

Given a spec directory containing requirements.md (or bugfix.md), design.md,
and tasks.md, this checks that every cross-reference resolves and reports
orphans. It is deterministic and token-free; the agent does the semantic pass
on whatever this flags.

Reference model
---------------
requirements.md : declares requirement ids via headings  "### R1 ..." / "#### R1: ..."
                  (bugfix.md declares them the same way)
design.md       : declares design anchors via headings    "### D-Auth ..."
                  and cites requirements via lines         "satisfies: R1, R2"
tasks.md        : each task carries an inline tag          "[id:T3 deps:T1,T2 impl:R2,D-Auth]"

Findings
--------
ERROR (exit 1): a reference points at something that does not exist
    - task impl cites an R-id absent from requirements
    - task impl cites a D-anchor absent from design
    - task deps cites an unknown task id
    - design satisfies cites an R-id absent from requirements
WARN  (exit 0 if no errors): something is orphaned / likely stale
    - requirement satisfied by no design section
    - design anchor referenced by no task
    - design section with an empty/missing satisfies list
    - task with an empty impl list

Usage:
    sync_check.py <spec-dir>
"""
import argparse
import re
import sys
from pathlib import Path

HEADING_R = re.compile(r"^#{1,6}\s*(R\d+)\b")
HEADING_D = re.compile(r"^#{1,6}\s*(D-[\w-]+)\b")
SATISFIES = re.compile(r"satisfies:\s*([^\n]+)", re.IGNORECASE)
TASK_TAG = re.compile(r"\[([^\]]*\bid:[^\]]*)\]")
R_REF = re.compile(r"\bR\d+\b")
D_REF = re.compile(r"\bD-[\w-]+\b")
T_REF = re.compile(r"\bT\d+\b")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def strip_fences(text: str) -> str:
    """Drop fenced ``` code blocks so documentation examples aren't parsed."""
    out = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def parse_tag(body: str) -> dict:
    """Tokenize 'id:T3 deps:T1,T2 impl:R2,D-Auth' into {id, deps[], impl[]}."""
    fields = {}
    for token in body.split():
        if ":" not in token:
            continue
        key, _, raw = token.partition(":")
        key = key.strip()
        if key == "id":
            fields["id"] = raw.strip()
        elif key in ("deps", "impl"):
            fields[key] = [x for x in re.split(r"[,]+", raw) if x]
    return fields


def main(argv=None) -> int:
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
    design_text = strip_fences(read(spec / "design.md"))
    tasks_text = strip_fences(read(spec / "tasks.md"))
    req_text = strip_fences(read(req_path))

    # Defined ids
    req_ids = {m.group(1) for line in req_text.splitlines() if (m := HEADING_R.match(line))}
    design_anchors = {m.group(1) for line in design_text.splitlines() if (m := HEADING_D.match(line))}

    # Design -> requirements
    design_satisfies = set()
    design_sections_without_satisfies = 0
    design_section_count = 0
    for line in design_text.splitlines():
        if HEADING_D.match(line):
            design_section_count += 1
    for m in SATISFIES.finditer(design_text):
        refs = R_REF.findall(m.group(1))
        design_satisfies.update(refs)
    # sections that declare an anchor but never appear with a satisfies nearby:
    # approximate by comparing counts (anchors vs satisfies lines)
    satisfies_line_count = len(SATISFIES.findall(design_text))
    if design_section_count and satisfies_line_count < design_section_count:
        design_sections_without_satisfies = design_section_count - satisfies_line_count

    # Tasks
    task_ids = set()
    task_deps = {}
    task_impl = {}
    tasks_without_impl = []
    for m in TASK_TAG.finditer(tasks_text):
        tag = parse_tag(m.group(1))
        tid = tag.get("id")
        if not tid:
            continue
        task_ids.add(tid)
        task_deps[tid] = tag.get("deps", [])
        impl = tag.get("impl", [])
        task_impl[tid] = impl
        if not impl:
            tasks_without_impl.append(tid)

    errors = []
    warnings = []

    # ERROR: design satisfies cites unknown R-id
    for ref in sorted(design_satisfies):
        if ref not in req_ids:
            errors.append(f"design.md satisfies cites {ref} but it is not defined in {req_path.name}")

    # Tasks
    impl_r_refs = set()
    impl_d_refs = set()
    for tid in sorted(task_ids):
        for ref in task_impl.get(tid, []):
            if R_REF.fullmatch(ref):
                impl_r_refs.add(ref)
                if ref not in req_ids:
                    errors.append(f"task {tid} impl cites {ref} but it is not defined in {req_path.name}")
            elif D_REF.fullmatch(ref):
                impl_d_refs.add(ref)
                if ref not in design_anchors:
                    errors.append(f"task {tid} impl cites {ref} but it is not defined in design.md")
            else:
                warnings.append(f"task {tid} impl has unrecognized reference '{ref}' (expected R<n> or D-<name>)")
        for dep in task_deps.get(tid, []):
            if dep not in task_ids:
                errors.append(f"task {tid} deps cites unknown task {dep}")

    # WARN: requirement satisfied by nothing (neither design nor any task)
    for rid in sorted(req_ids):
        if rid not in design_satisfies and rid not in impl_r_refs:
            warnings.append(f"requirement {rid} is satisfied by no design section and implemented by no task")

    # WARN: design anchor referenced by no task
    for anchor in sorted(design_anchors):
        if anchor not in impl_d_refs:
            warnings.append(f"design anchor {anchor} is referenced by no task")

    # WARN: design sections without a satisfies line
    if design_sections_without_satisfies:
        warnings.append(
            f"{design_sections_without_satisfies} design section(s) appear to have no 'satisfies:' line")

    # WARN: tasks without impl
    for tid in tasks_without_impl:
        warnings.append(f"task {tid} has an empty impl list (no requirement/design reference)")

    for w in warnings:
        print(f"WARN: {w}")
    for e in errors:
        print(f"ERROR: {e}")

    if not errors and not warnings:
        print("OK: traceability is consistent")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
