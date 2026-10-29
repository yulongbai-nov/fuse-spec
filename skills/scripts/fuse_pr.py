#!/usr/bin/env python3
"""PR publication and candidate-bound merge gates for managed Fuse runs."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

if TYPE_CHECKING:
    import fuse_git
    import fuse_record
else:
    try:
        from . import fuse_git, fuse_record
    except ImportError:
        import fuse_git
        import fuse_record

SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
REQUIRED_REVIEW_LEVELS = ("task", "spec_pr", "whole_codebase")


class ProviderError(RuntimeError):
    """A PR provider operation failed or could not be verified."""


class CandidateMergeProvider(Protocol):
    """Adapter whose merge operation is guarded by current server-side rules.

    The provider must pin the PR head in the merge request. Strict protected
    branch checks bind that head to the current base; a preflight read alone is
    not sufficient.
    """

    def supports_exact_head_binding(self) -> bool:
        """Return true only when the provider merge pins the validated PR head."""

    def view_pr(self, number: int) -> dict[str, Any]: ...

    def branch_rules(self, branch: str) -> list[dict[str, Any]]: ...

    def allowed_merge_methods(
        self, branch_rules: Sequence[Mapping[str, Any]]
    ) -> list[str]: ...

    def merge_pr(
        self,
        number: int,
        *,
        candidate_sha: str,
        base_sha: str,
        head_sha: str,
        merge_method: str,
        included_heads: Sequence[str],
        prerequisite_states: Mapping[str, Mapping[str, str]],
        required_checks: Sequence[str],
        check_evidence: Mapping[str, Mapping[str, Any]],
        review_evidence: Mapping[str, Mapping[str, Any]],
        unresolved_findings: Sequence[Mapping[str, Any]],
    ) -> str: ...


@dataclass(frozen=True)
class PRPlan:
    """A deterministic PR dependency DAG derived from the task DAG."""

    dependencies: Mapping[str, tuple[str, ...]]
    order: tuple[str, ...]


@dataclass(frozen=True)
class PRMergeCandidate:
    """Exact target candidate plus the merged prerequisite identities in its base."""

    candidate: fuse_git.MergeCandidate
    target_pr: str
    prerequisite_states: Mapping[str, Mapping[str, str]]

    @property
    def base_sha(self) -> str:
        return self.candidate.base_sha

    @property
    def head_shas(self) -> tuple[str, ...]:
        return self.candidate.head_shas

    @property
    def commit_sha(self) -> str:
        return self.candidate.commit_sha


@dataclass(frozen=True)
class MergeDecision:
    """A fail-closed decision for an exact integrated candidate."""

    allowed: bool
    reason: str
    candidate_sha: str
    details: Mapping[str, Any]


class GitHubCLI:
    """Thin `gh` adapter; methods are mockable without network access."""

    def __init__(
        self,
        repository: str,
        cwd: Path,
        *,
        executable: str = "gh",
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ProviderError("repository must use owner/name form")
        self.repository = repository
        self.cwd = cwd.resolve()
        self.executable = executable
        self.runner = runner or subprocess.run

    def _run(self, *arguments: str) -> str:
        if self.runner is subprocess.run and shutil.which(self.executable) is None:
            raise ProviderError(f"GitHub CLI is unavailable: {self.executable}")
        result = self.runner(
            [self.executable, *arguments],
            cwd=self.cwd,
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ProviderError(
                f"gh {' '.join(arguments)} failed ({result.returncode}): {detail}"
            )
        return result.stdout.strip()

    def find_open_prs(self, head_branch: str, base_branch: str) -> list[dict[str, Any]]:
        """Find open PRs for the exact branch pair."""
        output = self._run(
            "pr",
            "list",
            "--repo",
            self.repository,
            "--state",
            "open",
            "--head",
            head_branch,
            "--base",
            base_branch,
            "--json",
            "number,url,state,isDraft,baseRefName,headRefName,title",
        )
        try:
            data = json.loads(output or "[]")
        except json.JSONDecodeError as error:
            raise ProviderError("gh pr list returned invalid JSON") from error
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ProviderError("gh pr list returned an invalid result")
        return data

    def create_draft_pr(
        self,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        """Create a draft PR; never create a ready-for-review PR implicitly."""
        output = self._run(
            "pr",
            "create",
            "--repo",
            self.repository,
            "--head",
            head_branch,
            "--base",
            base_branch,
            "--title",
            title,
            "--body",
            body,
            "--draft",
        )
        match = re.search(r"/pull/(\d+)(?:\D|$)", output)
        if match is None:
            raise ProviderError("gh pr create did not return a pull request URL")
        return self.view_pr(int(match.group(1)))

    def view_pr(self, number: int) -> dict[str, Any]:
        """Read PR head/base, mergeability, reviews, and status-check data."""
        output = self._run(
            "pr",
            "view",
            str(number),
            "--repo",
            self.repository,
            "--json",
            "number,url,title,body,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,"
            "mergeable,mergeStateStatus,mergeCommit,mergedAt,reviewDecision,statusCheckRollup",
        )
        try:
            data = json.loads(output)
        except json.JSONDecodeError as error:
            raise ProviderError("gh pr view returned invalid JSON") from error
        if not isinstance(data, dict) or data.get("number") != number:
            raise ProviderError("gh pr view returned the wrong pull request")
        return data

    def update_pr_body(self, number: int, body: str) -> None:
        """Refresh managed spec/candidate context on an existing PR."""
        self._run("pr", "edit", str(number), "--repo", self.repository, "--body", body)

    def branch_rules(self, branch: str) -> list[dict[str, Any]]:
        """Read effective branch rules and their bypass lists, failing closed."""
        encoded = quote(branch, safe="")
        output = self._run(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{self.repository}/rules/branches/{encoded}?per_page=100",
        )
        try:
            data = json.loads(output)
        except json.JSONDecodeError as error:
            raise ProviderError(
                "GitHub effective branch rules returned invalid JSON"
            ) from error
        if not isinstance(data, list):
            raise ProviderError(
                "GitHub effective branch rules returned an invalid result"
            )
        if data and all(isinstance(row, dict) for row in data):
            pages = [data]
        elif all(
            isinstance(page, list) and all(isinstance(row, dict) for row in page)
            for page in data
        ):
            pages = data
        else:
            raise ProviderError(
                "GitHub paginated branch rules returned an invalid result"
            )
        effective_rules = [row for page in pages for row in page]

        rules: list[dict[str, Any]] = []

        ruleset_ids: set[int] = set()
        ruleset_sources: dict[int, tuple[str, str]] = {}
        for row in effective_rules:
            ruleset_id = row.get("ruleset_id")
            if not isinstance(ruleset_id, int) or isinstance(ruleset_id, bool):
                raise ProviderError("GitHub effective rule has an invalid ruleset ID")
            ruleset_ids.add(ruleset_id)
            source_type = row.get("ruleset_source_type")
            source = row.get("ruleset_source")
            if (
                not isinstance(source_type, str)
                or source_type not in {"Repository", "Organization"}
                or not isinstance(source, str)
                or not source
            ):
                raise ProviderError(
                    "GitHub effective rule has an unreadable ruleset source"
                )
            if (
                source_type == "Repository"
                and source.casefold() != self.repository.casefold()
            ):
                raise ProviderError(
                    "GitHub effective repository ruleset has an unexpected source"
                )
            identity = (source_type, source)
            previous = ruleset_sources.get(ruleset_id)
            if previous is not None and previous != identity:
                raise ProviderError(
                    "GitHub returned conflicting sources for an applicable ruleset"
                )
            ruleset_sources[ruleset_id] = identity
            parameters = row.get("parameters", {})
            if not isinstance(parameters, Mapping):
                raise ProviderError("GitHub effective rule has invalid parameters")
            rule_type = row.get("type")
            if rule_type == "pull_request":
                allowed_methods = parameters.get("allowed_merge_methods")
                if allowed_methods is not None:
                    if (
                        not isinstance(allowed_methods, Sequence)
                        or isinstance(allowed_methods, str)
                        or not allowed_methods
                        or any(
                            not isinstance(method, str)
                            or method not in {"merge", "squash", "rebase"}
                            for method in allowed_methods
                        )
                    ):
                        raise ProviderError(
                            "GitHub pull request merge methods are invalid"
                        )
                rules.append(
                    {
                        "type": "pull_request",
                        "required_approving_review_count": parameters.get(
                            "required_approving_review_count"
                        ),
                        "allowed_merge_methods": (
                            sorted(set(allowed_methods))
                            if allowed_methods is not None
                            else None
                        ),
                        "ruleset_id": ruleset_id,
                    }
                )
            elif rule_type == "required_status_checks":
                checks = parameters.get("required_status_checks", ())
                if not isinstance(checks, Sequence) or isinstance(checks, str):
                    raise ProviderError("GitHub required checks are invalid")
                contexts: list[str] = []
                for check in checks:
                    if (
                        not isinstance(check, Mapping)
                        or not isinstance(check.get("context"), str)
                        or not check.get("context")
                    ):
                        raise ProviderError(
                            "GitHub required check context is unreadable"
                        )
                    contexts.append(check["context"])
                rules.append(
                    {
                        "type": "required_status_checks",
                        "strict": parameters.get("strict_required_status_checks_policy")
                        is True,
                        "contexts": sorted(set(contexts)),
                        "ruleset_id": ruleset_id,
                    }
                )
            elif rule_type == "non_fast_forward":
                rules.append(
                    {
                        "type": "force_pushes",
                        "enabled": False,
                        "ruleset_id": ruleset_id,
                    }
                )
            elif rule_type == "deletion":
                rules.append(
                    {
                        "type": "deletions",
                        "enabled": False,
                        "ruleset_id": ruleset_id,
                    }
                )
            elif rule_type == "merge_queue":
                rules.append({"type": "merge_queue", "ruleset_id": ruleset_id})

        for ruleset_id in sorted(ruleset_ids):
            source_type, source = ruleset_sources[ruleset_id]
            ruleset_path = (
                f"repos/{self.repository}/rulesets/{ruleset_id}"
                if source_type == "Repository"
                else f"orgs/{quote(source, safe='')}/rulesets/{ruleset_id}"
            )
            details_output = self._run("api", ruleset_path)
            try:
                details = json.loads(details_output)
            except json.JSONDecodeError as error:
                raise ProviderError(
                    "GitHub ruleset details returned invalid JSON"
                ) from error
            if (
                not isinstance(details, dict)
                or details.get("id") != ruleset_id
                or details.get("enforcement") != "active"
            ):
                raise ProviderError("GitHub applicable ruleset is not confirmed active")
            bypass_actors = details.get("bypass_actors")
            if not isinstance(bypass_actors, Sequence) or isinstance(
                bypass_actors, str
            ):
                raise ProviderError("GitHub ruleset bypass actors are unreadable")
            rules.append(
                {
                    "type": "ruleset_bypass_actors",
                    "ruleset_id": ruleset_id,
                    "bypass_actors": list(bypass_actors),
                    "active": True,
                }
            )
        return rules

    def supports_exact_head_binding(self) -> bool:
        """GitHub's merge API pins the head; strict branch rules pin its base."""
        return True

    def allowed_merge_methods(
        self, branch_rules: Sequence[Mapping[str, Any]]
    ) -> list[str]:
        """Return methods enabled by both repository settings and all rulesets."""
        output = self._run("api", f"repos/{self.repository}")
        try:
            repository = json.loads(output)
        except json.JSONDecodeError as error:
            raise ProviderError(
                "GitHub repository settings returned invalid JSON"
            ) from error
        if not isinstance(repository, dict):
            raise ProviderError("GitHub repository settings are unreadable")
        setting_by_method = {
            "merge": "allow_merge_commit",
            "squash": "allow_squash_merge",
            "rebase": "allow_rebase_merge",
        }
        enabled: set[str] = set()
        for method, setting in setting_by_method.items():
            value = repository.get(setting)
            if not isinstance(value, bool):
                raise ProviderError("GitHub repository merge settings are unreadable")
            if value:
                enabled.add(method)
        for rule in branch_rules:
            if rule.get("type") != "pull_request":
                continue
            allowed = rule.get("allowed_merge_methods")
            if allowed is None:
                continue
            if (
                not isinstance(allowed, Sequence)
                or isinstance(allowed, str)
                or not allowed
                or any(method not in setting_by_method for method in allowed)
            ):
                raise ProviderError("GitHub ruleset merge methods are unreadable")
            enabled.intersection_update(allowed)
        return [method for method in ("squash", "merge", "rebase") if method in enabled]

    def merge_pr(
        self,
        number: int,
        *,
        candidate_sha: str,
        base_sha: str,
        head_sha: str,
        merge_method: str,
        included_heads: Sequence[str],
        prerequisite_states: Mapping[str, Mapping[str, str]],
        required_checks: Sequence[str],
        check_evidence: Mapping[str, Mapping[str, Any]],
        review_evidence: Mapping[str, Mapping[str, Any]],
        unresolved_findings: Sequence[Mapping[str, Any]],
    ) -> str:
        """Direct-merge only with the exact PR head and strict branch rules.

        The REST merge endpoint's `sha` field rejects a changed PR head. The
        protected base must require a pull request and strict required checks,
        so a changed base invalidates the successful merge-candidate checks.
        This call never requests a rules bypass.
        """
        del candidate_sha, base_sha, included_heads
        del prerequisite_states
        del required_checks, check_evidence, review_evidence, unresolved_findings
        if merge_method not in {"merge", "squash", "rebase"}:
            raise ProviderError("GitHub merge method is invalid")
        output = self._run(
            "api",
            "--method",
            "PUT",
            f"repos/{self.repository}/pulls/{number}/merge",
            "-f",
            f"sha={head_sha}",
            "-f",
            f"merge_method={merge_method}",
        )
        try:
            result = json.loads(output)
        except json.JSONDecodeError as error:
            raise ProviderError(
                "GitHub merge endpoint returned invalid JSON"
            ) from error
        if not isinstance(result, dict) or result.get("merged") is not True:
            raise ProviderError("GitHub did not confirm the guarded pull request merge")
        merge_sha = result.get("sha")
        if not isinstance(merge_sha, str) or not SHA_RE.fullmatch(merge_sha):
            raise ProviderError("GitHub merge endpoint returned an invalid merge SHA")
        return merge_sha


