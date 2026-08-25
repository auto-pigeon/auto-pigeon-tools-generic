#!/usr/bin/env python3
"""Resolve prompts/handoffs and maintain their machine-owned headers.

Topology — which repositories exist, what each is called, and where operational
data lives — comes from `workspace_config`, i.e. from the toolkit's one
`workspace.json`. Nothing in this module derives a path any other way, and there
is no per-repository configuration file to disagree with it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import execution_time
import prompt_frontmatter
import workspace_config
from typing import Any

BODY_MARKER = "<!-- HANDOFF_BODY -->"
# ---------------------------------------------------------------------------
# EXECUTION TIMING — measured by the runner, PRESERVED by this module
# ---------------------------------------------------------------------------
# `run-sequence.sh` is the timing authority: it is the only thing in this
# workspace that knows when an agent process started and when it stopped. This
# module rewrites a handoff's whole machine-owned header on every checkpoint, so
# without care it would erase those fields the first time the agent wrote its
# own status — which is precisely how a measurement becomes a guess.
#
# So the rule is: this module never MEASURES and never ARITHMETICS. It carries
# the five `execution_*` fields forward unchanged, and it appends one thing of
# its own, a UTC checkpoint timestamp per checkpoint. Those timestamps are the
# fallback evidence for the cases the runner cannot cover — a session restored
# by hand, a runner crash, work begun before any of this existed — and an
# estimate built from them is labelled `checkpoint_estimate`, never `runner`.
CHECKPOINT_TIMESTAMPS_FIELD = "checkpoint_timestamps"
# Enough to reconstruct a session's shape without the header growing without
# bound on a prompt that rolls over many times. The FIRST is never dropped: it is
# what an estimate's start time comes from.
MAX_CHECKPOINT_TIMESTAMPS = 40
# Completion is a filesystem fact: a finished prompt is moved out of the queue
# folder into done/ (a blocked one into blocked/) as the task's last step. See
# the toolkit's WORKFLOW.md and scripts/resolve_next_prompt.py. This module
# only *writes* handoffs — it does not select work — but it has to know about
# those folders for two reasons: a prompt it recorded may since have moved, and
# `prompt_path` must stay stable across that move.
DONE_DIRNAME = "done"
BLOCKED_DIRNAME = "blocked"
ARCHIVE_DIRNAMES = (DONE_DIRNAME, BLOCKED_DIRNAME)
TERMINAL_STATUSES = {"complete", "partial", "blocked", "failed"}
VALID_STATUSES = TERMINAL_STATUSES | {"in_progress"}

# ---------------------------------------------------------------------------
# BLOCKED HANDOFF METADATA — what a block costs the rest of the queue
# ---------------------------------------------------------------------------
# `status: blocked` says this prompt cannot finish. It says nothing about
# whether the other twelve prompts in tonight's queue can, and before
# 20260823_AUT_04 the runner had to assume the worst and stop the whole run.
# One missing fixture ended an overnight drain.
#
# So a blocked handoff now carries a small, machine-readable impact statement
# beside the status, and run-sequence.sh reads it:
#
#     status: blocked
#     block:
#       severity: local | dependent | catastrophic
#       reason: short_identifier
#       summary: one human sentence
#       can_continue_unrelated: true
#       blocks_prompts:
#         - 20260823_96_Some-Prompt
#       blocks_repositories:
#         - api
#
# SEVERITY IS THE ONLY FIELD THE RUNNER BRANCHES ON:
#   local          nothing else is known to depend on this. Defer this prompt's
#                  dependants (computed, not trusted) and carry on.
#   dependent      same, but the agent has named further casualties.
#   catastrophic   ULTIMA RATIO. The workspace itself cannot be trusted, so the
#                  whole sequence stops. A missing fixture is not this. A
#                  half-written cross-repository contract is.
#
# The agent's list WIDENS the deterministic dependency closure
# resolve_next_prompt.py computes; it never narrows it. An LLM that forgets a
# dependant must not be able to let the runner start work on it.
BLOCK_SEVERITIES = ("local", "dependent", "catastrophic")
DEFAULT_BLOCK_SEVERITY = "local"
BLOCK_SCALAR_FIELDS = ("severity", "reason", "summary", "can_continue_unrelated")
BLOCK_LIST_FIELDS = ("blocks_prompts", "blocks_repositories")
FORBIDDEN_DIRS = (
    "artifacts/handoffs/",
    "artifacts/review/",
    "artifacts/reports/",
)


class TaskError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepoConfig:
    """One repository's identity, derived from `workspace.json` and nothing else.

    The source toolkit read this from a small configuration file committed inside
    each child repository, which let a checkout disagree with the workspace about
    its own name and required a file to be seeded into every repository. Here the
    configured alias IS the name, and the prompt and handoff folders are that
    alias — one derivation, no second table.
    """

    repository: str
    alias: str
    prompt_directory: str
    handoff_directory: str


@dataclass(frozen=True)
class TaskState:
    repo_root: Path
    data_root: Path
    workspace: workspace_config.Workspace
    config: RepoConfig
    latest_prompt: Path | None
    expected_handoff: Path | None
    latest_handoff: Path | None
    handoff_meta: dict[str, str]
    action: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        def display(path: Path | None) -> str | None:
            if path is None:
                return None
            try:
                return path.relative_to(self.data_root).as_posix()
            except ValueError:
                return str(path)

        return {
            "repository": self.config.repository,
            "alias": self.config.alias,
            "repo_root": str(self.repo_root),
            "data_root": str(self.data_root),
            "latest_prompt": display(self.latest_prompt),
            "expected_handoff": display(self.expected_handoff),
            "latest_handoff": display(self.latest_handoff),
            "handoff_status": self.handoff_meta.get("status"),
            "action": self.action,
            "reason": self.reason,
        }


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise TaskError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise TaskError(f"{path} must contain a JSON object")
    return value


def repository_config(workspace: workspace_config.Workspace, repo_root: Path) -> RepoConfig:
    """Which configured repository this directory IS.

    Matched on the fully resolved path, so a caller that reached the checkout
    through a symlink identifies the same alias — the identity rule
    `workspace_config` documents.
    """
    try:
        real = repo_root.resolve()
    except OSError:
        real = repo_root
    for repo in workspace.repositories:
        if repo.real == real:
            return RepoConfig(
                repository=repo.alias,
                alias=repo.alias,
                prompt_directory=repo.alias,
                handoff_directory=repo.alias,
            )
    known = "\n".join(f"    {repo.alias}: {repo.path}" for repo in workspace.repositories)
    raise TaskError(
        f"{repo_root} is not a repository configured in {workspace.config_path}.\n"
        f"  Configured repositories:\n{known or '    (none)'}"
    )


# Workspace topology and the data root are resolved by scripts/workspace_config.py:
# the single implementation shared with run-agent.sh, run-sequence.sh, the planner
# and the history report. Its docstring explains why there is exactly one
# configuration file and exactly one precedence rule.
#
# `TaskError` is what the rest of this module raises and what `main()` catches, so
# the boundary is translated here rather than leaking a second exception type into
# every call site.
def load_workspace(config: Path | None = None) -> workspace_config.Workspace:
    try:
        return workspace_config.load(config)
    except workspace_config.WorkspaceError as error:
        raise TaskError(str(error)) from error


def markdown_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.glob("*.md") if path.is_file() and not path.is_symlink()),
        key=lambda path: (path.name, path.stat().st_mtime_ns),
    )


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def frontmatter(path: Path | None) -> dict[str, str]:
    """Top-level scalars of a handoff header, via the one canonical decoder.

    This used to be its own line splitter, which reported an
    INDENTED key as a top-level one — so `block:`'s `severity` arrived beside
    `status`, and `block_metadata` below exists partly because of that. Reading
    is now one implementation; writing the header is still this module's alone.
    """
    if path is None or not path.is_file():
        return {}
    return prompt_frontmatter.parse_handoff_tolerant(path).scalars()


def block_metadata(path: Path | None) -> dict[str, Any]:
    """The `block:` mapping out of a handoff's frontmatter, or {}.

    The nested structure comes back from the canonical decoder;
    what stays here is the SCHEMA on top of it — which keys are lists, which is
    a boolean, and the guarantee that a list field is always present as a list.
    A reader that has to remember which keys might be missing is a reader that
    will forget.
    """
    if path is None or not path.is_file():
        return {}
    raw = prompt_frontmatter.parse_handoff_tolerant(path).field_mapping("block")
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if key in BLOCK_LIST_FIELDS:
            if isinstance(value, list):
                result[key] = [str(item).strip() for item in value if str(item).strip()]
            elif value:
                result[key] = [str(value)]
            else:
                result[key] = []
        elif key == "can_continue_unrelated":
            result[key] = str(value or "").lower() not in ("false", "no", "0", "")
        elif isinstance(value, str) and value:
            result[key] = value
    if result:
        # The list fields always come back as lists, present or not: `render_block`
        # omits an empty one, and a reader that has to remember which keys might
        # be missing is a reader that will forget.
        for field in BLOCK_LIST_FIELDS:
            result.setdefault(field, [])
    return result


def normalise_block(block: dict[str, Any] | None, status: str) -> dict[str, Any]:
    """Fill in what the runner must be able to read, and refuse what it cannot."""
    if status != "blocked":
        return {}
    block = dict(block or {})
    severity = str(block.get("severity") or DEFAULT_BLOCK_SEVERITY).strip().lower()
    if severity not in BLOCK_SEVERITIES:
        raise TaskError(
            f"block severity {severity!r} is not one of {', '.join(BLOCK_SEVERITIES)}"
        )
    block["severity"] = severity
    block.setdefault("reason", "unspecified")
    block.setdefault("summary", "No impact summary was recorded.")
    if "can_continue_unrelated" not in block:
        # The safe default is the PERMISSIVE one, and that is not carelessness:
        # the deterministic dependency closure already defers everything that
        # actually depends on this prompt, so a bare `blocked` with no opinion
        # means "nothing else is known to be affected", which is what `local` is.
        block["can_continue_unrelated"] = severity != "catastrophic"
    for field in BLOCK_LIST_FIELDS:
        values = block.get(field) or []
        if isinstance(values, str):
            values = [values]
        block[field] = [str(value).strip() for value in values if str(value).strip()]
    return block


def render_block(block: dict[str, Any]) -> str:
    if not block:
        return ""
    lines = ["block:"]
    for field in BLOCK_SCALAR_FIELDS:
        if field not in block:
            continue
        value = block[field]
        if isinstance(value, bool):
            value = "true" if value else "false"
        lines.append(f"  {field}: {value}")
    for field in BLOCK_LIST_FIELDS:
        values = block.get(field) or []
        if not values:
            continue
        lines.append(f"  {field}:")
        lines.extend(f"    - {value}" for value in values)
    return "\n".join(lines) + "\n"


def relative_to_data_root(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise TaskError(f"{path} is outside the configured data root {root}") from error


def canonical_prompt_path(path: Path, root: Path) -> str:
    """`prompt_path` as recorded in a handoff — the prompt's identity.

    A prompt's file moves (queue -> done/ or blocked/) when it finishes, but
    the handoff already written for it must keep identifying it the same way
    afterwards. So the recorded path is always the queue-relative one, with a
    `done/`/`blocked/` segment stripped, whatever the file's current location.
    """
    relative = relative_to_data_root(path, root)
    parts = relative.split("/")
    if len(parts) >= 2 and parts[-2] in ARCHIVE_DIRNAMES:
        parts.pop(-2)
    return "/".join(parts)


def locate_prompt(data_root: Path, prompt_path: str) -> Path | None:
    """Find a prompt named by a handoff's `prompt_path`, wherever it now is.

    Handoffs written before the prompt was moved record the queue path; the
    file may since have been moved into done/ or blocked/. Both resolve.
    """
    direct = data_root / prompt_path
    if direct.is_file():
        return direct
    for archive in ARCHIVE_DIRNAMES:
        moved = direct.parent / archive / direct.name
        if moved.is_file():
            return moved
    return None


def find_queue_prompt(prompt_dir: Path, name: str) -> Path | None:
    """Resolve an explicitly requested prompt filename in this repo's queue."""
    for candidate in (prompt_dir / name, prompt_dir / DONE_DIRNAME / name, prompt_dir / BLOCKED_DIRNAME / name):
        if candidate.is_file():
            return candidate
    return None


