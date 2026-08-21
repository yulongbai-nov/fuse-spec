---
name: spec-requirements
description: Interview the user one question at a time to produce a traceable EARS requirements.md (or bugfix.md) with stable R-ids and a glossary. Use when starting the requirements phase of a spec, gathering requirements, or clarifying what to build before designing.
---

# spec-requirements

Produce `requirements.md` (feature) or `bugfix.md` (bugfix) for a spec at `.specs/NNN-<slug>/`, driven by a relentless interview.

Templates: `../templates/requirements.md`, `../templates/bugfix.md`.
Resolve script and template paths relative to this file's directory.

## Grilling protocol

Interview the user relentlessly until you reach shared understanding. Walk each branch of the decision tree, resolving dependencies between decisions one at a time.

- **One question at a time.** Wait for the answer before the next.
- **Always recommend an answer.** Give your recommended option and why, so the user can just confirm.
- **Explore instead of asking** when the codebase can answer. Read the code, then confirm what you found.
- **Sharpen fuzzy language.** When a term is vague or overloaded, propose a precise canonical term and record it in the `## Glossary`.
- **Probe with scenarios.** Invent concrete edge cases that force precise boundaries.
- **Record the why.** For every material product, safety, interface, scope, or
   lifecycle decision, record the chosen option, why it is preferred, the
   evidence or assumption behind it, and what result would reopen it. Do not
   leave rationale only in chat.

Grill intensity is set by the mode (the orchestrator passes it in):
- `requirements-first` — grill hard; cover happy path, edge cases, errors, non-functionals.
- `design-first` — lighter; capture the requirements the known design already implies.
- `bugfix` — focused on reproduction, expected behavior, and what must NOT regress.
- `quick` — **skip grilling**; synthesize requirements directly from context.

## Writing the file

1. Copy the appropriate template into the spec folder (`requirements.md`, or `bugfix.md` for bugfix mode).
2. Give every requirement a **stable `R<n>` id heading** (`### R1: Title`). These ids are referenced by design.md and tasks.md — never renumber an existing one.
3. Write acceptance criteria in **EARS**:
   - `WHEN <event> THEN the system SHALL <response>`
   - `IF <condition> THEN the system SHALL <behavior>`
   - `WHILE <ongoing> the system SHALL <continuous behavior>`
   - `WHERE <context> the system SHALL <contextual behavior>`
   - Bugfix regression guards: `WHEN <condition> THEN the system SHALL CONTINUE TO <existing behavior>`.
4. Maintain the `## Glossary` inline as terms are resolved — definitions only, no implementation detail.
5. Keep a tight `## Out of Scope` so dropped ideas are visibly considered.
6. Add a `## Decision Rationale Register` before the Requirements section. Use
   this shape:

   | Decision | Requirement IDs | Choice | Why | Evidence / assumption | Reopen trigger |
   |----------|-----------------|--------|-----|-----------------------|----------------|

   Give each material decision a stable `D<n>` id if later design or tasks
   need to refer to it. Keep implementation details out of the register; the
   register explains the constraint or tradeoff that the implementation must
   preserve.

## Evidence And Dependency Gates

Requirements may be drafted while external behavior, live-system facts, or
cross-repository dependencies remain unverified. Do not silently turn those
assumptions into requirements or implementation defaults. Record them in an
`## Evidence And Dependency Ledger` immediately after the Glossary and before
the Requirements section.

Use stable gate IDs (`G1`, `G2`, ...) and preserve them when the interview is
revised. Use this table shape:

| Gate | Requirement IDs | Assumption / dependency | Depends on | Evidence needed | Disconfirming result | Blocks | Status |
|------|-----------------|-------------------------|------------|-----------------|----------------------|--------|--------|
| G1 | R4, R7 | Machine target is available through the required interface. | — | Named command/check, source, or live artifact. | Missing, invalid, or contradictory result. | design, tasks, or execution | open |

Rules:

- `Status` is one of `open`, `supported`, `rejected`, or `deferred`.
- `supported` requires a dated evidence reference or reproducible command and
   observed result; `rejected` requires a requirements revision; `deferred`
   requires an explicit reason and scope boundary.
- `Depends on` contains gate IDs or external prerequisites, not vague prose.
   `Blocks` names downstream phases or requirement/design/task IDs that must not
   treat the assumption as fact until the gate is supported.
- State both the positive pass signal and the disconfirming result. A timeout,
   unavailable machine, missing dependency, or bad-quality sample is evidence
   of `open`/`rejected`, never permission to guess a default.
- When a gate changes, preserve the gate ID and append or update its evidence
   reference rather than renumbering it. Negative evidence is valuable and must
   remain visible.
- At the approval gate, summarize settled requirements, open gates, deferred
   gates, and the downstream phases blocked by each open gate. Requirements may
   be approved provisionally only when the user explicitly accepts the open
   gates; design, tasks, and execution must honor the `Blocks` column.
- When a user asks a question that the codebase or an available check can
   answer, investigate that dependency before asking the user. Ask one question
   at a time only for the remaining decision.
- Every rationale must name a mechanism, precedent, measured result, or
   explicit product constraint. Avoid unsupported phrases such as "for safety"
   without stating what unsafe behavior is prevented.
- If the why is not yet proven, label the decision or evidence as `open` or
   `deferred`; do not write an assumption as settled fact.
- When a requirement changes, update the rationale register and its reopen
   trigger in the same revision. Preserve stable decision IDs just as you
   preserve `R<n>` IDs.

## Gate

Unless the user opted into auto-approve (or mode is `quick`), end with an explicit approval gate: summarize the requirement ids, settled decision rationales, open/deferred decisions, and ask the user to approve before design begins. If they revise later, the orchestrator re-runs `sync_check.py` to catch downstream staleness.