def _pr_key(value: str | int) -> str:
    key = str(value).strip()
    if not key or "\n" in key:
        raise ProviderError("pull request identity must be a non-empty single line")
    return key


def derive_pr_dependencies(
    task_dependencies: Mapping[str, Sequence[str]],
    task_to_pr: Mapping[str, str | int],
    *,
    branch_dependencies: Mapping[str, Sequence[str]] | None = None,
) -> PRPlan:
    """Collapse the task DAG and branch ancestry into an acyclic PR DAG."""
    task_graph: dict[str, set[str]] = {}
    for task_id, dependencies in task_dependencies.items():
        if not isinstance(task_id, str) or not task_id.strip():
            raise ProviderError("task IDs must be non-empty strings")
        if isinstance(dependencies, str):
            raise ProviderError(f"task {task_id} dependencies must be a sequence")
        task_graph[task_id] = set()
        for dependency in dependencies:
            if not isinstance(dependency, str) or dependency not in task_dependencies:
                raise ProviderError(
                    f"task {task_id} has unknown dependency {dependency}"
                )
            task_graph[task_id].add(dependency)
    try:
        from graphlib import CycleError, TopologicalSorter

        list(TopologicalSorter(task_graph).static_order())
    except CycleError as error:
        raise ProviderError("task dependency graph contains a cycle") from error

    unknown = sorted(set(task_to_pr) - set(task_dependencies))
    if unknown:
        raise ProviderError(
            "PR mapping references unknown task(s): " + ", ".join(unknown)
        )
    graph: dict[str, set[str]] = {}
    for task_id, pull_request in task_to_pr.items():
        target = _pr_key(pull_request)
        graph.setdefault(target, set())
        pending = list(task_graph[task_id])
        visited_tasks: set[str] = set()
        while pending:
            dependency = pending.pop()
            if dependency in visited_tasks:
                continue
            visited_tasks.add(dependency)
            prerequisite_pr = task_to_pr.get(dependency)
            if prerequisite_pr is not None:
                source = _pr_key(prerequisite_pr)
                graph.setdefault(source, set())
                if source != target:
                    graph[target].add(source)
            pending.extend(task_graph[dependency])

    for pull_request, dependencies in (branch_dependencies or {}).items():
        target = _pr_key(pull_request)
        if target not in graph:
            raise ProviderError(f"branch dependency references unknown PR {target}")
        if isinstance(dependencies, str):
            raise ProviderError(f"PR {target} dependencies must be a sequence")
        for dependency in dependencies:
            source = _pr_key(dependency)
            if source not in graph:
                raise ProviderError(
                    f"PR {target} has unknown branch prerequisite {source}"
                )
            if source != target:
                graph[target].add(source)
    try:
        from graphlib import CycleError, TopologicalSorter

        sorter = TopologicalSorter(
            {node: sorted(dependencies) for node, dependencies in graph.items()}
        )
        sorter.prepare()
        order: list[str] = []
        while sorter.is_active():
            ready = sorted(sorter.get_ready())
            order.extend(ready)
            sorter.done(*ready)
    except CycleError as error:
        raise ProviderError(
            "derived pull request dependency graph contains a cycle"
        ) from error
    return PRPlan(
        {
            node: tuple(sorted(dependencies))
            for node, dependencies in sorted(graph.items())
        },
        tuple(order),
    )


