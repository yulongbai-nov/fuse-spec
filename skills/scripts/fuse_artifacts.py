#!/usr/bin/env python3
"""Confine run artifacts to their repository-owned run directory."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
from typing import Any


class ArtifactError(ValueError):
    """An artifact path is invalid or cannot be safely created."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def write_run_artifact(
    repository_root: Path,
    run_id: str,
    relative_path: Path,
    content: bytes,
) -> dict[str, Any]:
    """Create one exclusive artifact after resolving its path within the run."""
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ArtifactError(
            "artifact_path_invalid",
            "artifact path must be relative to the run directory",
        )
    root = repository_root.expanduser().resolve(strict=True)
    run_root = (root / ".fuse" / "runs" / run_id).resolve(strict=False)
    try:
        run_root.relative_to(root)
        (run_root / relative_path).resolve(strict=False).relative_to(run_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ArtifactError(
            "artifact_path_escape", "artifact path resolves outside the run directory"
        ) from error

    destination = run_root / relative_path
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.resolve(strict=False).relative_to(run_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ArtifactError(
            "artifact_path_escape", "artifact path resolves outside the run directory"
        ) from error

    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        if created:
            destination.unlink(missing_ok=True)
        if error.errno == errno.EEXIST:
            raise ArtifactError(
                "artifact_collision", "artifact path already exists"
            ) from error
        raise ArtifactError(
            "artifact_write_failed", f"cannot safely create artifact: {error}"
        ) from error

    return {
        "path": relative_path.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "required": True,
    }
