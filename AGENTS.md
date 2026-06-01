# fuse-spec

This repository is the `fuse-spec` plugin for Codex (and Claude Code). It provides a spec-driven development workflow that fuses three influences: Kiro's three-file spec structure and modes, superpowers' execution-grade task detail, and a relentless one-question-at-a-time requirements interview.

## Entry points

- **`spec`** — the orchestrator. Detects the mode, scaffolds the spec folder, routes through phases, enforces approval gates, and reports status. Start here.
- **`spec-requirements`** — interviews you into EARS requirements (`requirements.md`) with stable `R<n>` ids and a `## Glossary`. Bugfix mode writes `bugfix.md`.
- **`spec-design`** — produces `design.md`; each section declares which requirements it satisfies.
- **`spec-tasks`** — produces a collapsed, dependency-tagged `tasks.md`.
- **`spec-execute`** — runs tasks in dependency waves with capped parallel subagents.

## Spec storage convention

Each spec lives in its own folder:

```
.specs/NNN-<slug>/
  requirements.md   (or bugfix.md)
  design.md
  tasks.md
```

`NNN` is a zero-padded 3-digit sequential number (`001`, `002`, …) assigned by `skills/scripts/next_spec_number.py`.

## Traceability

Requirements have ids (`R1`, `R2`…). Design sections cite `satisfies: R1, R2` and define `D-<name>` anchors. Tasks carry an inline tag `[id:T3 deps:T1,T2 impl:R2,D-Auth]`. `skills/scripts/sync_check.py` validates these references; `skills/scripts/plan_waves.py` turns task dependencies into execution waves.

## Modes

- **requirements-first** — grill hard, gate after requirements and design.
- **design-first** — start from an existing/known design, then backfill requirements.
- **quick** — generate all three files with no grilling and no gates.
- **bugfix** — `bugfix.md` (Current / Expected / Unchanged) instead of `requirements.md`.