def ensure_draft_pr(
    provider: GitHubCLI,
    *,
    head_branch: str,
    base_branch: str,
    title: str,
    body: str,
    spec_id: str,
    candidate_id: str,
) -> tuple[dict[str, Any], bool]:
    """Reuse one compatible open PR or create a spec/candidate-linked draft."""
    if not spec_id.strip() or not candidate_id.strip():
        raise ProviderError("draft PR needs a spec ID and candidate identity")
    if spec_id not in body or candidate_id not in body:
        raise ProviderError(
            "draft PR body must identify the selected spec and candidate"
        )
    existing = provider.find_open_prs(head_branch, base_branch)
    if len(existing) > 1:
        raise ProviderError(
            "multiple compatible open PRs exist for the same branch pair"
        )
    if existing:
        row = existing[0]
        if row.get("state") != "OPEN" or row.get("isDraft") is not True:
            raise ProviderError("existing compatible PR is not an open draft")
        if (
            row.get("baseRefName") != base_branch
            or row.get("headRefName") != head_branch
        ):
            raise ProviderError(
                "existing PR branch pair does not match the requested pair"
            )
        number = row.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ProviderError("existing PR has no valid number")
        current = provider.view_pr(number)
        _validate_draft_pr_identity(current, head_branch, base_branch)
        current_body = current.get("body") or ""
        if spec_id not in current_body or candidate_id not in current_body:
            provider.update_pr_body(number, body)
            current = provider.view_pr(number)
            _validate_draft_pr_identity(current, head_branch, base_branch)
            refreshed_body = current.get("body") or ""
            if spec_id not in refreshed_body or candidate_id not in refreshed_body:
                raise ProviderError(
                    "updated PR body does not identify the current candidate"
                )
        return current, False
    created = provider.create_draft_pr(head_branch, base_branch, title, body)
    _validate_draft_pr_identity(created, head_branch, base_branch)
    created_body = created.get("body") or ""
    if spec_id not in created_body or candidate_id not in created_body:
        raise ProviderError(
            "new draft PR does not identify the selected spec and candidate"
        )
    return created, True


