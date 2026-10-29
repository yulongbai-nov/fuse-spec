---
name: poteto-mode
description: Route an approved Fuse task to one bounded engineering playbook before dispatch. Use only through spec-execute, which owns task state, assignments, checks, reviews, and acceptance.
---

# Poteto Mode

This skill supplies the method router for Fuse. It does not start a run, own a task graph, launch work, or accept results. `spec-execute` is the only coordinator and owns each of those transitions.

## Route selection

Read [`routes.json`](routes.json) first; it is the single source for method names, triggers, conditional evidence, and exclusions. Select one method only for an eligible implementation task, then follow its linked playbook. Do not add another route to solve an unapproved product question.

- Select **Feature**, **Bug Fix**, or **Refactoring** from the task's approved intent.
- Keep read-only investigation in Fuse's existing **research phase**. It has no separate Investigation method.
- Select **Performance Issue**, **Runtime/Trace Forensics**, **Prototype**, or **Visual Parity** only when the route's stated trigger and evidence are present.
- If the task does not fit, or the approved task is ambiguous, explain the gap to the parent and ask for a route decision. Do not invent scope.

For any conditional route, record a concise rationale and one or more references that exactly match the task's approved `[impl:...]` links or name an existing repository-relative source as `source:<path>`. Runtime/Trace Forensics also records the declared `live_process` or `existing_trace` source route. Fuse mechanically checks the presence and structure of this information and pins it to the current candidate and task assignment. Classification remains model judgment; reviewers audit whether the rationale and referenced evidence actually support the route. Do not use keyword matching to classify a task.

The route proposal is a model judgment. The parent records the selected method in `prepare-dispatch` before launch; the shared Fuse validator still decides whether dispatch is allowed. A worker may request a reroute from the parent, but cannot change its own method or task scope.

## Shared worker contract

Every assignment receives the approved task line and its detail block, including `Files:` write scope and `[impl:R...,D-...]` traceability, the selected route and playbook, measurable success criteria, role profile, required checks, and absolute consuming repository and spec paths. The task ID joins the assignment back to its authoritative requirement/design references; do not maintain a second reference ledger.

The playbook guides implementation. It does not replace the task contract, the project check definitions, or Fuse's shared transition and acceptance validator. Use project scripts/CLI for mechanical verification and capture their real results. Report uncertainty and ask the parent to reroute or resolve scope; never claim a check or acceptance yourself.

## Model and tool availability

Read role defaults, limits, required tools, and unavailable policy from the consuming repository's `.fuse/config.toml`. Resolve the configured model, effort, and required tools against the active Codex host before spawning an agent. Record the actual model and effort with the assignment result. If a requested model or required tool is unavailable, report the exact gap to the parent and block only that assignment; do not silently substitute or describe a model as unlimited.
