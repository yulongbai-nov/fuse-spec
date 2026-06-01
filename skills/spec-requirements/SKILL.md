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

## Gate

Unless the user opted into auto-approve (or mode is `quick`), end with an explicit approval gate: summarize the requirement ids and ask the user to approve before design begins. If they revise later, the orchestrator re-runs `sync_check.py` to catch downstream staleness.