def _validate_draft_pr_identity(
    pr: Mapping[str, Any], head_branch: str, base_branch: str
) -> None:
    if pr.get("isDraft") is not True or pr.get("state") != "OPEN":
        raise ProviderError("PR was not confirmed as an open draft")
    if pr.get("baseRefName") != base_branch or pr.get("headRefName") != head_branch:
        raise ProviderError("PR branch pair does not match the requested pair")


def record_task_pr(
    record: dict[str, Any],
    *,
    task_id: str,
    provider: str,
    repository: str,
    number: int,
    base_sha: str,
    head_sha: str,
) -> dict[str, Any]:
    """Record the current provider, PR, base, and head for one task."""
    if (
        not isinstance(task_id, str)
        or not task_id.strip()
        or not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
    ):
        raise ProviderError("task ID and positive PR number are required")
    if not SHA_RE.fullmatch(base_sha) or not SHA_RE.fullmatch(head_sha):
        raise ProviderError("PR base and head must be full commit identities")
    if not provider.strip() or not repository.strip():
        raise ProviderError("provider and repository identities are required")
    rows = record.get("git", {}).get("pull_requests")
    if not isinstance(rows, list):
        raise ProviderError("run record has no canonical pull_requests list")
    identity = {
        "task_id": task_id,
        "provider": provider,
        "repository": repository,
        "number": number,
        "base": base_sha,
        "head": head_sha,
    }
    matches = [index for index, row in enumerate(rows) if row.get("task_id") == task_id]
    if len(matches) > 1:
        raise ProviderError(f"run record has multiple current PRs for task {task_id}")
    if matches:
        rows[matches[0]] = identity
    else:
        rows.append(identity)
    try:
        fuse_record.validate_run_record(record)
    except fuse_record.RecordError as error:
        raise ProviderError(
            f"PR identity invalidates the run record: {error}"
        ) from error
    return identity


def publish_task_commit(
    root: Path,
    *,
    remote: str,
    branch: str,
    commit_sha: str,
    expected_remote_head: str | None,
    record: dict[str, Any],
    task_id: str,
    repository: str,
    pr_number: int,
    base_sha: str,
    checkpoint: Callable[[Mapping[str, Any]], None],
) -> str:
    """Push one task commit, then checkpoint its exact PR identity before release."""
    if not callable(checkpoint):
        raise ProviderError("a canonical checkpoint callback is required")
    if (
        not isinstance(task_id, str)
        or not task_id.strip()
        or not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or pr_number < 1
        or not isinstance(repository, str)
        or not repository.strip()
        or not isinstance(base_sha, str)
        or not SHA_RE.fullmatch(base_sha)
    ):
        raise ProviderError("task, PR, repository, and base identities must be valid")
    try:
        fuse_record.validate_run_record(record)
    except fuse_record.RecordError as error:
        raise ProviderError(
            f"cannot publish with an invalid run record: {error}"
        ) from error
    if task_id not in record.get("tasks", {}):
        raise ProviderError(
            f"task {task_id} is not present in the canonical run record"
        )
    expected_parent = expected_remote_head or base_sha
    try:
        fuse_git.validate_atomic_job_commit(root, commit_sha, expected_parent)
    except fuse_git.GitError as error:
        raise ProviderError(str(error)) from error

    def after_push(published_sha: str) -> None:
        record_task_pr(
            record,
            task_id=task_id,
            provider="github",
            repository=repository,
            number=pr_number,
            base_sha=base_sha,
            head_sha=published_sha,
        )
        record["git"]["base_commit"] = base_sha
        record["git"]["integration_branch"] = branch
        record["git"]["current_commit"] = published_sha
        try:
            checkpoint(record)
        except Exception as error:
            raise ProviderError(
                "code commit was pushed but the canonical checkpoint failed; "
                "reconcile the remote head before releasing dependent work"
            ) from error

    try:
        return fuse_git.push_fast_forward(
            root,
            remote,
            branch,
            commit_sha,
            expected_remote_head=expected_remote_head,
            after_push=after_push,
        )
    except fuse_git.GitError as error:
        raise ProviderError(str(error)) from error


