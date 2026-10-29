#!/usr/bin/env python3
"""Resolve consumer paths before entering Fuse's locked uv environment."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn


class LaunchArgumentError(ValueError):
    """Raised for invalid launcher arguments."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise LaunchArgumentError(message)


def resolve_roots(
    initial_cwd: Path, repo_root: str | None, spec_dir: str
) -> tuple[Path, Path]:
    """Resolve a relative repository from startup cwd and spec from that repo."""
    start = initial_cwd.expanduser().resolve()
    requested_root = Path(repo_root).expanduser() if repo_root else start
    root = requested_root if requested_root.is_absolute() else start / requested_root
    root = root.resolve(strict=True)
    requested_spec = Path(spec_dir).expanduser()
    spec = requested_spec if requested_spec.is_absolute() else root / requested_spec
    spec = spec.resolve(strict=True)
    if not root.is_dir() or not spec.is_dir():
        raise LaunchArgumentError("repository root and spec directory must exist")
    try:
        spec.relative_to(root)
    except ValueError as error:
        raise LaunchArgumentError(
            "spec directory must be inside the repository"
        ) from error
    return root, spec


def _emit_error(reason: str, message: str) -> None:
    print(
        json.dumps(
            {"ok": False, "reason": reason, "message": message},
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Fuse CLI with absolute paths from its locked plugin environment."""
    initial_cwd = Path.cwd()
    parser = _Parser(add_help=False)
    parser.add_argument("--repo-root")
    parser.add_argument("--spec-dir", required=True)
    try:
        path_args, cli_args = parser.parse_known_args(argv)
        root, spec = resolve_roots(initial_cwd, path_args.repo_root, path_args.spec_dir)
    except LaunchArgumentError as error:
        _emit_error("launcher_arguments_invalid", str(error))
        return 2
    except (OSError, RuntimeError) as error:
        _emit_error("project_path_invalid", str(error))
        return 2

    uv = shutil.which("uv")
    if uv is None:
        _emit_error("uv_unavailable", "uv is required to run Fuse's locked environment")
        return 2
    plugin_root = Path(__file__).resolve().parents[2]
    command = [
        uv,
        "run",
        "--project",
        str(plugin_root),
        "--directory",
        str(plugin_root),
        "--locked",
        "python",
        "-m",
        "skills.scripts.fuse_cli",
        *cli_args,
        "--repo-root",
        str(root),
        "--spec-dir",
        str(spec),
    ]
    try:
        return subprocess.run(command, cwd=plugin_root, check=False).returncode
    except OSError as error:
        _emit_error("uv_launch_failed", str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
