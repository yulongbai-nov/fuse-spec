"""Tests for the canonical versioned run-record contract."""

from __future__ import annotations

import copy
import hashlib
import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_DIR = Path(__file__).parents[1] / "skills" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
fuse_record: Any = importlib.import_module("fuse_record")


def make_record() -> Any:
    return fuse_record.new_run_record(
        "001-demo",
        {"roles": {"worker": {"model": "example-model"}}, "limits": {}},
    )


def test_unknown_schema_version_fails_closed_without_migration() -> None:
    record = make_record()
    original = copy.deepcopy(record)
    record["schema_version"] = 2

    with pytest.raises(
        fuse_record.RecordError, match="unsupported run-record schema version"
    ):
        fuse_record.validate_run_record(record)

    assert original["schema_version"] == 1
    assert record["schema_version"] == 2


def test_ids_are_unique_for_assignments_findings_and_evidence() -> None:
    record = make_record()
    first = fuse_record.prepare_assignment(
        record, "T1", "Feature", "worker", capacity=2
    )
    second = fuse_record.prepare_assignment(record, "T2", "Bug Fix", "worker")
    first_finding = fuse_record.add_finding(record, "T1", "Issue one")
    second_finding = fuse_record.add_finding(record, "T2", "Issue two")
    first_candidate = fuse_record.add_candidate(record, {"source": "first"})
    second_candidate = fuse_record.add_candidate(record, {"source": "second"})
    first_evidence = fuse_record.add_evidence(
        record, first_candidate["candidate_id"], "check", "pass", "checks passed"
    )
    second_evidence = fuse_record.add_evidence(
        record,
        second_candidate["candidate_id"],
        "review",
        "pass",
        "review passed",
    )

    fuse_record.validate_run_record(record)

    assert first["id"] != second["id"]
    assert first_finding["id"] != second_finding["id"]
    assert first_evidence["id"] != second_evidence["id"]
    assert fuse_record.reserved_capacity(record) == 3
    record["evidence"][1]["id"] = first_evidence["id"]
    with pytest.raises(fuse_record.RecordError, match="duplicate evidence id"):
        fuse_record.validate_run_record(record)


def test_candidate_identity_excludes_source_provenance() -> None:
    record = make_record()
    manifest = {"verification_inputs": {"source": "content digest input"}}

    first = fuse_record.add_candidate(
        record, manifest, {"commit": "a" * 40, "branch": "first"}
    )
    second = fuse_record.add_candidate(
        record, manifest, {"commit": "b" * 40, "branch": "second"}
    )

    assert first["candidate_id"] == second["candidate_id"]
    assert len(record["candidates"]) == 1
    assert first["source_provenance"]["commit"] == "a" * 40


def test_each_evidence_record_preserves_observed_provenance_and_check_inputs() -> None:
    record = make_record()
    manifest = {"verification_inputs": {"tree": "stable"}}
    candidate = fuse_record.add_candidate(record, manifest, {"commit": "a" * 40})
    same_candidate = fuse_record.add_candidate(record, manifest, {"commit": "b" * 40})

    first_evidence = fuse_record.add_evidence(
        record,
        candidate["candidate_id"],
        "check",
        "pass",
        "check passed",
        check_id="pytest",
        source_provenance={"commit": "a" * 40, "base": "c" * 40},
        tool_versions={"python": "3.11.8", "pytest": "9.0.0"},
        config_digest="d" * 64,
        check_definition_digest="e" * 64,
    )
    second_evidence = fuse_record.add_evidence(
        record,
        same_candidate["candidate_id"],
        "review",
        "pass",
        "review passed",
        source_provenance={"commit": "b" * 40, "base": "c" * 40},
        tool_versions={"reviewer": "example-reviewer"},
    )

    assert candidate["candidate_id"] == same_candidate["candidate_id"]
    assert first_evidence["source_provenance"]["commit"] == "a" * 40
    assert second_evidence["source_provenance"]["commit"] == "b" * 40
    assert first_evidence["tool_versions"]["pytest"] == "9.0.0"
    assert first_evidence["config_digest"] == "d" * 64
    assert first_evidence["check_definition_digest"] == "e" * 64

    missing_provenance = copy.deepcopy(record)
    del missing_provenance["evidence"][0]["source_provenance"]
    with pytest.raises(fuse_record.RecordError, match="source_provenance"):
        fuse_record.validate_run_record(missing_provenance)


def test_retry_appends_new_attempt_and_preserves_terminal_history() -> None:
    record = make_record()
    assignment = fuse_record.prepare_assignment(record, "T1", "Feature", "worker")
    attempt = assignment["attempts"][0]
    fuse_record.bind_launch(record, assignment["id"], attempt["id"], "host-1")
    fuse_record.record_attempt_result(record, assignment["id"], attempt["id"], "failed")
    before_retry = copy.deepcopy(record)

    retried = fuse_record.append_retry(record, assignment["id"])
    fuse_record.validate_run_record(record, previous=before_retry)

    assert len(retried["attempts"]) == 2
    assert retried["attempts"][0] == before_retry["assignments"][0]["attempts"][0]
    assert retried["attempts"][1]["id"] != retried["attempts"][0]["id"]
    assert fuse_record.reserved_capacity(record) == 1

    record["assignments"][0]["attempts"][0]["status"] = "succeeded"
    with pytest.raises(
        fuse_record.RecordError, match="terminal attempt history is immutable"
    ):
        fuse_record.validate_run_record(record, previous=before_retry)


