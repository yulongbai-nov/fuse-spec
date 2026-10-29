# Requirements: Cleanup Refactor and Deduplication

- **Spec**: 002-cleanup-refactor
- **Mode**: quick
- **Status**: approved

## Introduction

This cleanup round removes repeated implementation and test-fixture logic in the merged P-Stack integration while preserving behavior and public interfaces. It also restores the complete approved design and requirements files for spec 001, whose merged copies were truncated during the prior upload.

The work stays narrow: one source of truth for canonical JSON digests and UTC timestamps, one shared test Git/project fixture helper, and complete spec documentation. It does not redesign Fuse's modules or add dependencies.

## Glossary

| Term | Definition |
|------|------------|
| Canonical JSON digest | SHA-256 over JSON serialized with sorted keys, compact separators, UTF-8, non-ASCII preserved, and non-finite numbers rejected. |
| Shared test helper | A test-only utility used by multiple test modules for identical Git setup or a common minimal repository fixture. |

## Requirements

### R1: Preserve complete approved integration specifications

**User Story:** As a maintainer, I want the complete approved design and requirements retained on main, so that the specification remains the authoritative record of the merged implementation.

**Acceptance Criteria:**

1. WHEN this cleanup PR is prepared THEN the design.md and requirements.md files under spec 001 SHALL contain the complete approved content from the integration branch, including all sections and traceability references.
2. WHEN those files are restored THEN their content SHALL not be shortened, paraphrased, or otherwise revised as part of this cleanup.

Priority: High

### R2: Share canonical digest and timestamp logic

**User Story:** As a maintainer, I want one implementation of canonical JSON hashing and UTC timestamp formatting, so that records and checks cannot drift in serialization or timestamp format.

**Acceptance Criteria:**

1. WHEN record, check, or reconciliation code computes a JSON digest THEN it SHALL use the shared helper and retain that call site's existing serialization options and SHA-256 output.
2. WHEN state and record code create a UTC timestamp THEN both SHALL use the same helper and retain the existing ISO-8601 UTC format ending in Z.
3. WHEN candidates, check evidence, and persisted run records are created THEN their identities and serialized contracts SHALL remain unchanged for the same inputs.

Priority: High

### R3: Share repeated test Git and project setup

**User Story:** As a maintainer, I want common Git commands and minimal candidate/check repository fixtures defined once, so that test setup stays consistent and easier to update.

**Acceptance Criteria:**

1. WHEN multiple test modules need the same basic Git command wrapper THEN they SHALL import one test-only helper with equivalent cwd, output, and failure behavior.
2. WHEN candidate and check tests create the same minimal spec repository THEN the shared fixture builder SHALL own the common file and Git setup while callers specify only meaningful differences.
3. WHEN tests exercise different Git behavior or project shapes THEN those domain-specific fixtures SHALL remain local to their tests.

Priority: Medium

### R4: Keep the cleanup behavior-preserving and dependency-neutral

**User Story:** As a maintainer, I want this refactor to stay small and behavior-preserving, so that maintainability improves without introducing new operational risk.

**Acceptance Criteria:**

1. WHEN the refactor is complete THEN the existing full test suite, configured static checks, specification traceability, and dependency planning SHALL pass.
2. WHEN source and test helpers are consolidated THEN public commands, schemas, persisted record shapes, candidate identities, check semantics, and dependency selections SHALL remain unchanged.
3. WHEN the PR is reviewed THEN the reviewer SHALL check the whole codebase for further material duplication or accidental scope growth and record any remaining items.

Priority: High

## Non-Functional Requirements

- No new runtime or development dependency is introduced.
- The cleanup should reduce duplicated logic without introducing a general-purpose abstraction used only once.

## Out of Scope

- Splitting large modules or redesigning their public boundaries.
- Changing task routing, verification policy, candidate identity, record schema, CLI behavior, or dependency management.
- Broad formatting-only changes or unrelated cleanup.
