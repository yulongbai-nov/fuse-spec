---
description: Reconcile a spec with the codebase while keeping managed task state in Fuse's canonical run record.
argument-hint: spec dir (e.g. .specs/001-rate-limiter) — optional, will discover if omitted
allowed-tools: ["Read", "Write", "Edit", "Grep", "Glob", "Bash", "AskUserQuestion"]
---

# reconcile-spec

Compare the selected spec with verified repository state. Requirements and design describe approved intent; code does not silently redefine them. A managed run record is the only authority for accepted task state. Task checkboxes are a non-authoritative snapshot and never proof of acceptance.

Before running scripts, resolve absolute paths for the plugin checkout (`PLUGIN_ROOT`), the consuming repository (`REPO_ROOT`), and the selected spec (`SPEC_DIR`, inside `REPO_ROOT`). Every helper must run from the plugin checkout through its locked uv project; pass the consumer repository and spec as explicit absolute paths. Do not rely on the slash command's current working directory or the system `python3` environment.

## 1. Locate the spec and detect managed state

- If a spec directory was given as an argument, use it. Otherwise list `.specs/*/` and ask the user which spec to reconcile, or reconcile all if requested. For an all-spec request, process each spec separately: route managed specs through this helper and preserve the unmanaged legacy path for every other spec.
- Run the managed reconciliation helper with explicit absolute paths. Substitute the resolved paths in this command:

  ```text
  uv run --project "$PLUGIN_ROOT" --directory "$PLUGIN_ROOT" --locked \
    python "$PLUGIN_ROOT/skills/scripts/fuse_reconcile.py" \
    --repo-root "$REPO_ROOT" \
    --spec-dir "$SPEC_DIR"
  ```

- The helper selects the sole active run or reports that `--run-id` is required when multiple or terminal runs need a choice; ask the user which run to inspect and rerun with that ID. If it reports `mode: unmanaged`, follow **Unmanaged legacy reconciliation** below. If it reports a managed run, use only the managed workflow here.
- If the repository has a configured remote for canonical run state, pass it as `--remote <remote>`. When recorded pull requests exist, obtain fresh read-only PR identities from the configured provider when available and pass a JSON snapshot using `--observed-prs <path>`. If the provider cannot be queried, report PR head freshness as unverified; do not claim it is current.

## 2. Managed-run reconciliation

The helper loads the canonical local run record, validates the reconciliation transition, and reports approved spec/policy drift, current candidate identity, stale or unavailable evidence/artifacts, accepted tasks tied to an older candidate, checkbox mismatches, unresolved launches, and recorded or observed PR head discrepancies. It also evaluates each task through the existing shared `accept` validator on an isolated copy of the record. These checks are diagnostic only; they do not accept tasks or modify run state.

Use the report as the checklist for recovery:

- If approved requirements, design, or task details changed, report the exact discrepancy and ask for an operator decision. Never rewrite approved intent to make it match the code or run record.
- Treat checked-but-unaccepted tasks as a checkbox mismatch. Do not import them as accepted work.
- For each gate reporting missing, stale, or unavailable evidence, capture the required evidence with the existing `fuse_launch.py` operations: run the configured check, record an independent review, resolve findings with current evidence, then request `accept`. All status changes go through the shared transition validator. If an accepted task's candidate or current acceptance gate is stale, checkbox projection stays blocked until its required evidence is current. If evidence cannot be collected, report the precise blocker.
- If the report shows changed PR heads, stale candidate evidence, missing artifacts, unresolved launches, or a remote state conflict, reconcile those facts before claiming completion. Do not overwrite either side of a canonical-state conflict.
- Once the canonical record is correct and accepted tasks still pass the current evidence gate, project its accepted task states into `tasks.md` by rerunning the helper with the same run/remote/PR observation arguments and `--sync-task-checkboxes`. It writes only checkbox characters and refuses changed approved inputs, undeclared record tasks, stale accepted evidence, PR discrepancies, and canonical remote conflicts. Other Fuse projections serialize through the same advisory lock, and edits observed before final byte validation are reread and retried. Before projection, ensure no editor or other writer that ignores this lock is modifying `tasks.md`; standard file APIs have no content compare-and-swap, so such a writer can still race in the final read/write interval. It rechecks the current candidate and local/remote run state immediately afterward and reports a stale snapshot if detected. It never accepts a task.
- Do not edit `.fuse/runs/*/run.json` directly. Use `accept`, `record-check-outcome`, `record-review`, `resume`, and `checkpoint` through `fuse_launch.py` so the shared validator remains authoritative.

## 3. Unmanaged legacy reconciliation

When the helper reports no managed run record, preserve the existing behavior:

- Read `requirements.md` (or `bugfix.md`), `design.md`, and `tasks.md`.
- Verify task implementation and tests against repository files and executable evidence; do not infer completion from a checkbox.
- In this unmanaged workflow only, update task checkboxes to reflect verified codebase progress. Never renumber existing requirement, design, or task IDs.
- Flag design drift and requirement gaps. Propose design or requirement changes and get user confirmation before editing approved intent.
- Add newly discovered work with fresh task IDs and valid dependencies/traceability.

## 4. Validate and report

For either path, run:

```text
uv run --project "$PLUGIN_ROOT" --directory "$PLUGIN_ROOT" --locked \
  python "$PLUGIN_ROOT/skills/scripts/sync_check.py" "$SPEC_DIR"
uv run --project "$PLUGIN_ROOT" --directory "$PLUGIN_ROOT" --locked \
  python "$PLUGIN_ROOT/skills/scripts/plan_waves.py" "$SPEC_DIR/tasks.md"
```

Summarize verified changes, remaining evidence gates, design drift, requirement gaps, new tasks, PR/state freshness, `sync_check` status, and remaining waves. Distinguish verified facts from provider state that could not be checked.
