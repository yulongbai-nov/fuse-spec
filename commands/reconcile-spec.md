---
description: Reconcile a spec's three files against the actual codebase — mark done tasks, flag design drift and requirement gaps, and report what the spec claims versus what the code shows.
argument-hint: spec dir (e.g. .specs/001-rate-limiter) — optional, will discover if omitted
allowed-tools: ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "AskUserQuestion"]
---

# reconcile-spec

Update a spec so it reflects **codebase truth**, not stale intentions. Specs drift: tasks get implemented outside the runner, designs change during coding, requirements get dropped or added. This command compares the spec to the real code and brings the spec back in line.

Scripts live in the plugin's `skills/scripts/`. Resolve their path relative to this plugin (the `skills/scripts/` sibling of this `commands/` dir).

## 1. Locate the spec

- If a spec dir was given as an argument, use it.
- Otherwise list `.specs/*/` and ask the user which spec to reconcile (or reconcile all, if they say so).

Read all three files: `requirements.md` (or `bugfix.md`), `design.md`, `tasks.md`.

## 2. Establish codebase truth

Explore the repository for the actual state — do not trust the checkboxes:

- For each task, find the files/symbols it concerns (use its title, `impl` anchors, and any listed file paths). Check whether the implementation exists and whether its tests exist and pass (run them if cheap and safe).
- For each design `D-<Name>` anchor, check whether the described component actually exists as designed.
- For each requirement `R<n>`, check whether code satisfying it is present.

Prefer evidence (a passing test, a present function) over assumption. When unsure, say so rather than guessing.

## 3. Reconcile, surgically

Apply only changes that reflect verified reality. Never renumber existing `R<n>`/`D-<Name>`/`T<n>` ids.

- **Task status:** flip `- [ ]` ↔ `- [x]` to match what the code/tests actually show. Note any task marked done whose tests now fail.
- **Design drift:** when the code diverges from a design section, flag it. Propose updating `design.md` to match reality (or, if the divergence is a bug, propose a fix task instead) — confirm with the user before rewriting design prose.
- **Requirement coverage:** report requirements with no implementing code (gaps) and implemented behavior that no requirement covers (undocumented scope). Propose adding/adjusting requirements only with user confirmation.
- **New work discovered:** if reconciliation reveals needed work, add tasks with fresh `T<n>` ids and correct `deps`/`impl` tags.

## 4. Validate consistency

Run the deterministic checks and resolve anything flagged:

```
python3 <plugin>/skills/scripts/sync_check.py <spec-dir>
python3 <plugin>/skills/scripts/plan_waves.py <spec-dir>/tasks.md
```

`sync_check` ERRORs must reach zero. Do a semantic pass on each WARN.

## 5. Report

Summarize, concisely:
- Tasks re-marked (done/undone) and why.
- Design sections that drifted (and whether you updated them).
- Requirement gaps / undocumented scope.
- New tasks added.
- Final `sync_check` status and remaining waves from `plan_waves`.

Keep edits traceable to evidence — every change should map to something you actually observed in the code.