def resolve_state(repo_root: Path, prompt_name: str | None = None,
                  config_path: Path | None = None,
                  workspace: workspace_config.Workspace | None = None) -> TaskState:
    repo_root = repo_root.resolve()
    workspace = workspace or load_workspace(config_path)
    config = repository_config(workspace, repo_root)
    data_root = workspace.data_root
    prompt_dir = data_root / "LLM" / "prompts" / config.prompt_directory
    handoff_dir = data_root / "LLM" / "handoffs" / config.handoff_directory
    prompts = markdown_files(prompt_dir)
    handoffs = markdown_files(handoff_dir)
    if prompt_name:
        # Explicit target. This is the reliable way to checkpoint the prompt
        # you are actually working on: the default below picks the newest file
        # in the queue folder, which is the wrong answer whenever more than one
        # prompt is outstanding (WORKFLOW.md documents this gap at length).
        prompt = find_queue_prompt(prompt_dir, Path(prompt_name).name)
        if prompt is None:
            raise TaskError(
                f"prompt {Path(prompt_name).name!r} not found in {prompt_dir} "
                f"(nor its {DONE_DIRNAME}/ or {BLOCKED_DIRNAME}/ subfolders)"
            )
    else:
        prompt = prompts[-1] if prompts else None
    expected = handoff_dir / prompt.name if prompt else None
    latest_handoff = handoffs[-1] if handoffs else None
    meta = frontmatter(expected if expected and expected.is_file() else latest_handoff)

    if prompt is None:
        action, reason = "idle", f"no prompt exists in {prompt_dir}"
    else:
        prompt_path = canonical_prompt_path(prompt, data_root)
        matches = (
            expected is not None
            and expected.is_file()
            and meta.get("prompt_path") == prompt_path
            and meta.get("prompt_sha256") == digest(prompt)
        )
        status = meta.get("status")
        if matches and status == "complete":
            action, reason = "stop", "latest prompt already has a complete matching handoff"
        elif matches and status == "blocked":
            action, reason = "blocked", "latest prompt has a matching blocked handoff"
        elif matches and status in {"in_progress", "partial", "failed"}:
            action, reason = "resume", f"matching handoff status is {status}"
        elif expected and expected.is_file():
            action, reason = "execute", "handoff exists but prompt path or digest does not match"
        else:
            action, reason = "execute", "latest prompt has no corresponding handoff"

    return TaskState(
        repo_root=repo_root,
        data_root=data_root,
        workspace=workspace,
        config=config,
        latest_prompt=prompt,
        expected_handoff=expected,
        latest_handoff=latest_handoff,
        handoff_meta=meta,
        action=action,
        reason=reason,
    )


