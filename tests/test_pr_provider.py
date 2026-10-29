"""Local Git and mocked-provider scenarios for PR publication and merge gates."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPT_DIR = Path(__file__).parents[1] / "skills" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
fuse_git: Any = importlib.import_module("fuse_git")
fuse_pr: Any = importlib.import_module("fuse_pr")
fuse_record: Any = importlib.import_module("fuse_record")


def strict_branch_rules(checks: tuple[str, ...] = ("tests",)) -> list[dict[str, Any]]:
    ruleset_id = 42
    return [
        {
            "type": "pull_request",
            "required_approving_review_count": 0,
            "allowed_merge_methods": ["squash"],
            "ruleset_id": ruleset_id,
        },
        {
            "type": "required_status_checks",
            "strict": True,
            "contexts": list(checks),
            "ruleset_id": ruleset_id,
        },
        {"type": "force_pushes", "enabled": False, "ruleset_id": ruleset_id},
        {"type": "deletions", "enabled": False, "ruleset_id": ruleset_id},
        {
            "type": "ruleset_bypass_actors",
            "ruleset_id": ruleset_id,
            "bypass_actors": [],
            "active": True,
        },
    ]


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    )
    return str(result.stdout.strip())


def init_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(root)],
        check=True,
        capture_output=True,
    )
    git(root, "config", "user.name", "Fuse test")
    git(root, "config", "user.email", "fuse-test@example.invalid")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "base.txt")
    git(root, "commit", "--quiet", "-m", "base")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "--quiet", "-u", "origin", "main")
    base = git(root, "rev-parse", "HEAD")
    return root, remote, base


def commit_file(root: Path, name: str, contents: str, message: str) -> str:
    (root / name).write_text(contents, encoding="utf-8")
    git(root, "add", name)
    git(root, "commit", "--quiet", "-m", message)
    return git(root, "rev-parse", "HEAD")


def test_push_is_fast_forward_only_and_checkpoints_after_remote_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _remote, base = init_repo(tmp_path)
    git(root, "checkout", "--quiet", "-b", "feature/T1")
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    events: list[str] = []
    real_git = fuse_git._git

    def guarded_git(
        repository: Path,
        *arguments: str,
        env: Any = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if arguments and arguments[0] == "push":
            assert "--force" not in arguments
            assert "-f" not in arguments
        return cast(
            subprocess.CompletedProcess[str],
            real_git(repository, *arguments, env=env, check=check),
        )

    monkeypatch.setattr(fuse_git, "_git", guarded_git)

    def checkpoint(published: str) -> None:
        assert fuse_git.remote_branch_head(root, "origin", "feature/T1") == published
        events.append("checkpoint")

    published = fuse_git.push_fast_forward(
        root,
        "origin",
        "feature/T1",
        head,
        expected_remote_head=None,
        after_push=checkpoint,
    )

    assert published == head
    assert fuse_git.remote_branch_head(root, "origin", "feature/T1") == head
    assert fuse_git.is_ancestor(root, base, head)
    assert events == ["checkpoint"]


def test_publish_rejects_stale_expected_remote_head(tmp_path: Path) -> None:
    root, remote, base = init_repo(tmp_path)
    git(root, "checkout", "--quiet", "-b", "feature/T1")
    proposed = commit_file(root, "feature.txt", "local\n", "local candidate")

    competitor = tmp_path / "competitor"
    subprocess.run(
        ["git", "clone", "--quiet", str(remote), str(competitor)],
        check=True,
        capture_output=True,
    )
    git(competitor, "config", "user.name", "Competitor")
    git(competitor, "config", "user.email", "competitor@example.invalid")
    git(competitor, "checkout", "--quiet", "-b", "feature/T1")
    other = commit_file(competitor, "other.txt", "other\n", "concurrent candidate")
    git(competitor, "push", "--quiet", "origin", "HEAD:refs/heads/feature/T1")

    with pytest.raises(fuse_git.GitConflict, match="remote branch .* moved"):
        fuse_git.push_fast_forward(
            root,
            "origin",
            "feature/T1",
            proposed,
            expected_remote_head=base,
        )
    assert fuse_git.remote_branch_head(root, "origin", "feature/T1") == other


def test_parallel_publishers_do_not_overwrite_a_newer_branch_tip(
    tmp_path: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root, _remote, base = init_repo(tmp_path)
    git(root, "checkout", "--quiet", "-b", "feature/T1")
    git(root, "push", "--quiet", "origin", f"{base}:refs/heads/feature/T1")
    first = commit_file(root, "one.txt", "one\n", "first atomic job")
    second = commit_file(root, "two.txt", "two\n", "second atomic job")
    barrier = Barrier(2)

    def publish(commit: str) -> str:
        barrier.wait(timeout=5)
        try:
            return cast(
                str,
                fuse_git.push_fast_forward(
                    root,
                    "origin",
                    "feature/T1",
                    commit,
                    expected_remote_head=base,
                ),
            )
        except fuse_git.GitConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, (first, second)))

    assert results.count("conflict") == 1
    final = fuse_git.remote_branch_head(root, "origin", "feature/T1")
    assert final in {first, second}
    assert fuse_git.is_ancestor(root, base, str(final))
    if final == second:
        assert fuse_git.is_ancestor(root, first, second)


def test_materialize_merge_candidate_uses_ordered_heads_without_moving_source_branch(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    git(root, "checkout", "--quiet", "-b", "feature/T1")
    first = commit_file(root, "one.txt", "one\n", "first PR")
    git(root, "checkout", "--quiet", "-b", "feature/T2", base)
    second = commit_file(root, "two.txt", "two\n", "second PR")
    source_head = git(root, "rev-parse", "HEAD")

    candidate = fuse_git.materialize_merge_candidate(root, base, [first, second])

    assert candidate.base_sha == base
    assert candidate.head_shas == (first, second)
    assert candidate.commit_sha != second
    assert git(root, "show", f"{candidate.commit_sha}:one.txt") == "one"
    assert git(root, "show", f"{candidate.commit_sha}:two.txt") == "two"
    assert git(root, "rev-parse", "HEAD") == source_head
    assert not (root / ".git" / "MERGE_HEAD").exists()


def test_materialize_merge_candidate_rejects_conflict_without_changing_source(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    git(root, "checkout", "--quiet", "-b", "feature/T1")
    first = commit_file(root, "base.txt", "first change\n", "first PR")
    git(root, "checkout", "--quiet", "-b", "feature/T2", base)
    second = commit_file(root, "base.txt", "second change\n", "second PR")
    source_head = git(root, "rev-parse", "HEAD")

    with pytest.raises(fuse_git.GitConflict, match="merge candidate conflicts"):
        fuse_git.materialize_merge_candidate(root, base, [first, second])

    assert git(root, "rev-parse", "HEAD") == source_head
    assert not (root / ".git" / "MERGE_HEAD").exists()


def test_pr_graph_collapses_task_dependencies_and_rejects_cross_pr_cycle() -> None:
    plan = fuse_pr.derive_pr_dependencies(
        {"T1": [], "T2": ["T1"], "T3": []},
        {"T1": 10, "T2": 20, "T3": 30},
    )
    assert plan.dependencies == {"10": (), "20": ("10",), "30": ()}
    assert plan.order.index("10") < plan.order.index("20")

    transitive = fuse_pr.derive_pr_dependencies(
        {"T1": [], "T2": ["T1"], "T3": ["T2"]},
        {"T1": "pr-a", "T3": "pr-c"},
    )
    assert transitive.dependencies == {"pr-a": (), "pr-c": ("pr-a",)}

    with pytest.raises(fuse_pr.ProviderError, match="derived pull request .* cycle"):
        fuse_pr.derive_pr_dependencies(
            {"T1": [], "T2": ["T1"], "T3": [], "T4": ["T3"]},
            {"T1": "pr-a", "T2": "pr-b", "T3": "pr-b", "T4": "pr-a"},
        )


def test_pr_graph_rejects_unknown_task_mapping() -> None:
    with pytest.raises(fuse_pr.ProviderError, match="unknown task"):
        fuse_pr.derive_pr_dependencies({"T1": []}, {"T9": 42})


def test_pr_graph_rejects_cycles_in_unmapped_task_dependencies() -> None:
    with pytest.raises(
        fuse_pr.ProviderError, match="task dependency graph contains a cycle"
    ):
        fuse_pr.derive_pr_dependencies({"T1": ["T2"], "T2": ["T1"]}, {"T1": "pr-a"})


class FakeProvider:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.created: list[tuple[str, str, str, str]] = []
        self.snapshot: dict[str, Any] = {}
        self.pr_snapshots: dict[int, dict[str, Any]] = {}
        self.rules: list[dict[str, Any]] = strict_branch_rules()
        self.merge_methods = ["squash"]
        self.merged: list[dict[str, Any]] = []

    def find_open_prs(self, head_branch: str, base_branch: str) -> list[dict[str, Any]]:
        return list(self.rows)

    def create_draft_pr(
        self, head_branch: str, base_branch: str, title: str, body: str
    ) -> dict[str, Any]:
        self.created.append((head_branch, base_branch, title, body))
        self.snapshot = {
            "number": 7,
            "url": "https://github.com/example/project/pull/7",
            "title": title,
            "body": body,
            "state": "OPEN",
            "isDraft": True,
            "baseRefName": base_branch,
            "headRefName": head_branch,
        }
        return self.snapshot

    def view_pr(self, number: int) -> dict[str, Any]:
        return dict(self.pr_snapshots.get(number, self.snapshot))

    def supports_exact_head_binding(self) -> bool:
        return True

    def branch_rules(self, branch: str) -> list[dict[str, Any]]:
        return list(self.rules)

    def allowed_merge_methods(self, branch_rules: Any) -> list[str]:
        methods = list(self.merge_methods)
        for rule in branch_rules:
            if rule.get("type") == "pull_request":
                allowed = rule.get("allowed_merge_methods")
                if allowed is not None:
                    methods = [method for method in methods if method in allowed]
        return methods

    def update_pr_body(self, number: int, body: str) -> None:
        self.snapshot["body"] = body

    def merge_pr(
        self,
        number: int,
        *,
        candidate_sha: str,
        base_sha: str,
        head_sha: str,
        merge_method: str,
        included_heads: list[str] | tuple[str, ...],
        prerequisite_states: dict[str, dict[str, str]],
        required_checks: list[str] | tuple[str, ...],
        check_evidence: dict[str, dict[str, Any]],
        review_evidence: dict[str, dict[str, Any]],
        unresolved_findings: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> str:
        self.merged.append(
            {
                "number": number,
                "candidate_sha": candidate_sha,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "merge_method": merge_method,
                "included_heads": tuple(included_heads),
                "prerequisite_states": prerequisite_states,
                "required_checks": tuple(required_checks),
                "check_evidence": check_evidence,
                "review_evidence": review_evidence,
                "unresolved_findings": tuple(unresolved_findings),
            }
        )
        return "merged"


def test_ensure_draft_pr_reuses_one_compatible_pr_or_creates_linked_draft() -> None:
    provider = FakeProvider()
    body = "Spec 001 and candidate abc123"
    result, created = fuse_pr.ensure_draft_pr(
        provider,
        head_branch="feature/T1",
        base_branch="main",
        title="Implement T1",
        body=body,
        spec_id="001",
        candidate_id="abc123",
    )
    assert result["isDraft"] is True
    assert created is True
    assert provider.created[0][0:2] == ("feature/T1", "main")

    provider.rows = [
        {
            "number": 8,
            "state": "OPEN",
            "isDraft": True,
            "baseRefName": "main",
            "headRefName": "feature/T1",
        }
    ]
    provider.snapshot = {**provider.rows[0], "url": "https://github.com/e/r/pull/8"}
    reused, created_again = fuse_pr.ensure_draft_pr(
        provider,
        head_branch="feature/T1",
        base_branch="main",
        title="Implement T1",
        body=body,
        spec_id="001",
        candidate_id="abc123",
    )
    assert reused["number"] == 8
    assert created_again is False
    assert "abc123" in reused["body"]


def test_draft_pr_requires_spec_and_candidate_in_description() -> None:
    with pytest.raises(fuse_pr.ProviderError, match="identify the selected spec"):
        fuse_pr.ensure_draft_pr(
            FakeProvider(),
            head_branch="feature/T1",
            base_branch="main",
            title="Implement T1",
            body="No traceability",
            spec_id="001",
            candidate_id="abc123",
        )


def test_github_cli_uses_draft_flag_and_reads_pinned_pr_fields(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    snapshot = {
        "number": 7,
        "url": "https://github.com/example/project/pull/7",
        "body": "Spec 001 candidate " + "c" * 64,
        "state": "OPEN",
        "isDraft": True,
        "baseRefName": "main",
        "baseRefOid": "a" * 40,
        "headRefName": "feature/T1",
        "headRefOid": "b" * 40,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": None,
        "statusCheckRollup": [],
    }

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[1:3] == ["pr", "list"]:
            output = "[]"
        elif args[1:3] == ["pr", "create"]:
            output = "https://github.com/example/project/pull/7"
        else:
            output = json.dumps(snapshot)
        return subprocess.CompletedProcess(args, 0, output, "")

    provider = fuse_pr.GitHubCLI("example/project", tmp_path, runner=runner)
    pr, created = fuse_pr.ensure_draft_pr(
        provider,
        head_branch="feature/T1",
        base_branch="main",
        title="Implement T1",
        body="Spec 001 candidate " + "c" * 64,
        spec_id="001",
        candidate_id="c" * 64,
    )
    assert created is True
    assert pr["headRefOid"] == "b" * 40
    create_call = next(args for args in calls if args[1:3] == ["pr", "create"])
    assert "--draft" in create_call
    assert "--admin" not in create_call


def test_github_cli_direct_merge_pins_head_and_does_not_bypass_rules(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"sha": "d" * 40, "merged": True}), ""
        )

    provider = fuse_pr.GitHubCLI("example/project", tmp_path, runner=runner)
    result = provider.merge_pr(
        7,
        candidate_sha="a" * 40,
        base_sha="b" * 40,
        head_sha="c" * 40,
        merge_method="rebase",
        included_heads=["c" * 40],
        prerequisite_states={},
        required_checks=["tests"],
        check_evidence={},
        review_evidence={},
        unresolved_findings=[],
    )

    assert result == "d" * 40
    assert calls == [
        [
            "gh",
            "api",
            "--method",
            "PUT",
            "repos/example/project/pulls/7/merge",
            "-f",
            "sha=" + "c" * 40,
            "-f",
            "merge_method=rebase",
        ]
    ]
    assert "merge-async" not in calls[0]
    assert not any("bypass" in argument for argument in calls[0])
    assert "--admin" not in calls[0]


def test_github_cli_reads_active_ruleset_and_empty_bypass_list(
    tmp_path: Path,
) -> None:
    applied_rules = [
        {
            "type": "pull_request",
            "ruleset_source_type": "Repository",
            "ruleset_source": "Example/Project",
            "ruleset_id": 42,
            "parameters": {
                "required_approving_review_count": 0,
                "allowed_merge_methods": ["rebase"],
            },
        },
        {
            "type": "required_status_checks",
            "ruleset_source_type": "Repository",
            "ruleset_source": "Example/Project",
            "ruleset_id": 42,
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [{"context": "verify"}],
            },
        },
        {
            "type": "deletion",
            "ruleset_source_type": "Repository",
            "ruleset_source": "Example/Project",
            "ruleset_id": 42,
        },
        {
            "type": "non_fast_forward",
            "ruleset_source_type": "Repository",
            "ruleset_source": "Example/Project",
            "ruleset_id": 42,
        },
    ]
    ruleset = {"id": 42, "enforcement": "active", "bypass_actors": []}
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        result = ruleset if args[-1].endswith("/rulesets/42") else [applied_rules]
        return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

    provider = fuse_pr.GitHubCLI("example/project", tmp_path, runner=runner)
    rules = provider.branch_rules("main")

    assert rules == [
        {
            "type": "pull_request",
            "required_approving_review_count": 0,
            "allowed_merge_methods": ["rebase"],
            "ruleset_id": 42,
        },
        {
            "type": "required_status_checks",
            "strict": True,
            "contexts": ["verify"],
            "ruleset_id": 42,
        },
        {"type": "deletions", "enabled": False, "ruleset_id": 42},
        {"type": "force_pushes", "enabled": False, "ruleset_id": 42},
        {
            "type": "ruleset_bypass_actors",
            "ruleset_id": 42,
            "bypass_actors": [],
            "active": True,
        },
    ]
    assert calls == [
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "repos/example/project/rules/branches/main?per_page=100",
        ],
        ["gh", "api", "repos/example/project/rulesets/42"],
    ]


def test_github_cli_intersects_repository_and_ruleset_merge_methods(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        repository_settings = {
            "allow_merge_commit": True,
            "allow_squash_merge": True,
            "allow_rebase_merge": True,
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(repository_settings), "")

    provider = fuse_pr.GitHubCLI("example/project", tmp_path, runner=runner)
    methods = provider.allowed_merge_methods(
        [
            {"type": "pull_request", "allowed_merge_methods": ["merge", "rebase"]},
            {"type": "pull_request", "allowed_merge_methods": ["rebase"]},
        ]
    )

    assert methods == ["rebase"]
    assert calls == [["gh", "api", "repos/example/project"]]


def test_github_cli_reads_bypass_actors_from_later_rules_pages(
    tmp_path: Path,
) -> None:
    first_page = [
        {
            "type": "pull_request",
            "ruleset_source_type": "Repository",
            "ruleset_source": "example/project",
            "ruleset_id": 42,
            "parameters": {"required_approving_review_count": 0},
        },
        {
            "type": "required_status_checks",
            "ruleset_source_type": "Repository",
            "ruleset_source": "example/project",
            "ruleset_id": 42,
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [{"context": "verify"}],
            },
        },
        {
            "type": "deletion",
            "ruleset_source_type": "Repository",
            "ruleset_source": "example/project",
            "ruleset_id": 42,
        },
        {
            "type": "non_fast_forward",
            "ruleset_source_type": "Repository",
            "ruleset_source": "example/project",
            "ruleset_id": 42,
        },
    ]
    second_page = [
        {
            "type": "required_signatures",
            "ruleset_source_type": "Organization",
            "ruleset_source": "example-org",
            "ruleset_id": 43,
        }
    ]
    rulesets = {
        42: {"id": 42, "enforcement": "active", "bypass_actors": []},
        43: {
            "id": 43,
            "enforcement": "active",
            "bypass_actors": [
                {"actor_type": "RepositoryRole", "bypass_mode": "always"}
            ],
        },
    }
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "/rulesets/" in args[-1]:
            ruleset_id = int(args[-1].rsplit("/", maxsplit=1)[1])
            result: Any = rulesets[ruleset_id]
        else:
            result = [first_page, second_page]
        return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

    provider = fuse_pr.GitHubCLI("example/project", tmp_path, runner=runner)
    rules = provider.branch_rules("main")

    assert [call[-1] for call in calls if "/rulesets/" in call[-1]] == [
        "repos/example/project/rulesets/42",
        "orgs/example-org/rulesets/43",
    ]
    assert fuse_pr._strict_branch_ruleset_ready(rules) is False


def test_github_cli_rejects_effective_rule_without_a_ruleset_id(
    tmp_path: Path,
) -> None:
    rules = [[{"type": "required_signatures"}]]

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, json.dumps(rules), "")

    provider = fuse_pr.GitHubCLI("example/project", tmp_path, runner=runner)
    with pytest.raises(fuse_pr.ProviderError, match="invalid ruleset ID"):
        provider.branch_rules("main")


def test_github_cli_fails_closed_when_ruleset_bypass_list_is_hidden(
    tmp_path: Path,
) -> None:
    applied_rules = [
        {
            "type": "pull_request",
            "ruleset_source_type": "Repository",
            "ruleset_source": "example/project",
            "ruleset_id": 42,
            "parameters": {"required_approving_review_count": 0},
        }
    ]

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = (
            {"id": 42, "enforcement": "active"}
            if args[-1].endswith("/42")
            else [applied_rules]
        )
        return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

    provider = fuse_pr.GitHubCLI("example/project", tmp_path, runner=runner)
    with pytest.raises(fuse_pr.ProviderError, match="bypass actors are unreadable"):
        provider.branch_rules("main")


def test_record_task_pr_persists_provider_number_base_and_head() -> None:
    record = fuse_record.new_run_record("001-demo", {"roles": {}})
    record["tasks"]["T1"] = {
        "status": "pending",
        "assignment_ids": [],
        "accepted_candidate_id": None,
    }
    row = fuse_pr.record_task_pr(
        record,
        task_id="T1",
        provider="github",
        repository="example/project",
        number=7,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    assert row == {
        "task_id": "T1",
        "provider": "github",
        "repository": "example/project",
        "number": 7,
        "base": "a" * 40,
        "head": "b" * 40,
    }
    assert record["git"]["pull_requests"] == [row]


def test_push_and_canonical_checkpoint_are_one_serialized_publication(
    tmp_path: Path,
) -> None:
    root, _remote, _base = init_repo(tmp_path)
    git(root, "checkout", "--quiet", "-b", "feature/T1")
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    record = fuse_record.new_run_record("001-demo", {"roles": {}})
    record["tasks"]["T1"] = {
        "status": "pending",
        "assignment_ids": [],
        "accepted_candidate_id": None,
    }
    events: list[str] = []

    def checkpoint(saved: Any) -> None:
        events.append("checkpoint")
        assert saved["git"]["pull_requests"][0]["head"] == head
        assert fuse_git.remote_branch_head(root, "origin", "feature/T1") == head

    result = fuse_pr.publish_task_commit(
        root,
        remote="origin",
        branch="feature/T1",
        commit_sha=head,
        expected_remote_head=None,
        record=record,
        task_id="T1",
        repository="example/project",
        pr_number=7,
        base_sha=git(root, "rev-parse", "main"),
        checkpoint=checkpoint,
    )

    assert result == head
    assert events == ["checkpoint"]
    assert record["git"]["current_commit"] == head
    assert record["git"]["pull_requests"][0]["number"] == 7


def test_coordinator_publishes_commit_returned_from_an_isolated_worker(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    git(root, "push", "--quiet", "origin", f"{base}:refs/heads/feature/T1")
    worker = tmp_path / "worker-T1"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worker), base],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    git(worker, "config", "user.name", "Worker")
    git(worker, "config", "user.email", "worker@example.invalid")
    worker_head = commit_file(worker, "worker.txt", "isolated\n", "worker job")
    record = fuse_record.new_run_record("001-demo", {"roles": {}})
    record["tasks"]["T1"] = {
        "status": "pending",
        "assignment_ids": [],
        "accepted_candidate_id": None,
    }

    published = fuse_pr.publish_task_commit(
        root,
        remote="origin",
        branch="feature/T1",
        commit_sha=worker_head,
        expected_remote_head=base,
        record=record,
        task_id="T1",
        repository="example/project",
        pr_number=7,
        base_sha=base,
        checkpoint=lambda _record: None,
    )

    assert published == worker_head
    assert fuse_git.remote_branch_head(root, "origin", "feature/T1") == worker_head
    assert git(root, "rev-parse", "HEAD") == base
    assert git(worker, "rev-parse", "HEAD") == worker_head


def test_task_publish_rejects_non_atomic_commit_before_pushing(tmp_path: Path) -> None:
    root, _remote, base = init_repo(tmp_path)
    git(root, "checkout", "--quiet", "-b", "feature/T1")
    first = commit_file(root, "first.txt", "first\n", "first job commit")
    second = commit_file(root, "second.txt", "second\n", "second job commit")
    record = fuse_record.new_run_record("001-demo", {"roles": {}})
    record["tasks"]["T1"] = {
        "status": "pending",
        "assignment_ids": [],
        "accepted_candidate_id": None,
    }

    with pytest.raises(fuse_pr.ProviderError, match="exactly the expected destination"):
        fuse_pr.publish_task_commit(
            root,
            remote="origin",
            branch="feature/T1",
            commit_sha=second,
            expected_remote_head=None,
            record=record,
            task_id="T1",
            repository="example/project",
            pr_number=7,
            base_sha=base,
            checkpoint=lambda _record: None,
        )

    assert fuse_git.remote_branch_head(root, "origin", "feature/T1") is None
    assert fuse_git.is_ancestor(root, base, first)


def merge_fixtures(
    root: Path, base: str, head: str
) -> tuple[fuse_pr.PRMergeCandidate, dict[str, Any], dict[str, Any], dict[str, Any]]:
    git_candidate = fuse_git.materialize_merge_candidate(root, base, [head])
    candidate = fuse_pr.PRMergeCandidate(git_candidate, "7", {})
    pr = {
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "baseRefOid": base,
        "headRefOid": head,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
        "baseRefName": "main",
    }
    checks = {
        "tests": {
            "candidate_sha": candidate.commit_sha,
            "base_sha": base,
            "head_sha": head,
            "outcome": "pass",
        }
    }
    reviews = {
        level: {
            "candidate_sha": candidate.commit_sha,
            "base_sha": base,
            "head_sha": head,
            "outcome": "pass",
        }
        for level in fuse_pr.REQUIRED_REVIEW_LEVELS
    }
    return candidate, pr, checks, reviews


def test_merge_candidate_derives_branch_ancestry_and_rejects_conflicting_dag(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    git(root, "checkout", "--quiet", "-b", "feature/T1")
    first = commit_file(root, "first.txt", "first\n", "first PR")
    git(root, "checkout", "--quiet", "-b", "feature/T2")
    second = commit_file(root, "second.txt", "second\n", "second PR")
    git(root, "checkout", "--quiet", "main")
    git(root, "merge", "--quiet", "--no-ff", first, "-m", "merge prerequisite PR")
    current_base = git(root, "rev-parse", "HEAD")
    task_dependencies: dict[str, list[str]] = {"T1": [], "T2": []}
    task_to_pr = {"T1": "pr-a", "T2": "pr-b"}
    pr_heads = {"pr-a": first, "pr-b": second}
    merged_prerequisite = {
        "pr-a": {
            "state": "MERGED",
            "mergedAt": "2026-10-29T12:00:00Z",
            "headRefOid": first,
            "mergeCommit": {"oid": current_base},
        }
    }

    candidate = fuse_pr.build_merge_candidate(
        root,
        base_sha=current_base,
        task_dependencies=task_dependencies,
        task_to_pr=task_to_pr,
        pr_heads=pr_heads,
        pr_states=merged_prerequisite,
        target_pr="pr-b",
    )
    assert candidate.head_shas == (second,)
    assert candidate.prerequisite_states == {
        "pr-a": {"head_sha": first, "merge_sha": current_base}
    }

    with pytest.raises(fuse_pr.ProviderError, match="derived pull request .* cycle"):
        fuse_pr.build_merge_candidate(
            root,
            base_sha=current_base,
            task_dependencies={"T1": ["T2"], "T2": []},
            task_to_pr=task_to_pr,
            pr_heads=pr_heads,
            pr_states=merged_prerequisite,
            target_pr="pr-b",
        )


def test_merge_candidate_requires_merged_prerequisites_in_current_base(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    git(root, "checkout", "--quiet", "-b", "feature/T1")
    prerequisite_head = commit_file(root, "first.txt", "first\n", "first PR")
    git(root, "checkout", "--quiet", "-b", "feature/T2")
    target_head = commit_file(root, "second.txt", "second\n", "second PR")
    task_dependencies = {"T1": [], "T2": ["T1"]}
    task_to_pr = {"T1": "8", "T2": "7"}
    pr_heads = {"8": prerequisite_head, "7": target_head}

    integrated_candidate = fuse_pr.build_integrated_candidate(
        root,
        base_sha=base,
        task_dependencies=task_dependencies,
        task_to_pr=task_to_pr,
        pr_heads=pr_heads,
    )
    assert integrated_candidate.head_shas == (prerequisite_head, target_head)
    with pytest.raises(fuse_pr.ProviderError, match="requires a PR candidate"):
        fuse_pr.assess_merge(
            {},
            integrated_candidate,
            expected_base_sha=base,
            expected_head_sha=target_head,
            required_checks=[],
            check_evidence={},
            review_evidence={},
            unresolved_findings=[],
            branch_rules=strict_branch_rules(),
        )

    with pytest.raises(
        fuse_pr.ProviderError, match="prerequisite PR 8 is not confirmed merged"
    ):
        fuse_pr.build_merge_candidate(
            root,
            base_sha=base,
            task_dependencies=task_dependencies,
            task_to_pr=task_to_pr,
            pr_heads=pr_heads,
            pr_states={"8": {"state": "OPEN"}},
            target_pr="7",
        )

    git(root, "checkout", "--quiet", "main")
    git(root, "merge", "--quiet", "--no-ff", prerequisite_head, "-m", "merge PR A")
    merged_base = git(root, "rev-parse", "HEAD")
    merged_state = {
        "8": {
            "state": "MERGED",
            "mergedAt": "2026-10-29T12:00:00Z",
            "headRefOid": prerequisite_head,
            "mergeCommit": {"oid": merged_base},
        }
    }
    candidate = fuse_pr.build_merge_candidate(
        root,
        base_sha=merged_base,
        task_dependencies=task_dependencies,
        task_to_pr=task_to_pr,
        pr_heads=pr_heads,
        pr_states=merged_state,
        target_pr="7",
    )
    assert candidate.base_sha == merged_base
    assert candidate.head_shas == (target_head,)

    with pytest.raises(
        fuse_pr.ProviderError,
        match="current base does not contain prerequisite PR 8",
    ):
        fuse_pr.build_merge_candidate(
            root,
            base_sha=base,
            task_dependencies=task_dependencies,
            task_to_pr=task_to_pr,
            pr_heads=pr_heads,
            pr_states=merged_state,
            target_pr="7",
        )

    provider = FakeProvider()
    target_pr = {
        "number": 7,
        "state": "OPEN",
        "isDraft": False,
        "baseRefOid": merged_base,
        "headRefOid": target_head,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
        "baseRefName": "main",
    }
    provider.snapshot = target_pr
    provider.pr_snapshots[8] = {
        "number": 8,
        **merged_state["8"],
    }
    checks: dict[str, dict[str, Any]] = {}
    reviews = {
        level: {
            "candidate_sha": candidate.commit_sha,
            "base_sha": merged_base,
            "head_sha": target_head,
            "outcome": "pass",
        }
        for level in fuse_pr.REQUIRED_REVIEW_LEVELS
    }
    decision = fuse_pr.assess_merge(
        target_pr,
        candidate,
        provider=provider,
        expected_base_sha=merged_base,
        expected_head_sha=target_head,
        required_checks=[],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=strict_branch_rules(),
    )
    assert decision.allowed is True
    provider.pr_snapshots[8]["mergeCommit"] = {"oid": base}
    with pytest.raises(
        fuse_pr.ProviderError,
        match="prerequisite PR 8 is no longer the validated merge",
    ):
        fuse_pr.merge_pr(
            provider,
            number=7,
            expected_head_sha=target_head,
            decision=decision,
            refresh_assessment=lambda: decision,
        )
    assert provider.merged == []


def test_merge_candidate_does_not_require_unrelated_pr_heads(tmp_path: Path) -> None:
    root, _remote, base = init_repo(tmp_path)
    target_head = commit_file(root, "target.txt", "target\n", "target PR")

    candidate = fuse_pr.build_merge_candidate(
        root,
        base_sha=base,
        task_dependencies={"T1": [], "T2": []},
        task_to_pr={"T1": "pr-target", "T2": "pr-unrelated"},
        pr_heads={"pr-target": target_head},
        pr_states={},
        target_pr="pr-target",
    )

    assert candidate.target_pr == "pr-target"
    assert candidate.head_shas == (target_head,)


def test_merge_gate_requires_exact_candidate_reviews_and_strict_protection(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    candidate, pr, checks, reviews = merge_fixtures(root, base, head)
    blocked_without_binding = fuse_pr.assess_merge(
        pr,
        candidate,
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=strict_branch_rules(),
    )
    assert blocked_without_binding.allowed is False
    assert (
        blocked_without_binding.reason == "provider_cannot_bind_exact_merge_candidate"
    )

    provider = FakeProvider()
    decision = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=provider,
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=strict_branch_rules(),
    )
    assert decision.allowed is True
    assert decision.details["review_levels"] == list(fuse_pr.REQUIRED_REVIEW_LEVELS)

    no_protection = fuse_pr.assess_merge(
        pr,
        candidate,
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=[],
    )
    assert no_protection.allowed is False
    assert no_protection.reason == "strict_branch_protection_unavailable"


@pytest.mark.parametrize(
    "weakening",
    [
        "not_strict",
        "second_check_rule_not_strict",
        "missing_context",
        "bypass_actor",
        "unreadable_bypass_list",
        "force_push",
        "unknown_force_push_setting",
        "delete",
        "unknown_deletion_setting",
        "invalid_review_count",
        "merge_queue",
    ],
)
def test_merge_gate_rejects_weakened_branch_protection(
    tmp_path: Path, weakening: str
) -> None:
    root, _remote, base = init_repo(tmp_path)
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    candidate, pr, checks, reviews = merge_fixtures(root, base, head)
    rules = strict_branch_rules()
    if weakening == "not_strict":
        rules[1]["strict"] = False
    elif weakening == "second_check_rule_not_strict":
        rules.append(
            {
                "type": "required_status_checks",
                "strict": False,
                "contexts": ["deploy"],
                "ruleset_id": 42,
            }
        )
    elif weakening == "missing_context":
        rules[1]["contexts"] = []
    elif weakening == "bypass_actor":
        rules[-1]["bypass_actors"] = [
            {"actor_type": "RepositoryRole", "bypass_mode": "always"}
        ]
    elif weakening == "unreadable_bypass_list":
        rules[-1]["bypass_actors"] = None
    elif weakening == "force_push":
        rules[2]["enabled"] = True
    elif weakening == "unknown_force_push_setting":
        rules[2]["enabled"] = None
    elif weakening == "delete":
        rules[3]["enabled"] = True
    elif weakening == "unknown_deletion_setting":
        rules[3]["enabled"] = None
    elif weakening == "invalid_review_count":
        rules[0]["required_approving_review_count"] = True
    elif weakening == "merge_queue":
        rules.append({"type": "merge_queue", "ruleset_id": 42})

    decision = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=FakeProvider(),
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=rules,
    )

    assert decision.allowed is False
    assert decision.reason == "strict_branch_protection_unavailable"


def test_merge_gate_requires_every_protected_provider_check_to_pass(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    candidate, pr, checks, reviews = merge_fixtures(root, base, head)
    pr["statusCheckRollup"] = []

    decision = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=FakeProvider(),
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=strict_branch_rules(),
    )

    assert decision.allowed is False
    assert decision.reason == "provider_required_check_missing_or_failed"


def test_merge_gate_selects_only_a_method_allowed_by_repo_and_rulesets(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    candidate, pr, checks, reviews = merge_fixtures(root, base, head)
    provider = FakeProvider()
    provider.merge_methods = ["squash", "rebase"]
    rules = strict_branch_rules()
    rules[0]["allowed_merge_methods"] = ["rebase"]

    decision = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=provider,
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=rules,
    )

    assert decision.allowed is True
    assert decision.details["merge_method"] == "rebase"

    provider.merge_methods = ["squash"]
    blocked = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=provider,
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=rules,
    )

    assert blocked.allowed is False
    assert blocked.reason == "provider_has_no_compatible_merge_method"


def test_merge_gate_requires_checks_from_every_applicable_ruleset(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    candidate, pr, checks, reviews = merge_fixtures(root, base, head)
    rules = strict_branch_rules()
    rules.extend(
        [
            {
                "type": "required_status_checks",
                "strict": True,
                "contexts": ["deploy"],
                "ruleset_id": 43,
            },
            {
                "type": "ruleset_bypass_actors",
                "ruleset_id": 43,
                "bypass_actors": [],
                "active": True,
            },
        ]
    )

    decision = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=FakeProvider(),
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=rules,
    )

    assert decision.allowed is False
    assert decision.reason == "provider_required_check_missing_or_failed"


@pytest.mark.parametrize(
    "status_context",
    [
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "SUCCESS"},
        {
            "__typename": "CheckRun",
            "name": "ci/actions",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
        },
    ],
)
def test_merge_gate_accepts_successful_github_check_rollup_shapes(
    tmp_path: Path, status_context: dict[str, Any]
) -> None:
    root, _remote, base = init_repo(tmp_path)
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    candidate, pr, checks, reviews = merge_fixtures(root, base, head)
    pr["statusCheckRollup"] = [status_context]
    context_name = status_context.get("context") or status_context.get("name")
    assert isinstance(context_name, str)

    decision = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=FakeProvider(),
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=strict_branch_rules((context_name,)),
    )

    assert decision.allowed is True


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("base", "base_candidate_stale"),
        ("head", "pull_request_head_stale"),
        ("draft", "pull_request_is_draft"),
        ("pending", "provider_check_pending"),
        ("unknown_conclusion", "provider_check_failed"),
        ("review", "merge_candidate_reviews_missing_or_stale"),
        ("finding", "unresolved_review_findings"),
    ],
)
def test_merge_gate_refuses_stale_pending_or_unreviewed_candidates(
    tmp_path: Path, mutation: str, expected_reason: str
) -> None:
    root, _remote, base = init_repo(tmp_path)
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    candidate, pr, checks, reviews = merge_fixtures(root, base, head)
    findings: list[dict[str, Any]] = []
    if mutation == "base":
        pr["baseRefOid"] = "0" * 40
    elif mutation == "head":
        pr["headRefOid"] = "1" * 40
    elif mutation == "draft":
        pr["isDraft"] = True
    elif mutation == "pending":
        pr["statusCheckRollup"] = [
            {"__typename": "StatusContext", "context": "ci", "state": "PENDING"}
        ]
    elif mutation == "unknown_conclusion":
        pr["statusCheckRollup"] = [
            {"name": "ci", "status": "COMPLETED", "conclusion": None}
        ]
    elif mutation == "review":
        reviews.pop("whole_codebase")
    elif mutation == "finding":
        findings.append({"id": "finding-1"})
    decision = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=FakeProvider(),
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=findings,
        branch_rules=strict_branch_rules(),
    )
    assert decision.allowed is False
    assert decision.reason == expected_reason


def test_direct_merge_rechecks_base_head_and_protection_before_mutation(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    candidate, pr, checks, reviews = merge_fixtures(root, base, head)
    provider = FakeProvider()
    decision = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=provider,
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=strict_branch_rules(),
    )
    provider.snapshot = {**pr}

    assert (
        fuse_pr.merge_pr(
            provider,
            number=7,
            expected_head_sha=head,
            decision=decision,
            refresh_assessment=lambda: decision,
        )
        == "merged"
    )
    assert provider.merged == [
        {
            "number": 7,
            "candidate_sha": candidate.commit_sha,
            "base_sha": base,
            "head_sha": head,
            "merge_method": "squash",
            "included_heads": (head,),
            "prerequisite_states": {},
            "required_checks": ("tests",),
            "check_evidence": checks,
            "review_evidence": reviews,
            "unresolved_findings": (),
        }
    ]

    provider.rules = []
    with pytest.raises(
        fuse_pr.ProviderError, match="strict branch protection is missing"
    ):
        fuse_pr.merge_pr(
            provider,
            number=7,
            expected_head_sha=head,
            decision=decision,
            refresh_assessment=lambda: decision,
        )
    assert len(provider.merged) == 1

    provider.rules = strict_branch_rules()
    provider.snapshot = {**pr, "headRefOid": "3" * 40}
    with pytest.raises(fuse_pr.ProviderError, match="head moved"):
        fuse_pr.merge_pr(
            provider,
            number=7,
            expected_head_sha=head,
            decision=decision,
            refresh_assessment=lambda: decision,
        )
    assert len(provider.merged) == 1

    provider.snapshot = {**pr, "statusCheckRollup": []}
    with pytest.raises(fuse_pr.ProviderError, match="required provider checks"):
        fuse_pr.merge_pr(
            provider,
            number=7,
            expected_head_sha=head,
            decision=decision,
            refresh_assessment=lambda: decision,
        )
    assert len(provider.merged) == 1

    provider.snapshot = {**pr, "baseRefOid": "2" * 40}
    with pytest.raises(fuse_pr.ProviderError, match="base moved"):
        fuse_pr.merge_pr(
            provider,
            number=7,
            expected_head_sha=head,
            decision=decision,
            refresh_assessment=lambda: decision,
        )
    assert len(provider.merged) == 1


def test_direct_merge_refreshes_fuse_reviews_and_findings_before_mutation(
    tmp_path: Path,
) -> None:
    root, _remote, base = init_repo(tmp_path)
    head = commit_file(root, "feature.txt", "feature\n", "implement T1")
    candidate, pr, checks, reviews = merge_fixtures(root, base, head)
    provider = FakeProvider()
    provider.snapshot = {**pr}
    decision = fuse_pr.assess_merge(
        pr,
        candidate,
        provider=provider,
        expected_base_sha=base,
        expected_head_sha=head,
        required_checks=["tests"],
        check_evidence=checks,
        review_evidence=reviews,
        unresolved_findings=[],
        branch_rules=strict_branch_rules(),
    )
    updated_record_decision = fuse_pr.MergeDecision(
        False,
        "unresolved_review_findings",
        candidate.commit_sha,
        {"finding_ids": ["new-finding"]},
    )

    with pytest.raises(
        fuse_pr.ProviderError,
        match="current Fuse review/check evidence changed",
    ):
        fuse_pr.merge_pr(
            provider,
            number=7,
            expected_head_sha=head,
            decision=decision,
            refresh_assessment=lambda: updated_record_decision,
        )

    assert provider.merged == []
