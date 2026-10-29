#!/usr/bin/env python3
"""Build candidate manifests from source, approved specs, policy, and checks."""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fuse_config import validate_config
    from fuse_markdown import markdown_fence_line_flags, task_checkbox_row_match
    from fuse_record import add_candidate, validate_run_record
else:
    try:
        from .fuse_config import validate_config
        from .fuse_markdown import markdown_fence_line_flags, task_checkbox_row_match
        from .fuse_record import add_candidate, validate_run_record
    except ImportError:
        from fuse_config import validate_config
        from fuse_markdown import markdown_fence_line_flags, task_checkbox_row_match
        from fuse_record import add_candidate, validate_run_record

STATE_PATH_PREFIX = PurePosixPath(".fuse/runs")


class CandidateError(ValueError):
    """Raised when an immutable candidate cannot be captured safely."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_task_checkboxes(markdown: str) -> str:
    """Normalize declared task progress while preserving all approved task text."""
    output: list[str] = []
    lines = markdown.splitlines(keepends=True)
    for line, in_fence in zip(lines, markdown_fence_line_flags(lines), strict=True):
        if in_fence:
            output.append(line)
        else:
            checkbox_row = task_checkbox_row_match(line)
            if checkbox_row is not None and checkbox_row.group("status").lower() == "x":
                status_start = checkbox_row.start("status")
                status_end = checkbox_row.end("status")
                output.append(line[:status_start] + " " + line[status_end:])
            else:
                output.append(line)
    return "".join(output)


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CandidateError(f"git {' '.join(args)} failed: {detail}")
    return result


def _repository_paths(root: Path) -> list[str]:
    result = _run_git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    paths = [os.fsdecode(item) for item in result.stdout.split(b"\0") if item]
    return sorted(set(paths))


def _is_excluded_source_path(path: PurePosixPath) -> bool:
    return bool(path.parts) and (
        path.parts[0] == ".specs"
        or path.parts[: len(STATE_PATH_PREFIX.parts)] == STATE_PATH_PREFIX.parts
    )


def _source_tree(root: Path) -> dict[str, dict[str, Any]]:
    tree: dict[str, dict[str, Any]] = {}
    for raw_path in _repository_paths(root):
        relative = PurePosixPath(raw_path)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or _is_excluded_source_path(relative)
        ):
            continue
        file_path = root.joinpath(*relative.parts)
        try:
            metadata = file_path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            try:
                file_path.resolve(strict=False).relative_to(root)
            except (RuntimeError, ValueError) as error:
                raise CandidateError(
                    f"source symlink escapes repository: {relative.as_posix()}"
                ) from error
            target = os.readlink(file_path).encode("utf-8", errors="surrogateescape")
            tree[relative.as_posix()] = {
                "kind": "symlink",
                "target_sha256": _sha256(target),
            }
        elif stat.S_ISREG(metadata.st_mode):
            try:
                content = file_path.read_bytes()
            except OSError as error:
                raise CandidateError(
                    f"cannot read source input {relative.as_posix()}: {error}"
                ) from error
            tree[relative.as_posix()] = {
                "kind": "file",
                "sha256": _sha256(content),
                "executable": bool(metadata.st_mode & stat.S_IXUSR),
            }
        elif stat.S_ISDIR(metadata.st_mode):
            tree[relative.as_posix()] = {"kind": "directory"}
    return tree


def _approved_spec_inputs(spec_root: Path, repository_root: Path) -> dict[str, str]:
    requirement_name = next(
        (
            name
            for name in ("requirements.md", "bugfix.md")
            if (spec_root / name).is_file()
        ),
        None,
    )
    if requirement_name is None:
        raise CandidateError("approved spec is missing requirements.md or bugfix.md")
    file_names = (requirement_name, "design.md", "tasks.md")
    inputs: dict[str, str] = {}
    for name in file_names:
        path = spec_root / name
        try:
            path.resolve(strict=True).relative_to(repository_root)
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, RuntimeError, ValueError) as error:
            raise CandidateError(
                f"cannot read approved spec input {name}: {error}"
            ) from error
        if not text.strip():
            raise CandidateError(f"approved spec input {name} is empty")
        if name == "tasks.md":
            text = normalize_task_checkboxes(text)
        inputs[name] = _sha256(text.encode("utf-8"))
    return inputs


def _required_input_digests(
    root: Path, required_inputs: Iterable[str]
) -> dict[str, dict[str, Any]]:
    names = list(required_inputs)
    if len(names) != len(set(names)):
        raise CandidateError("duplicate required input path")
    digests: dict[str, dict[str, Any]] = {}
    for raw_name in sorted(names):
        relative = PurePosixPath(raw_name)
        if (
            not raw_name.strip()
            or relative.is_absolute()
            or ".." in relative.parts
            or "\\" in raw_name
        ):
            raise CandidateError("required inputs must be repository-relative paths")
        path = root.joinpath(*relative.parts)
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file():
                raise OSError("not a file")
            content = resolved.read_bytes()
            executable = bool(resolved.stat().st_mode & stat.S_IXUSR)
        except (OSError, RuntimeError, ValueError) as error:
            raise CandidateError(
                f"missing required input {raw_name}: {error}"
            ) from error
        digests[relative.as_posix()] = {
            "sha256": _sha256(content),
            "executable": executable,
        }
    return digests


def source_provenance(repository_root: Path) -> dict[str, Any]:
    """Capture Git location metadata separately from candidate content identity."""
    root = repository_root.expanduser().resolve()
    commit = _run_git(root, "rev-parse", "HEAD").stdout.decode().strip()
    branch_result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    status = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    return {
        "commit": commit,
        "branch": (
            branch_result.stdout.decode("utf-8", errors="replace").strip()
            if branch_result.returncode == 0
            else None
        ),
        "worktree_dirty": bool(status.stdout.strip()),
    }


def build_candidate_manifest(
    repository_root: Path,
    spec_dir: Path,
    effective_policy_snapshot: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    required_inputs: Iterable[str] = (),
) -> dict[str, Any]:
    """Build normalized verification inputs without recording runtime metadata."""
    root = repository_root.expanduser().resolve()
    if not root.is_dir():
        raise CandidateError(f"repository root is not a directory: {root}")
    spec_root = spec_dir.expanduser().resolve()
    try:
        spec_root.relative_to(root)
    except ValueError as error:
        raise CandidateError(
            "approved spec directory must be inside the repository"
        ) from error
    if not spec_root.is_dir():
        raise CandidateError(f"approved spec directory is not a directory: {spec_root}")
    try:
        validate_config(config)
    except ValueError as error:
        raise CandidateError(f"project configuration is invalid: {error}") from error

    toolchain = config.get("toolchain")
    checks = config.get("checks")
    if not isinstance(toolchain, Mapping) or not isinstance(checks, Mapping):
        raise CandidateError(
            "project configuration has no toolchain or check definitions"
        )
    verification_inputs = {
        "source_tree": _source_tree(root),
        "approved_spec": _approved_spec_inputs(spec_root, root),
        "effective_policy": copy.deepcopy(dict(effective_policy_snapshot)),
        "check_definitions": copy.deepcopy(dict(checks)),
        "check_execution_limits": copy.deepcopy(dict(config["limits"]["checks"])),
        "toolchain": copy.deepcopy(dict(toolchain)),
        "required_inputs": _required_input_digests(root, required_inputs),
    }
    return {"verification_inputs": verification_inputs}


def capture_candidate(
    record: dict[str, Any],
    repository_root: Path,
    spec_dir: Path,
    config: Mapping[str, Any],
    *,
    required_inputs: Iterable[str] = (),
) -> dict[str, Any]:
    """Add a T3 candidate with content identity and separate Git provenance."""
    validate_run_record(record)
    manifest = build_candidate_manifest(
        repository_root,
        spec_dir,
        record["effective_policy_snapshot"],
        config,
        required_inputs=required_inputs,
    )
    return add_candidate(record, manifest, source_provenance(repository_root))
