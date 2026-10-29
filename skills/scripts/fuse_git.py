#!/usr/bin/env python3
"""Small Git primitives for serialized, fast-forward-only Fuse publication."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows hosts only
    fcntl = None  # type: ignore[assignment]

COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


class GitError(RuntimeError):
    """A Git operation failed or could not be proven safe."""


class GitConflict(GitError):
    """A branch moved or a candidate cannot be integrated without conflict."""


@dataclass(frozen=True)
class MergeCandidate:
    """An immutable synthetic merge commit and the refs used to build it."""

    base_sha: str
    head_shas: tuple[str, ...]
    commit_sha: str


def _git(
    root: Path,
    *arguments: str,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=command_env,
        capture_output=True,
        check=False,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise GitError(f"git {' '.join(arguments)} failed: {detail}")
    return result


def resolve_commit(root: Path, revision: str) -> str:
    """Resolve a local commit to a full immutable object ID."""
    if not isinstance(revision, str) or not revision.strip():
        raise GitError("commit revision is required")
    result = _git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    commit = result.stdout.strip()
    if not COMMIT_RE.fullmatch(commit):
        raise GitError(f"Git returned an invalid commit ID for {revision!r}")
    return commit


def validate_branch_name(root: Path, branch: str) -> str:
    """Validate a branch name before using it in a refspec."""
    if not isinstance(branch, str) or not branch or branch.startswith("-"):
        raise GitError("branch name is required")
    result = _git(root, "check-ref-format", "--branch", branch, check=False)
    if result.returncode != 0:
        raise GitError(f"invalid branch name: {branch!r}")
    return branch


def remote_branch_head(root: Path, remote: str, branch: str) -> str | None:
    """Read a remote branch tip without changing local refs."""
    validate_branch_name(root, branch)
    if not remote or remote.startswith("-"):
        raise GitError("remote is required")
    result = _git(
        root,
        "ls-remote",
        "--heads",
        remote,
        f"refs/heads/{branch}",
    )
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 2:
        raise GitError(f"remote returned an invalid ref result for {branch!r}")
    commit = rows[0][0]
    if not COMMIT_RE.fullmatch(commit) or rows[0][1] != f"refs/heads/{branch}":
        raise GitError(f"remote returned an invalid ref result for {branch!r}")
    return commit


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    """Return whether two known local commits are ordered by ancestry."""
    first = resolve_commit(root, ancestor)
    second = resolve_commit(root, descendant)
    result = _git(root, "merge-base", "--is-ancestor", first, second, check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or result.stdout.strip()
    raise GitError(f"could not compare commit ancestry: {detail}")


def validate_atomic_job_commit(
    root: Path,
    commit_sha: str,
    expected_parent_sha: str,
) -> str:
    """Require one task/job commit directly atop the destination PR head."""
    commit = resolve_commit(root, commit_sha)
    expected_parent = resolve_commit(root, expected_parent_sha)
    output = _git(root, "rev-list", "--parents", "-n", "1", commit).stdout.split()
    if len(output) != 2 or output[0] != commit or output[1] != expected_parent:
        raise GitConflict(
            "task-scoped commit must have exactly the expected destination head "
            "as its single parent"
        )
    return commit


def _common_git_dir(root: Path) -> Path:
    result = _git(root, "rev-parse", "--git-common-dir")
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise GitError(f"cannot resolve Git common directory: {error}") from error
    if not resolved.is_dir():
        raise GitError("Git common directory is not a directory")
    return resolved


@contextmanager
def serialized_branch_publish(root: Path, branch: str) -> Iterator[None]:
    """Serialize this process and other local processes publishing one branch."""
    validate_branch_name(root, branch)
    git_dir = _common_git_dir(root)
    identity = f"{git_dir}\0{branch}"
    with _thread_locks_guard:
        lock = _thread_locks.setdefault(identity, threading.Lock())
    with lock:
        if fcntl is None:
            raise GitError("cross-process publication lock is unavailable")
        lock_path = (
            git_dir
            / f"fuse-publish-{hashlib.sha256(identity.encode()).hexdigest()}.lock"
        )
        try:
            with lock_path.open("a+b") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise GitError(
                f"cannot acquire branch publication lock: {error}"
            ) from error


def push_fast_forward(
    root: Path,
    remote: str,
    branch: str,
    commit_sha: str,
    *,
    expected_remote_head: str | None,
    after_push: Callable[[str], None] | None = None,
) -> str:
    """Publish a task-scoped commit with an exact precondition and no force push.

    ``after_push`` runs while the branch lock is still held. Callers use it to
    checkpoint the canonical run record before another code job can publish.
    If checkpointing fails, the branch remains pushed and the error is raised so
    resume must reconcile the known remote head before releasing dependencies.
    """
    branch = validate_branch_name(root, branch)
    commit = resolve_commit(root, commit_sha)
    expected = (
        resolve_commit(root, expected_remote_head)
        if expected_remote_head is not None
        else None
    )
    with serialized_branch_publish(root, branch):
        current = remote_branch_head(root, remote, branch)
        if current != expected:
            raise GitConflict(
                f"remote branch {branch!r} moved: expected {expected}, found {current}"
            )
        if expected is not None and not is_ancestor(root, expected, commit):
            raise GitConflict(f"commit {commit} is not a fast-forward from {expected}")
        _git(
            root,
            "push",
            "--porcelain",
            remote,
            f"{commit}:refs/heads/{branch}",
        )
        published = remote_branch_head(root, remote, branch)
        if published != commit:
            raise GitConflict(
                f"remote branch {branch!r} does not point to published commit {commit}"
            )
        if after_push is not None:
            after_push(commit)
    return commit


def materialize_merge_candidate(
    root: Path,
    base_sha: str,
    head_shas: Sequence[str],
) -> MergeCandidate:
    """Build a temporary merge commit for base plus ordered PR heads.

    The repository's branches and worktree are not changed. Conflicts fail
    closed and the temporary worktree is always removed.
    """
    base = resolve_commit(root, base_sha)
    heads = tuple(resolve_commit(root, item) for item in head_shas)
    if len(heads) != len(set(heads)):
        raise GitError("merge candidate contains duplicate head commits")
    with tempfile.TemporaryDirectory(prefix="fuse-merge-candidate-") as temporary:
        worktree = Path(temporary) / "candidate"
        _git(root, "worktree", "add", "--detach", str(worktree), base)
        current = base
        commit_env = {
            "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "Fuse Candidate"),
            "GIT_AUTHOR_EMAIL": os.environ.get(
                "GIT_AUTHOR_EMAIL", "fuse-candidate@example.invalid"
            ),
            "GIT_COMMITTER_NAME": os.environ.get(
                "GIT_COMMITTER_NAME", "Fuse Candidate"
            ),
            "GIT_COMMITTER_EMAIL": os.environ.get(
                "GIT_COMMITTER_EMAIL", "fuse-candidate@example.invalid"
            ),
        }
        try:
            for head in heads:
                if is_ancestor(root, head, current):
                    continue
                merge = _git(
                    worktree,
                    "merge",
                    "--no-commit",
                    "--no-ff",
                    head,
                    check=False,
                )
                if merge.returncode != 0:
                    _git(worktree, "merge", "--abort", check=False)
                    detail = merge.stderr.strip() or merge.stdout.strip()
                    raise GitConflict(
                        f"merge candidate conflicts at head {head}: {detail}"
                    )
                tree = _git(worktree, "write-tree").stdout.strip()
                merged_commit = _git(
                    worktree,
                    "commit-tree",
                    tree,
                    "-p",
                    current,
                    "-p",
                    head,
                    "-m",
                    "Fuse exact merge candidate",
                    env=commit_env,
                ).stdout.strip()
                current = resolve_commit(root, merged_commit)
                _git(worktree, "reset", "--hard", current)
        finally:
            _git(root, "worktree", "remove", "--force", str(worktree), check=False)
    return MergeCandidate(base, heads, current)
