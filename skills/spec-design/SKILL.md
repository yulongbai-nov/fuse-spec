---
name: spec-design
description: Turn approved requirements into a traceable design.md whose sections cite the requirement ids they satisfy and define D-anchors for tasks to reference. Use when starting the design phase of a spec, after requirements are approved, or when revising a design.
---

# spec-design

Produce `design.md` for a spec at `.specs/NNN-<slug>/` from its approved `requirements.md` (or `bugfix.md`).

Template: `../templates/design.md`. Script: `../scripts/sync_check.py`.
Resolve paths relative to this file's directory.

## Process

1. Read the requirements file. Note every `R<n>` id and every `D<n>` decision
	rationale — the design must account for all requirements and preserve the
	reasons behind settled decisions.
2. Explore the codebase. Prefer reusing existing modules, seams, and patterns over inventing new ones. Respect any ADRs in the area. Use the project's glossary vocabulary.
3. Draft the design **section by section**, presenting each section and asking the user if it looks right before continuing (lighter grilling than requirements). Cover, as needed: overview + key decisions, architecture, components, data model/contracts, sequence, testing strategy. For every material design choice, state `Decision`, `Why`, `Evidence or assumption`, and `Reopen trigger`. Reuse the requirements decision IDs when the design implements an existing choice; introduce a new rationale only when the design adds a real decision.
4. Each component is a section with a **stable anchor `### D-<Name>`** and a **`satisfies: R<x>, R<y>`** line. Tasks will reference these anchors; never rename one that tasks already cite.
5. Ensure every requirement id is satisfied by at least one design section.
6. Add a concise decision-rationale summary near the start of the design and
	repeat the reason locally where a component boundary or lifecycle rule would
	otherwise be easy to misread. Rationale must explain mechanism or tradeoff,
	not merely restate the decision.

## Consistency check

Before the gate, run the sync check and do a semantic pass on whatever it flags:

```
python3 <skill-dir>/../scripts/sync_check.py .specs/NNN-<slug>
```

- `ERROR` = a reference doesn't resolve (e.g. `satisfies:` cites a missing requirement) → fix before proceeding.
- `WARN` = an orphan (e.g. a requirement satisfied by no section) → decide whether the design is incomplete or the warning is acceptable.
- Also perform a semantic rationale pass: flag a design choice whose `Why` no
	longer matches the codebase, evidence ledger, or requirements decision
	register. A resolved `R` link does not prove that its rationale is still
	valid.

The script is deterministic; you do the judgment call on each flagged item.

## Gate

Unless auto-approve is on, end with an approval gate: summarize the design sections and their `satisfies` coverage, list settled and open decision rationales, and ask the user to approve before tasks are generated.
