#!/usr/bin/env python3
"""Compute execution waves from a tasks.md file.

Parses inline task tags of the form:

    - [ ] T3 Title  [id:T3 deps:T1,T2 impl:R2,D-Auth]

and topologically sorts tasks by their `deps` into "waves": each wave is a list
of task ids whose dependencies are all satisfied by earlier waves. Tasks within
a wave are independent and may run concurrently.

Output (stdout): JSON, e.g.
    {"waves": [["T1","T2"], ["T3"], ["T4"]], "tasks": {"T1": {...}, ...}}

Exits non-zero on a dependency cycle or an unknown dependency.

Usage:
    plan_waves.py <tasks.md>
"""
import argparse
import json
import re
import sys
from pathlib import Path

TASK_TAG = re.compile(r"\[([^\]]*\bid:[^\]]*)\]")
DONE_RE = re.compile(r"^\s*-\s*\[[xX]\]")


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


def compute_waves(tasks: dict, already_done=None) -> list:
    """Kahn's algorithm, grouping ready nodes per level. Deterministic order.

    `already_done` seeds the satisfied set so tasks depending on completed
    tasks become schedulable without those completed tasks reappearing.
    """
    remaining = dict(tasks)
    done = set(already_done or ())
    waves = []
    while remaining:
        ready = sorted(
            tid for tid, t in remaining.items()
            if all(dep in done for dep in t["deps"])
        )
        if not ready:
            cycle = sorted(remaining)
            print(f"error: dependency cycle or unresolved deps among {cycle}", file=sys.stderr)
            raise SystemExit(1)
        waves.append(ready)
        for tid in ready:
            done.add(tid)
            del remaining[tid]
    return waves


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compute execution waves from tasks.md.")
    parser.add_argument("tasks_file", help="path to tasks.md")
    parser.add_argument("--include-done", action="store_true",
                        help="include already-checked tasks (default: skip them)")
    args = parser.parse_args(argv)

    path = Path(args.tasks_file)
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2

    tasks = {}
    all_ids = set()
    done_ids = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = TASK_TAG.search(line)
        if not m:
            continue
        tag = parse_tag(m.group(1))
        tid = tag.get("id")
        if not tid:
            continue
        all_ids.add(tid)
        is_done = bool(DONE_RE.match(line))
        if is_done:
            done_ids.add(tid)
        if is_done and not args.include_done:
            continue
        tasks[tid] = {"deps": tag.get("deps", []), "impl": tag.get("impl", [])}

    # Validate deps reference a real task id (done or not)
    unknown = []
    for tid, t in tasks.items():
        for dep in t["deps"]:
            if dep not in all_ids:
                unknown.append((tid, dep))
    if unknown:
        for tid, dep in unknown:
            print(f"error: task {tid} depends on unknown task {dep}", file=sys.stderr)
        return 1

    seed = set() if args.include_done else (done_ids - set(tasks))
    waves = compute_waves(tasks, already_done=seed)
    print(json.dumps({"waves": waves, "tasks": tasks}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
