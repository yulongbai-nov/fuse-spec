---
name: spec-execute
description: Execute an approved tasks.md in dependency waves, launching capped parallel subagents that share a cache-friendly context prefix and follow TDD, with a barrier and stop-on-failure between waves. Use when implementing a spec's tasks, running the task list, or executing a plan.
---

# spec-execute

Implement the tasks in `.specs/NNN-<slug>/tasks.md` wave by wave.

Script: `../scripts/plan_waves.py`. Resolve paths relative to this file's directory.

## 1. Compute waves

```
python3 <skill-dir>/../scripts/plan_waves.py .specs/NNN-<slug>/tasks.md
```

This prints `{"waves": [[...], ...], "tasks": {...}}`. Already-checked (`- [x]`) tasks are skipped; their ids still satisfy dependencies. A cycle or unknown dep exits non-zero — stop and fix `tasks.md`.

## 2. Build the shared prefix (do this once)

Assemble a single context block reused verbatim as the **opening** of every subagent prompt — keeping it byte-identical maximizes prompt-cache hits:

- Absolute paths to `requirements.md`/`bugfix.md`, `design.md`, `tasks.md`.
- The `## Glossary`.
- A short design summary (the `D-<Name>` sections and their `satisfies` lines).
- Project conventions: use the `tdd` skill (red → green → commit), test through public seams, match existing style, make the minimum viable change.

Only the **task-specific tail** (the task line + its detail block) varies between subagents.

## 3. Run each wave

For each wave, in order:

1. Launch one subagent per task in the wave, **capped at N concurrent** (default 3; honor a user-specified cap). If a wave is larger than the cap, run it in batches.
2. Each subagent prompt = shared prefix + that task's line and detail block. Instruct it to implement the task via the `tdd` skill, run the tests, and commit.
3. **Barrier:** wait for the whole wave to finish before starting the next.
4. **Stop on failure:** if any task in a wave fails (tests red, blocked, or ambiguous), halt. Report which task failed and why; do not start the next wave.
5. After a task succeeds, check its box in `tasks.md` (`- [ ]` → `- [x]`).

## 4. Report

When all waves complete (or on a stop), report: tasks done, tasks remaining, and any failure detail. Re-running resumes from the unchecked tasks.

## Concurrency note

The cap bounds token burst and cost. Lower it for large/expensive tasks; raise it for many small independent ones. The wave structure already guarantees only independent tasks run together.
