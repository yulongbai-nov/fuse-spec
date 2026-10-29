#!/usr/bin/env python3
"""Prepare locked project tools and record candidate-bound check results."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, TypedDict, cast

if TYPE_CHECKING:
    from fuse_artifacts import ArtifactError, write_run_artifact
    from fuse_candidate import (
        build_candidate_manifest,
        normalize_task_checkboxes,
        source_provenance,
    )
    from fuse_config import _validate_project_toolchain, validate_config
    from fuse_graph import (
        applicable_check_graph,
    )
    from fuse_graph import (
        ready_check_ids as graph_ready_check_ids,
    )
    from fuse_record import (
        add_evidence,
        canonical_json_digest,
        evidence_available,
        validate_run_record,
    )
else:
    try:
        from .fuse_artifacts import ArtifactError, write_run_artifact
        from .fuse_candidate import (
            build_candidate_manifest,
            normalize_task_checkboxes,
            source_provenance,
        )
        from .fuse_config import _validate_project_toolchain, validate_config
        from .fuse_graph import (
            applicable_check_graph,
        )
        from .fuse_graph import (
            ready_check_ids as graph_ready_check_ids,
        )
        from .fuse_record import (
            add_evidence,
            canonical_json_digest,
            evidence_available,
            validate_run_record,
        )
    except ImportError:
        from fuse_artifacts import ArtifactError, write_run_artifact
        from fuse_candidate import (
            build_candidate_manifest,
            normalize_task_checkboxes,
            source_provenance,
        )
        from fuse_config import _validate_project_toolchain, validate_config
        from fuse_graph import (
            applicable_check_graph,
        )
        from fuse_graph import (
            ready_check_ids as graph_ready_check_ids,
        )
        from fuse_record import (
            add_evidence,
            canonical_json_digest,
            evidence_available,
            validate_run_record,
        )


class CheckError(ValueError):
    """Raised when check inputs or candidate-bound evidence are invalid."""


class ToolchainPreparation(TypedDict):
    """Result of verifying/restoring the project's selected locked tools."""

    ready: bool
    summary: str
    tool_versions: dict[str, str]


class CheckResult(TypedDict):
    """Concise result returned after a configured check is recorded."""

    check_id: str
    candidate_id: str
    outcome: str
    exit_code: int | None
    summary: str
    evidence_id: str


class BoundedCommandResult(TypedDict):
    """Captured result of a check process with shared resource limits."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    lingering_output_process: bool
    capture_incomplete: bool
    stdout_truncated: bool
    stderr_truncated: bool


GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
OUTPUT_METADATA_PREFIX = "fuse-check-output-v1 "


def _is_git_object_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and GIT_OBJECT_ID.fullmatch(value) is not None
        and set(value) != {"0"}
    )


def _read_bounded_pipe(pipe: Any, maximum_bytes: int) -> tuple[str, bool]:
    """Drain a child pipe while retaining no more than the configured bytes."""
    captured = bytearray()
    truncated = False
    while True:
        chunk = pipe.read(64 * 1024)
        if not chunk:
            break
        remaining = maximum_bytes - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
    return captured.decode("utf-8", errors="replace"), truncated


def _terminate_check_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate a check process group where supported, then fall back to child."""
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if process.poll() is None:
            process.kill()
    except OSError:
        process.kill()


