# Requirements: [Feature Name]

- **Spec**: NNN-<slug>
- **Mode**: requirements-first | design-first | quick
- **Status**: draft | approved

## Introduction

[2-3 paragraphs: what this feature is, the problem it solves, and why it's needed.]

## Glossary

<!-- Canonical project terms used in this spec. A glossary, not a spec: no
implementation details. Add a row the first time a term needs pinning down. -->

| Term | Definition |
|------|------------|
| [Term] | [Precise, single-meaning definition.] |

## Decision Rationale Register

<!-- Record why each material decision was chosen, not only what was chosen.
Use stable D<n> ids when later design or tasks need to refer to a decision. -->

| Decision | Requirement IDs | Choice | Why | Evidence / assumption | Reopen trigger |
|----------|-----------------|--------|-----|-----------------------|----------------|
| D1: [decision] | R1 | [chosen option] | [mechanism, tradeoff, or constraint] | [evidence or explicit assumption] | [result that would reopen it] |

## Requirements

<!--
Each requirement has a stable id heading "### R<n>: Title" — the id is what
design.md and tasks.md reference, so never renumber an existing requirement.
Acceptance criteria use EARS (Easy Approach to Requirements Syntax):
  WHEN  <event/trigger>      THEN the system SHALL <response>
  IF    <precondition/state> THEN the system SHALL <response>
  WHILE <ongoing condition>       the system SHALL <continuous behavior>
  WHERE <context/location>        the system SHALL <contextual behavior>
-->

### R1: [Requirement title]

**User Story:** As a [role], I want [capability], so that [benefit].

**Acceptance Criteria:**

1. WHEN [event] THEN the system SHALL [response].
2. IF [condition] THEN the system SHALL [behavior].

Priority: High | Medium | Low

### R2: [Requirement title]

**User Story:** As a [role], I want [capability], so that [benefit].

**Acceptance Criteria:**

1. WHEN [event] THEN the system SHALL [response].

Priority: High | Medium | Low

## Non-Functional Requirements

<!-- Optional. Give these ids too (continue the R<n> sequence) if design or
tasks need to reference them; otherwise keep them as plain prose. -->

### R3: [Performance / security / reliability requirement]

1. WHEN [load/condition] THEN the system SHALL [measurable criteria].

## Out of Scope

- [Explicitly excluded item, so reviewers know it was considered and dropped.]
