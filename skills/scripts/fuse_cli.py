#!/usr/bin/env python3
"""Control a managed Fuse run from an explicit consumer repository root."""

from __future__ import annotations

import argparse
import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    import fuse_candidate
    import fuse_checks
    import fuse_config
    import fuse_control
    import fuse_graph
    import fuse_record
    import fuse_state
else:
    try:
        from . import (
            fuse_candidate,
            fuse_checks,
            fuse_config,
            fuse_control,
            fuse_graph,
            fuse_record,
            fuse_state,
        )
    except ImportError:
        import fuse_candidate
        import fuse_checks
        import fuse_config
        import fuse_control
        import fuse_graph
        import fuse_record
        import fuse_state


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


def _add_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--spec-dir", required=True, type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Manage one Fuse execution record.")
    commands = parser.add_subparsers(
        dest="operation", required=True, parser_class=_Parser
    )

    initialize = commands.add_parser("initialize", help="start a new run")
    _add_paths(initialize)
    initialize.add_argument("--run-id")
    initialize.add_argument("--template", type=Path)

    resume = commands.add_parser("resume", help="resume the active run")
    _add_paths(resume)
    resume.add_argument("--run-id")

    inspect = commands.add_parser("inspect", help="inspect run and task readiness")
    _add_paths(inspect)
    inspect.add_argument("--run-id")

    reconcile = commands.add_parser("reconcile", help="compare saved and current state")
    _add_paths(reconcile)
    reconcile.add_argument("--run-id")
    reconcile.add_argument("--remote")

    checkpoint = commands.add_parser(
        "checkpoint", help="checkpoint to the run-state ref"
    )
    _add_paths(checkpoint)
    checkpoint.add_argument("--run-id", required=True)
    checkpoint.add_argument("--remote", required=True)
    checkpoint.add_argument("--expected-revision")

    prepare = commands.add_parser(
        "prepare-dispatch", help="reserve a task before launch"
    )
    _add_paths(prepare)
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--task-id", required=True)
    prepare.add_argument("--method", required=True)
    prepare.add_argument("--role-profile", required=True)
    prepare.add_argument("--finding-id")

    bind = commands.add_parser("bind-launch", help="bind a returned host handle")
    _add_paths(bind)
    bind.add_argument("--run-id", required=True)
    bind.add_argument("--assignment-id", required=True)
    bind.add_argument("--attempt-id", required=True)
    bind.add_argument("--host-handle", required=True)

    result = commands.add_parser(
        "record-result", help="record a worker attempt outcome"
    )
    _add_paths(result)
    result.add_argument("--run-id", required=True)
    result.add_argument("--assignment-id", required=True)
    result.add_argument("--attempt-id", required=True)
    result.add_argument(
        "--outcome",
        required=True,
        choices=("unknown", "succeeded", "failed", "cancelled"),
    )
    result.add_argument("--summary", default="attempt result recorded")
    result.add_argument("--candidate-id")

    check = commands.add_parser("run-check", help="run one configured check")
    _add_paths(check)
    check.add_argument("--run-id", required=True)
    check.add_argument("--check-id", required=True)
    check.add_argument("--candidate-id")

    review = commands.add_parser(
        "record-review", help="record a completed reviewer report"
    )
    _add_paths(review)
    review.add_argument("--run-id", required=True)
    review.add_argument("--task-id", required=True)
    review.add_argument("--assignment-id", required=True)
    review.add_argument("--candidate-id", required=True)
    review.add_argument("--outcome", choices=("pass", "fail"), required=True)
    review.add_argument("--summary", required=True)
    review.add_argument("--finding", action="append", default=[])
    review.add_argument("--resolve-finding-id", action="append", default=[])
    review.add_argument("--resolution-rationale")
    review.add_argument("--report", required=True, type=Path)

    accept = commands.add_parser("accept", help="accept a task with current evidence")
    _add_paths(accept)
    accept.add_argument("--run-id", required=True)
    accept.add_argument("--task-id", required=True)
    accept.add_argument("--candidate-id")
    return parser


def _success(operation: str, **fields: Any) -> None:
    print(json.dumps({"ok": True, "operation": operation, **fields}, sort_keys=True))


def _failure(operation: str, reason: str, message: str, **details: Any) -> None:
    print(
        json.dumps(
            {
                "ok": False,
                "operation": operation,
                "reason": reason,
                "message": message,
                "details": details,
            },
            sort_keys=True,
        )
    )


