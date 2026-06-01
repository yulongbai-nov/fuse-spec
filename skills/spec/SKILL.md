---
name: spec
description: Orchestrate spec-driven development end to end — pick the mode, scaffold the spec folder, route through requirements, design, tasks, and execution with approval gates and consistency checks. Use when the user wants to build a feature, fix a bug, write a spec, or start spec-driven development.
---

# spec

The orchestrator for the fuse-spec workflow. It picks the mode, scaffolds the spec, and routes through the phases, keeping the three files consistent.

Scripts live in `../scripts/` and sub-skills in sibling folders; resolve paths relative to this file's directory.

## Sub-skills

Each phase has its own skill. In Claude Code, invoke them by name; in Codex, follow the relative path.

- Requirements — `spec-requirements` (`../spec-requirements/SKILL.md`)
- Design — `spec-design` (`../spec-design/SKILL.md`)
- Tasks — `spec-tasks` (`../spec-tasks/SKILL.md`)
- Execute — `spec-execute` (`../spec-execute/SKILL.md`)

## 1. Pick the mode

Infer from the request; confirm if ambiguous.

| Mode | When | Requirements file | Grilling | Gates |
|------|------|-------------------|----------|-------|
| `requirements-first` | New feature, behavior not yet pinned down | `requirements.md` | hard | after requirements + design |
| `design-first` | Technical approach is known/constrained up front | `requirements.md` (backfilled) | light | after design |
| `quick` | Small, well-understood feature | `requirements.md` | none | none — auto-run all phases |
| `bugfix` | Fixing a defect | `bugfix.md` | focused | after analysis |

**Auto-approve:** if the user explicitly opts in ("auto-approve", "no gates"), skip the gates for this run. Otherwise gates are interactive.

## 2. Scaffold the spec folder

Get the next folder name and create it:

```
python3 <skill-dir>/../scripts/next_spec_number.py "<feature slug>"
# e.g. -> 004-token-rotation
mkdir -p .specs/004-token-rotation
```

## 3. Route through phases

Run phases in order, handing the spec dir and mode to each sub-skill. Honor each phase's gate unless auto-approve is on (or mode is `quick`, which runs straight through):

- **requirements-first / design-first / quick:** requirements → design → tasks → (execute on request).
- **bugfix:** bug analysis (`bugfix.md`) → design (root cause + regression-safe fix) → tasks → execute.

Execution is usually a separate, explicit step — confirm before launching subagents unless told to proceed.

## 4. Interactive revision (keep files in sync)

Requirements and design are not a one-way street. When the user revises an earlier file:

1. Make the edit (preserving existing `R<n>` / `D-<Name>` ids — never renumber).
2. Re-run the sync check:
   ```
   python3 <skill-dir>/../scripts/sync_check.py .specs/NNN-<slug>
   ```
3. For each `ERROR`/`WARN`, do a semantic pass and prompt the user to refine the affected downstream sections/tasks so nothing goes stale.

## 5. Status

On request, report: current mode, which phase/gate the spec is at, and the latest `sync_check` result (clean / errors / warnings).
