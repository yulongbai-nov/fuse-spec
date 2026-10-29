# Tasks: pstack integration

- **Spec**: 001-pstack-integration
- **Status**: approved
- **Requirements**: ./requirements.md
- **Design**: ./design.md

## Tasks

- [x] T1 Add the locked development setup and regression coverage for existing helpers  [id:T1 deps: impl:R3,R8,D-RunControl,D-Verification]
    - **Files:** `pyproject.toml`, `.python-version`, `uv.lock`, `skills/scripts/plan_waves.py`, `skills/scripts/sync_check.py`, `skills/scripts/next_spec_number.py`, `tests/`
    - Add pytest cases first for the observed missing-spec, duplicate-task-ID, checkbox-only completion, invalid-dependency, cycle, and overlapping-write-scope cases; verify the new cases fail against the current helpers.
    - Add the Python >=3.11 project environment with uv, pytest, Ruff, mypy, and the selected runtime dependency; pin versions in `uv.lock`. Configure their project settings in `pyproject.toml` and invoke them through locked `uv run` commands. T9 owns the unified local/CI check entry point so the command list has one owner.
    - Implement the minimum parser and validation changes so malformed or incomplete inputs fail with a clear error and the new cases pass. Preserve the existing task and requirement formats.
    - Parse nested `Files:` bullets with comma-separated plain or inline-code paths. Compare directories and wildcard patterns conservatively by static path prefix. A missing, empty, or unusable write scope is exclusive and serializes against other ready tasks. Cover mixed quoted/unquoted paths, `src/**`, Markdown indentation, and empty scopes in regression tests.
    - Annotate new or changed Python code; format and lint it, then run the selected type checker and tests.
    - Commit this atomic code job and push it; open or update the draft PR after its first pushable code commit.

- [x] T2 Add the shared Fuse policy and project-check configuration  [id:T2 deps:T1 impl:R2,R3,R7,R8,D-Verification]
    - **Files:** `.fuse/config.toml`, `schemas/project-config.schema.json`, `skills/scripts/fuse_config.py`, `tests/`
    - Write failing tests for malformed configuration, invalid role profiles, unavailable-profile fallback policy, invalid check IDs/references, and repeated initialization.
    - Define one `.fuse/config.toml` authority for model profiles, review/RCA limits, concurrency and optional budgets, allowed overrides, toolchain policy, and named check definitions with commands, prerequisites, inputs, and pass criteria.
    - Validate the parsed TOML against the versioned JSON Schema, then apply semantic checks for references, bounds, and supported check/role shapes. Store defaults as data and record the host-resolved profile at execution time.
    - Record the selected runtime, package manager, manifest, lockfile, and tool-restoration policy. Initialization validates that the selected files exist and are compatible, reuses an existing configuration, and fails closed without changing consumer package files when the default or supplied pre-assessed template does not match. It does not install tools.

- [ ] T3 Add the versioned run record and single-writer state backend  [id:T3 deps:T1,T2 impl:R2,R3,R5,D-RunControl]
    - **Files:** `.gitignore`, `schemas/run-record.schema.json`, `skills/scripts/fuse_record.py`, `skills/scripts/fuse_state.py`, `tests/`
    - Ignore the local `.fuse/runs/` replica in ordinary code branches; the state backend force-includes it only in the per-run state ref.
    - Test record schema/version validation, stable assignment/finding/evidence IDs, append-only retry history, immutable effective-policy snapshots, and refusal to silently migrate an unknown schema version.
    - Implement the one `run.json` contract and per-run remote state ref defined in `design.md`. Use fast-forward-only updates, fetch/reconcile on a competing update, and never force-push or create a second authoritative copy.
    - Persist a dispatch assignment and reserve capacity before host launch; bind the returned host handle afterward. On resume, unresolved launches remain reserved until reconciled and cannot be relaunched as duplicates.
    - Checkpoint the required state transitions and exact code/PR identities. Keep large output in referenced artifacts and ensure a missing required artifact makes its evidence unavailable.

- [ ] T4 Implement deterministic task/check readiness and candidate-bound evidence  [id:T4 deps:T2,T3 impl:R1,R3,R4,R8,D-RunControl,D-Verification]
    - **Files:** `skills/scripts/fuse_graph.py`, `skills/scripts/fuse_candidate.py`, `skills/scripts/fuse_checks.py`, `tests/`
    - Test duplicate/unknown/self/cyclic task and check references, independent ready checks, real prerequisites, stable ready-ID presentation, and blocked work that does not stop unrelated eligible work.
    - Derive task readiness from approved task declarations plus accepted run state. Derive the check DAG from `.fuse/config.toml` plus task applicability; use `graphlib` for scheduling mechanics after Fuse validation.
    - Create canonical immutable candidate manifests covering the source tree, approved spec inputs (normalizing checkbox-only status), effective policy, check definitions, and required fixtures/toolchain inputs. Exclude run-state and progress metadata.
    - Execute project-owned commands from the shared local/CI check entry point in pinned worktrees. Capture exit status, output artifacts, config/tool versions, and candidate IDs; reject stale, missing, failed, or mismatched results at acceptance.
    - Before a required check, restore a missing selected project-local tool only through the configured package manager, version constraints, and existing lockfile policy. If restoration fails or the selection conflicts, report an unavailable prerequisite and block affected work while unrelated checks remain eligible.
    - Test that code or policy changes invalidate affected evidence and that state-only or checkbox-only changes do not change the implementation candidate.