def _config(
    root: Path, *, initialize: bool = False, template: Path | None = None
) -> dict[str, Any]:
    config_path = root / ".fuse" / "config.toml"
    if not config_path.is_file():
        if not initialize:
            raise fuse_control.ControlError(
                "project_config_missing", "project .fuse/config.toml is missing"
            )
        template_path = template
        if template_path is not None and not template_path.is_absolute():
            template_path = root / template_path
        try:
            fuse_config.initialize_config(root, template_path)
        except fuse_config.ConfigError as error:
            raise fuse_control.ControlError(
                "project_config_invalid", str(error)
            ) from error
    try:
        return fuse_config.load_config(config_path)
    except fuse_config.ConfigError as error:
        raise fuse_control.ControlError("project_config_invalid", str(error)) from error


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    return fuse_control.validate_paths(args.repo_root, args.spec_dir)


def _decision(
    operation: str,
    root: Path,
    spec: Path,
    config: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    **fields: Any,
) -> fuse_control.TransitionDecision:
    decision = fuse_control.validate_transition(
        operation,
        repository_root=root,
        spec_dir=spec,
        config=config,
        record=record,
        **fields,
    )
    if not decision.allowed:
        raise fuse_control.ControlError(
            decision.reason, decision.message, decision.details
        )
    return decision


def _load_run(root: Path, spec: Path, run_id: str | None) -> dict[str, Any]:
    spec_id = spec.name
    try:
        if run_id is None:
            return fuse_state.select_run(root, spec_id)
        record = fuse_state.load_run(root, run_id)
        if record["spec_id"] != spec_id:
            raise fuse_control.ControlError(
                "run_spec_mismatch", "run belongs to another spec"
            )
        return record
    except fuse_state.StateError as error:
        raise fuse_control.ControlError("run_unavailable", str(error)) from error


def _record_path(root: Path, record: Mapping[str, Any]) -> str:
    return str(fuse_state.run_record_path(root, str(record["run_id"])))


def _save(root: Path, record: dict[str, Any]) -> None:
    try:
        fuse_state.save_run(root, record)
    except fuse_state.StateError as error:
        raise fuse_control.ControlError("run_save_failed", str(error)) from error


def _initialize(
    root: Path, spec: Path, config: Mapping[str, Any], run_id: str | None
) -> dict[str, Any]:
    decision = fuse_control.validate_transition(
        "initialize", repository_root=root, spec_dir=spec, config=config
    )
    if not decision.allowed:
        raise fuse_control.ControlError(
            decision.reason, decision.message, decision.details
        )
    try:
        manifest = fuse_candidate.build_candidate_manifest(root, spec, config, config)
        provenance = fuse_candidate.source_provenance(root)
        record = fuse_state.initialize_run(
            root,
            spec.name,
            config,
            run_id=run_id,
            spec_revision=provenance["commit"],
            spec_digests=manifest["verification_inputs"]["approved_spec"],
        )
        fuse_control.current_candidate(record, root, spec, config)
        _save(root, record)
    except fuse_state.StateError as error:
        reason = (
            "active_run_exists"
            if "active run" in str(error)
            else "run_initialize_failed"
        )
        raise fuse_control.ControlError(reason, str(error)) from error
    except (fuse_candidate.CandidateError, fuse_record.RecordError) as error:
        raise fuse_control.ControlError("run_initialize_failed", str(error)) from error
    return record


def _inspect(
    root: Path, spec: Path, config: Mapping[str, Any], run_id: str | None
) -> None:
    graph = fuse_control.validate_spec_files(spec)
    try:
        runs = fuse_state.list_runs(root, spec.name)
    except fuse_state.StateError as error:
        raise fuse_control.ControlError("run_state_invalid", str(error)) from error
    record: dict[str, Any] | None
    if run_id is not None:
        record = _load_run(root, spec, run_id)
    else:
        active = [item for item in runs if item["status"] in {"running", "paused"}]
        record = active[0] if len(active) == 1 else None
    if record is None:
        active_count = sum(
            1 for item in runs if item["status"] in {"running", "paused"}
        )
        _success(
            "inspect",
            spec_id=spec.name,
            run_ids=[item["run_id"] for item in runs],
            active_run_ids=[
                item["run_id"]
                for item in runs
                if item["status"] in {"running", "paused"}
            ],
            eligible_task_ids=(
                fuse_graph.ready_task_ids(graph, {"tasks": {}, "assignments": []})
                if active_count == 0
                else []
            ),
            ambiguous_active_runs=active_count > 1,
        )
        return
    _decision("inspect", root, spec, config, record)
    _success(
        "inspect",
        run_id=record["run_id"],
        status=record["status"],
        record_path=_record_path(root, record),
        eligible_task_ids=fuse_graph.ready_task_ids(graph, record),
        unresolved_launches=fuse_control.unresolved_launches(record),
        accepted_task_ids=sorted(
            task_id
            for task_id, state in record["tasks"].items()
            if state["status"] == "accepted" and state["accepted_candidate_id"]
        ),
    )


