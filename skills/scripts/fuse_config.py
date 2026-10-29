#!/usr/bin/env python3
"""Initialize and validate Fuse's project policy configuration."""

from __future__ import annotations

import argparse
import copy
import json
import math
import shutil
import sys
import tomllib
from collections.abc import Mapping, Sequence
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


class ConfigError(ValueError):
    """Raised when project configuration cannot be loaded or validated."""


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_config_path() -> Path:
    return _plugin_root() / ".fuse" / "config.toml"


def _default_schema_path() -> Path:
    return _plugin_root() / "schemas" / "project-config.schema.json"


def _read_schema(schema_path: Path | None) -> dict[str, Any]:
    path = schema_path or _default_schema_path()
    try:
        schema_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(
            f"cannot read configuration schema {path}: {error}"
        ) from error
    if not isinstance(schema_value, dict):
        raise ConfigError(f"configuration schema is not an object: {path}")
    try:
        Draft202012Validator.check_schema(schema_value)
    except SchemaError as error:
        raise ConfigError(
            f"invalid configuration schema {path}: {error.message}"
        ) from error
    return schema_value


def _format_schema_error(error: ValidationError) -> str:
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"


def _has_cycle(graph: Mapping[str, list[str]]) -> bool:
    try:
        TopologicalSorter(graph).prepare()
    except CycleError:
        return True
    return False


def _get_config_path(config: Mapping[str, Any], dotted_path: str) -> Any:
    value: Any = config
    for part in dotted_path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise KeyError(dotted_path)
        value = value[part]
    return value