def git_changes(repo_root: Path) -> list[tuple[str, str]]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise TaskError(f"cannot inspect Git status in {repo_root}: {error}") from error
    chunks = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    rows: list[tuple[str, str]] = []
    index = 0
    while index < len(chunks):
        entry = chunks[index]
        index += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            if index < len(chunks) and chunks[index]:
                path = f"{path} -> {chunks[index]}"
                index += 1
        label = {
            "??": "added (untracked)",
            "A ": "added",
            " A": "added",
            "D ": "deleted",
            " D": "deleted",
        }.get(code, "renamed/copied" if "R" in code or "C" in code else "modified")
        rows.append((path.replace("\\", "/"), label))
    return sorted(rows)


def git_tracked(repo_root: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise TaskError(f"cannot inspect tracked files in {repo_root}: {error}") from error
    return {
        path.replace("\\", "/")
        for path in result.stdout.decode("utf-8", "surrogateescape").split("\0")
        if path
    }


def checkpoint_timestamps(path: Path | None) -> list[str]:
    """The `checkpoint_timestamps:` block list already in a handoff, in order."""
    if path is None or not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.DOTALL)
    if not match:
        return []
    values: list[str] = []
    inside = False
    for line in match.group(1).splitlines():
        if re.match(rf"^{CHECKPOINT_TIMESTAMPS_FIELD}:\s*$", line):
            inside = True
            continue
        if not inside:
            continue
        item = re.match(r"^\s+-\s*(.+)$", line)
        if item:
            value = item.group(1).strip().strip("\"'")
            if value:
                values.append(value)
            continue
        break
    return values


