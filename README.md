# Fuse Spec

Fuse is a spec-driven development plugin for Codex, with shared Markdown and Python helpers. It interviews through requirements, records design decisions, creates dependency-tagged implementation tasks, and coordinates bounded execution with checks, independent reviews, and recovery.

Start with the [`spec` skill](skills/spec/SKILL.md) for a new feature or bug fix. The [user guide](docs/user-guide.md) explains installation, configuration, routes, lifecycle commands, evidence gates, and recovery.

## One owner for intent and progress

- `.specs/<id>-<slug>/requirements.md` or `bugfix.md`, `design.md`, and `tasks.md` hold approved intent and declared dependencies.
- `.fuse/config.toml` owns role defaults, resource limits, toolchain policy, and the check definitions and their prerequisites.
- `.fuse/runs/<run-id>/run.json` holds managed execution state and evidence. A configured remote stores checkpoints on `refs/heads/fuse-state/<spec-id>/<run-id>` using fast-forward-only updates.
- `tasks.md` checkboxes are a display projection. In a managed run they never establish acceptance.

The `spec-execute` session is the coordinator for one spec. It selects a Poteto Mode method from [`routes.json`](skills/poteto-mode/routes.json), dispatches workers, and asks the shared Fuse CLI to validate transitions. Poteto Mode supplies playbooks; it does not own run state, readiness, or acceptance.

## Verify locally

Use the same configured check runner that CI invokes:

```sh
uv sync --locked
uv run --locked python skills/scripts/fuse_verify.py \
  --repo-root "$PWD" \
  --spec-dir "$PWD/.specs/001-pstack-integration"
```

It validates the selected spec and `.fuse/config.toml`, captures a content candidate, and runs all configured checks in dependency order. `--check-id ID` runs that check and its transitive prerequisites. The command prints JSON and writes check output under `.fuse/runs/<run-id>/artifacts/checks/`. It is a one-shot verification report and does not persist a managed lifecycle record; acceptance and resumable evidence belong to the managed Fuse run.

See [docs/user-guide.md](docs/user-guide.md) for the full workflow and limitations.
