# fuse-spec

A spec-driven development plugin for **Claude Code** and **Codex**, fusing the strengths of three workflows:

- **[Kiro](https://kiro.dev/docs/specs/)** — the three-file spec skeleton (`requirements.md` → `design.md` → `tasks.md`), workflow modes, EARS requirement notation, approval gates, and dependency "waves".
- **[superpowers](https://github.com/obra/superpowers)** — execution-grade task detail (exact files, test→verify→commit steps, no placeholders) and subagent-driven execution.
- **grill-me / grill-with-docs** (Matt Pocock) — a relentless one-question-at-a-time interview that extracts real requirements and sharpens a glossary.

The fusion: **Kiro's structure is the container, grilling is the requirements engine, superpowers is the task-detail + execution engine.**

## How it works

```
spec (orchestrator)
  └─ pick mode ─ scaffold .specs/NNN-<slug>/ ─ route + gate
       ├─ spec-requirements → requirements.md  (EARS R-ids + ## Glossary)   | bugfix.md
       ├─ spec-design       → design.md        (sections cite satisfies: R-ids)
       ├─ spec-tasks        → tasks.md         (collapsed, [id deps impl] tags)
       └─ spec-execute      → dependency waves → capped parallel subagents → TDD
```

Each spec lives in `.specs/NNN-<slug>/`. Requirements get stable ids (`R1`…); design sections cite the requirements they satisfy and define `D-<name>` anchors; tasks cite both. `sync_check.py` keeps the three files consistent as you revise them.

### Modes

| Mode | Requirements | Grilling | Gates |
|------|--------------|----------|-------|
| `requirements-first` | `requirements.md` | hard | after requirements + design |
| `design-first` | `requirements.md` (backfilled) | light | after design |
| `quick` | `requirements.md` | none | none (auto-runs all three) |
| `bugfix` | `bugfix.md` (Current/Expected/Unchanged) | focused | after analysis |

Gates can be skipped by explicitly opting into auto-approve.

## Install

### Claude Code

```
/plugin marketplace add <git-url-of-this-repo>
/plugin install fuse-spec
```

Or install directly from a local clone / git URL via the `/plugin` menu.

### Codex

Add via the `/plugins` interface, or symlink the skills into your Codex skills directory:

```
ln -s "$(pwd)/skills"/* ~/.codex/skills/
```

Both harnesses load the same `skills/` directory — there is no platform-specific skill code.

## Usage

Start the orchestrator and follow the prompts:

```
Use the spec skill to build <feature>.
```

Or jump straight to a phase (e.g. `spec-execute` to run an existing `tasks.md`).

## Layout

```
.claude-plugin/plugin.json   Claude manifest
.codex-plugin/plugin.json    Codex manifest
AGENTS.md                    Codex root doc
skills/
  spec/                      orchestrator
  spec-requirements/         grill → requirements.md | bugfix.md
  spec-design/               → design.md
  spec-tasks/                → tasks.md
  spec-execute/              wave execution
  templates/                 requirements / bugfix / design / tasks
  scripts/                   next_spec_number.py, sync_check.py, plan_waves.py
```

## Requirements

- Python 3 (for the bundled scripts).
- The `tdd` skill is invoked during execution if available; otherwise the test-first steps in each task are followed directly.