def build_merge_candidate(
    root: Path,
    *,
    base_sha: str,
    task_dependencies: Mapping[str, Sequence[str]],
    task_to_pr: Mapping[str, str | int],
    pr_heads: Mapping[str, str],
    pr_states: Mapping[str, Mapping[str, Any]],
    target_pr: str | int,
) -> PRMergeCandidate:
    """Build one merge candidate after proving mapped prerequisites are merged.

    Only PR heads in the target's task or supplied branch-ancestry closure are
    required. A prerequisite must be merged, its recorded head must still match,
    and its provider merge commit must be contained in the current base.
    """
    plan = derive_pr_dependencies(task_dependencies, task_to_pr)
    target = _pr_key(target_pr)
    if target not in plan.dependencies:
        raise ProviderError(f"target PR {target} has no task mapping")
    normalized_heads = {_pr_key(key): value for key, value in pr_heads.items()}
    normalized_states = {_pr_key(key): value for key, value in pr_states.items()}

    required: set[str] = set()

    def add_prerequisites(pr: str, dependencies: Mapping[str, Sequence[str]]) -> None:
        if pr in required:
            return
        required.add(pr)
        for dependency in dependencies[pr]:
            add_prerequisites(dependency, dependencies)

    add_prerequisites(target, plan.dependencies)
    missing = sorted(required - set(normalized_heads))
    if missing:
        raise ProviderError("PR head identity missing for: " + ", ".join(missing))
    branch_dependencies: dict[str, list[str]] = {}
    try:
        changed = True
        while changed:
            changed = False
            for child in sorted(required):
                child_head = normalized_heads[child]
                for parent, parent_head in normalized_heads.items():
                    if (
                        parent not in plan.dependencies
                        or parent == child
                        or parent in required
                    ):
                        continue
                    if fuse_git.is_ancestor(root, parent_head, child_head):
                        branch_dependencies.setdefault(child, []).append(parent)
                        required.add(parent)
                        changed = True
        plan = derive_pr_dependencies(
            task_dependencies,
            task_to_pr,
            branch_dependencies=branch_dependencies,
        )
    except fuse_git.GitError as error:
        raise ProviderError(str(error)) from error
    required = set()
    add_prerequisites(target, plan.dependencies)
    missing = sorted(required - set(normalized_heads))
    if missing:
        raise ProviderError("PR head identity missing for: " + ", ".join(missing))
    ordered = [pr for pr in plan.order if pr in required]

    prerequisite_states: dict[str, Mapping[str, str]] = {}
    for prerequisite in ordered:
        if prerequisite == target:
            continue
        state = normalized_states.get(prerequisite)
        if (
            state is None
            or state.get("state") not in {"MERGED", "CLOSED"}
            or not state.get("mergedAt")
        ):
            raise ProviderError(
                f"prerequisite PR {prerequisite} is not confirmed merged"
            )
        prerequisite_head = state.get("headRefOid")
        if prerequisite_head != normalized_heads[prerequisite]:
            raise ProviderError(f"prerequisite PR {prerequisite} head identity changed")
        merge_commit = state.get("mergeCommit")
        merge_sha = (
            merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None
        )
        if not isinstance(merge_sha, str) or not SHA_RE.fullmatch(merge_sha):
            raise ProviderError(
                f"prerequisite PR {prerequisite} has no verified merge commit"
            )
        try:
            if not fuse_git.is_ancestor(root, merge_sha, base_sha):
                raise ProviderError(
                    f"current base does not contain prerequisite PR {prerequisite}"
                )
        except fuse_git.GitError as error:
            raise ProviderError(str(error)) from error
        prerequisite_states[prerequisite] = {
            "head_sha": prerequisite_head,
            "merge_sha": merge_sha,
        }

    try:
        candidate = fuse_git.materialize_merge_candidate(
            root, base_sha, [normalized_heads[target]]
        )
    except fuse_git.GitError as error:
        raise ProviderError(str(error)) from error
    return PRMergeCandidate(candidate, target, prerequisite_states)


def build_integrated_candidate(
    root: Path,
    *,
    base_sha: str,
    task_dependencies: Mapping[str, Sequence[str]],
    task_to_pr: Mapping[str, str | int],
    pr_heads: Mapping[str, str],
) -> fuse_git.MergeCandidate:
    """Materialize all mapped PR heads for the integrated review level.

    This candidate can contain open prerequisite PRs and is useful for broad
    review. It is deliberately a plain Git candidate, so `assess_merge` cannot
    mistake it for the exact per-PR candidate with merged prerequisites.
    """
    plan = derive_pr_dependencies(task_dependencies, task_to_pr)
    normalized_heads = {_pr_key(key): value for key, value in pr_heads.items()}
    missing = sorted(set(plan.dependencies) - set(normalized_heads))
    if missing:
        raise ProviderError("PR head identity missing for: " + ", ".join(missing))
    branch_dependencies: dict[str, list[str]] = {}
    try:
        for child, child_head in normalized_heads.items():
            if child not in plan.dependencies:
                continue
            for parent, parent_head in normalized_heads.items():
                if (
                    parent != child
                    and parent in plan.dependencies
                    and fuse_git.is_ancestor(root, parent_head, child_head)
                ):
                    branch_dependencies.setdefault(child, []).append(parent)
        plan = derive_pr_dependencies(
            task_dependencies,
            task_to_pr,
            branch_dependencies=branch_dependencies,
        )
        ordered_heads = [normalized_heads[pr] for pr in plan.order]
        return fuse_git.materialize_merge_candidate(root, base_sha, ordered_heads)
    except fuse_git.GitError as error:
        raise ProviderError(str(error)) from error