def _resume(
    root: Path, spec: Path, config: Mapping[str, Any], run_id: str | None
) -> None:
    record = _load_run(root, spec, run_id)
    decision = _decision("resume", root, spec, config, record)
    if record["status"] == "paused":
        fuse_control.set_run_status(record, "running", root, spec, config)
        _save(root, record)
    _success(
        "resume",
        run_id=record["run_id"],
        status=record["status"],
        record_path=_record_path(root, record),
        unresolved_launches=decision.details["unresolved_launches"],
        eligible_task_ids=fuse_graph.ready_task_ids(
            fuse_control.validate_spec_files(spec), record
        ),
    )


def _reconcile(
    root: Path,
    spec: Path,
    config: Mapping[str, Any],
    run_id: str | None,
    remote: str | None,
) -> None:
    record = _load_run(root, spec, run_id)
    decision = _decision("reconcile", root, spec, config, record)
    probe_record = copy.deepcopy(record)
    try:
        candidate = fuse_control.current_candidate(probe_record, root, spec, config)
        known_candidates = {item["candidate_id"] for item in record["candidates"]}
        result: dict[str, Any] = {
            "run_id": record["run_id"],
            "status": record["status"],
            "record_path": _record_path(root, record),
            **decision.details,
            "current_candidate_id": candidate["candidate_id"],
            "candidate_is_recorded": candidate["candidate_id"] in known_candidates,
            "stale_evidence_ids": [
                item["id"]
                for item in record["evidence"]
                if item["candidate_id"] != candidate["candidate_id"]
            ],
            "unresolved_launches": fuse_control.unresolved_launches(record),
        }
        if remote:
            revision, remote_record = fuse_state._fetch_remote_state(
                root, remote, spec.name, record["run_id"]
            )
            result["remote_revision"] = revision
            result["remote_status"] = "missing" if remote_record is None else "present"
            result["remote_reconciliation"] = (
                None
                if remote_record is None
                else fuse_state.reconcile_remote(record, remote_record)
            )
    except (fuse_state.StateError, fuse_control.ControlError) as error:
        raise fuse_control.ControlError("reconcile_failed", str(error)) from error
    _success("reconcile", **result)


def _checkpoint(
    root: Path,
    spec: Path,
    config: Mapping[str, Any],
    run_id: str,
    remote: str,
    expected_revision: str | None,
) -> None:
    record = _load_run(root, spec, run_id)
    _decision("checkpoint", root, spec, config, record)
    if expected_revision is None:
        expected_revision, _remote_record = fuse_state._fetch_remote_state(
            root, remote, spec.name, run_id
        )
    try:
        checkpoint_commit = fuse_state.checkpoint_run(
            root, remote, record, expected_revision=expected_revision
        )
    except fuse_state.StateError as error:
        raise fuse_control.ControlError("checkpoint_failed", str(error)) from error
    _success(
        "checkpoint",
        run_id=run_id,
        checkpoint_commit=checkpoint_commit,
        state_ref=record["git"]["state_ref"],
        state_revision=record["git"]["state_revision"],
        record_path=_record_path(root, record),
    )