def render_checkpoint_timestamps(values: list[str]) -> str:
    if not values:
        return ""
    lines = [f"{CHECKPOINT_TIMESTAMPS_FIELD}:"]
    lines.extend(f'  - "{value}"' for value in values)
    return "\n".join(lines) + "\n"


def render_execution_fields(fields: dict[str, str]) -> str:
    """Re-emit the runner's timing fields verbatim, in their fixed order.

    Verbatim matters. This module has no way to check a duration and no business
    recomputing one; anything it does other than copy is a way for a measured
    number to become a different number.
    """
    lines: list[str] = []
    for key in execution_time.FIELD_ORDER:
        if key not in fields:
            continue
        value = fields[key]
        if value in ("", "null", "None"):
            lines.append(f"{key}: null")
        elif key in ("execution_started_at", "execution_finished_at"):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    return ("\n".join(lines) + "\n") if lines else ""


def handoff_header(state: TaskState, status: str, block: dict[str, Any] | None = None) -> str:
    if state.latest_prompt is None or state.expected_handoff is None:
        raise TaskError("cannot create a handoff without a prompt")
    prompt_path = canonical_prompt_path(state.latest_prompt, state.data_root)
    prompt_text = state.latest_prompt.read_text(encoding="utf-8").rstrip()
    changed = git_changes(state.repo_root)
    changed_lines = (
        "\n".join(f"- `{path}` — {label}" for path, label in changed)
        if changed
        else "- None at this checkpoint."
    )
    updated = datetime.now(timezone.utc).isoformat()
    block_yaml = render_block(block or {})
    # Carried forward, never invented: whatever the runner measured, plus this
    # checkpoint's own timestamp appended to the fallback trail.
    execution_yaml = render_execution_fields(execution_time.read_fields(state.expected_handoff))
    stamps = checkpoint_timestamps(state.expected_handoff)
    stamps.append(execution_time.utc_now())
    if len(stamps) > MAX_CHECKPOINT_TIMESTAMPS:
        # Keep the FIRST — an estimate's start comes from it — and the most
        # recent tail. Dropping the first would move a prompt's apparent start
        # forward every time it rolled over.
        stamps = stamps[:1] + stamps[-(MAX_CHECKPOINT_TIMESTAMPS - 1):]
    timestamps_yaml = render_checkpoint_timestamps(stamps)
    return f"""---
handoff_schema: agent-handoff/1.0
repository: {state.config.repository}
repository_alias: {state.config.alias}
prompt_path: {prompt_path}
prompt_sha256: {digest(state.latest_prompt)}
status: {status}
{block_yaml}updated_at: {updated}
{timestamps_yaml}{execution_yaml}---

# {state.config.alias} handoff — {state.latest_prompt.stem}

## Prompt provenance

- Prompt: `{prompt_path}`
- SHA-256: `{digest(state.latest_prompt)}`
- Status: `{status}`
{_block_prose(block or {})}
## Prompt (verbatim)

```markdown
{prompt_text}
```

## Repository files changed

{changed_lines}

{BODY_MARKER}
"""