def _blocked(reason: str, candidate_sha: str, **details: Any) -> MergeDecision:
    return MergeDecision(False, reason, candidate_sha, details)


def _provider_checks_ready(value: Any) -> bool:
    """Accept completed check runs or successful commit status contexts."""
    if not isinstance(value, Mapping):
        return False
    state = value.get("state")
    if state is not None:
        return isinstance(state, str) and state == "SUCCESS"
    status = value.get("status")
    conclusion = value.get("conclusion")
    return status == "COMPLETED" and conclusion in {
        "SUCCESS",
        "NEUTRAL",
        "SKIPPED",
    }


def _provider_check_pending(value: Mapping[str, Any]) -> bool:
    return value.get("state") in {"PENDING", "EXPECTED"} or value.get("status") in {
        "QUEUED",
        "IN_PROGRESS",
        "PENDING",
        "REQUESTED",
        "WAITING",
    }


def _strict_branch_ruleset_ready(
    branch_rules: Sequence[Mapping[str, Any]],
) -> bool:
    """Require active, non-bypassable PR and strict-check rules on the target."""
    pull_requests = [
        rule for rule in branch_rules if rule.get("type") == "pull_request"
    ]
    status_checks = [
        rule for rule in branch_rules if rule.get("type") == "required_status_checks"
    ]
    force_pushes = next(
        (rule for rule in branch_rules if rule.get("type") == "force_pushes"), None
    )
    deletions = next(
        (rule for rule in branch_rules if rule.get("type") == "deletions"), None
    )
    bypass_rules = [
        rule for rule in branch_rules if rule.get("type") == "ruleset_bypass_actors"
    ]
    if any(
        rule.get("ruleset_id") is not None
        and (
            not isinstance(rule.get("ruleset_id"), int)
            or isinstance(rule.get("ruleset_id"), bool)
        )
        for rule in branch_rules
    ):
        return False
    ruleset_ids = {
        rule.get("ruleset_id")
        for rule in branch_rules
        if isinstance(rule.get("ruleset_id"), int)
        and not isinstance(rule.get("ruleset_id"), bool)
    }
    required_rule_ids = {
        rule.get("ruleset_id")
        for rule in (*pull_requests, *status_checks, force_pushes, deletions)
        if isinstance(rule, Mapping)
    }
    if (
        not pull_requests
        or not all(isinstance(rule, Mapping) for rule in pull_requests)
        or not all(isinstance(rule, Mapping) for rule in (force_pushes, deletions))
        or not status_checks
        or not all(isinstance(rule, Mapping) for rule in status_checks)
    ):
        return False
    assert force_pushes is not None
    assert deletions is not None
    no_bypass = (
        bool(ruleset_ids)
        and required_rule_ids <= ruleset_ids
        and {rule.get("ruleset_id") for rule in bypass_rules} == ruleset_ids
        and all(
            rule.get("active") is True and rule.get("bypass_actors") == []
            for rule in bypass_rules
        )
    )
    return (
        all(
            rule.get("type") == "pull_request"
            and isinstance(rule.get("required_approving_review_count"), int)
            and not isinstance(rule.get("required_approving_review_count"), bool)
            and rule.get("required_approving_review_count", -1) >= 0
            and (
                rule.get("allowed_merge_methods") is None
                or (
                    isinstance(rule.get("allowed_merge_methods"), Sequence)
                    and not isinstance(rule.get("allowed_merge_methods"), str)
                    and bool(rule.get("allowed_merge_methods"))
                    and all(
                        isinstance(method, str)
                        and method in {"merge", "squash", "rebase"}
                        for method in rule.get("allowed_merge_methods", ())
                    )
                )
            )
            for rule in pull_requests
        )
        and no_bypass
        and all(
            rule.get("strict") is True
            and isinstance((contexts := rule.get("contexts", ())), Sequence)
            and not isinstance(contexts, str)
            and bool(contexts)
            and all(isinstance(context, str) and context for context in contexts)
            for rule in status_checks
        )
        and force_pushes.get("enabled") is False
        and deletions.get("enabled") is False
        and not any(rule.get("type") == "merge_queue" for rule in branch_rules)
    )


def _required_provider_checks_ready(
    branch_rules: Sequence[Mapping[str, Any]],
    external_checks: Sequence[Mapping[str, Any]],
) -> bool:
    rules = [
        item for item in branch_rules if item.get("type") == "required_status_checks"
    ]
    if not rules or not all(isinstance(rule, Mapping) for rule in rules):
        return False
    for rule in rules:
        contexts = rule.get("contexts", ())
        if not isinstance(contexts, Sequence) or isinstance(contexts, str):
            return False
        if not all(
            isinstance(context, str)
            and any(
                (check.get("name") or check.get("context")) == context
                and _provider_checks_ready(check)
                for check in external_checks
            )
            for context in contexts
        ):
            return False
    return True