def _prepare(
    root: Path,
    spec: Path,
    config: Mapping[str, Any],
    args: argparse.Namespace,
) -> None:
    record = _load_run(root, spec, args.run_id)
    try:
        decision = _decision(
            "prepare-dispatch",
            root,
            spec,
            config,
            record,
            task_id=args.task_id,
            method=args.method,
            role_profile=args.role_profile,
            finding_id=args.finding_id,
        )
    except fuse_control.ControlError as error:
        if error.reason in {
            "elapsed_time_limit_reached",
            "model_usage_limit_unverifiable",
        }:
            fuse_control.set_run_status(record, "paused", root, spec, config)
            _save(root, record)
            error.details["record_path"] = _record_path(root, record)
        raise
    review_candidate_id: str | None = None
    if args.role_profile in fuse_control.REVIEW_ROLES:
        candidate = fuse_control.current_candidate(record, root, spec, config)
        if candidate["candidate_id"] != decision.details["candidate_id"]:
            raise fuse_control.ControlError(
                "candidate_changed_during_dispatch",
                "review candidate changed while reserving the assignment",
            )
        review_candidate_id = candidate["candidate_id"]
    assignment = fuse_record.prepare_assignment(
        record,
        args.task_id,
        args.method,
        args.role_profile,
        capacity=int(decision.details["reserved_capacity"]),
        review_candidate_id=review_candidate_id,
        finding_id=args.finding_id,
    )
    repair_kind = decision.details.get("repair_kind")
    if repair_kind == "task":
        fuse_record.reserve_task_repair(
            record,
            args.task_id,
            int(config["limits"]["review"]["max_repair_rounds_per_level"]),
        )
    elif repair_kind == "rca":
        if args.finding_id is None:
            raise fuse_control.ControlError(
                "hard_blocker_required", "RCA dispatch requires --finding-id"
            )
        fuse_record.reserve_rca_attempt(
            record,
            args.finding_id,
            int(config["limits"]["rca"]["attempts_per_hard_blocker"]),
        )
    _save(root, record)
    _success(
        "prepare-dispatch",
        run_id=record["run_id"],
        task_id=args.task_id,
        assignment_id=assignment["id"],
        attempt_id=assignment["attempts"][-1]["id"],
        candidate_id=decision.details["candidate_id"] or None,
        finding_id=args.finding_id,
        reserved_capacity=fuse_record.reserved_capacity(record),
        capacity_limit=decision.details["capacity_limit"],
        record_path=_record_path(root, record),
    )


def _bind(
    root: Path, spec: Path, config: Mapping[str, Any], args: argparse.Namespace
) -> None:
    record = _load_run(root, spec, args.run_id)
    _decision(
        "bind-launch",
        root,
        spec,
        config,
        record,
        assignment_id=args.assignment_id,
        attempt_id=args.attempt_id,
        host_handle=args.host_handle,
    )
    fuse_record.bind_launch(
        record, args.assignment_id, args.attempt_id, args.host_handle
    )
    _save(root, record)
    _success(
        "bind-launch",
        run_id=record["run_id"],
        assignment_id=args.assignment_id,
        attempt_id=args.attempt_id,
        host_handle=args.host_handle,
        record_path=_record_path(root, record),
    )


def _record_result(
    root: Path, spec: Path, config: Mapping[str, Any], args: argparse.Namespace
) -> None:
    record = _load_run(root, spec, args.run_id)
    _decision(
        "record-result",
        root,
        spec,
        config,
        record,
        assignment_id=args.assignment_id,
        attempt_id=args.attempt_id,
        outcome=args.outcome,
        candidate_id=args.candidate_id,
    )
    result_data: dict[str, Any] = {"summary": args.summary}
    if args.candidate_id:
        result_data["candidate_id"] = args.candidate_id
    fuse_record.record_attempt_result(
        record,
        args.assignment_id,
        args.attempt_id,
        args.outcome,
        result_data,
    )
    _save(root, record)
    _success(
        "record-result",
        run_id=record["run_id"],
        assignment_id=args.assignment_id,
        attempt_id=args.attempt_id,
        outcome=args.outcome,
        note="worker result is not acceptance evidence",
        record_path=_record_path(root, record),
    )


def _run_check(
    root: Path, spec: Path, config: Mapping[str, Any], args: argparse.Namespace
) -> None:
    record = _load_run(root, spec, args.run_id)
    decision = _decision(
        "run-check",
        root,
        spec,
        config,
        record,
        check_id=args.check_id,
        candidate_id=args.candidate_id,
    )
    candidate = fuse_control.current_candidate(record, root, spec, config)
    if candidate["candidate_id"] != decision.details["candidate_id"]:
        raise fuse_control.ControlError(
            "candidate_changed_during_check",
            "candidate content changed while preparing the check",
        )
    _save(root, record)
    before = len(record["evidence"])
    try:
        result = fuse_checks.run_check(
            root, spec, config, record, candidate["candidate_id"], args.check_id
        )
    finally:
        if len(record["evidence"]) != before:
            _save(root, record)
    _success("run-check", run_id=record["run_id"], **result)


