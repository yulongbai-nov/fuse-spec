---
name: spec-execute
description: Coordinate an approved spec through Fuse's validated task, verification, review, and recovery gates; route each eligible task to one method before dispatch. Use when implementing or resuming a spec.
---

# spec-execute

Implement the approved tasks in `.specs/NNN-<slug>/tasks.md`. For a managed run with `.fuse/config.toml` and a Fuse run record, use the validated coordinator loop below. The wave planner remains available for unmanaged legacy execution.

Script: `../scripts/plan_waves.py`. Resolve paths relative to this file's directory.

## Managed Fuse run

The current `spec-execute` session is the sole coordinator for one selected spec. It is the manager; do not create a second router, task board, run record, acceptance rule, or always-on service. Read the approved requirements, design, task graph, `.fuse/config.toml`, and canonical run record. Initialize or resume explicitly, then use `inspect` to learn which tasks are eligible and which assignments remain unresolved.

Invoke every managed operation through `fuse_launch.py` with absolute consuming-repository and spec paths. This makes the selected project explicit before Fuse changes working directory and invokes its locked CLI environment:

```text
python3 <fuse-plugin-root>/skills/scripts/fuse_launch.py \
  --repo-root <absolute-consuming-repository> \
  --spec-dir <absolute-consuming-repository>/.specs/NNN-<slug> \
  inspect --run-id <run-id>
```

For each eligible task:

1. Read its task line, nested detail block, `[impl:R...,D-...]` references, and declared `Files:` scope. Do not infer requirements or broaden scope from nearby tasks.
2. Select one method from [`poteto-mode/SKILL.md`](../poteto-mode/SKILL.md) using its authoritative [`routes.json`](../poteto-mode/routes.json). Follow the route's trigger; research stays in Fuse's existing research phase, and conditional methods require a concise rationale plus references to the task's exact `[impl:...]` links or an existing repository-relative source file. Runtime/Trace Forensics also requires its declared `live_process` or `existing_trace` source route. Classification remains model judgment; reviewers audit the rationale and evidence substance without keyword heuristics. Ask the operator only when approved intent or scope is ambiguous.
3. Select a role profile from `.fuse/config.toml`; verify the host can supply its configured model/effort and required tools. Pass no unsupported substitution. If unavailable, report the exact gap and leave only that assignment blocked.
4. Call `prepare-dispatch` with the task ID, selected method, and role profile **before** spawning the worker. For a conditional method, also pass `--route-rationale`, each `--route-evidence-ref`, and for forensics `--source-route`; the validator records the decision with its current candidate ID on the assignment. Structural presence is mechanically checked, while route classification and evidence substance remain reviewable model judgments. The task declaration remains the single source of requirement/design references. Fuse's shared transition validator decides whether the assignment is ready.
5. Give the worker the approved task line and complete detail block, method and playbook, requirement/design references, write scope, measurable success criteria, role profile, required check IDs, and explicit absolute repository/spec paths. The worker may request a reroute, but only the parent can decide it. Do not mutate an active assignment's method or scope.
6. Launch the worker only after `prepare-dispatch` succeeds. Bind the returned host handle; if launch state is unknown, inspect/reconcile it and preserve the reservation rather than duplicating the launch. Record the actual model ID and effort in the attempt summary. A worker may return a task-scoped commit but cannot accept work or write canonical run state.
7. Use the same launcher and shared validator to record results, run configured checks, record independent review evidence, and request acceptance. Do not reimplement a check, resolve a finding from reviewer prose when it requires executable evidence, or advance a dependent task on a checkbox alone.
8. Continue unrelated eligible tasks while one task is blocked, subject to shared concurrency and resource limits in the policy. At a blocker, exhausted limit, missing prerequisite, or unresolved launch, checkpoint and report the precise decision needed; otherwise proceed until the selected spec is complete.

For managed reviews and repairs, read open findings and counters from the run record and pass them forward for explicit verification. Keep task, integrated-PR, and whole-codebase reviews on their own configured counters. A hard blocker may use the configured single RCA attempt; the RCA fixer and its independent RCA reviewer must be separate agents, and that review does not erase the ordinary task review. Nested agents consume the same configured concurrency pool. The parent may split or reorder work only within approved intent, must preserve `[impl:R...,D-...]` links and declared file scope, and must revalidate the edited task graph before dispatch. Scope expansion or gate waivers require the operator.

The `.fuse/config.toml` profile settings are the single source for model names, reasoning efforts, required tools, and limits. They are defaults, not a promise that a named model or usage quota is available. Resolve them at dispatch, report actual model/effort and unavailable capabilities, and keep the active run's policy snapshot unchanged. Hooks can restore context or remind an agent, but they remain optional and never replace CLI validation or evidence.

## Unmanaged legacy execution

Use the wave-only path below when there is no initialized managed run. It does not establish Fuse acceptance evidence and must not be mixed with managed task status.

### 1. Compute waves

```
python3 <skill-dir>/../scripts/plan_waves.py .specs/NNN-<slug>/tasks.md
```

This prints `{"waves": [[...], ...], "tasks": {...}}`. Already-checked (`- [x]`) tasks are skipped; their ids still satisfy dependencies. A cycle or unknown dep exits non-zero — stop and fix `tasks.md`.

### 2. Build the shared prefix (do this once)

Assemble a single context block reused verbatim as the **opening** of every subagent prompt — keeping it byte-identical maximizes prompt-cache hits:

- Absolute paths to `requirements.md`/`bugfix.md`, `design.md`, `tasks.md`.
- The `## Glossary`.
- A short design summary (the `D-<Name>` sections and their `satisfies` lines).
- Project conventions: use the `tdd` skill (red → green → commit), test through public seams, match existing style, make the minimum viable change.

Only the **task-specific tail** (the task line + its detail block) varies between subagents.

### 3. Run each wave

For each wave, in order:

1. Launch one subagent per task in the wave, **capped at N concurrent** (default 3; honor a user-specified cap). If a wave is larger than the cap, run it in batches.
2. Each subagent prompt = shared prefix + that task's line and detail block. Instruct it to implement the task via the `tdd` skill, run the tests, and commit.
3. **Barrier:** wait for the whole wave to finish before starting the next.
4. **Stop on failure:** if any task in an unmanaged wave fails (tests red, blocked, or ambiguous), halt and report which task failed and why.
5. After a task succeeds, check its box in `tasks.md` (`- [ ]` → `- [x]`).

### 4. Report

When all waves complete (or on a stop), report: tasks done, tasks remaining, and any failure detail. Re-running resumes from the unchecked tasks.

## Concurrency note

The cap bounds token burst and cost. Lower it for large/expensive tasks; raise it for many small independent ones. The wave structure already guarantees only independent tasks run together.