def _block_prose(block: dict[str, Any]) -> str:
    if not block:
        return ""
    lines = [f"- Block severity: `{block.get('severity', '')}` — {block.get('reason', '')}"]
    if block.get("summary"):
        lines.append(f"- Block summary: {block['summary']}")
    for field, label in (
        ("blocks_prompts", "Blocks prompts"),
        ("blocks_repositories", "Blocks repositories"),
    ):
        values = block.get(field) or []
        if values:
            lines.append(f"- {label}: {', '.join(values)}")
    lines.append(
        "- Unrelated queued work may continue: "
        f"`{'yes' if block.get('can_continue_unrelated') else 'no'}`"
    )
    return "\n".join(lines) + "\n"


def checkpoint(state: TaskState, status: str, block: dict[str, Any] | None = None) -> Path:
    if status not in VALID_STATUSES:
        raise TaskError(f"invalid status {status!r}")
    if state.expected_handoff is None:
        raise TaskError("no prompt exists, so no handoff can be checkpointed")
    # Re-checkpointing must not silently drop an impact statement the agent
    # already wrote: the header is rewritten wholesale every time, so what is
    # already on disk is the base and the new flags are the override.
    merged = dict(block_metadata(state.expected_handoff))
    merged.update(block or {})
    block = normalise_block(merged, status)
    body = ""
    if state.expected_handoff.is_file():
        current = state.expected_handoff.read_text(encoding="utf-8")
        if BODY_MARKER in current:
            body = current.split(BODY_MARKER, 1)[1].lstrip("\n")
        else:
            body = current
    if not body:
        body = (
            "## Requirement status\n\n"
            "- Record requirement-by-requirement status here.\n\n"
            "## Work completed and decisions\n\n"
            "- Pending.\n\n"
            "## Commands and results\n\n"
            "- Pending.\n\n"
            "## Incomplete work and next executable step\n\n"
            "- Pending.\n"
        )
    state.expected_handoff.parent.mkdir(parents=True, exist_ok=True)
    staged = state.expected_handoff.with_suffix(".md.staged")
    staged.write_text(
        handoff_header(state, status, block) + "\n" + body.rstrip() + "\n", encoding="utf-8"
    )
    os.replace(staged, state.expected_handoff)
    return state.expected_handoff