def _record_review(
    root: Path, spec: Path, config: Mapping[str, Any], args: argparse.Namespace
) -> None:
    record = _load_run(root, spec, args.run_id)
    report_path = args.report if args.report.is_absolute() else root / args.report
    report_path = report_path.expanduser().resolve(strict=True)
    report = report_path.read_bytes()
    evidence = fuse_control.record_review_evidence(
        record,
        root,
        spec,
        config,
        args.task_id,
        args.assignment_id,
        args.candidate_id,
        args.outcome,
        args.summary,
        report,
        finding_summaries=args.finding,
        resolve_finding_ids=args.resolve_finding_id,
        resolution_rationale=args.resolution_rationale,
    )
    _save(root, record)
    _success(
        "record-review",
        run_id=record["run_id"],
        task_id=args.task_id,
        evidence_id=evidence["id"],
        candidate_id=args.candidate_id,
        outcome=args.outcome,
        artifact=evidence["artifacts"][0]["path"],
        record_path=_record_path(root, record),
    )


def _accept(
    root: Path, spec: Path, config: Mapping[str, Any], args: argparse.Namespace
) -> None:
    record = _load_run(root, spec, args.run_id)
    decision = fuse_control.accept_task(
        record,
        root,
        spec,
        config,
        args.task_id,
        candidate_id=args.candidate_id,
    )
    if not decision.allowed:
        raise fuse_control.ControlError(
            decision.reason, decision.message, decision.details
        )
    _save(root, record)
    _success(
        "accept",
        run_id=record["run_id"],
        task_id=args.task_id,
        candidate_id=decision.details["candidate_id"],
        check_evidence_ids=decision.details["check_evidence_ids"],
        review_evidence_id=decision.details["review_evidence_id"],
        record_path=_record_path(root, record),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one control operation and emit a stable JSON result."""
    try:
        args = _parser().parse_args(argv)
    except _ArgumentError as error:
        _failure("arguments", "invalid_arguments", str(error))
        return 2
    operation = str(args.operation)
    try:
        root, spec = _paths(args)
        config = _config(
            root,
            initialize=operation == "initialize",
            template=getattr(args, "template", None),
        )
        if operation == "initialize":
            record = _initialize(root, spec, config, args.run_id)
            _success(
                "initialize",
                run_id=record["run_id"],
                status=record["status"],
                record_path=_record_path(root, record),
                candidate_id=record["candidates"][-1]["candidate_id"],
                task_ids=sorted(fuse_control.validate_spec_files(spec)),
            )
        elif operation == "inspect":
            _inspect(root, spec, config, args.run_id)
        elif operation == "resume":
            _resume(root, spec, config, args.run_id)
        elif operation == "prepare-dispatch":
            _prepare(root, spec, config, args)
        elif operation == "bind-launch":
            _bind(root, spec, config, args)
        elif operation == "record-result":
            _record_result(root, spec, config, args)
        elif operation == "run-check":
            _run_check(root, spec, config, args)
        elif operation == "record-review":
            _record_review(root, spec, config, args)
        elif operation == "accept":
            _accept(root, spec, config, args)
        elif operation == "checkpoint":
            _checkpoint(
                root, spec, config, args.run_id, args.remote, args.expected_revision
            )
        elif operation == "reconcile":
            _reconcile(root, spec, config, args.run_id, args.remote)
        else:
            _failure(operation, "unknown_operation", f"unknown operation {operation}")
            return 2
        return 0
    except fuse_control.ControlError as error:
        _failure(operation, error.reason, str(error), **error.details)
        return 2
    except fuse_state.StateError as error:
        _failure(operation, "run_state_error", str(error))
        return 2
    except fuse_config.ConfigError as error:
        _failure(operation, "project_config_invalid", str(error))
        return 2
    except fuse_checks.CheckError as error:
        _failure(operation, "check_failed", str(error))
        return 2
    except (OSError, fuse_record.RecordError, fuse_candidate.CandidateError) as error:
        _failure(operation, "operation_failed", str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
