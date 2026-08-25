#!/usr/bin/env python3
"""The dirty-worktree baseline behind `run-sequence.sh --allow-dirty`.

WHAT THIS CAN AND CANNOT DO — READ THIS FIRST
---------------------------------------------
The default runner refuses to start a prompt in a repository with uncommitted
work, and that refusal is correct: everything uncommitted afterwards then
demonstrably belongs to the prompt's attempt, which is what makes the rollover
checkpoint safe. `--allow-dirty` gives that up deliberately, and this module is
what makes the loss VISIBLE rather than silent.

It records, before the attempt, exactly what was already dirty. Afterwards it can
prove three things: that a pre-existing change is still there, that one has
vanished, and that one has been absorbed into a commit. Those are worth having —
the failure this exists to catch is a `git add -A` that quietly commits somebody
else's half-finished work under a prompt's message.

WHAT IT CANNOT DO is separate two edits to the same hunk. If the agent's work and
the human's uncommitted work touch the same lines of the same file, no mechanism
here — and no mechanism anywhere short of reading both intentions — can tell
which change belongs to whom. That case is reported as an OVERLAP and stops the
run. It is not isolation and must never be described as isolation.

COST
----
Once per attempt, per repository, and it has to stay unnoticeable. So: no copy
of the repository, no tree hash, no walk of anything that is not already dirty.
Tracked dirty files are hashed — there are, by definition, few of them, and their
content is the only thing that can prove preservation. Untracked files get
BOUNDED METADATA (size and mtime) and no hash at all: an untracked path can be a
500 MB build directory, and hashing one would turn a preflight into a coffee
break.

Usage:
    dirty_baseline.py capture  --repo-root DIR --repo ALIAS --out FILE \\
                               [--prompt NAME] [--run-id ID] [--attempt N]
    dirty_baseline.py compare  --baseline FILE [--json]
    dirty_baseline.py paths    --baseline FILE [--kind staged|unstaged|untracked|all]
    dirty_baseline.py new-paths --baseline FILE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "run-sequence.dirty-baseline/1"

# A tracked file that is already dirty is hashed. This ceiling is a safety valve
# for the pathological case (a committed 2 GB fixture, edited): above it the path
# is still recorded and still watched for disappearance, it simply has no content
# fingerprint. Preservation of a file that size is not decided by its bytes.
MAX_HASH_BYTES = 32 * 1024 * 1024


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def _is_repo(repo_root: Path) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-dir"],
        capture_output=True,
        check=False,
    ).returncode == 0


def _status(repo_root: Path) -> list[dict]:
    """`git status --porcelain=v1 -z`, parsed. The machine-readable form.

    `-z` because a filename may contain a newline, and a status parser that
    splits on newlines is a parser that will one day report half a path.
    """
    raw = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    fields = raw.split("\0")
    entries: list[dict] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        origin = None
        if code[0] in ("R", "C"):
            # A rename's second path follows in its own NUL-terminated field.
            origin = path
            if index < len(fields):
                path = fields[index]
                index += 1
        entries.append({"code": code, "path": path, "origin": origin})
    return entries


def _hash_file(path: Path) -> str | None:
    try:
        if path.is_symlink():
            return "symlink:" + hashlib.sha256(os.readlink(path).encode()).hexdigest()
        if not path.is_file():
            return None
        if path.stat().st_size > MAX_HASH_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _staged_hash(repo_root: Path, path: str) -> str | None:
    """The blob the INDEX holds for a path, which is what "staged" means.

    Compared with the working-tree hash it also answers the question the
    worktree alone cannot: whether a staged change survived, independently of
    whether the file was edited again afterwards.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", f":{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return value or None


def capture(
    repo_root: Path,
    repo: str,
    prompt: str | None = None,
    run_id: str | None = None,
    attempt: int | None = None,
) -> dict:
    repo_root = repo_root.resolve()
    if not _is_repo(repo_root):
        return {
            "schema": SCHEMA,
            "repository": repo,
            "repo_root": str(repo_root),
            "is_git_repository": False,
            "dirty": False,
            "entries": [],
        }
    entries: list[dict] = []
    for record in _status(repo_root):
        code, path = record["code"], record["path"]
        absolute = repo_root / path
        untracked = code == "??"
        kinds: list[str] = []
        if untracked:
            kinds.append("untracked")
        else:
            if code[0] not in (" ", "?"):
                kinds.append("staged")
            if code[1] not in (" ", "?"):
                kinds.append("unstaged")
        entry: dict = {
            "path": path,
            "code": code,
            "origin": record["origin"],
            "kinds": kinds or ["unstaged"],
            "worktree_sha256": None if untracked else _hash_file(absolute),
            "index_blob": None if untracked else _staged_hash(repo_root, path),
            "exists": absolute.exists() or absolute.is_symlink(),
        }
        if untracked:
            # BOUNDED METADATA ONLY. Size and mtime prove "still there, still the
            # same shape" for a fraction of the cost of reading the bytes, and an
            # untracked path is exactly the one that might be enormous.
            try:
                stat = absolute.lstat()
                entry["size"] = stat.st_size
                entry["mtime"] = int(stat.st_mtime)
            except OSError:
                entry["size"] = None
                entry["mtime"] = None
        entries.append(entry)

    return {
        "schema": SCHEMA,
        "repository": repo,
        "repo_root": str(repo_root),
        "is_git_repository": True,
        "head": _git(repo_root, "rev-parse", "HEAD").strip() or None,
        "branch": _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").strip() or None,
        "captured_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "prompt": prompt,
        "run_id": run_id,
        "attempt": attempt,
        "dirty": bool(entries),
        "counts": {
            "staged": sum(1 for entry in entries if "staged" in entry["kinds"]),
            "unstaged": sum(1 for entry in entries if "unstaged" in entry["kinds"]),
            "untracked": sum(1 for entry in entries if "untracked" in entry["kinds"]),
        },
        "entries": entries,
        # The raw porcelain, verbatim, so a later reader can re-derive anything
        # this schema did not think to record.
        "porcelain": _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all"),
    }


def _commits_since(repo_root: Path, head: str | None) -> list[str]:
    if not head:
        return []
    raw = _git(repo_root, "rev-list", f"{head}..HEAD")
    return [line for line in raw.splitlines() if line]


def _paths_in_commits(repo_root: Path, head: str | None) -> set[str]:
    if not head:
        return set()
    raw = _git(repo_root, "diff", "--name-only", head, "HEAD")
    return {line for line in raw.splitlines() if line}


def compare(baseline: dict) -> dict:
    """Did the pre-existing work survive? Three verdicts, and only one is fine.

    preserved       every recorded path is still there, unchanged
    changed         a recorded path VANISHED or was absorbed into a commit —
                    the failure this whole mechanism exists to catch
    overlap_blocked a recorded path is still dirty but its content moved, so
                    prompt work and pre-existing work are in the same file and
                    cannot be mechanically told apart
    """
    repo_root = Path(baseline["repo_root"])
    if not baseline.get("is_git_repository") or not baseline.get("entries"):
        return {"verdict": "preserved", "findings": [], "new_paths": [], "repository": baseline.get("repository")}

    current = {entry["path"]: entry for entry in _status(repo_root)}
    committed_paths = _paths_in_commits(repo_root, baseline.get("head"))
    findings: list[dict] = []

    for entry in baseline["entries"]:
        path = entry["path"]
        absolute = repo_root / path
        still_dirty = path in current
        untracked = "untracked" in entry["kinds"]

        if not still_dirty:
            if path in committed_paths:
                findings.append(
                    {
                        "path": path,
                        "kind": "absorbed",
                        "detail": (
                            "pre-existing uncommitted work was committed by this "
                            "attempt (the path appears in the commits it made)"
                        ),
                    }
                )
            elif not (absolute.exists() or absolute.is_symlink()):
                findings.append(
                    {"path": path, "kind": "deleted", "detail": "the file no longer exists"}
                )
            else:
                findings.append(
                    {
                        "path": path,
                        "kind": "reverted",
                        "detail": "the change is gone and the path is no longer dirty",
                    }
                )
            continue

        if untracked:
            try:
                stat = absolute.lstat()
            except OSError:
                findings.append(
                    {"path": path, "kind": "deleted", "detail": "the untracked file no longer exists"}
                )
                continue
            if entry.get("size") is not None and stat.st_size != entry["size"]:
                findings.append(
                    {
                        "path": path,
                        "kind": "overlap",
                        "detail": (
                            f"an untracked file that already existed changed size "
                            f"({entry['size']} -> {stat.st_size})"
                        ),
                    }
                )
            continue

        now_hash = _hash_file(absolute)
        if entry["worktree_sha256"] and now_hash and now_hash != entry["worktree_sha256"]:
            findings.append(
                {
                    "path": path,
                    "kind": "overlap",
                    "detail": (
                        "a file that was already dirty was edited during the attempt; "
                        "prompt work and pre-existing work cannot be separated here"
                    ),
                }
            )
            continue
        if "staged" in entry["kinds"]:
            now_blob = _staged_hash(repo_root, path)
            if entry["index_blob"] and now_blob != entry["index_blob"]:
                findings.append(
                    {
                        "path": path,
                        "kind": "overlap",
                        "detail": "a pre-existing STAGED change was re-staged with different content",
                    }
                )

    baseline_paths = {entry["path"] for entry in baseline["entries"]}
    new_paths = sorted(path for path in current if path not in baseline_paths)

    if any(finding["kind"] in ("absorbed", "deleted", "reverted") for finding in findings):
        verdict = "changed"
    elif findings:
        verdict = "overlap_blocked"
    else:
        verdict = "preserved"
    return {
        "verdict": verdict,
        "repository": baseline.get("repository"),
        "repo_root": str(repo_root),
        "findings": findings,
        "new_paths": new_paths,
        "commits_made": _commits_since(repo_root, baseline.get("head")),
    }


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    grab = sub.add_parser("capture")
    grab.add_argument("--repo-root", required=True, type=Path)
    grab.add_argument("--repo", required=True)
    grab.add_argument("--out", required=True, type=Path)
    grab.add_argument("--prompt")
    grab.add_argument("--run-id")
    grab.add_argument("--attempt", type=int)

    check = sub.add_parser("compare")
    check.add_argument("--baseline", required=True, type=Path)
    check.add_argument("--json", action="store_true")

    listing = sub.add_parser("paths")
    listing.add_argument("--baseline", required=True, type=Path)
    listing.add_argument("--kind", default="all",
                         choices=("all", "staged", "unstaged", "untracked"))

    fresh = sub.add_parser("new-paths")
    fresh.add_argument("--baseline", required=True, type=Path)

    args = parser.parse_args()

    if args.command == "capture":
        payload = capture(args.repo_root, args.repo, args.prompt, args.run_id, args.attempt)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        handle, staged = tempfile.mkstemp(dir=str(args.out.parent), prefix=".baseline-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(staged, args.out)
        print(json.dumps(payload["counts"] if payload.get("dirty") else {"staged": 0, "unstaged": 0, "untracked": 0}))
        return 0

    if args.command == "paths":
        baseline = _load(args.baseline)
        for entry in baseline.get("entries", []):
            if args.kind == "all" or args.kind in entry["kinds"]:
                print(entry["path"])
        return 0

    if args.command == "new-paths":
        baseline = _load(args.baseline)
        repo_root = Path(baseline["repo_root"])
        known = {entry["path"] for entry in baseline.get("entries", [])}
        for record in _status(repo_root):
            if record["path"] not in known:
                print(record["path"])
        return 0

    result = compare(_load(args.baseline))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(result["verdict"])
    for finding in result["findings"]:
        print(f"  {finding['kind']}: {finding['path']} — {finding['detail']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