def frontmatter_lint_errors(state: TaskState) -> list[str]:
    """Malformed machine-owned frontmatter in this repository's queue.

    Read-only, and reusing the validation command that
    already runs at the end of every task and from the `Stop` hook rather than
    adding a second one nobody would remember to run. Scope is this repository's
    own prompt folder (queue, `done/`, `blocked/`) and its handoff folder: a
    prompt whose dependency block cannot be decoded has UNKNOWN prerequisites,
    and the resolver now refuses to schedule it rather than reading it as
    declaring none. Finding that at validation time is a one-line fix; finding
    it when the queue stalls is an investigation.
    """
    directories = [
        state.data_root / "LLM" / "prompts" / state.config.prompt_directory,
        state.data_root / "LLM" / "handoffs" / state.config.handoff_directory,
    ]
    paths: list[Path] = []
    for directory in directories:
        if directory.is_dir():
            paths.extend(sorted(item for item in directory.rglob("*.md") if item.is_file()))
    return [
        f"malformed frontmatter: {finding['message']}"
        for finding in prompt_frontmatter.lint_paths(paths)
    ]


def validate(state: TaskState, *, stop_hook: bool = False) -> list[str]:
    errors: list[str] = frontmatter_lint_errors(state)
    changes = git_changes(state.repo_root)
    tracked = git_tracked(state.repo_root)
    for path, label in changes:
        tested = path.split(" -> ")[-1]
        if any(tested.startswith(prefix) for prefix in FORBIDDEN_DIRS):
            # A removal is the authorized migration of a historical repository-local
            # artifact out to <data_root>/LLM/. Only additions and edits are rejected.
            if label != "deleted":
                errors.append(f"repository-local task artifact changed: {tested}")

    for prefix in FORBIDDEN_DIRS:
        root = state.repo_root / prefix
        if not root.is_dir():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
            relative = path.relative_to(state.repo_root).as_posix()
            if relative not in tracked:
                errors.append(f"untracked repository-local task artifact exists: {relative}")
    # Validate the handoff this session actually wrote — the most recently
    # modified one — not the newest prompt in the folder.
    #
    # The previous rule demanded a handoff for `latest_prompt`, which made
    # validation impossible to pass whenever more than one prompt was
    # outstanding: completing prompt 13 while 14 and 15 sat unrun failed with
    # "latest prompt has no canonical handoff," and the only way to
    # silence it was to fabricate handoffs for work nobody had done. That is the
    # same newest-prompt resolution bug WORKFLOW.md documents for `checkpoint`
    # and that `resolve_next_prompt.py` was written to replace. A stop hook's
    # job is to confirm the run just finished is recorded correctly; a prompt
    # that has never been executed is not this run's problem.
    refreshed = resolve_state(state.repo_root, workspace=state.workspace)
    handoff_dir = refreshed.data_root / "LLM" / "handoffs" / refreshed.config.handoff_directory
    handoffs = markdown_files(handoff_dir)

    if refreshed.latest_prompt is not None and not handoffs:
        errors.append(f"no canonical handoff exists in {handoff_dir}")
    elif handoffs:
        recent = max(handoffs, key=lambda path: path.stat().st_mtime_ns)
        meta = frontmatter(recent)
        text = recent.read_text(encoding="utf-8")
        label = recent.name

        # MANUAL_* records work done outside the prompt workflow: no prompt
        # path, no verbatim prompt section. Everything else must pin a prompt.
        if label.startswith("MANUAL_"):
            if meta.get("prompt_path") not in {None, "", "null", "~"}:
                errors.append(f"{label}: MANUAL handoff must declare prompt_path: null")
            if BODY_MARKER not in text:
                errors.append(f"{label}: handoff is missing the {BODY_MARKER} marker")
        else:
            prompt_path = meta.get("prompt_path")
            if not prompt_path:
                errors.append(f"{label}: handoff declares no prompt_path")
            else:
                # A completed prompt has been moved into done/ (a blocked one
                # into blocked/) by the time validation runs, so resolve the
                # recorded queue path through those folders too. Without this,
                # doing the move correctly is what makes validation fail.
                target = locate_prompt(refreshed.data_root, prompt_path)
                if target is None:
                    errors.append(f"{label}: prompt_path does not identify an existing prompt: {prompt_path}")
                elif meta.get("prompt_sha256") != digest(target):
                    errors.append(f"{label}: prompt_sha256 does not match {prompt_path}")
            if BODY_MARKER not in text or "## Prompt (verbatim)" not in text:
                errors.append(f"{label}: handoff is missing the canonical prompt/header structure")

        if stop_hook and meta.get("status") not in TERMINAL_STATUSES:
            errors.append(
                f"{label}: handoff status is not terminal; "
                "checkpoint complete/partial/blocked/failed"
            )

        # A blocked handoff has to say what the block costs the rest of the
        # queue. run-sequence.sh branches on `block.severity` to decide between
        # deferring this prompt's dependants and stopping the whole run, so a
        # blocked handoff without one is a handoff the runner cannot act on.
        if meta.get("status") == "blocked":
            found = block_metadata(recent)
            if not found:
                errors.append(
                    f"{label}: status is blocked but no `block:` impact statement is "
                    "recorded. Re-checkpoint with --block-severity "
                    f"({'|'.join(BLOCK_SEVERITIES)}), --block-reason and --block-summary."
                )
            elif found.get("severity") not in BLOCK_SEVERITIES:
                errors.append(
                    f"{label}: block severity {found.get('severity')!r} is not one of "
                    + ", ".join(BLOCK_SEVERITIES)
                )

        # TIMING FIELDS ARE OPTIONAL AND, WHEN PRESENT, STRICT. Optional because
        # every handoff written before the runner measured anything is still a
        # valid handoff and must stay one — refusing them would demand a
        # migration of the whole history to record something nobody observed.
        # Strict when present because a malformed duration that validates is a
        # number that ends up in a total.
        errors.extend(_timing_errors(label, meta))
    return errors


