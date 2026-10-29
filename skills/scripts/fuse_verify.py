#!/usr/bin/env python3
"""Run configured project checks through one candidate-bound entry point."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

fuse_candidate: Any = importlib.import_module("fuse_candidate")
fuse_checks: Any = importlib.import_module("fuse_checks")
fuse_config: Any = importlib.import_module("fuse_config")
fuse_control: Any = importlib.import_module("fuse_control")
fuse_graph: Any = importlib.import_module("fuse_graph")
fuse_record: Any = importlib.import_module("fuse_record")

RUN_ID = re.compile(r"^run_[0-9a-f]{32}$")


class VerificationError(ValueError):
    """The verification request cannot be safely prepared."""


def _check_result(
    check_id: str,
    candidate_id: str,
    outcome: str,
    summary: str,
    *,
    exit_code: int | None = None,
    evidence_id: str | None = None,
    blocked_by: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "candidate_id": candidate_id,
        "outcome": outcome,
        "exit_code": exit_code,
        "summary": summary,
        "evidence_id": evidence_id,
        "blocked_by": list(blocked_by),
    }


def _run_check_dag(
    repository_root: Path,
    spec_dir: Path,
    config: Mapping[str, Any],
    record: dict[str, Any],
    candidate_id: str,
    graph: Mapping[str, Sequence[str]],
) -> dict[str, dict[str, Any]]:
    """Run ready checks in stable order and keep unrelated paths moving."""
    pending = set(graph)
    results: dict[str, dict[str, Any]] = {}

    while pending:
        while True:
            blocked_now = {
                check_id: sorted(
                    prerequisite
                    for prerequisite in graph[check_id]
                    if prerequisite in results
                    and results[prerequisite]["outcome"] != "pass"
                )
                for check_id in pending
            }
            newly_blocked = {
                check_id: blockers
                for check_id, blockers in blocked_now.items()
                if blockers
            }
            if not newly_blocked:
                break
            for check_id, blockers in sorted(newly_blocked.items()):
                results[check_id] = _check_result(
                    check_id,
                    candidate_id,
                    "blocked",
                    "prerequisite checks did not pass: " + ", ".join(blockers),
                    blocked_by=blockers,
                )
            pending.difference_update(newly_blocked)

        completed = {
            check_id
            for check_id, result in results.items()
            if result["outcome"] == "pass"
        }
        blocked = set(results) - completed
        graph_ready = fuse_graph.ready_check_ids(
            config["checks"],
            graph,
            completed=completed,
            blocked=blocked,
        )
        ready = sorted(set(graph_ready) & pending)
        if not ready:
            if pending:
                raise VerificationError(
                    "validated check graph could not make progress: "
                    + ", ".join(sorted(pending))
                )
            break

        for check_id in ready:
            try:
                result = fuse_checks.run_check(
                    repository_root,
                    spec_dir,
                    config,
                    record,
                    candidate_id,
                    check_id,
                )
            except fuse_checks.CheckError as error:
                results[check_id] = _check_result(
                    check_id,
                    candidate_id,
                    "inconclusive",
                    str(error),
                )
            else:
                results[check_id] = _check_result(
                    check_id,
                    candidate_id,
                    result["outcome"],
                    result["summary"],
                    exit_code=result["exit_code"],
                    evidence_id=result["evidence_id"],
                )
            pending.remove(check_id)

    return results


def run_verification(
    repository_root: Path,
    spec_dir: Path,
    config: Mapping[str, Any],
    *,
    check_ids: Sequence[str] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Validate inputs, capture a candidate, and execute configured check DAG."""
    root, spec = fuse_control.validate_paths(repository_root, spec_dir)
    fuse_control.validate_spec_files(spec)
    try:
        fuse_config.validate_config(config)
        check_definitions = config["checks"]
        if not isinstance(check_definitions, Mapping):
            raise VerificationError("project configuration has no check definitions")
        all_ids = sorted(check_definitions)
        graph = fuse_graph.applicable_check_graph(
            check_definitions, list(check_ids) if check_ids else all_ids
        )
    except (KeyError, ValueError) as error:
        raise VerificationError(str(error)) from error

    selected_run_id = run_id or f"run_{uuid.uuid4().hex}"
    if not RUN_ID.fullmatch(selected_run_id):
        raise VerificationError(
            "run ID must match run_ followed by 32 lowercase hex digits"
        )

    record = fuse_record.new_run_record(
        spec.name,
        config,
        run_id=selected_run_id,
    )
    try:
        candidate = fuse_candidate.capture_candidate(record, root, spec, config)
    except (OSError, ValueError) as error:
        raise VerificationError(
            f"cannot capture verification candidate: {error}"
        ) from error

    candidate_id = str(candidate["candidate_id"])
    results = _run_check_dag(root, spec, config, record, candidate_id, graph)
    failed = any(result["outcome"] != "pass" for result in results.values())
    return {
        "ok": not failed,
        "run_id": selected_run_id,
        "spec_id": spec.name,
        "repository_root": str(root),
        "spec_dir": str(spec),
        "candidate_id": candidate_id,
        "selected_checks": sorted(graph),
        "results": [results[check_id] for check_id in sorted(results)],
        "artifact_root": str(
            root / ".fuse" / "runs" / selected_run_id / "artifacts" / "checks"
        ),
        "record_persisted": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the project-owned check DAG against one pinned candidate. "
            "Managed lifecycle evidence remains in Fuse's canonical run record."
        )
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--spec-dir", type=Path, required=True)
    parser.add_argument(
        "--check-id",
        action="append",
        dest="check_ids",
        help=(
            "run this check and its configured prerequisites (repeatable; default: all)"
        ),
    )
    parser.add_argument("--run-id", help="optional CI artifact run ID")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.repo_root.is_absolute() or not args.spec_dir.is_absolute():
        print(
            json.dumps(
                {"ok": False, "error": "--repo-root and --spec-dir must be absolute"},
                sort_keys=True,
            )
        )
        return 2

    root = args.repo_root.expanduser().resolve()
    config_path = root / ".fuse" / "config.toml"
    try:
        config = fuse_config.load_config(config_path)
        report = run_verification(
            root,
            args.spec_dir.expanduser().resolve(),
            config,
            check_ids=args.check_ids,
            run_id=args.run_id,
        )
    except (OSError, ValueError, fuse_control.ControlError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        return 2

    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