def assess_merge(
    pr: Mapping[str, Any],
    candidate: PRMergeCandidate,
    *,
    provider: CandidateMergeProvider | None = None,
    expected_base_sha: str,
    expected_head_sha: str,
    required_checks: Sequence[str],
    check_evidence: Mapping[str, Mapping[str, Any]],
    review_evidence: Mapping[str, Mapping[str, Any]],
    unresolved_findings: Sequence[Mapping[str, Any]],
    branch_rules: Sequence[Mapping[str, Any]],
) -> MergeDecision:
    """Require candidate-bound checks, reviews, prerequisite, and provider gates."""
    if not isinstance(candidate, PRMergeCandidate):
        raise ProviderError(
            "merge assessment requires a PR candidate with verified prerequisite state"
        )
    candidate_sha = candidate.commit_sha
    pr_number = pr.get("number")
    if (
        not isinstance(pr_number, (str, int))
        or isinstance(pr_number, bool)
        or _pr_key(pr_number) != _pr_key(candidate.target_pr)
    ):
        return _blocked("candidate_targets_a_different_pull_request", candidate_sha)
    if pr.get("state") != "OPEN":
        return _blocked("pull_request_not_open", candidate_sha)
    if pr.get("isDraft") is not False:
        return _blocked("pull_request_is_draft", candidate_sha)
    if (
        pr.get("baseRefOid") != expected_base_sha
        or candidate.base_sha != expected_base_sha
    ):
        return _blocked("base_candidate_stale", candidate_sha)
    if pr.get("headRefOid") != expected_head_sha:
        return _blocked("pull_request_head_stale", candidate_sha)
    if not candidate.head_shas or candidate.head_shas[-1] != expected_head_sha:
        return _blocked("target_head_not_in_merge_candidate", candidate_sha)
    if pr.get("mergeable") not in {True, "MERGEABLE"}:
        return _blocked("provider_reports_not_mergeable", candidate_sha)
    if pr.get("mergeStateStatus") != "CLEAN":
        return _blocked("provider_merge_state_not_clean", candidate_sha)
    if pr.get("reviewDecision") in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
        return _blocked("repository_review_gate_not_satisfied", candidate_sha)

    external_checks = pr.get("statusCheckRollup", ())
    if not isinstance(external_checks, Sequence) or isinstance(external_checks, str):
        return _blocked("provider_checks_unreadable", candidate_sha)
    for item in external_checks:
        if not isinstance(item, Mapping):
            return _blocked("provider_check_invalid", candidate_sha)
        if _provider_check_pending(item):
            return _blocked(
                "provider_check_pending", candidate_sha, check=item.get("name")
            )
        if not _provider_checks_ready(item):
            return _blocked(
                "provider_check_failed", candidate_sha, check=item.get("name")
            )

    missing_checks: list[str] = []
    for check_id in required_checks:
        evidence = check_evidence.get(check_id)
        if (
            evidence is None
            or evidence.get("candidate_sha") != candidate_sha
            or evidence.get("base_sha") != expected_base_sha
            or evidence.get("head_sha") != expected_head_sha
            or evidence.get("outcome") != "pass"
        ):
            missing_checks.append(check_id)
    if missing_checks:
        return _blocked(
            "merge_candidate_checks_missing_or_stale",
            candidate_sha,
            checks=sorted(missing_checks),
        )

    missing_reviews: list[str] = []
    for level in REQUIRED_REVIEW_LEVELS:
        evidence = review_evidence.get(level)
        if (
            evidence is None
            or evidence.get("candidate_sha") != candidate_sha
            or evidence.get("base_sha") != expected_base_sha
            or evidence.get("head_sha") != expected_head_sha
            or evidence.get("outcome") != "pass"
        ):
            missing_reviews.append(level)
    if missing_reviews:
        return _blocked(
            "merge_candidate_reviews_missing_or_stale",
            candidate_sha,
            levels=missing_reviews,
        )
    if unresolved_findings:
        return _blocked(
            "unresolved_review_findings",
            candidate_sha,
            finding_ids=[item.get("id") for item in unresolved_findings],
        )
    if not _strict_branch_ruleset_ready(branch_rules):
        return _blocked("strict_branch_protection_unavailable", candidate_sha)
    if not _required_provider_checks_ready(branch_rules, external_checks):
        return _blocked("provider_required_check_missing_or_failed", candidate_sha)
    try:
        binding_supported = (
            provider is not None and provider.supports_exact_head_binding()
        )
    except Exception:
        binding_supported = False
    if provider is None or not binding_supported:
        return _blocked(
            "provider_cannot_bind_exact_merge_candidate",
            candidate_sha,
            base_sha=expected_base_sha,
            head_sha=expected_head_sha,
            prerequisite_states=dict(candidate.prerequisite_states),
        )
    try:
        available_merge_methods = provider.allowed_merge_methods(branch_rules)
    except Exception:
        return _blocked("provider_merge_methods_unavailable", candidate_sha)
    if (
        not isinstance(available_merge_methods, Sequence)
        or isinstance(available_merge_methods, str)
        or not available_merge_methods
        or any(
            not isinstance(method, str) or method not in {"merge", "squash", "rebase"}
            for method in available_merge_methods
        )
    ):
        return _blocked("provider_has_no_compatible_merge_method", candidate_sha)
    return MergeDecision(
        True,
        "ready_for_direct_merge",
        candidate_sha,
        {
            "base_sha": expected_base_sha,
            "head_sha": expected_head_sha,
            "target_pr": candidate.target_pr,
            "merge_method": available_merge_methods[0],
            "included_heads": list(candidate.head_shas),
            "prerequisite_states": {
                key: dict(value) for key, value in candidate.prerequisite_states.items()
            },
            "review_levels": list(REQUIRED_REVIEW_LEVELS),
            "checks": list(required_checks),
            "check_evidence": {
                check_id: dict(check_evidence[check_id]) for check_id in required_checks
            },
            "review_evidence": {
                level: dict(review_evidence[level]) for level in REQUIRED_REVIEW_LEVELS
            },
            "unresolved_findings": [],
        },
    )