def test_effective_policy_snapshot_cannot_change_between_checkpoints() -> None:
    original = make_record()
    changed = copy.deepcopy(original)
    changed["effective_policy_snapshot"]["roles"]["worker"]["model"] = "other"

    with pytest.raises(
        fuse_record.RecordError, match="effective policy snapshot is immutable"
    ):
        fuse_record.validate_run_record(changed, previous=original)


def test_review_and_rca_counters_must_be_unique() -> None:
    record = make_record()
    review_counter = {
        "scope_id": "T1",
        "level": "task",
        "repairs_used": 0,
        "limit": 2,
    }
    record["review_counters"] = [
        copy.deepcopy(review_counter),
        copy.deepcopy(review_counter),
    ]

    with pytest.raises(fuse_record.RecordError, match="duplicate review counter"):
        fuse_record.validate_run_record(record)

    record["review_counters"] = []
    finding = fuse_record.add_finding(record, "T1", "Hard blocker")
    rca_attempt = {"finding_id": finding["id"], "attempts_used": 0, "limit": 1}
    record["rca_attempts"] = [copy.deepcopy(rca_attempt), copy.deepcopy(rca_attempt)]
    with pytest.raises(fuse_record.RecordError, match="duplicate RCA counter"):
        fuse_record.validate_run_record(record)


def test_review_and_rca_counter_usage_is_monotonic_and_limits_are_immutable() -> None:
    record = make_record()
    finding = fuse_record.add_finding(record, "T1", "Hard blocker")
    record["review_counters"] = [
        {"scope_id": "T1", "level": "task", "repairs_used": 1, "limit": 2}
    ]
    record["rca_attempts"] = [
        {"finding_id": finding["id"], "attempts_used": 1, "limit": 1}
    ]
    fuse_record.validate_run_record(record)
    previous = copy.deepcopy(record)

    appended_counter = copy.deepcopy(record)
    appended_counter["review_counters"].append(
        {"scope_id": "spec", "level": "spec_pr", "repairs_used": 0, "limit": 2}
    )
    fuse_record.validate_run_record(appended_counter, previous=previous)

    reordered_counters = copy.deepcopy(appended_counter)
    reordered_counters["review_counters"].reverse()
    with pytest.raises(
        fuse_record.RecordError, match="review counter history is append-only"
    ):
        fuse_record.validate_run_record(reordered_counters, previous=appended_counter)

    reset_review = copy.deepcopy(record)
    reset_review["review_counters"][0]["repairs_used"] = 0
    with pytest.raises(
        fuse_record.RecordError, match="review counter usage is monotonic"
    ):
        fuse_record.validate_run_record(reset_review, previous=previous)

    changed_review_limit = copy.deepcopy(record)
    changed_review_limit["review_counters"][0]["limit"] = 3
    with pytest.raises(
        fuse_record.RecordError, match="review counter limit is immutable"
    ):
        fuse_record.validate_run_record(changed_review_limit, previous=previous)

    reset_rca = copy.deepcopy(record)
    reset_rca["rca_attempts"][0]["attempts_used"] = 0
    with pytest.raises(fuse_record.RecordError, match="RCA counter usage is monotonic"):
        fuse_record.validate_run_record(reset_rca, previous=previous)

    extended_rca = copy.deepcopy(record)
    extended_rca["rca_attempts"][0]["limit"] = 2
    with pytest.raises(fuse_record.RecordError, match="RCA counter limit is immutable"):
        fuse_record.validate_run_record(extended_rca, previous=previous)


def test_operator_decisions_are_append_only() -> None:
    original = make_record()
    first_decision = {
        "id": "dec_" + "1" * 32,
        "at": original["started_at"],
        "decision": "continue",
        "rationale": "operator confirmed",
    }
    appended = copy.deepcopy(original)
    appended["operator_decisions"].append(first_decision)
    fuse_record.validate_run_record(appended, previous=original)

    changed = copy.deepcopy(appended)
    changed["operator_decisions"][0]["decision"] = "stop"
    with pytest.raises(
        fuse_record.RecordError, match="operator decision history is immutable"
    ):
        fuse_record.validate_run_record(changed, previous=appended)

    removed = copy.deepcopy(appended)
    removed["operator_decisions"].clear()
    with pytest.raises(
        fuse_record.RecordError, match="operator decision history is append-only"
    ):
        fuse_record.validate_run_record(removed, previous=appended)


def test_missing_required_artifact_makes_evidence_unavailable(tmp_path: Path) -> None:
    record = make_record()
    artifact = tmp_path / "result.txt"
    content = b"captured result\n"
    digest = hashlib.sha256(content).hexdigest()
    candidate = fuse_record.add_candidate(record, {"source": "tested"})
    evidence = fuse_record.add_evidence(
        record,
        candidate["candidate_id"],
        "check",
        "pass",
        "check passed",
        artifacts=[{"path": "result.txt", "sha256": digest, "required": True}],
    )

    assert not fuse_record.evidence_available(evidence, tmp_path)
    artifact.write_bytes(content)
    assert fuse_record.evidence_available(evidence, tmp_path)


def test_artifact_path_cannot_escape_run_directory(tmp_path: Path) -> None:
    record = make_record()
    candidate = fuse_record.add_candidate(record, {"source": "tested"})
    evidence = fuse_record.add_evidence(
        record,
        candidate["candidate_id"],
        "review",
        "pass",
        "review passed",
        artifacts=[{"path": "../outside.txt", "sha256": "0" * 64, "required": True}],
    )

    assert not fuse_record.evidence_available(evidence, tmp_path)