def _timing_errors(label: str, meta: dict[str, str]) -> list[str]:
    errors: list[str] = []
    raw_seconds = meta.get("execution_seconds")
    if raw_seconds not in (None, "", "null"):
        try:
            if int(raw_seconds) < 0:
                errors.append(f"{label}: execution_seconds must not be negative")
        except (TypeError, ValueError):
            errors.append(
                f"{label}: execution_seconds must be a non-negative integer, got {raw_seconds!r}"
            )
    raw_attempts = meta.get("execution_attempts")
    if raw_attempts not in (None, "", "null"):
        try:
            if int(raw_attempts) < 1:
                errors.append(f"{label}: execution_attempts must be a positive integer")
        except (TypeError, ValueError):
            errors.append(
                f"{label}: execution_attempts must be a positive integer, got {raw_attempts!r}"
            )
    measurement = meta.get("execution_measurement")
    if measurement not in (None, "", "null") and measurement not in execution_time.MEASUREMENTS:
        errors.append(
            f"{label}: execution_measurement {measurement!r} is not one of "
            + ", ".join(execution_time.MEASUREMENTS)
        )
    for field in ("execution_started_at", "execution_finished_at"):
        value = meta.get(field)
        if value in (None, "", "null"):
            continue
        if execution_time.parse(value) is None:
            errors.append(f"{label}: {field} is not an RFC3339 UTC timestamp: {value!r}")
    return errors