def merge_pr(
    provider: CandidateMergeProvider,
    *,
    number: int,
    expected_head_sha: str,
    decision: MergeDecision,
    refresh_assessment: Callable[[], MergeDecision],
) -> str:
    """Direct-merge only after reloading current Fuse evidence and server gates.

    ``refresh_assessment`` must reload the authoritative run record, current
    reviews/findings, and candidate-bound checks, then rerun ``assess_merge``.
    This catches evidence invalidated while the provider preflight is running.
    """
    if not decision.allowed:
        raise ProviderError(f"merge is blocked: {decision.reason}")
    if (
        decision.reason != "ready_for_direct_merge"
        or decision.details.get("head_sha") != expected_head_sha
        or decision.details.get("target_pr") != str(number)
    ):
        raise ProviderError("merge decision is not bound to the requested PR head")
    try:
        binding_supported = provider.supports_exact_head_binding()
    except Exception as error:
        raise ProviderError(
            "provider exact-candidate binding capability could not be verified"
        ) from error
    if not binding_supported:
        raise ProviderError(
            "provider cannot atomically bind the actual merge to Fuse's exact "
            "candidate, checks, reviews, and prerequisite identities"
        )
    try:
        current = provider.view_pr(number)
        expected_base = decision.details.get("base_sha")
        expected_heads = tuple(decision.details.get("included_heads", ()))
        expected_prerequisites = decision.details.get("prerequisite_states", {})
        if not isinstance(expected_prerequisites, Mapping):
            raise ProviderError("merge decision has invalid prerequisite identities")
        if current.get("state") != "OPEN" or current.get("isDraft") is not False:
            raise ProviderError(
                "merge guard changed before merge: PR is not open and ready"
            )
        if current.get("baseRefOid") != expected_base:
            raise ProviderError("merge guard changed before merge: base moved")
        if current.get("headRefOid") != expected_head_sha:
            raise ProviderError("merge guard changed before merge: PR head moved")
        if expected_heads != (expected_head_sha,):
            raise ProviderError(
                "merge guard changed before merge: candidate is not the exact "
                "single-target merge candidate"
            )
        for prerequisite, expected in expected_prerequisites.items():
            if not isinstance(expected, Mapping):
                raise ProviderError(
                    "merge decision has invalid prerequisite identities"
                )
            try:
                prerequisite_number = int(prerequisite)
            except (TypeError, ValueError) as error:
                raise ProviderError(
                    f"cannot refresh prerequisite PR {prerequisite!r} by provider ID"
                ) from error
            current_state = provider.view_pr(prerequisite_number)
            if current_state.get("number") != prerequisite_number:
                raise ProviderError(
                    f"provider returned the wrong prerequisite PR {prerequisite}"
                )
            merge_commit = current_state.get("mergeCommit")
            current_merge_sha = (
                merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None
            )
            if (
                current_state.get("state") not in {"MERGED", "CLOSED"}
                or not current_state.get("mergedAt")
                or current_state.get("headRefOid") != expected.get("head_sha")
                or current_merge_sha != expected.get("merge_sha")
            ):
                raise ProviderError(
                    f"merge guard changed before merge: prerequisite PR "
                    f"{prerequisite} is no longer the validated merge"
                )
        if current.get("mergeable") not in {True, "MERGEABLE"}:
            raise ProviderError("merge guard changed before merge: PR is not mergeable")
        if current.get("mergeStateStatus") != "CLEAN":
            raise ProviderError(
                "merge guard changed before merge: provider state is not clean"
            )
        if current.get("reviewDecision") in {"CHANGES_REQUESTED", "REVIEW_REQUIRED"}:
            raise ProviderError("merge guard changed before merge: review is required")
        external_checks = current.get("statusCheckRollup", ())
        if not isinstance(external_checks, Sequence) or isinstance(
            external_checks, str
        ):
            raise ProviderError(
                "merge guard changed before merge: checks are unreadable"
            )
        if any(not _provider_checks_ready(item) for item in external_checks):
            raise ProviderError(
                "merge guard changed before merge: checks are not green"
            )
        rules = provider.branch_rules(str(current.get("baseRefName", "")))
        if not _strict_branch_ruleset_ready(rules):
            raise ProviderError(
                "merge guard is unavailable: strict branch protection is missing"
            )
        if not _required_provider_checks_ready(rules, external_checks):
            raise ProviderError(
                "merge guard changed before merge: required provider checks are missing"
            )
        merge_method = decision.details.get("merge_method")
        try:
            current_merge_methods = provider.allowed_merge_methods(rules)
        except Exception as error:
            raise ProviderError(
                "merge guard is unavailable: repository merge methods are unreadable"
            ) from error
        if merge_method not in current_merge_methods:
            raise ProviderError(
                "merge guard changed before merge: selected merge method is "
                "no longer allowed"
            )
        try:
            refreshed = refresh_assessment()
        except Exception as error:
            raise ProviderError(
                "current Fuse merge evidence could not be refreshed"
            ) from error
        if not isinstance(refreshed, MergeDecision) or refreshed != decision:
            raise ProviderError(
                "current Fuse review/check evidence changed after merge assessment"
            )
        try:
            return provider.merge_pr(
                number,
                candidate_sha=decision.candidate_sha,
                base_sha=str(expected_base),
                head_sha=expected_head_sha,
                merge_method=str(merge_method),
                included_heads=expected_heads,
                prerequisite_states=expected_prerequisites,
                required_checks=decision.details.get("checks", ()),
                check_evidence=decision.details.get("check_evidence", {}),
                review_evidence=decision.details.get("review_evidence", {}),
                unresolved_findings=decision.details.get("unresolved_findings", ()),
            )
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                "provider guarded merge failed; automatic merge is blocked"
            ) from error
    except ProviderError:
        raise