- [ ] T5 Add the control CLI and validated transition/acceptance gates  [id:T5 deps:T3,T4 impl:R2,R3,R4,R5,R6,D-RunControl,D-Verification]
    - **Files:** `skills/scripts/fuse_launch.py`, `skills/scripts/fuse_cli.py`, `skills/scripts/fuse_control.py`, `tests/`
    - Add CLI scenario tests for inspect, initialize/resume, prepare-dispatch, bind-launch, record-result, accept, checkpoint, and reconcile operations, including invalid order, duplicate/late results, missing evidence, exhausted limits, and recovery from uncertain launch.
    - Require explicit absolute `--repo-root` and `--spec-dir` arguments at the CLI boundary. Run Git and project check subprocesses with the consuming repository root as their working directory, regardless of the Fuse plugin's launcher directory.
    - Implement a standard-library launcher that captures its starting directory, resolves the target root and spec path before invoking uv, and passes the absolute paths through to the CLI after uv changes to the plugin root.
    - Keep one transition validator as the shared owner used by the CLI and `/reconcile-spec`. Reject invalid state with machine-readable reasons; do not treat agent claims or task checkboxes as proof.
    - Continue unrelated eligible work when a task/check is blocked. Stop only when no authorized action remains or a configured limit is reached, and preserve a resumable checkpoint.
    - Invoke executable checks directly and summarize verified output for agents; do not spend model calls re-interpreting successful mechanical checks.

- [ ] T6 Integrate Poteto routing with the existing Fuse coordinator and worker roles  [id:T6 deps:T2,T4,T5 impl:R1,R2,R3,R6,R7,D-Coordinator,D-RunControl]
    - **Files:** `skills/spec-execute/SKILL.md`, `skills/poteto-mode/SKILL.md`, `skills/poteto-mode/playbooks/`, `skills/agents/`, `tests/test_poteto_routing.py`
    - Add a focused route-table/scenario test set for Feature, Bug Fix, Refactoring, existing research, and the conditional performance, runtime/trace forensics, prototype, and visual-parity methods. Keep investigation inside Fuse's current research phase and Hillclimb deferred.
    - Make the `spec-execute` parent own the run and select/record one method before validating and dispatching each assignment. Give each worker scope, requirements/design references, success criteria, role profile, and checks; workers request reroutes through the parent.
    - Invoke managed operations through `fuse_launch.py` with the selected consuming repository root and spec directory; do not rely on the plugin's changed working directory for repository context.
    - Configure manager, Luna-Max worker, Sol-xhigh regular reviewer, and Astra-Max RCA plus separate Astra-Max RCA reviewer profiles. Resolve actual host model/effort/tools at dispatch and record or report unavailability; do not silently substitute.
    - Preserve bounded one-spec execution, in-scope task splitting/reordering, review counters, carry-forward findings, the one RCA attempt per hard blocker, and shared nested-agent concurrency accounting. Hooks remain optional context aids, never required acceptance gates.

- [ ] T7 Add serialized GitHub PR publication and exact merge-candidate gates  [id:T7 deps:T3,T4,T5 impl:R3,R4,R5,D-PRIntegration,D-RunControl,D-Verification]
    - **Files:** `skills/scripts/fuse_git.py`, `skills/scripts/fuse_pr.py`, `tests/test_pr_provider.py`
    - Test isolated worker commits, one coordinator publisher, task-to-PR mapping, early draft-PR creation, per-job push/checkpoint order, acyclic derived PR dependencies, and refusal to force-push or bypass repository protection.
    - Use `gh` where available for PR/status operations and record provider, PR, base, and head identities. Keep provider calls behind a testable adapter and report missing CLI/auth/permissions as a blocker.
    - Materialize the integrated review candidate and the exact current base plus prerequisites plus target PR candidate before each merge. Recompute and rerun affected checks/reviews after base or prior-merge changes.
    - Merge only the selected PR when current checks, all required review levels, unresolved-finding policy, and repository mergeability satisfy R4; otherwise continue monitoring or report the blocker.

- [ ] T8 Route `/reconcile-spec` and task checkbox updates through the shared record  [id:T8 deps:T3,T5,T6 impl:R2,R3,R4,R5,D-RunControl,D-Coordinator]
    - **Files:** `commands/reconcile-spec.md`, `skills/scripts/fuse_reconcile.py`, `tests/`
    - Test reconciliation against edited spec inputs, changed PR heads, absent or stale artifacts, accepted and unaccepted tasks, and out-of-band code changes.
    - Make the command load the canonical record, report spec/candidate/PR discrepancies, collect required missing evidence, and update status only through the transition validator. Never repair a mismatch by trusting a checkbox or rewriting approved intent.
    - Ensure ordinary non-managed reconciliation retains its current behavior and the managed path cannot create a parallel acceptance rule.

- [ ] T9 Add shared CI/check entry points, end-to-end scenarios, and user documentation  [id:T9 deps:T1,T2,T4,T5,T6,T7,T8 impl:R1,R2,R3,R4,R5,R6,R7,R8,D-Coordinator,D-RunControl,D-PRIntegration,D-Verification]
    - **Files:** `.github/workflows/`, `.fuse/config.toml`, `README.md`, `tests/`
    - Add end-to-end local-repository scenarios for a feature, bug fix, launch from outside the plugin root into a consuming repo, resume after an uncertain launch, forgotten verification, stale evidence, unavailable tools, review repair/carry-forward, and blocked work alongside unrelated progress.
    - Configure CI to invoke the same project-owned check entry point and definitions as local Fuse execution. Keep only provisioning, credentials, triggers, caching, and artifact transport specific to the workflow environment.
    - Document setup, supported Python version, configuration, command usage, state-ref recovery, Codex capability limits, routes, evidence gates, and clear unverified/blocked outcomes. Keep full transcripts and unrelated work outside the run record.
    - Run traceability, dependency planning, all project checks, and the task-, integrated-PR-, and whole-codebase review levels. Resolve findings within their configured maximum of two rounds per level before merge.
