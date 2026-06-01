---
name: spec-tasks
description: Derive an executable tasks.md from an approved design, with collapsed one-line tasks carrying inline dependency/traceability tags and expanded TDD detail only where complexity warrants. Use when starting the tasks phase of a spec, after design is approved, or when regenerating tasks.
---

# spec-tasks

Produce `tasks.md` for a spec at `.specs/NNN-<slug>/` from its approved `design.md`.

Template: `../templates/tasks.md`. Script: `../scripts/sync_check.py`.
Resolve paths relative to this file's directory.

## Process

1. Read `design.md` and the requirements file. Break the work into tasks that, in order, implement every design section and satisfy every requirement.
2. Prefer **thin vertical slices** (a slice cuts through all layers and is independently verifiable) over horizontal layer-by-layer tasks.
3. Write each task as **one collapsed checklist line** with an inline tag:

   ```
   - [ ] T3 Implement token rotation  [id:T3 deps:T1,T2 impl:R2,D-Auth]
   ```

   - `id` — stable `T<n>` (never renumber).
   - `deps` — task ids that must finish first (drives execution waves). Empty if none.
   - `impl` — the `R<n>` requirements and `D-<Name>` design anchors this task implements.

4. **Adaptive depth.** Keep simple tasks to one line. For a complex or risky task, add an indented detail block beneath it with: the files touched, and the test-first steps (write failing test → run, expect FAIL → implement minimally → run, expect PASS → commit). Expand only where the detail earns its keep — judge by complexity. The execution phase invokes the `tdd` skill, so the detail should align with a red-green vertical-slice loop.

## Consistency check

Run the sync check and resolve everything it flags:

```
python3 <skill-dir>/../scripts/sync_check.py .specs/NNN-<slug>
```

Confirm full traceability: every `R<n>` is implemented by some task, every `D-<Name>` is referenced, and no task cites a missing id. `ERROR` must be zero before tasks are considered ready. Validate the dependency graph is acyclic:

```
python3 <skill-dir>/../scripts/plan_waves.py .specs/NNN-<slug>/tasks.md
```

A clean run prints the wave plan; a cycle or unknown dep exits non-zero — fix the `deps`.

## Gate

Unless auto-approve is on, present the task list (and the computed waves) and ask the user to approve before execution.
