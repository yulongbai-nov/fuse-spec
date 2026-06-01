# Bugfix: [Bug Title]

- **Spec**: NNN-<slug>
- **Mode**: bugfix
- **Status**: draft | approved

## Summary

[1-2 sentences: what is broken, observed where, and the impact.]

## Glossary

| Term | Definition |
|------|------------|
| [Term] | [Precise definition.] |

## Bug Analysis

<!--
Bugfix specs replace the requirements phase with bug analysis. Each item still
gets a stable R<n> id so design.md and tasks.md can reference it. Use EARS:
  Current   = what happens now (the defect)
  Expected  = what SHALL happen after the fix
  Unchanged = what SHALL CONTINUE TO happen (regression guard)
-->

### R1: [Defect title]

- **Current behavior:** WHEN [condition] THEN the system [incorrect behavior].
- **Expected behavior:** WHEN [condition] THEN the system SHALL [correct behavior].
- **Steps to reproduce:**
  1. [Step]
  2. [Step]

### R2: [Behavior that must not regress]

- **Unchanged behavior:** WHEN [condition] THEN the system SHALL CONTINUE TO [existing behavior].

## Out of Scope

- [Related issues deliberately not addressed by this fix.]
