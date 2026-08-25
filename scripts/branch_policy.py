#!/usr/bin/env python3
"""The main-branch completion rule, in the one place both runners can call it.

THE RULE:

    A normal run-agent.sh / run-sequence.sh prompt is NOT finished until every
    repository it mutates has its prompt commit(s) on `main`.

It exists because an overnight `--drain` in the source workspace produced this:
one agent created a feature branch, finished its prompt there, every following
prompt inherited the branch, and a later cross-repository task committed one
repository on the branch while its siblings committed on main. Nothing was lost
and nothing looked wrong; the work was simply in two places, and only a human
reading `git log` could tell.

WHAT THIS MODULE WILL NEVER DO
    reset, force-delete a branch, drop a stash, rebase, force-push, push at all,
    or discard a commit. Every operation here is either a read or a
    `git checkout` of an existing branch whose loss-of-work question has already
    been answered in the affirmative. When reconciliation is ambiguous it
    REPORTS the exact repository, branch and commits and refuses — an agent, or
    a human, decides.

FOUR COMMANDS
    targets    which repositories a prompt declares it will mutate
    snapshot   every repository's refs before an attempt, so drift is provable
    preflight  put the declared targets on the target branch, or refuse and say why
    verify     after an attempt: on target, clean, and no commit left off it

`verify` is the gate that a completion has to pass, and it is deliberately
computed from REFS rather than from filesystem timestamps:

    stray = git rev-list <refs now> --not <target> <refs before>

Anything in that set is a commit this attempt created which the target branch
cannot reach — which is exactly, and only, the failure the rule exists to catch.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prompt_frontmatter  # noqa: E402
import workspace_config  # noqa: E402

SCHEMA_SNAPSHOT = "aut.branch-snapshot/1"
DEFAULT_TARGET_BRANCH = "main"

#: Prompt frontmatter keys that name repositories this prompt is allowed to change.
#: `repo` is the owning repository; the other two are how cross-repository prompts
#: declare the rest. All three are read because all three appear in real prompts.
TARGET_KEYS = ("mutation_targets", "touches", "repo", "repository")

#: There is deliberately NO alias table in this module. The source toolkit kept a
#: hand-maintained one here — a second opinion about which repositories exist,
#: which drifted from the workspace's own list. Every alias now resolves through
#: `workspace_config`, so a prompt's `mutation_targets: [api]` and the runner's
#: `--queue api` are answered by the same file.


class BranchPolicyError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# git, kept to the read-only verbs plus one checkout
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str, check: bool = False) -> tuple[int, str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and result.returncode != 0:
        raise BranchPolicyError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.returncode, result.stdout.strip()


def is_git_repo(repo: Path) -> bool:
    return repo.is_dir() and _git(repo, "rev-parse", "--git-dir")[0] == 0


def current_branch(repo: Path) -> str | None:
    """The checked-out branch, or None when HEAD is detached."""
    code, out = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out if code == 0 and out else None


def head_sha(repo: Path) -> str:
    code, out = _git(repo, "rev-parse", "HEAD")
    return out if code == 0 else ""


def local_branches(repo: Path) -> dict[str, str]:
    code, out = _git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    if code != 0 or not out:
        return {}
    refs: dict[str, str] = {}
    for line in out.splitlines():
        name, _, sha = line.partition(" ")
        if name and sha:
            refs[name] = sha
    return refs


def is_dirty(repo: Path) -> bool:
    code, out = _git(repo, "status", "--porcelain")
    return code == 0 and bool(out.strip())


def dirty_paths(repo: Path) -> list[str]:
    code, out = _git(repo, "status", "--porcelain")
    return out.splitlines() if code == 0 and out else []


def object_exists(repo: Path, sha: str) -> bool:
    return bool(sha) and _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")[0] == 0


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", older, newer)[0] == 0


def rev_list(repo: Path, include: list[str], exclude: list[str]) -> list[str]:
    if not include:
        return []
    args = ["rev-list", *include]
    for sha in exclude:
        args.append(f"^{sha}")
    code, out = _git(repo, *args)
    return out.splitlines() if code == 0 and out else []


def describe(repo: Path, sha: str) -> str:
    code, out = _git(repo, "log", "-1", "--format=%h %s", sha)
    return out if code == 0 else sha[:12]


# ---------------------------------------------------------------------------
# which branch is "main" here
# ---------------------------------------------------------------------------
def resolve_target_branch(repo: Path, preferred: str = DEFAULT_TARGET_BRANCH) -> tuple[str | None, str]:
    """The branch a completion has to land on, and why it was chosen.

    `main` when it exists — that is the frozen policy and the answer in every
    repository of this workspace. The one fallback is a repository with exactly
    ONE local branch and no `main`: a fresh `git init` under an older
    `init.defaultBranch`, which is common enough in throwaway fixtures that
    refusing it would only teach people to disable the check. Anything else —
    several branches and no `main` — is genuinely ambiguous and is refused.
    """
    branches = local_branches(repo)
    if preferred in branches:
        return preferred, f"`{preferred}` exists"
    if len(branches) == 1:
        only = next(iter(branches))
        return only, f"no `{preferred}` branch; `{only}` is the only local branch"
    if not branches:
        return None, "the repository has no local branches (no commits yet?)"
    return None, (
        f"no `{preferred}` branch, and {len(branches)} local branches exist "
        f"({', '.join(sorted(branches))}) — which one is the trunk is not this "
        "script's guess to make"
    )


# ---------------------------------------------------------------------------
# mutation targets
# ---------------------------------------------------------------------------
#: Inside a mapping list item, the key that names the repository.
_TARGET_ITEM_KEYS = ("repo", "repository", "directory", "path")


def declared_targets(prompt_text: str, *, path: Path | None = None) -> list[str]:
    """Repository tokens named by a prompt's frontmatter, in declaration order.

    Reads `repo`/`repository` — the owning repository — plus `mutation_targets`
    and `touches`. Every accepted spelling is decoded by
    `prompt_frontmatter`, the one decoder here: it was written after this
    function was found reading `-` bullets while `sequence_plan` read `-` and
    `*` and `resolve_next_prompt` read neither reliably, over the same eleven
    lines of the same file.

    Still no YAML dependency, and for the same reason as before: this runs from
    a shell script (`run-agent.sh`, `run-sequence.sh`) on machines with nothing
    installed but python3. The decoder is stdlib-only and sits beside this file.

    Raises `FrontmatterError` when the block cannot be decoded. The branch
    preflight refusing to guess at a prompt's scope is the same rule as
    everywhere else in this file: a wrong guess about somebody's history is
    unrecoverable, a refusal is not.
    """
    block = prompt_frontmatter.parse(prompt_text, path=path, inline_comments=True)
    tokens: list[str] = []
    # Declaration order, not TARGET_KEYS order: the decoder preserves the order
    # the keys appear in the file, and a caller comparing two runs' output
    # should not see it reshuffled by a constant in this module.
    for key, _ in block.fields.items():
        if key not in TARGET_KEYS:
            continue
        for item in block.field_list(key) or []:
            if isinstance(item, dict):
                value = next(
                    (str(item[name]) for name in _TARGET_ITEM_KEYS if item.get(name)), ""
                )
            else:
                value = str(item)
            if value.strip():
                tokens.append(value.strip())
    return tokens


def resolve_repo_token(token: str, workspace: workspace_config.Workspace) -> str | None:
    """A prompt's spelling of a repository -> its configured alias, or None.

    Case-insensitive, exactly as every other alias lookup in this toolkit is, and
    it accepts the checkout's directory BASENAME as well: prompts are written by
    people who think in directory names, and a prompt that names a repository the
    workspace really has must not be read as naming nothing.
    """
    token = token.strip().strip("/")
    if not token:
        return None
    found = workspace.find(token)
    if found is not None:
        return found.alias
    lowered = token.lower()
    for repo in workspace.repositories:
        if repo.path.name.lower() == lowered:
            return repo.alias
    return None


# ---------------------------------------------------------------------------
# snapshot / preflight / verify
# ---------------------------------------------------------------------------
def snapshot_repo(repo: Path) -> dict:
    if not is_git_repo(repo):
        return {"is_git": False}
    return {
        "is_git": True,
        "head": head_sha(repo),
        "branch": current_branch(repo),
        "refs": local_branches(repo),
        "dirty": is_dirty(repo),
    }


def take_snapshot(workspace: workspace_config.Workspace, aliases: list[str]) -> dict:
    return {
        "schema": SCHEMA_SNAPSHOT,
        "config": str(workspace.config_path),
        "repos": {
            alias: snapshot_repo(workspace.require(alias).path) for alias in aliases
        },
    }


def preflight_repo(repo: Path, name: str, target_preferred: str, apply: bool) -> dict:
    result = {"repo": name, "ok": True, "action": "none", "notes": []}
    if not is_git_repo(repo):
        result["action"] = "skipped"
        result["notes"].append("not a Git repository")
        return result

    target, why = resolve_target_branch(repo, target_preferred)
    result["target"] = target
    result["target_reason"] = why
    if target is None:
        result["ok"] = False
        result["reason"] = why
        return result

    branch = current_branch(repo)
    result["branch"] = branch
    result["detached"] = branch is None
    dirty = is_dirty(repo)
    result["clean"] = not dirty

    if branch == target:
        if dirty:
            result["notes"].append("worktree is dirty (left exactly as it is)")
        return result

    # Off the target branch. The only question that matters is whether anything
    # reachable from HEAD would stop being reachable — and the only honest answer
    # comes from the graph, never from the branch's name.
    head = head_sha(repo)
    ahead = rev_list(repo, [head], [target]) if head else []
    result["commits_off_target"] = [describe(repo, sha) for sha in ahead]

    if ahead:
        result["ok"] = False
        result["reason"] = (
            f"{'detached HEAD' if branch is None else 'branch ' + branch} carries "
            f"{len(ahead)} commit(s) that `{target}` cannot reach. Switching now would "
            f"leave that work behind, so this prompt is stopped before it starts rather "
            f"than guessed at."
        )
        return result

    if dirty:
        result["ok"] = False
        result["reason"] = (
            f"{'detached HEAD' if branch is None else 'branch ' + branch} has uncommitted "
            f"changes, so it cannot be moved to `{target}` without stashing somebody "
            f"else's work — which nothing here will do automatically."
        )
        result["dirty_paths"] = dirty_paths(repo)
        return result

    # Safe: everything here is already on the target branch, so the checkout is
    # a pure navigation.
    if not apply:
        result["action"] = "would-checkout"
        return result
    code, _ = _git(repo, "checkout", target)
    if code != 0:
        result["ok"] = False
        result["action"] = "checkout-failed"
        result["reason"] = f"could not check out `{target}`"
        return result
    result["action"] = "checked-out"
    result["branch"] = target
    return result


def verify_repo(
    repo: Path,
    name: str,
    before: dict,
    target_preferred: str,
    declared: bool,
    allow_dirty: bool = False,
) -> dict:
    """After an attempt: on the target branch, clean, nothing stranded off it.

    `allow_dirty` relaxes EXACTLY ONE of those three — cleanliness — and nothing
    else. It is set when the run was started with `run-sequence.sh
    --allow-dirty`, where a dirty tree is the operator's declared starting
    condition rather than evidence of an unfinished attempt, and where whether
    that dirt is still the SAME dirt is answered by `dirty_baseline.py` instead.
    Branch placement and stray commits stay fully enforced: nothing about
    pre-existing uncommitted work makes it acceptable to leave a prompt's commits
    where `main` cannot reach them.
    """
    result = {"repo": name, "declared": declared, "ok": True, "changed": False}
    result["allow_dirty"] = allow_dirty
    if not is_git_repo(repo):
        result["skipped"] = "not a Git repository"
        return result

    after = snapshot_repo(repo)
    result["branch"] = after["branch"]
    result["clean"] = not after["dirty"]
    result["changed"] = (
        not before.get("is_git")
        or after["head"] != before.get("head")
        or after["refs"] != before.get("refs", {})
        or after["dirty"] != before.get("dirty")
    )

    target, why = resolve_target_branch(repo, target_preferred)
    result["target"] = target
    if target is None:
        result["ok"] = False
        result["reason"] = why
        return result

    before_shas = []
    if before.get("is_git"):
        for sha in [before.get("head", "")] + list(before.get("refs", {}).values()):
            if sha and object_exists(repo, sha) and sha not in before_shas:
                before_shas.append(sha)
    after_shas = []
    for sha in [after["head"]] + list(after["refs"].values()):
        if sha and sha not in after_shas:
            after_shas.append(sha)

    stray = rev_list(repo, after_shas, [target] + before_shas)
    result["stray_commits"] = [describe(repo, sha) for sha in stray]
    result["stray_count"] = len(stray)

    reasons = []
    if after["branch"] != target:
        where = "a detached HEAD" if after["branch"] is None else f"branch `{after['branch']}`"
        reasons.append(f"the repository is on {where}, not `{target}`")
    if after["dirty"]:
        result["dirty_paths"] = dirty_paths(repo)
        if allow_dirty:
            result["notes"] = result.get("notes", []) + [
                "worktree is dirty; --allow-dirty was given, so preservation of the "
                "recorded baseline is what decides this, not cleanliness"
            ]
        else:
            reasons.append("the worktree is dirty")
    if stray:
        reasons.append(
            f"{len(stray)} commit(s) made during this attempt are not reachable from `{target}`"
        )
    if reasons:
        result["ok"] = False
        result["reason"] = "; ".join(reasons)

    # Is this the shape one bounded remediation session can safely fix? Only when
    # the work exists, the tree is clean, and nothing has to be invented.
    # Remediation still demands a CLEAN tree, `--allow-dirty` or not. That
    # session's whole job is to move commits between branches, and doing that
    # around somebody's uncommitted work is the destructive guess this module
    # exists to refuse. An off-target repository with dirty work is reported and
    # closed, which is recoverable; a bad merge over a dirty tree is not.
    result["remediable"] = bool(
        not result["ok"] and not after["dirty"] and (stray or after["branch"] != target)
    )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_report(rows: list[dict], heading: str) -> None:
    print(heading)
    for row in rows:
        mark = "ok  " if row.get("ok", True) else "REFUSED"
        detail = row.get("reason") or row.get("action") or ""
        branch = row.get("branch")
        branch_text = f" [{branch}]" if branch else (" [detached HEAD]" if row.get("detached") else "")
        print(f"  {mark:8} {row['repo']}{branch_text}  {detail}".rstrip())
        for note in row.get("notes", []) or []:
            print(f"           note: {note}")
        for commit in (row.get("commits_off_target") or row.get("stray_commits") or [])[:20]:
            print(f"           commit: {commit}")
        for path in (row.get("dirty_paths") or [])[:20]:
            print(f"           dirty: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("targets", "snapshot", "preflight", "verify", "target-branch")
    )
    parser.add_argument("--config", type=Path, help="an explicit workspace.json")
    parser.add_argument("--prompt", type=Path, help="prompt file, for `targets`")
    parser.add_argument("--repo", action="append", default=[], metavar="ALIAS",
                        help="repository alias from workspace.json (repeatable)")
    parser.add_argument("--snapshot", type=Path, help="snapshot file, for `verify`")
    parser.add_argument("--out", type=Path, help="write the snapshot here instead of stdout")
    parser.add_argument("--target-branch", default=DEFAULT_TARGET_BRANCH)
    parser.add_argument("--apply", action="store_true", help="preflight: actually check out the target")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="verify: a dirty worktree is not itself a failure (branch placement and "
        "stray commits are still enforced). Set by run-sequence.sh --allow-dirty.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--verdict-file",
        type=Path,
        help="verify: also write KEY=value lines a shell can read without parsing JSON",
    )
    args = parser.parse_args()

    try:
        workspace = workspace_config.load(args.config)
    except workspace_config.WorkspaceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    def repo_path(alias: str) -> Path:
        """The checkout for one alias, or a path that cannot exist.

        A snapshot recorded an alias that has since been removed from the
        configuration; `verify` still has to produce a row for it rather than
        crash, and `is_git_repo` on a nonexistent path answers cleanly.
        """
        found = workspace.find(alias)
        return found.path if found is not None else Path(alias)


    if args.command == "targets":
        if not args.prompt:
            print("targets needs --prompt", file=sys.stderr)
            return 2
        text = args.prompt.read_text(encoding="utf-8", errors="replace")
        resolved: list[str] = []
        unknown: list[str] = []
        try:
            tokens = declared_targets(text, path=args.prompt)
        except prompt_frontmatter.FrontmatterError as error:
            # Exit 3, not 0-with-an-empty-list. A caller that swallows stderr
            # and reads an empty target list would run the branch preflight
            # over the running repository alone and believe it had covered the
            # prompt's declared scope.
            print(str(error), file=sys.stderr)
            if args.json:
                print(json.dumps({"targets": [], "unresolved": [], "error": str(error)}))
            return 3
        for token in tokens:
            name = resolve_repo_token(token, workspace)
            if name is None:
                unknown.append(token)
            elif name not in resolved:
                resolved.append(name)
        if args.json:
            print(json.dumps({"targets": resolved, "unresolved": unknown}))
        else:
            for name in resolved:
                print(name)
        return 0

    if args.command == "target-branch":
        for name in args.repo:
            target, why = resolve_target_branch(repo_path(name), args.target_branch)
            print(f"{name}\t{target or ''}\t{why}")
        return 0

    if args.command == "snapshot":
        data = take_snapshot(workspace, args.repo)
        text = json.dumps(data, indent=2, sort_keys=True) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        return 0

    if args.command == "preflight":
        rows = [
            preflight_repo(repo_path(name), name, args.target_branch, args.apply)
            for name in args.repo
        ]
        ok = all(row["ok"] for row in rows)
        if args.json:
            print(json.dumps({"ok": ok, "repos": rows}))
        else:
            _print_report(rows, "branch preflight (target: %s)" % args.target_branch)
        return 0 if ok else 1

    # verify
    if not args.snapshot or not args.snapshot.is_file():
        print("verify needs an existing --snapshot", file=sys.stderr)
        return 2
    before = json.loads(args.snapshot.read_text(encoding="utf-8"))
    declared = set(args.repo)
    names = list(dict.fromkeys(list(before.get("repos", {})) + args.repo))
    rows = [
        verify_repo(
            repo_path(name),
            name,
            before.get("repos", {}).get(name, {}),
            args.target_branch,
            name in declared,
            args.allow_dirty,
        )
        for name in names
    ]
    # A repository nobody declared and nobody touched is not part of this
    # verdict — reporting it would bury the two rows that matter.
    rows = [row for row in rows if row["declared"] or row.get("changed")]
    ok = all(row["ok"] for row in rows)
    drift = [row["repo"] for row in rows if not row["declared"] and row.get("changed")]
    off_target = [row["repo"] for row in rows if not row.get("ok", True)]
    remediable = bool(off_target) and all(
        row.get("remediable") for row in rows if not row.get("ok", True)
    )
    if args.verdict_file:
        # KEY=value, not JSON, and every value is a repository directory name or a
        # yes/no — so run-sequence.sh can read the verdict with `read`, and does
        # not grow a second opinion about what "ok" means.
        args.verdict_file.parent.mkdir(parents=True, exist_ok=True)
        args.verdict_file.write_text(
            "BRANCH_GATE_OK=%s\n"
            "BRANCH_GATE_REMEDIABLE=%s\n"
            "BRANCH_GATE_OFF_TARGET=%s\n"
            "BRANCH_GATE_DRIFT=%s\n"
            % (
                "yes" if ok else "no",
                "yes" if remediable else "no",
                " ".join(off_target),
                " ".join(drift),
            ),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps({"ok": ok, "repos": rows, "scope_drift": drift, "remediable": remediable}))
    else:
        _print_report(rows, "branch completion gate (target: %s)" % args.target_branch)
        if drift:
            print("  scope drift: %s changed but was not declared by the prompt" % ", ".join(drift))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BranchPolicyError as error:
        print(f"branch_policy: {error}", file=sys.stderr)
        raise SystemExit(2)