def session_message(state: TaskState) -> str:
    payload = state.to_json()
    lines = [
        "TASK ROUTER (mandatory):",
        json.dumps(payload, indent=2, sort_keys=True),
        "Read the newest handoff before the newest prompt.",
    ]
    if state.action == "stop":
        lines.append("The latest prompt is complete. Stop immediately without tools or file changes.")
    elif state.action == "blocked":
        lines.append("Report the recorded blocker and stop. Do not retry without new user direction.")
    elif state.action in {"execute", "resume"}:
        lines.extend(
            [
                "Execute or resume only the resolved prompt.",
                "Run agent_task.py checkpoint with status in_progress before task edits.",
                "Write the handoff below the HANDOFF_BODY marker and checkpoint a truthful terminal status before stopping.",
            ]
        )
    else:
        lines.append("No file-based task is pending. Wait for explicit user direction.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("status", "session-start", "checkpoint", "validate", "validate-stop", "block-info"),
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--status", choices=sorted(VALID_STATUSES))
    parser.add_argument(
        "--prompt",
        help=(
            "prompt filename to checkpoint against (e.g. 20260804_02_Title.md). "
            "Use this whenever the prompt you are completing is not the newest "
            "one in the queue folder — without it the default target is the "
            "newest, which is the wrong handoff to write."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Path to the toolkit's workspace.json. Only needed when it is not the "
            "one at the toolkit repository root; see workspace_config.py for the "
            "single precedence rule."
        ),
    )
    # The blocked-handoff impact statement. Every flag is optional: a bare
    # `checkpoint --status blocked` still produces a valid, honest `local` block,
    # which is what an agent that simply cannot finish should be able to write
    # without first learning a schema.
    block_group = parser.add_argument_group("blocked-handoff impact (only with --status blocked)")
    block_group.add_argument("--block-severity", choices=BLOCK_SEVERITIES)
    block_group.add_argument("--block-reason", help="short machine-readable-ish identifier")
    block_group.add_argument("--block-summary", help="one human sentence")
    block_group.add_argument(
        "--blocks-prompt",
        action="append",
        default=[],
        metavar="STEM",
        help="a queued prompt this block makes unrunnable; repeatable. WIDENS the "
        "dependency closure the runner computes for itself — it never narrows it.",
    )
    block_group.add_argument(
        "--blocks-repository", action="append", default=[], metavar="DIRECTORY"
    )
    block_group.add_argument(
        "--can-continue-unrelated",
        choices=("true", "false"),
        help="may prompts with no dependency on this one still run? (default: "
        "true, except for a catastrophic block)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    block: dict[str, Any] = {}
    if args.block_severity:
        block["severity"] = args.block_severity
    if args.block_reason:
        block["reason"] = args.block_reason
    if args.block_summary:
        block["summary"] = args.block_summary
    if args.blocks_prompt:
        block["blocks_prompts"] = args.blocks_prompt
    if args.blocks_repository:
        block["blocks_repositories"] = args.blocks_repository
    if args.can_continue_unrelated:
        block["can_continue_unrelated"] = args.can_continue_unrelated == "true"
    try:
        state = resolve_state(args.repo_root, args.prompt, args.config)
        if args.command == "status":
            print(json.dumps(state.to_json(), indent=2, sort_keys=True) if args.json else session_message(state))
            return 0
        if args.command == "session-start":
            print(session_message(state))
            return 0
        if args.command == "checkpoint":
            if not args.status:
                raise TaskError("checkpoint requires --status")
            if block and args.status != "blocked":
                raise TaskError(
                    "block metadata is only meaningful with --status blocked"
                )
            path = checkpoint(state, args.status, block)
            print(path)
            return 0
        if args.command == "block-info":
            # How run-sequence.sh reads a block's severity and declared impact
            # without a second parser in Bash.
            found = block_metadata(state.expected_handoff)
            print(json.dumps(found, sort_keys=True) if args.json else render_block(found), end="" if not args.json else "\n")
            return 0
        errors = validate(state, stop_hook=args.command == "validate-stop")
        if args.command == "validate-stop":
            if errors:
                print(json.dumps({"decision": "block", "reason": "Workspace hygiene validation failed: " + "; ".join(errors)}))
            return 0
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("workspace hygiene validation passed")
        return 0
    except TaskError as error:
        if args.command == "validate-stop":
            print(json.dumps({"decision": "block", "reason": f"Workspace hygiene validation failed: {error}"}))
            return 0
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