def _run_bounded_check_command(
    command: list[str],
    cwd: Path,
    *,
    timeout_seconds: int,
    max_output_bytes_per_stream: int,
) -> BoundedCommandResult:
    """Execute a check with bounded wall time and in-memory output capture."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    if process.stdout is None or process.stderr is None:
        _terminate_check_process(process)
        raise CheckError("check process pipes could not be created")

    captured: dict[str, tuple[str, bool]] = {}
    capture_errors: dict[str, str] = {}

    def drain(name: str, pipe: Any) -> None:
        try:
            captured[name] = _read_bounded_pipe(pipe, max_output_bytes_per_stream)
        except (OSError, ValueError) as error:
            capture_errors[name] = str(error)

    readers = [
        threading.Thread(
            target=drain,
            args=(name, pipe),
            name=f"fuse-check-{name}",
            daemon=True,
        )
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_check_process(process)
        process.wait()

    for reader in readers:
        reader.join(timeout=1)
    lingering_output_process = any(reader.is_alive() for reader in readers)
    if lingering_output_process:
        _terminate_check_process(process)
        process.stdout.close()
        process.stderr.close()
        for reader in readers:
            reader.join(timeout=1)

    returncode = process.returncode
    if returncode is None:
        raise CheckError("check process did not terminate after its execution limit")
    stdout, stdout_truncated = captured.get("stdout", ("", True))
    stderr, stderr_truncated = captured.get("stderr", ("", True))
    if "stdout" in capture_errors:
        stdout += f"\n[output capture error: {capture_errors['stdout']}]"
        stdout_truncated = True
    if "stderr" in capture_errors:
        stderr += f"\n[output capture error: {capture_errors['stderr']}]"
        stderr_truncated = True
    if lingering_output_process:
        stdout_truncated = True
        stderr_truncated = True

    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "lingering_output_process": lingering_output_process,
        "capture_incomplete": bool(capture_errors) or lingering_output_process,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


def _git(
    root: Path,
    *arguments: str,
    env: Mapping[str, str] | None = None,
    input_data: bytes | None = None,
) -> str:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", *arguments],
            cwd=root,
            env=env,
            input=input_data,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise CheckError(f"cannot run git {' '.join(arguments)}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CheckError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _blob_id(repository_root: Path, content: bytes) -> str:
    return _git(repository_root, "hash-object", "-w", "--stdin", input_data=content)


def _source_blob(
    repository_root: Path, relative: str, expected: Mapping[str, Any]
) -> tuple[str, str]:
    path = PurePosixPath(relative)
    source = repository_root.joinpath(*path.parts)
    try:
        metadata = source.lstat()
    except OSError as error:
        raise CheckError(
            f"candidate input disappeared while pinning: {relative}"
        ) from error

    kind = expected.get("kind")
    if kind == "file" and stat.S_ISREG(metadata.st_mode):
        try:
            content = source.read_bytes()
        except OSError as error:
            raise CheckError(
                f"cannot read candidate input {relative}: {error}"
            ) from error
        digest = hashlib.sha256(content).hexdigest()
        executable = bool(metadata.st_mode & stat.S_IXUSR)
        if digest != expected.get("sha256") or executable != expected.get("executable"):
            raise CheckError(f"candidate input changed while pinning: {relative}")
        return (
            "100755" if executable else "100644",
            _blob_id(repository_root, content),
        )
    if kind == "symlink" and stat.S_ISLNK(metadata.st_mode):
        try:
            source.resolve(strict=False).relative_to(repository_root)
        except (RuntimeError, ValueError) as error:
            raise CheckError(
                f"candidate symlink escapes repository: {relative}"
            ) from error
        content = os.readlink(source).encode("utf-8", errors="surrogateescape")
        if hashlib.sha256(content).hexdigest() != expected.get("target_sha256"):
            raise CheckError(f"candidate symlink changed while pinning: {relative}")
        return "120000", _blob_id(repository_root, content)
    raise CheckError(f"candidate input has an unsupported type: {relative}")


def _snapshot_entries(
    repository_root: Path,
    spec_dir: Path,
    manifest: Mapping[str, Any],
) -> list[tuple[str, str, str]]:
    verification = manifest.get("verification_inputs")
    if not isinstance(verification, Mapping):
        raise CheckError("candidate verification manifest is malformed")
    source_tree = verification.get("source_tree")
    approved_spec = verification.get("approved_spec")
    required_inputs = verification.get("required_inputs")
    if (
        not isinstance(source_tree, Mapping)
        or not isinstance(approved_spec, Mapping)
        or not isinstance(required_inputs, Mapping)
    ):
        raise CheckError(
            "candidate source, spec, or required-input manifest is malformed"
        )

    entries: list[tuple[str, str, str]] = []
    for raw_path, source_info in sorted(source_tree.items()):
        if not isinstance(raw_path, str) or not isinstance(source_info, Mapping):
            raise CheckError("candidate source manifest contains an invalid entry")
        mode, object_id = _source_blob(repository_root, raw_path, source_info)
        entries.append((mode, object_id, raw_path))

    try:
        relative_spec = spec_dir.resolve(strict=True).relative_to(repository_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise CheckError("approved spec directory is outside the repository") from error
    for raw_name, expected_digest in sorted(approved_spec.items()):
        if not isinstance(raw_name, str) or not isinstance(expected_digest, str):
            raise CheckError("approved spec manifest contains an invalid entry")
        if PurePosixPath(raw_name).name != raw_name or raw_name in {".", ".."}:
            raise CheckError("approved spec manifest contains an invalid file name")
        spec_path = spec_dir / raw_name
        try:
            content = spec_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise CheckError(
                f"cannot read approved spec input {raw_name}: {error}"
            ) from error
        normalized = (
            normalize_task_checkboxes(content) if raw_name == "tasks.md" else content
        )
        if hashlib.sha256(normalized.encode("utf-8")).hexdigest() != expected_digest:
            raise CheckError(f"approved spec input changed while pinning: {raw_name}")
        raw_content = normalized.encode("utf-8")
        spec_path_in_tree = (relative_spec / raw_name).as_posix()
        entries.append(
            ("100644", _blob_id(repository_root, raw_content), spec_path_in_tree)
        )

    for raw_path, input_info in sorted(required_inputs.items()):
        if (
            not isinstance(raw_path, str)
            or not isinstance(input_info, Mapping)
            or not isinstance(input_info.get("sha256"), str)
            or type(input_info.get("executable")) is not bool
        ):
            raise CheckError("required-input manifest contains an invalid entry")
        relative = PurePosixPath(raw_path)
        if (
            not raw_path.strip()
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in raw_path
        ):
            raise CheckError("required-input manifest contains an invalid path")
        if raw_path in source_tree:
            source_info = source_tree[raw_path]
            if not isinstance(source_info, Mapping):
                raise CheckError(
                    f"required input has an invalid source entry: {raw_path}"
                )
            if source_info.get("kind") == "file":
                if (
                    source_info.get("sha256") != input_info["sha256"]
                    or source_info.get("executable") != input_info["executable"]
                ):
                    raise CheckError(
                        f"required input changed while pinning: {raw_path}"
                    )
            elif source_info.get("kind") == "symlink":
                target = repository_root.joinpath(*relative.parts).resolve(strict=True)
                try:
                    target_relative = target.relative_to(repository_root).as_posix()
                except ValueError as error:
                    raise CheckError(
                        f"required input symlink escapes repository: {raw_path}"
                    ) from error
                target_info = source_tree.get(target_relative)
                if (
                    not isinstance(target_info, Mapping)
                    or target_info.get("kind") != "file"
                    or target_info.get("sha256") != input_info["sha256"]
                    or target_info.get("executable") != input_info["executable"]
                ):
                    raise CheckError(
                        f"required input symlink target is not in candidate source: "
                        f"{raw_path}"
                    )
            else:
                raise CheckError(f"required input is not a candidate file: {raw_path}")
    return entries


def _materialize_ignored_required_inputs(
    repository_root: Path, worktree: Path, manifest: Mapping[str, Any]
) -> None:
    verification = manifest.get("verification_inputs")
    if not isinstance(verification, Mapping):
        raise CheckError("candidate verification manifest is malformed")
    source_tree = verification.get("source_tree")
    required_inputs = verification.get("required_inputs")
    if not isinstance(source_tree, Mapping) or not isinstance(required_inputs, Mapping):
        raise CheckError("candidate source or required-input manifest is malformed")

    for raw_path, input_info in sorted(required_inputs.items()):
        if raw_path in source_tree:
            continue
        if (
            not isinstance(raw_path, str)
            or not isinstance(input_info, Mapping)
            or not isinstance(input_info.get("sha256"), str)
            or type(input_info.get("executable")) is not bool
        ):
            raise CheckError("required-input manifest contains an invalid entry")
        relative = PurePosixPath(raw_path)
        source = repository_root.joinpath(*relative.parts)
        try:
            metadata = source.lstat()
            resolved = source.resolve(strict=True)
            resolved.relative_to(repository_root)
        except (OSError, RuntimeError, ValueError) as error:
            raise CheckError(
                f"cannot materialize required input {raw_path}: {error}"
            ) from error
        if not stat.S_ISREG(metadata.st_mode):
            raise CheckError(
                f"ignored required input must be a regular file: {raw_path}"
            )
        try:
            content = source.read_bytes()
        except OSError as error:
            raise CheckError(
                f"cannot read required input {raw_path}: {error}"
            ) from error
        executable = bool(metadata.st_mode & stat.S_IXUSR)
        if (
            hashlib.sha256(content).hexdigest() != input_info["sha256"]
            or executable != input_info["executable"]
        ):
            raise CheckError(f"required input changed while pinning: {raw_path}")
        destination = worktree.joinpath(*relative.parts)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.resolve(strict=False).relative_to(worktree.resolve())
        except (OSError, RuntimeError, ValueError) as error:
            raise CheckError(
                f"required input path escapes candidate: {raw_path}"
            ) from error
        if os.path.lexists(destination):
            raise CheckError(f"required input path collides in candidate: {raw_path}")
        try:
            destination.write_bytes(content)
            destination.chmod(0o755 if executable else 0o644)
        except OSError as error:
            raise CheckError(
                f"cannot materialize required input {raw_path}: {error}"
            ) from error


def _snapshot_tree(
    repository_root: Path,
    spec_dir: Path,
    manifest: Mapping[str, Any],
) -> str:
    """Build the one canonical Git tree used for pinning and verification."""
    entries = _snapshot_entries(repository_root, spec_dir, manifest)
    with tempfile.TemporaryDirectory(prefix="fuse-index-") as index_directory:
        index_path = Path(index_directory) / "index"
        index_env = os.environ.copy()
        index_env["GIT_INDEX_FILE"] = str(index_path)
        _git(repository_root, "read-tree", "--empty", env=index_env)
        index_data = b"".join(
            f"{mode} {object_id}\t{path}\0".encode("utf-8", errors="surrogateescape")
            for mode, object_id, path in entries
        )
        if index_data:
            _git(
                repository_root,
                "update-index",
                "-z",
                "--index-info",
                env=index_env,
                input_data=index_data,
            )
        return _git(repository_root, "write-tree", env=index_env)


def _synthetic_candidate_commit(
    repository_root: Path,
    spec_dir: Path,
    manifest: Mapping[str, Any],
    base_commit: str,
    candidate_id: str,
) -> tuple[str, str]:
    tree_id = _snapshot_tree(repository_root, spec_dir, manifest)
    author_env = os.environ.copy()
    author_env.update(
        {
            "GIT_AUTHOR_NAME": "Fuse Candidate",
            "GIT_AUTHOR_EMAIL": "fuse-candidate@example.invalid",
            "GIT_COMMITTER_NAME": "Fuse Candidate",
            "GIT_COMMITTER_EMAIL": "fuse-candidate@example.invalid",
        }
    )
    commit_id = _git(
        repository_root,
        "commit-tree",
        tree_id,
        "-p",
        base_commit,
        "-m",
        f"Fuse candidate {candidate_id}",
        env=author_env,
    )
    return commit_id, tree_id


@contextmanager
def _detached_worktree(repository_root: Path, commit_id: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="fuse-check-") as temporary_directory:
        worktree = Path(temporary_directory).resolve() / "worktree"
        _git(
            repository_root,
            "-c",
            "core.hooksPath=/dev/null",
            "worktree",
            "add",
            "--detach",
            str(worktree),
            commit_id,
        )
        try:
            yield worktree
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=repository_root,
                capture_output=True,
                check=False,
            )


@contextmanager
def _pinned_worktree(
    repository_root: Path,
    spec_dir: Path,
    manifest: Mapping[str, Any],
    base_commit: str,
    candidate_id: str,
) -> Iterator[tuple[Path, str, str]]:
    """Create a detached temporary worktree for the exact captured candidate."""
    commit_id, tree_id = _synthetic_candidate_commit(
        repository_root, spec_dir, manifest, base_commit, candidate_id
    )
    with _detached_worktree(repository_root, commit_id) as worktree:
        _materialize_ignored_required_inputs(repository_root, worktree, manifest)
        yield worktree, commit_id, tree_id


def _safe_version(
    command: list[str], repository_root: Path, *, timeout: float = 10
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=repository_root,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    output = (result.stdout or result.stderr).strip()
    return output if result.returncode == 0 and output else "unavailable"


def prepare_toolchain(
    repository_root: Path, config: Mapping[str, Any]
) -> ToolchainPreparation:
    """Restore selected project tools only through the configured locked manager."""
    root = repository_root.expanduser().resolve()
    try:
        validate_config(config)
        toolchain = config["toolchain"]
        _validate_project_toolchain(root, toolchain)
    except (KeyError, ValueError, OSError) as error:
        return {
            "ready": False,
            "summary": f"selected toolchain is unavailable: {error}",
            "tool_versions": {},
        }

    manager = str(toolchain["package_manager"])
    if (
        toolchain["runtime"] != "python"
        or manager != "uv"
        or toolchain["restore_policy"]
        != "selected_tools_existing_version_and_lock_only"
        or toolchain["selection_policy"] != "reuse_existing"
    ):
        return {
            "ready": False,
            "summary": "selected toolchain has no supported locked restore operation",
            "tool_versions": {},
        }
    if shutil.which(manager) is None:
        return {
            "ready": False,
            "summary": f"selected package manager {manager!r} is unavailable",
            "tool_versions": {},
        }

    check_limits = config["limits"]["checks"]
    try:
        sync = _run_bounded_check_command(
            [manager, "sync", "--locked"],
            root,
            timeout_seconds=check_limits["timeout_seconds"],
            max_output_bytes_per_stream=check_limits["max_output_bytes_per_stream"],
        )
    except (OSError, CheckError) as error:
        return {
            "ready": False,
            "summary": f"locked tool restore failed: {error}",
            "tool_versions": {"package_manager": manager},
        }
    if sync["timed_out"]:
        return {
            "ready": False,
            "summary": (
                "locked tool restore timed out after "
                f"{check_limits['timeout_seconds']} seconds"
            ),
            "tool_versions": {"package_manager": manager},
        }
    if sync["capture_incomplete"]:
        return {
            "ready": False,
            "summary": "locked tool restore output capture did not complete",
            "tool_versions": {"package_manager": manager},
        }
    if sync["returncode"] != 0:
        detail = (sync["stderr"] or sync["stdout"]).strip()
        if sync["stderr_truncated"] or sync["stdout_truncated"]:
            detail += " (restore output was truncated)"
        return {
            "ready": False,
            "summary": f"{manager} sync --locked failed: {detail}",
            "tool_versions": {"package_manager": manager},
        }

    manager_version = _safe_version([manager, "--version"], root)
    runtime = _safe_version([manager, "run", "--locked", "python", "--version"], root)
    lockfile_path = root / str(toolchain["lockfile"])
    try:
        lockfile_digest = hashlib.sha256(lockfile_path.read_bytes()).hexdigest()
    except OSError:
        lockfile_digest = "unavailable"
    return {
        "ready": True,
        "summary": f"{manager} synchronized the selected lockfile",
        "tool_versions": {
            "package_manager": manager_version,
            "runtime": runtime,
            "lockfile_sha256": lockfile_digest,
        },
    }


def _check_tool_version(command: list[str], manager: str, repository_root: Path) -> str:
    if command[:3] == [manager, "run", "--locked"] and len(command) >= 4:
        if command[3] == "python" and command[4:5] == ["-m"] and len(command) >= 6:
            probe = [*command[:6], "--version"]
        else:
            probe = [*command[:4], "--version"]
    else:
        probe = [command[0], "--version"]
    return _safe_version(probe, repository_root)


def _write_output_artifact(
    repository_root: Path,
    run_id: str,
    candidate_id: str,
    check_id: str,
    content: bytes,
) -> dict[str, Any]:
    root = repository_root.expanduser().resolve()
    execution_id = uuid.uuid4().hex
    relative = (
        Path("artifacts") / "checks" / candidate_id / check_id / f"{execution_id}.log"
    )
    try:
        return write_run_artifact(root, run_id, relative, content)
    except ArtifactError as error:
        raise CheckError(f"check output artifact {error}") from error


def _record_result(
    record: dict[str, Any],
    candidate_id: str,
    check_id: str,
    outcome: str,
    summary: str,
    source: Mapping[str, Any],
    tool_versions: Mapping[str, str],
    config: Mapping[str, Any],
    artifacts: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    check = config["checks"][check_id]
    return add_evidence(
        record,
        candidate_id,
        "check",
        outcome,
        summary,
        check_id=check_id,
        artifacts=artifacts,
        source_provenance=source,
        tool_versions=tool_versions,
        config_digest=canonical_json_digest(config),
        check_definition_digest=canonical_json_digest(check),
    )


def _required_output_artifacts(evidence: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    artifacts = evidence.get("artifacts", ())
    if not isinstance(artifacts, list):
        return []
    candidate_id = evidence.get("candidate_id")
    check_id = evidence.get("check_id")
    if not isinstance(candidate_id, str) or not isinstance(check_id, str):
        return []
    prefix = f"artifacts/checks/{candidate_id}/{check_id}/"
    return [
        artifact
        for artifact in artifacts
        if isinstance(artifact, Mapping)
        and artifact.get("required") is True
        and isinstance(artifact.get("path"), str)
        and artifact["path"].startswith(prefix)
    ]


def _has_check_provenance(evidence: Mapping[str, Any]) -> bool:
    source = evidence.get("source_provenance")
    tool_versions = evidence.get("tool_versions")
    return (
        isinstance(source, Mapping)
        and _is_git_object_id(source.get("commit"))
        and _is_git_object_id(source.get("pinned_worktree_commit"))
        and _is_git_object_id(source.get("pinned_worktree_tree"))
        and type(source.get("worktree_dirty")) is bool
        and isinstance(tool_versions, Mapping)
        and bool(tool_versions)
        and all(
            isinstance(name, str) and isinstance(version, str) and bool(version.strip())
            for name, version in tool_versions.items()
        )
    )


def _candidate_matches_config(
    candidate: Mapping[str, Any], config: Mapping[str, Any]
) -> None:
    try:
        validate_config(config)
    except ValueError as error:
        raise CheckError(f"project configuration is invalid: {error}") from error
    manifest = candidate.get("manifest")
    verification = (
        manifest.get("verification_inputs") if isinstance(manifest, Mapping) else None
    )
    if not isinstance(verification, Mapping):
        raise CheckError("candidate configuration context is missing")
    if verification.get("check_definitions") != config.get(
        "checks"
    ) or verification.get("toolchain") != config.get("toolchain"):
        raise CheckError("candidate configuration context is stale")
    if verification.get("check_execution_limits") != config.get("limits", {}).get(
        "checks"
    ):
        raise CheckError("candidate check execution limits are stale")


def _artifact_execution_metadata(
    evidence: Mapping[str, Any], artifact_root: Path, config: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Read framed metadata only; command arguments and stdout cannot add headers."""
    output_artifacts = _required_output_artifacts(evidence)
    source = evidence["source_provenance"]
    expected = {
        "candidate_id": evidence["candidate_id"],
        "check_id": evidence["check_id"],
        "config_digest": evidence["config_digest"],
        "check_definition_digest": evidence["check_definition_digest"],
        "timeout_seconds": config["limits"]["checks"]["timeout_seconds"],
        "max_output_bytes_per_stream": config["limits"]["checks"][
            "max_output_bytes_per_stream"
        ],
        "source_commit": source["commit"],
        "pinned_commit": source["pinned_worktree_commit"],
        "pinned_tree": source["pinned_worktree_tree"],
    }
    artifact_root_resolved = artifact_root.expanduser().resolve()
    for artifact in output_artifacts:
        relative = Path(str(artifact["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        try:
            artifact_path = (artifact_root_resolved / relative).resolve(strict=True)
            artifact_path.relative_to(artifact_root_resolved)
            with artifact_path.open(encoding="utf-8") as stream:
                header = stream.readline()
            if not header.startswith(OUTPUT_METADATA_PREFIX):
                continue
            metadata = json.loads(header[len(OUTPUT_METADATA_PREFIX) :])
        except (OSError, UnicodeError, RuntimeError, ValueError):
            continue
        if not isinstance(metadata, dict) or any(
            metadata.get(key) != value for key, value in expected.items()
        ):
            continue
        exit_code = metadata.get("exit_code")
        spec_dir = metadata.get("spec_dir")
        if (
            type(exit_code) is not int
            or (evidence["outcome"] == "pass" and exit_code != 0)
            or not isinstance(spec_dir, str)
            or not spec_dir.strip()
            or PurePosixPath(spec_dir).is_absolute()
            or ".." in PurePosixPath(spec_dir).parts
            or "\\" in spec_dir
        ):
            continue
        if (
            type(metadata.get("timed_out")) is not bool
            or type(metadata.get("capture_incomplete")) is not bool
            or type(metadata.get("stdout_truncated")) is not bool
            or type(metadata.get("stderr_truncated")) is not bool
            or type(metadata.get("lingering_output_process")) is not bool
            or (
                evidence["outcome"] == "pass"
                and (
                    metadata["timed_out"]
                    or metadata["capture_incomplete"]
                    or metadata["lingering_output_process"]
                )
            )
        ):
            continue
        configured_command = config["checks"][evidence["check_id"]]["command"]
        executed_command = [
            argument.replace("{spec_dir}", spec_dir) for argument in configured_command
        ]
        if (
            metadata.get("configured_command") != configured_command
            or metadata.get("executed_command") != executed_command
        ):
            continue
        return cast(Mapping[str, Any], metadata)
    raise CheckError("check artifact execution metadata or provenance does not match")


def _validate_pinned_candidate(
    evidence: Mapping[str, Any],
    candidate: Mapping[str, Any],
    config: Mapping[str, Any],
    artifact_root: Path,
    metadata: Mapping[str, Any],
) -> None:
    """Bind an unsigned execution claim to real Git objects and candidate contents.

    The canonical writer remains trusted to report execution honestly. Missing Git
    objects or ignored fixtures make evidence unavailable; headers alone cannot
    replace them. Provenance is deliberately outside the candidate content ID.
    """
    root = Path(_git(artifact_root.resolve(), "rev-parse", "--show-toplevel"))
    source = evidence["source_provenance"]
    for field, expected_type in (
        ("commit", "commit"),
        ("pinned_worktree_commit", "commit"),
        ("pinned_worktree_tree", "tree"),
    ):
        if _git(root, "cat-file", "-t", source[field]) != expected_type:
            raise CheckError(f"check provenance {field} is not a Git {expected_type}")
    commit = _git(root, "cat-file", "commit", source["pinned_worktree_commit"])
    headers, _, message = commit.partition("\n\n")
    tree = [line[5:] for line in headers.splitlines() if line.startswith("tree ")]
    parents = [line[7:] for line in headers.splitlines() if line.startswith("parent ")]
    if tree != [source["pinned_worktree_tree"]] or parents != [source["commit"]]:
        raise CheckError("check pinned commit tree or source provenance mismatch")
    if message != f"Fuse candidate {candidate['candidate_id']}":
        raise CheckError("check pinned commit is not bound to this candidate")

    manifest = candidate["manifest"]
    verification = manifest["verification_inputs"]
    required_inputs = verification.get("required_inputs")
    policy = verification.get("effective_policy")
    if not isinstance(required_inputs, Mapping) or not isinstance(policy, Mapping):
        raise CheckError("candidate required inputs or policy are malformed")
    with _detached_worktree(root, source["pinned_worktree_commit"]) as worktree:
        _materialize_ignored_required_inputs(root, worktree, manifest)
        spec_dir = worktree / str(metadata["spec_dir"])
        try:
            actual_manifest = build_candidate_manifest(
                worktree, spec_dir, policy, config, required_inputs=required_inputs
            )
        except (OSError, ValueError) as error:
            raise CheckError(
                f"pinned candidate inputs are unavailable: {error}"
            ) from error
        if actual_manifest != manifest:
            raise CheckError("check pinned tree does not match candidate contents")
        if (
            _snapshot_tree(worktree, spec_dir, manifest)
            != source["pinned_worktree_tree"]
        ):
            raise CheckError(
                "check pinned tree is not the canonical candidate snapshot"
            )


def _validate_check_evidence_context(
    evidence: Mapping[str, Any],
    candidate: Mapping[str, Any],
    config: Mapping[str, Any],
    check_id: str,
    artifact_root: Path,
) -> None:
    _candidate_matches_config(candidate, config)
    if check_id not in config["checks"]:
        raise CheckError(f"unknown project check {check_id}")
    expected_config_digest = canonical_json_digest(config)
    expected_check_digest = canonical_json_digest(config["checks"][check_id])
    if evidence.get("config_digest") != expected_config_digest:
        raise CheckError(f"check {check_id} evidence configuration digest mismatch")
    if evidence.get("check_definition_digest") != expected_check_digest:
        raise CheckError(f"check {check_id} definition digest mismatch")
    if evidence.get("candidate_id") != candidate.get("candidate_id"):
        raise CheckError(f"check {check_id} evidence candidate mismatch")
    if not _has_check_provenance(evidence):
        raise CheckError(f"check {check_id} evidence provenance is invalid")
    if not _required_output_artifacts(evidence):
        raise CheckError(f"check {check_id} evidence has no required output artifact")
    if not evidence_available(evidence, artifact_root):
        raise CheckError(f"check {check_id} evidence has a missing required artifact")
    metadata = _artifact_execution_metadata(evidence, artifact_root, config)
    _validate_pinned_candidate(evidence, candidate, config, artifact_root, metadata)


def run_check(
    repository_root: Path,
    spec_dir: Path,
    config: Mapping[str, Any],
    record: dict[str, Any],
    candidate_id: str,
    check_id: str,
) -> CheckResult:
    """Run one configured check in a pinned worktree and append its evidence."""
    root = repository_root.expanduser().resolve()
    validate_run_record(record)
    try:
        validate_config(config)
    except ValueError as error:
        raise CheckError(f"project configuration is invalid: {error}") from error
    if check_id not in config["checks"]:
        raise CheckError(f"unknown project check {check_id}")
    candidate = next(
        (item for item in record["candidates"] if item["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise CheckError(f"candidate {candidate_id} is missing from the run record")
    verification_inputs = candidate["manifest"].get("verification_inputs", {})
    required_inputs = verification_inputs.get("required_inputs", {})
    if not isinstance(required_inputs, Mapping):
        raise CheckError("candidate required-input manifest is invalid")
    try:
        current_manifest = build_candidate_manifest(
            root,
            spec_dir,
            record["effective_policy_snapshot"],
            config,
            required_inputs=required_inputs.keys(),
        )
    except ValueError as error:
        raise CheckError(f"candidate is stale: {error}") from error
    if current_manifest != candidate["manifest"]:
        raise CheckError(f"candidate is stale before check {check_id}")

    readiness_record = dict(record)
    prior_check_evidence_ids = {
        evidence["id"]
        for evidence in record["evidence"]
        if (
            evidence["kind"] == "check"
            and evidence.get("check_id") == check_id
            and evidence.get("candidate_id") == candidate_id
        )
    }
    readiness_record["evidence"] = [
        evidence
        for evidence in record["evidence"]
        if evidence["id"] not in prior_check_evidence_ids
    ]
    readiness_record["findings"] = copy.deepcopy(record["findings"])
    for finding in readiness_record["findings"]:
        finding["evidence_ids"] = [
            evidence_id
            for evidence_id in finding["evidence_ids"]
            if evidence_id not in prior_check_evidence_ids
        ]
        for entry in finding["history"]:
            entry["evidence_ids"] = [
                evidence_id
                for evidence_id in entry["evidence_ids"]
                if evidence_id not in prior_check_evidence_ids
            ]
    artifact_root = root / ".fuse" / "runs" / record["run_id"]
    if check_id not in ready_check_ids(
        config, [check_id], readiness_record, candidate_id, artifact_root
    ):
        raise CheckError(
            f"check {check_id} is not ready because a prerequisite is not passing"
        )

    check = config["checks"][check_id]
    execution_limits = config["limits"]["checks"]
    timeout_seconds = execution_limits["timeout_seconds"]
    max_output_bytes_per_stream = execution_limits["max_output_bytes_per_stream"]
    configured_command = list(check["command"])
    command = list(configured_command)
    if check["pass_criteria"] != "exit_code == 0":
        source = source_provenance(root)
        evidence = _record_result(
            record,
            candidate_id,
            check_id,
            "unavailable",
            "unsupported pass criteria; only 'exit_code == 0' is executable",
            source,
            {},
            config,
        )
        return {
            "check_id": check_id,
            "candidate_id": candidate_id,
            "outcome": "unavailable",
            "exit_code": None,
            "summary": evidence["summary"],
            "evidence_id": evidence["id"],
        }

    source = source_provenance(root)
    try:
        relative_spec = spec_dir.expanduser().resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise CheckError("approved spec directory is outside the repository") from error
    spec_argument = relative_spec.as_posix()
    command = [argument.replace("{spec_dir}", spec_argument) for argument in command]

    with _pinned_worktree(
        root,
        spec_dir,
        candidate["manifest"],
        str(source["commit"]),
        candidate_id,
    ) as (check_root, pinned_commit, pinned_tree):
        check_spec_dir = check_root / relative_spec
        try:
            pinned_manifest = build_candidate_manifest(
                check_root,
                check_spec_dir,
                record["effective_policy_snapshot"],
                config,
                required_inputs=required_inputs.keys(),
            )
        except ValueError as error:
            raise CheckError(f"candidate snapshot is invalid: {error}") from error
        if pinned_manifest != candidate["manifest"]:
            raise CheckError(f"candidate snapshot does not match {candidate_id}")

        pinned_source = dict(source)
        pinned_source["pinned_worktree_commit"] = pinned_commit
        pinned_source["pinned_worktree_tree"] = pinned_tree
        preparation = prepare_toolchain(check_root, config)
        tool_versions = dict(preparation["tool_versions"])
        if not preparation["ready"]:
            evidence = _record_result(
                record,
                candidate_id,
                check_id,
                "unavailable",
                preparation["summary"],
                pinned_source,
                tool_versions,
                config,
            )
            return {
                "check_id": check_id,
                "candidate_id": candidate_id,
                "outcome": "unavailable",
                "exit_code": None,
                "summary": evidence["summary"],
                "evidence_id": evidence["id"],
            }

        try:
            prepared_manifest = build_candidate_manifest(
                check_root,
                check_spec_dir,
                record["effective_policy_snapshot"],
                config,
                required_inputs=required_inputs.keys(),
            )
        except ValueError:
            prepared_manifest = None
        if prepared_manifest != candidate["manifest"]:
            evidence = _record_result(
                record,
                candidate_id,
                check_id,
                "inconclusive",
                "candidate snapshot changed during toolchain preparation",
                pinned_source,
                tool_versions,
                config,
            )
            return {
                "check_id": check_id,
                "candidate_id": candidate_id,
                "outcome": "inconclusive",
                "exit_code": None,
                "summary": evidence["summary"],
                "evidence_id": evidence["id"],
            }

        tool_versions["check_command"] = _check_tool_version(
            command, str(config["toolchain"]["package_manager"]), check_root
        )
        try:
            completed = _run_bounded_check_command(
                command,
                check_root,
                timeout_seconds=timeout_seconds,
                max_output_bytes_per_stream=max_output_bytes_per_stream,
            )
        except (OSError, CheckError) as error:
            evidence = _record_result(
                record,
                candidate_id,
                check_id,
                "unavailable",
                f"configured command is unavailable: {error}",
                pinned_source,
                tool_versions,
                config,
            )
            return {
                "check_id": check_id,
                "candidate_id": candidate_id,
                "outcome": "unavailable",
                "exit_code": None,
                "summary": evidence["summary"],
                "evidence_id": evidence["id"],
            }

        try:
            after_manifest = build_candidate_manifest(
                check_root,
                check_spec_dir,
                record["effective_policy_snapshot"],
                config,
                required_inputs=required_inputs.keys(),
            )
        except ValueError:
            after_manifest = None
        candidate_changed = (
            after_manifest != candidate["manifest"]
            or _git(check_root, "rev-parse", "HEAD") != pinned_commit
        )
        if (
            candidate_changed
            or completed["timed_out"]
            or completed["capture_incomplete"]
        ):
            outcome = "inconclusive"
        elif completed["returncode"] == 0:
            outcome = "pass"
        else:
            outcome = "fail"

        if candidate_changed:
            summary = f"candidate snapshot changed while check {check_id} was running"
        elif completed["timed_out"]:
            summary = f"check {check_id} timed out after {timeout_seconds} seconds"
        elif completed["capture_incomplete"]:
            summary = f"check {check_id} output capture did not complete"
        else:
            summary = f"check {check_id} exited with code {completed['returncode']}"
        metadata = {
            "candidate_id": candidate_id,
            "check_id": check_id,
            "configured_command": configured_command,
            "executed_command": command,
            "config_digest": canonical_json_digest(config),
            "check_definition_digest": canonical_json_digest(check),
            "source_commit": source["commit"],
            "pinned_commit": pinned_commit,
            "pinned_tree": pinned_tree,
            "spec_dir": relative_spec.as_posix(),
            "exit_code": completed["returncode"],
            "timeout_seconds": timeout_seconds,
            "max_output_bytes_per_stream": max_output_bytes_per_stream,
            "timed_out": completed["timed_out"],
            "capture_incomplete": completed["capture_incomplete"],
            "lingering_output_process": completed["lingering_output_process"],
            "stdout_truncated": completed["stdout_truncated"],
            "stderr_truncated": completed["stderr_truncated"],
        }
        output = (
            OUTPUT_METADATA_PREFIX
            + json.dumps(metadata, sort_keys=True)
            + "\n[stdout]\n"
            + completed["stdout"]
        )
        if completed["stdout_truncated"]:
            output += f"\n[stdout truncated at {max_output_bytes_per_stream} bytes]"
        if completed["stderr"]:
            output += f"\n[stderr]\n{completed['stderr']}"
        if completed["stderr_truncated"]:
            output += f"\n[stderr truncated at {max_output_bytes_per_stream} bytes]"
        output_bytes = output.encode("utf-8", errors="replace")
        try:
            artifact = _write_output_artifact(
                root, record["run_id"], candidate_id, check_id, output_bytes
            )
        except OSError as error:
            evidence = _record_result(
                record,
                candidate_id,
                check_id,
                "unavailable",
                f"cannot persist required check output: {error}",
                pinned_source,
                tool_versions,
                config,
            )
            return {
                "check_id": check_id,
                "candidate_id": candidate_id,
                "outcome": "unavailable",
                "exit_code": completed["returncode"],
                "summary": evidence["summary"],
                "evidence_id": evidence["id"],
            }

        evidence = _record_result(
            record,
            candidate_id,
            check_id,
            outcome,
            summary,
            pinned_source,
            tool_versions,
            config,
            artifacts=[artifact],
        )
        return {
            "check_id": check_id,
            "candidate_id": candidate_id,
            "outcome": outcome,
            "exit_code": completed["returncode"],
            "summary": summary,
            "evidence_id": evidence["id"],
        }


def ready_check_ids(
    config: Mapping[str, Any],
    applicable: Iterable[str],
    record: Mapping[str, Any],
    candidate_id: str,
    artifact_root: Path,
) -> list[str]:
    """Return applicable checks with current candidate-matched prerequisites."""
    validate_run_record(record)
    candidate = next(
        (item for item in record["candidates"] if item["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise CheckError(f"candidate {candidate_id} is stale or missing")
    _candidate_matches_config(candidate, config)
    checks = config["checks"]
    applicable_checks = list(applicable)
    graph = applicable_check_graph(checks, applicable_checks)
    completed: set[str] = set()
    blocked: set[str] = set()
    latest: dict[str, Mapping[str, Any]] = {}
    for evidence in record["evidence"]:
        if (
            evidence["kind"] == "check"
            and evidence.get("candidate_id") == candidate_id
            and evidence.get("check_id") in graph
        ):
            latest[str(evidence["check_id"])] = evidence
    for check_id, evidence in latest.items():
        try:
            _validate_check_evidence_context(
                evidence, candidate, config, check_id, artifact_root
            )
        except CheckError:
            blocked.add(check_id)
            continue
        if evidence["outcome"] == "pass":
            completed.add(check_id)
        else:
            blocked.add(check_id)
    return graph_ready_check_ids(
        checks,
        applicable_checks,
        completed=completed,
        blocked=blocked,
    )


def require_check_evidence(
    record: Mapping[str, Any],
    check_id: str,
    candidate_id: str,
    artifact_root: Path,
    config: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Require the latest check result to pass for the exact candidate."""
    validate_run_record(record)
    candidates = record["candidates"]
    candidate_ids = {item["candidate_id"] for item in candidates}
    if candidate_id not in candidate_ids:
        raise CheckError(f"candidate {candidate_id} is stale or missing")
    candidate = next(
        item for item in candidates if item["candidate_id"] == candidate_id
    )
    all_check_evidence = [
        evidence
        for evidence in record["evidence"]
        if evidence["kind"] == "check" and evidence.get("check_id") == check_id
    ]
    if all_check_evidence and all_check_evidence[-1]["candidate_id"] != candidate_id:
        raise CheckError(f"check {check_id} evidence is for a stale candidate")
    matching = [
        evidence
        for evidence in all_check_evidence
        if evidence["candidate_id"] == candidate_id
    ]
    if not matching:
        if all_check_evidence:
            raise CheckError(f"check {check_id} evidence is for a stale candidate")
        raise CheckError(f"check {check_id} evidence is missing")
    evidence = matching[-1]
    if evidence["outcome"] != "pass":
        raise CheckError(f"check {check_id} evidence is not passing")
    _validate_check_evidence_context(
        evidence, candidate, config, check_id, artifact_root
    )
    return cast(Mapping[str, Any], evidence)