def _semantic_errors(config: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []

    roles = config["roles"]
    role_ids = set(roles)
    fallback_graph: dict[str, list[str]] = {}
    for role_id, profile in roles.items():
        fallback_roles = profile["fallback_roles"]
        fallback_policy = profile["unavailable_policy"]
        if fallback_policy == "fallback" and not fallback_roles:
            errors.append(
                f"role {role_id} selects fallback policy but has no fallback_roles"
            )
        if fallback_policy == "block" and fallback_roles:
            errors.append(
                f"role {role_id} lists fallback_roles while unavailable_policy is block"
            )
        valid_targets: list[str] = []
        for fallback_role in fallback_roles:
            if fallback_role not in role_ids:
                errors.append(
                    f"role {role_id} has unknown fallback role {fallback_role}"
                )
            elif fallback_role == role_id:
                errors.append(f"role {role_id} cannot fall back to itself")
            else:
                valid_targets.append(fallback_role)
        fallback_graph[role_id] = valid_targets
    if _has_cycle(fallback_graph):
        errors.append("role fallback graph contains a cycle")

    checks = config["checks"]
    check_ids = set(checks)
    check_graph: dict[str, list[str]] = {}
    for check_id, check in checks.items():
        valid_prerequisites: list[str] = []
        for prerequisite in check["prerequisites"]:
            if prerequisite not in check_ids:
                errors.append(
                    f"check {check_id} references unknown prerequisite {prerequisite}"
                )
            else:
                valid_prerequisites.append(prerequisite)
        check_graph[check_id] = valid_prerequisites
    if _has_cycle(check_graph):
        errors.append("check prerequisite graph contains a cycle")

    overrides = config["overrides"]
    allowed_fields = set(overrides["allowed_fields"])
    bounds = overrides["bounds"]
    bound_fields = set(bounds)
    for field in sorted(allowed_fields - bound_fields):
        errors.append(f"allowed override {field} has no configured bounds")
    for field in sorted(bound_fields - allowed_fields):
        errors.append(f"override bounds for {field} are not allowed")
    for field in sorted(allowed_fields & bound_fields):
        try:
            current_value = _get_config_path(config, field)
        except KeyError:
            errors.append(
                f"allowed override {field} does not name a configuration field"
            )
            continue
        field_bounds = bounds[field]
        minimum = field_bounds.get("minimum")
        maximum = field_bounds.get("maximum")
        value_type = field_bounds["value_type"]
        if minimum is None and maximum is None:
            errors.append(f"override {field} must define a minimum or maximum bound")
            continue
        if minimum is not None and maximum is not None and minimum > maximum:
            errors.append(f"override {field} has a minimum greater than its maximum")
            continue
        if current_value == "unset":
            continue
        if value_type == "integer" and type(current_value) is not int:
            errors.append(
                f"override {field} must target an integer configuration value"
            )
            continue
        if value_type == "number" and (
            isinstance(current_value, bool)
            or not isinstance(current_value, (int, float))
        ):
            errors.append(f"override {field} must target a numeric configuration value")
            continue
        if isinstance(current_value, float) and not math.isfinite(current_value):
            errors.append(f"configured value for {field} must be finite")
            continue
        if minimum is not None and current_value < minimum:
            errors.append(f"configured value for {field} is below its override minimum")
        if maximum is not None and current_value > maximum:
            errors.append(f"configured value for {field} exceeds its override maximum")
    return errors


def validate_config(config: Mapping[str, Any], schema_path: Path | None = None) -> None:
    """Validate JSON Schema shape and semantic cross-references."""
    schema = _read_schema(schema_path)
    validator = Draft202012Validator(schema)
    schema_errors = sorted(
        validator.iter_errors(dict(config)),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if schema_errors:
        details = "; ".join(_format_schema_error(error) for error in schema_errors)
        raise ConfigError(f"configuration schema validation failed: {details}")

    errors = _semantic_errors(config)
    if errors:
        raise ConfigError("configuration validation failed: " + "; ".join(errors))


def load_config(path: Path, schema_path: Path | None = None) -> dict[str, Any]:
    """Load TOML and validate it against the versioned project schema."""
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"invalid TOML in {path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"cannot read configuration {path}: {error}") from error
    validate_config(config, schema_path)
    return config


def validate_overrides(
    config: Mapping[str, Any], overrides: Mapping[str, object]
) -> None:
    """Reject run overrides that are not explicitly allowed and bounded."""
    policy = config["overrides"]
    allowed_fields = set(policy["allowed_fields"])
    bounds = policy["bounds"]
    for field, value in overrides.items():
        if field not in allowed_fields:
            raise ConfigError(
                f"override field {field} is not allowed by project policy"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"override {field} must be numeric")
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"override {field} must be finite")
        field_bounds = bounds[field]
        value_type = field_bounds["value_type"]
        if value_type == "integer" and type(value) is not int:
            raise ConfigError(f"override {field} must be an integer")
        minimum = field_bounds.get("minimum")
        maximum = field_bounds.get("maximum")
        if minimum is not None and value < minimum:
            raise ConfigError(f"override {field} is below its configured bound")
        if maximum is not None and value > maximum:
            raise ConfigError(f"override {field} exceeds its configured bound")
    effective_config = copy.deepcopy(dict(config))
    for field, value in overrides.items():
        path = field.split(".")
        target: dict[str, Any] = effective_config
        for part in path[:-1]:
            nested = target.get(part)
            if not isinstance(nested, dict):
                raise ConfigError(
                    f"override {field} does not name a configuration field"
                )
            target = nested
        target[path[-1]] = value
    validate_config(effective_config)


def _require_project_file(root: Path, value: str, label: str) -> Path:
    relative_path = Path(value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ConfigError(
            f"toolchain assessment/config required: {label} must be inside the project"
        )
    try:
        path = (root / relative_path).resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise ConfigError(
            f"toolchain assessment/config required: {label} is missing or outside "
            "the project"
        ) from error
    if not path.is_file():
        raise ConfigError(
            f"toolchain assessment/config required: {label} is not a file"
        )
    return path


def _python_selector_satisfies(
    selected_version: Version, constraint: SpecifierSet
) -> bool:
    """Check a uv major/minor selector against the stable patch versions it selects."""
    release = selected_version.release
    if (
        len(release) != 2
        or selected_version.is_prerelease
        or selected_version.is_postrelease
        or selected_version.is_devrelease
        or selected_version.local is not None
    ):
        return constraint.contains(selected_version, prereleases=True)

    major, minor = release
    candidate_patches = {0}
    for specifier in constraint:
        boundary_text = specifier.version.removesuffix(".*")
        try:
            boundary = Version(boundary_text)
        except InvalidVersion:
            continue
        if boundary.release[:2] != (major, minor):
            continue
        patch = boundary.release[2] if len(boundary.release) > 2 else 0
        candidate_patches.update(max(0, patch + offset) for offset in (-1, 0, 1))

    return any(
        constraint.contains(Version(f"{major}.{minor}.{patch}"), prereleases=True)
        for patch in candidate_patches
    )


def _validate_python_uv_stack(
    manifest_path: Path, lockfile_path: Path, runtime_version: str
) -> None:
    try:
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        lockfile = tomllib.loads(lockfile_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(
            "toolchain assessment/config required: Python/uv manifest and lockfile "
            f"must be valid TOML ({error})"
        ) from error

    project = manifest.get("project")
    manifest_constraint = (
        project.get("requires-python") if isinstance(project, Mapping) else None
    )
    lock_constraint = lockfile.get("requires-python")
    if not isinstance(manifest_constraint, str) or not manifest_constraint.strip():
        raise ConfigError(
            "toolchain assessment/config required: pyproject.toml must declare "
            "project.requires-python"
        )
    if lock_constraint != manifest_constraint:
        raise ConfigError(
            "toolchain assessment/config required: uv.lock and pyproject.toml "
            "must declare the same requires-python constraint"
        )
    try:
        constraint = SpecifierSet(manifest_constraint)
        selected_version = Version(runtime_version)
    except (InvalidSpecifier, InvalidVersion) as error:
        raise ConfigError(
            "toolchain assessment/config required: Python runtime constraint is "
            f"invalid ({error})"
        ) from error
    runtime_satisfies_manifest = _python_selector_satisfies(
        selected_version, constraint
    )
    if not runtime_satisfies_manifest:
        raise ConfigError(
            "toolchain assessment/config required: selected Python runtime does "
            "not satisfy pyproject.toml and uv.lock constraints"
        )
    if type(lockfile.get("version")) is not int or not isinstance(
        lockfile.get("package"), list
    ):
        raise ConfigError(
            "toolchain assessment/config required: uv.lock has no supported lock "
            "version or package entries"
        )


def _validate_project_toolchain(root: Path, toolchain: Mapping[str, Any]) -> None:
    package_manager = toolchain["package_manager"]
    if shutil.which(package_manager) is None:
        raise ConfigError(
            "toolchain assessment/config required: selected package manager "
            f"{package_manager!r} is unavailable"
        )
    if toolchain["runtime"] != "python" or package_manager != "uv":
        raise ConfigError(
            "toolchain assessment/config required: unsupported selected toolchain; "
            "this initializer supports only Python with uv"
        )

    runtime_file = _require_project_file(
        root, toolchain["runtime_file"], "runtime selection file"
    )
    try:
        selected_runtime = runtime_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigError(
            "toolchain assessment/config required: cannot read runtime selection: "
            f"{error}"
        ) from error
    if selected_runtime != toolchain["runtime_version"]:
        raise ConfigError(
            "toolchain assessment/config required: runtime selection does not match "
            "the configured runtime version"
        )

    manifest_path = _require_project_file(root, toolchain["manifest"], "manifest")
    lockfile_path = _require_project_file(root, toolchain["lockfile"], "lockfile")
    _validate_python_uv_stack(
        manifest_path, lockfile_path, toolchain["runtime_version"]
    )


def _project_config_path(root: Path) -> Path:
    candidate = root / ".fuse" / "config.toml"
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except RuntimeError as error:
        raise ConfigError(
            "project config destination cannot be safely resolved"
        ) from error
    except ValueError as error:
        raise ConfigError(
            "project config destination resolves outside the project"
        ) from error
    return resolved


def initialize_config(project_root: Path, template_path: Path | None = None) -> bool:
    """Create a validated template only when its declared stack matches the project."""
    root = project_root.expanduser().resolve()
    if not root.is_dir():
        raise ConfigError(f"project root is not a directory: {root}")
    target = _project_config_path(root)
    if target.exists():
        existing = load_config(target)
        _validate_project_toolchain(root, existing["toolchain"])
        return False

    selected_template = template_path or _default_config_path()
    template = load_config(selected_template)
    _validate_project_toolchain(root, template["toolchain"])
    content = selected_template.read_text(encoding="utf-8")
    try:
        target = _project_config_path(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _project_config_path(root)
        with target.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except FileExistsError:
        target = _project_config_path(root)
        existing = load_config(target)
        _validate_project_toolchain(root, existing["toolchain"])
        return False
    return True


def _parse_overrides(raw: str) -> dict[str, Any]:
    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConfigError(f"overrides must be a JSON object: {error}") from error
    if not isinstance(overrides, dict) or any(
        not isinstance(field, str) for field in overrides
    ):
        raise ConfigError("overrides must be a JSON object with string field names")
    return overrides


def main(argv: Sequence[str] | None = None) -> int:
    """Initialize or validate a project configuration via the public CLI."""
    parser = argparse.ArgumentParser(description="Manage Fuse project configuration.")
    commands = parser.add_subparsers(dest="action", required=True)
    init_parser = commands.add_parser("init", help="create a project config")
    init_parser.add_argument("project_root", type=Path)
    init_parser.add_argument(
        "--template",
        type=Path,
        help="schema-validated, pre-assessed toolchain template",
    )
    validate_parser = commands.add_parser("validate", help="validate project config")
    validate_parser.add_argument("config_path", type=Path)
    validate_parser.add_argument(
        "--overrides",
        help="optional JSON object of dotted run-override fields to validate",
    )
    args = parser.parse_args(argv)

    try:
        if args.action == "init":
            project_root = args.project_root.expanduser().resolve()
            created = initialize_config(project_root, args.template)
            config_path = _project_config_path(project_root)
            config = load_config(config_path)
            result = {
                "created": created,
                "config_path": str(config_path),
                "schema_version": config["schema_version"],
                "valid": True,
            }
        else:
            config = load_config(args.config_path)
            if args.overrides is not None:
                validate_overrides(config, _parse_overrides(args.overrides))
            result = {"valid": True, "schema_version": config["schema_version"]}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ConfigError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
