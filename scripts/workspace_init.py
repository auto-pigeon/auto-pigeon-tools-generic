#!/usr/bin/env python3
"""Plan — and only on request, apply — this toolkit's initialization.

TWO MODES, AND THE DEFAULT IS THE ONE THAT CANNOT HURT
------------------------------------------------------
Without `--apply` this module is a READ-ONLY PLANNER. It resolves the toolkit's
own directory and its father, looks at the immediate siblings, proposes a
configuration, inventories which documents already exist, prints exactly what it
would write — and writes nothing. No directory is created, no Git repository is
initialized, no agent is started, and nothing under the data root is touched.
That property is what makes it safe to run against a real workspace to find out
what initialization would mean.

With `--apply` it creates what is missing and NOTHING ELSE:

    * workspace.json, when there is none. An existing one is AUTHORITATIVE and is
      never rewritten, never merged and never regenerated — discrepancies between
      it and what discovery found are REPORTED instead.
    * the data directory skeleton.
    * missing seed documents at toolkit level.
    * missing seed documents inside child repositories.

It never overwrites a file that exists — not AGENTS.md, not DESIGN.md, not
UI-DESIGN.md, not any of them — never commits anything in a child repository,
never pushes, never touches a Git remote, and never looks below an immediate
sibling's root.

A second identical `--apply` is a no-op, because every write is conditional on
the target not existing.

WHAT DISCOVERY WILL AND WILL NOT LOOK AT
----------------------------------------
IMMEDIATE SIBLINGS ONLY: the entries of the toolkit's parent directory, one level
deep. A directory is a repository candidate when `<sibling>/.git` exists — as a
directory (a normal clone) or as a file (a worktree or a submodule). A Git
repository nested BELOW a sibling is deliberately invisible: a monorepo with
vendored checkouts inside it is one repository, and walking into it would both
propose nonsense and cost an unbounded amount of time on a large tree.

The toolkit itself is excluded, and so is the data directory — which is why the
data directory is resolved BEFORE discovery runs rather than after.

SEEDS SAY WHAT IS TRUE AND NOTHING MORE
---------------------------------------
A generated document may state the product's id and name, the configured
repositories and their paths, the workflow/data split, and where a real document
should go. It may not invent a repository's responsibilities, a service
relationship, an API contract, an architecture, a deployment topology or any UI
behavior. Every seed is therefore mostly headings and TODO markers: a document
that guesses is worse than a document that is honestly empty, because somebody
downstream will believe it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workspace_config  # noqa: E402

DEFAULT_DATA_DIRNAME = "workspace-data"

#: Toolkit-level documents init.sh will seed when they are missing.
TOOLKIT_SEEDS = ("AGENTS.md", "DESIGN.md", "UI-DESIGN.md", "WORKFLOW.md", "README.md")

#: Child-repository documents. UI-DESIGN.md is deliberately NOT here: a workspace
#: having one does not mean every repository in it has a user interface, and
#: seeding an empty UI document into a worker repository is an invented claim.
CHILD_SEEDS = ("AGENTS.md", "DESIGN.md")


class InitError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Alias proposal — deterministic, from the basename, and stable across runs
# ---------------------------------------------------------------------------
_ALIAS_STRIP = re.compile(r"[^a-z0-9]+")


def propose_alias(directory_name: str) -> str:
    """A repository directory name -> the alias this would propose for it.

    Lowercased, every run of non-alphanumeric characters collapsed to a single
    `-`, and trimmed. Deterministic and reversible enough to recognise: `Example
    API` and `example-api` both become `example-api`.
    """
    alias = _ALIAS_STRIP.sub("-", directory_name.strip().lower()).strip("-")
    return alias or "repository"


def deduplicate(aliases: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """Make proposed aliases unique WITHOUT reordering or renaming arbitrarily.

    Collisions are resolved by suffixing `-2`, `-3`... in the order discovery
    produced (which is sorted), so the same tree always proposes the same names.
    """
    used: set[str] = set()
    result: list[tuple[str, Path]] = []
    for alias, path in aliases:
        candidate = alias
        counter = 2
        while candidate.lower() in used:
            candidate = f"{alias}-{counter}"
            counter += 1
        used.add(candidate.lower())
        result.append((candidate, path))
    return result


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def is_git_repository(path: Path) -> bool:
    """A repository ROOT: `.git` directly inside it.

    A file counts as well as a directory — that is what a linked worktree and a
    submodule look like — and both are real checkouts a prompt can be run in.
    """
    return (path / ".git").exists()


@dataclass
class Discovery:
    toolkit_root: Path
    father: Path
    data_root: Path
    repositories: list[tuple[str, Path]] = field(default_factory=list)
    ignored: list[tuple[Path, str]] = field(default_factory=list)


def discover(toolkit_root: Path, data_root: Path) -> Discovery:
    """Immediate siblings only, one level deep, nothing below them."""
    father = toolkit_root.parent
    result = Discovery(toolkit_root=toolkit_root, father=father, data_root=data_root)
    try:
        entries = sorted(father.iterdir(), key=lambda p: p.name)
    except OSError as error:
        raise InitError(f"cannot read the toolkit's parent directory {father}: {error}") from error

    try:
        data_real = data_root.resolve()
    except OSError:
        data_real = data_root

    candidates: list[tuple[str, Path]] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            # `.cache`, `.Trash`, an editor's dot-directory. None of them is a
            # product repository, and none of them should be listed as ignored
            # either — that noise buries the entries an operator has to read.
            continue
        try:
            entry_real = entry.resolve()
        except OSError:
            entry_real = entry
        if entry_real == toolkit_root.resolve():
            result.ignored.append((entry, "this toolkit repository"))
            continue
        if entry_real == data_real:
            result.ignored.append((entry, "the configured data directory"))
            continue
        if not is_git_repository(entry):
            result.ignored.append((entry, "not a Git repository root"))
            continue
        candidates.append((propose_alias(entry.name), entry))

    result.repositories = deduplicate(candidates)
    return result


# ---------------------------------------------------------------------------
# Seed documents
# ---------------------------------------------------------------------------
def _repository_table(repositories: list[tuple[str, str]]) -> str:
    if not repositories:
        return "_No repositories are configured yet — add them to `workspace.json`._\n"
    lines = ["| Alias | Path |", "| --- | --- |"]
    lines.extend(f"| `{alias}` | `{path}` |" for alias, path in repositories)
    return "\n".join(lines) + "\n"


def seed_text(name: str, product_id: str, product_name: str, data_root: str,
              repositories: list[tuple[str, str]]) -> str:
    """The content of one toolkit-level seed. Facts and headings only."""
    table = _repository_table(repositories)
    if name == "AGENTS.md":
        return f"""# {product_name} — agent instructions

This workspace is driven by the toolkit in this repository. The workflow, the
queue layout and the completion rules are documented in `WORKFLOW.md`; this file
is where {product_name}'s own conventions go.

## Repositories

{table}
Paths are resolved from `workspace.json`, which is the only place they are
configured.

## Operational data

Prompts, handoffs, run state and reports live under `{data_root}`. That tree is
NOT version-controlled and is the operator's to back up.

## Conventions

TODO: record this product's conventions — commit style, test commands, review
expectations, anything an agent must know before it edits a repository here.
Nothing has been assumed on your behalf.
"""
    if name == "DESIGN.md":
        return f"""# {product_name} — design

TODO: describe what {product_name} is and how its repositories relate.

Nothing in this file was generated from inspection: the toolkit knows which
repositories are configured and where they are, and it deliberately does not
guess at what they do.

## Repositories

{table}

## Responsibilities

TODO: one line per repository, written by someone who knows.

## Relationships

TODO: how these repositories depend on each other, if they do.

## Constraints

TODO: anything a change here must not break.
"""
    if name == "UI-DESIGN.md":
        return f"""# {product_name} — UI design

TODO: describe {product_name}'s user-facing surfaces, if it has any.

This file was seeded empty on purpose. Whether this product has a user interface
at all, what it looks like and how it behaves are not things the toolkit can
observe.

## Surfaces

TODO.

## Interaction rules

TODO.

## Visual language

TODO.
"""
    if name == "WORKFLOW.md":
        return f"""# {product_name} — prompt workflow

The lifecycle below is the toolkit's, not this product's. It is reproduced here
because agents are pointed at this file by name.

## The queue IS the folder

    {data_root}/LLM/prompts/<alias>/           outstanding work
    {data_root}/LLM/prompts/<alias>/done/      finished
    {data_root}/LLM/prompts/<alias>/blocked/   attempted and genuinely stuck

The next prompt is the OLDEST file directly in the queue folder whose declared
prerequisites are all in `done/`. Completion is a filesystem fact: a prompt is
finished when its file has been moved into `done/` — not when a handoff says so.

## Handoffs

Every prompt gets a handoff at
`{data_root}/LLM/handoffs/<alias>/<same-filename>.md`, written through
`scripts/agent_task.py checkpoint`. It pins the prompt's path and SHA-256, its
status, and the runner's execution timing.

## Per-prompt rules

TODO: anything {product_name} requires beyond the toolkit's own lifecycle.
"""
    if name == "README.md":
        return f"""# {product_name}

Product id: `{product_id}`

This repository is the workflow and control repository for {product_name}. It
holds the prompt runners, the queue resolution and the workspace configuration;
it holds no product code.

## Repositories

{table}

## Operational data

`{data_root}` — prompts, handoffs, run state, reports. Unversioned, potentially
large, and the operator's to back up.

## Commands

    ./run-agent.sh <alias> <prompt.md>
    ./run-sequence.sh --queue <alias> <prompt.md>...
    ./run-sequence.sh --extract-sequence
    ./run-sequence.sh --history

See `WORKFLOW.md` for the prompt lifecycle and `AGENTS.md` for this product's
conventions.
"""
    raise InitError(f"no seed defined for {name}")


def child_seed_text(name: str, alias: str, toolkit_root: Path, repo_path: Path,
                    product_name: str) -> str:
    """A child repository's seed. A POINTER, never a description."""
    try:
        relative = os.path.relpath(toolkit_root, repo_path)
    except ValueError:
        relative = str(toolkit_root)
    if name == "AGENTS.md":
        return f"""# {alias} — agent instructions

This repository is part of the **{product_name}** workspace and is worked through
the toolkit at `{relative}`.

Read these before starting a task here:

- `{relative}/AGENTS.md` — the workspace's agent instructions
- `{relative}/WORKFLOW.md` — the prompt/handoff lifecycle and completion rules
- `{relative}/README.md` — the toolkit's commands and layout

This repository's queue is `LLM/prompts/{alias}/` under the workspace data root,
and its handoffs are `LLM/handoffs/{alias}/`.

## Repository-specific instructions

TODO: anything an agent must know about THIS repository — build commands, test
commands, conventions. Nothing has been assumed on your behalf.
"""
    if name == "DESIGN.md":
        return f"""# {alias} — design

TODO: describe what this repository is and what it is responsible for.

Seeded by the {product_name} toolkit. Nothing here was inferred from the code.

## Responsibility

TODO.

## Structure

TODO.

## Interfaces

TODO: what this repository exposes to the rest of the workspace, if anything.
"""
    raise InitError(f"no child seed defined for {name}")


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
@dataclass
class Action:
    kind: str        #: create-config | create-dir | create-file | exists | discrepancy
    path: Path
    note: str = ""


@dataclass
class Plan:
    toolkit_root: Path
    father: Path
    config_path: Path
    config_exists: bool
    product_id: str
    product_name: str
    data_root: Path
    repositories: list[tuple[str, Path]]
    ignored: list[tuple[Path, str]]
    actions: list[Action] = field(default_factory=list)
    discrepancies: list[str] = field(default_factory=list)

    @property
    def writes(self) -> list[Action]:
        return [a for a in self.actions if a.kind.startswith("create")]


def config_payload(plan: Plan) -> dict:
    """The exact JSON `--apply` would write. Relative only where that is honest.

    A path INSIDE the father directory is stored relative to workspace.json,
    because that is what makes a workspace movable: rename the father, or clone
    the whole tree onto another machine, and every path still resolves.

    A path outside it is stored ABSOLUTE, exactly as the operator gave it. The
    alternative — emitting `../../../srv/data` for `--data-dir /srv/data` — is
    technically equivalent and practically a trap: it silently repoints at a
    different directory the moment the toolkit is moved, and it hides a decision
    the operator made explicitly.
    """
    father = plan.father.resolve()

    def store(path: Path) -> str:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved == father or father not in resolved.parents:
            return str(path)
        try:
            return os.path.relpath(path, plan.config_path.parent)
        except ValueError:
            return str(path)

    return {
        "schema": workspace_config.SCHEMA,
        "product": {"id": plan.product_id, "name": plan.product_name},
        "data_root": store(plan.data_root),
        "repositories": [
            {"alias": alias, "path": store(path)} for alias, path in plan.repositories
        ],
        "agents": {
            "claude": {"enabled": True, "command": "claude"},
            "codex": {"enabled": True, "command": "codex"},
        },
    }


def build_plan(toolkit_root: Path, product_id: str | None, product_name: str | None,
               data_dir: str | None, config_path: Path | None = None) -> Plan:
    toolkit_root = toolkit_root.resolve()
    config = config_path or (toolkit_root / workspace_config.CONFIG_BASENAME)
    config_exists = config.is_file()

    if config_exists:
        # AUTHORITATIVE. Existing configuration decides the product, the data root
        # and the repository list; discovery is still run, but only to REPORT what
        # it found that the configuration does not have.
        workspace = workspace_config.load(config)
        resolved_product_id = workspace.product_id
        resolved_product_name = workspace.product_name
        data_root = workspace.data_root
        repositories = [(repo.alias, repo.path) for repo in workspace.repositories]
    else:
        if not product_id:
            raise InitError(
                "no workspace.json exists yet, so --product ID is required.\n"
                '  e.g. ./init.sh --product example --name "Example Product"'
            )
        resolved_product_id = product_id
        resolved_product_name = product_name or product_id
        raw_data = data_dir or f"../{DEFAULT_DATA_DIRNAME}"
        candidate = Path(raw_data).expanduser()
        if not candidate.is_absolute():
            candidate = toolkit_root / candidate
        data_root = Path(os.path.normpath(str(candidate)))
        repositories = []

    found = discover(toolkit_root, data_root)
    discrepancies: list[str] = []

    if config_exists:
        # Discovery does not ADD anything here — an operator who left a sibling out
        # of the configuration meant to. It only reports, so the difference between
        # "not configured" and "forgotten" stays a human decision.
        configured_real = set()
        for _, path in repositories:
            try:
                configured_real.add(path.resolve())
            except OSError:
                configured_real.add(path)
        for alias, path in found.repositories:
            try:
                real = path.resolve()
            except OSError:
                real = path
            if real not in configured_real:
                discrepancies.append(
                    f"sibling {path.name} is a Git repository but is not configured "
                    f"in {config.name} (discovery would have called it `{alias}`)"
                )
    else:
        repositories = found.repositories

    plan = Plan(
        toolkit_root=toolkit_root,
        father=found.father,
        config_path=config,
        config_exists=config_exists,
        product_id=resolved_product_id,
        product_name=resolved_product_name,
        data_root=data_root,
        repositories=repositories,
        ignored=found.ignored,
    )
    plan.discrepancies.extend(discrepancies)

    if config_exists:
        plan.actions.append(Action("exists", config, "authoritative; left unchanged"))
        for alias, path in repositories:
            if not path.is_dir():
                plan.discrepancies.append(
                    f"configured repository `{alias}` does not exist at {path}"
                )
    else:
        plan.actions.append(Action("create-config", config))

    # --- data skeleton ----------------------------------------------------
    for relative in ("",) + workspace_config.DATA_SUBDIRS:
        target = plan.data_root / relative if relative else plan.data_root
        plan.actions.append(
            Action("exists" if target.is_dir() else "create-dir", target)
        )
    for alias, _ in repositories:
        for kind in ("prompts", "handoffs"):
            target = plan.data_root / "LLM" / kind / alias
            plan.actions.append(
                Action("exists" if target.is_dir() else "create-dir", target)
            )
        for archive in ("done", "blocked"):
            target = plan.data_root / "LLM" / "prompts" / alias / archive
            plan.actions.append(
                Action("exists" if target.is_dir() else "create-dir", target)
            )

    # --- toolkit seeds ----------------------------------------------------
    for name in TOOLKIT_SEEDS:
        target = toolkit_root / name
        plan.actions.append(
            Action("exists" if target.exists() else "create-file", target,
                   "left byte-for-byte unchanged" if target.exists() else "seed")
        )

    # --- child seeds ------------------------------------------------------
    for alias, path in repositories:
        if not path.is_dir():
            continue
        for name in CHILD_SEEDS:
            target = path / name
            plan.actions.append(
                Action(
                    "exists" if target.exists() else "create-file",
                    target,
                    "left byte-for-byte unchanged" if target.exists()
                    else f"seed in child repository `{alias}` — MAKES IT DIRTY",
                )
            )
    return plan


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def write_atomic(path: Path, text: str) -> None:
    """Write via a temporary file in the same directory, then rename.

    Every machine-owned file this toolkit writes goes through a rename, so a
    reader never sees a half-written JSON document — including a reader that is
    another copy of this toolkit running concurrently.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, path)


def apply_plan(plan: Plan) -> list[Action]:
    """Create exactly what the plan said was missing. Never overwrite."""
    done: list[Action] = []
    repositories = [(alias, str(path)) for alias, path in plan.repositories]

    for action in plan.actions:
        if action.kind == "create-config":
            if action.path.exists():
                continue  # somebody else won the race; theirs is authoritative
            write_atomic(
                action.path,
                json.dumps(config_payload(plan), indent=2) + "\n",
            )
            done.append(action)
        elif action.kind == "create-dir":
            if action.path.is_dir():
                continue
            action.path.mkdir(parents=True, exist_ok=True)
            done.append(action)
        elif action.kind == "create-file":
            if action.path.exists():
                continue  # NEVER overwrite, NEVER merge
            if action.path.parent == plan.toolkit_root:
                text = seed_text(
                    action.path.name, plan.product_id, plan.product_name,
                    str(plan.data_root), repositories,
                )
            else:
                alias = next(
                    (alias for alias, path in plan.repositories
                     if path.resolve() == action.path.parent.resolve()),
                    action.path.parent.name,
                )
                text = child_seed_text(
                    action.path.name, alias, plan.toolkit_root,
                    action.path.parent, plan.product_name,
                )
            write_atomic(action.path, text)
            done.append(action)
    return done


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def render(plan: Plan, applied: list[Action] | None = None) -> str:
    out: list[str] = []
    mode = "APPLIED" if applied is not None else "PLAN ONLY — nothing was written"
    out.append("")
    out.append(f"  toolkit:   {plan.toolkit_root}")
    out.append(f"  father:    {plan.father}")
    out.append(f"  product:   {plan.product_id} ({plan.product_name})")
    out.append(f"  data root: {plan.data_root}")
    out.append(f"  config:    {plan.config_path}"
               + ("  [exists — authoritative]" if plan.config_exists else "  [would be created]"))
    out.append("")

    out.append("  REPOSITORIES")
    if plan.repositories:
        width = max(len(alias) for alias, _ in plan.repositories)
        for alias, path in plan.repositories:
            mark = "" if path.is_dir() else "   MISSING"
            out.append(f"    {alias.ljust(width)}  {path}{mark}")
    else:
        out.append("    (none)")
    out.append("")

    if plan.ignored:
        out.append("  IGNORED SIBLINGS")
        for path, why in plan.ignored:
            out.append(f"    {path.name}  —  {why}")
        out.append("")

    writes = plan.writes
    if applied is None:
        out.append(f"  WOULD CREATE ({len(writes)})")
        rows = writes
    else:
        out.append(f"  CREATED ({len(applied)})")
        rows = applied
    if rows:
        for action in rows:
            suffix = f"   [{action.note}]" if action.note else ""
            out.append(f"    {action.path}{suffix}")
    else:
        out.append("    (nothing — everything the plan needs already exists)")
    out.append("")

    child_writes = [
        a for a in rows
        if a.kind == "create-file" and a.path.parent != plan.toolkit_root
    ]
    if child_writes:
        if applied is not None:
            out.append("  !! FILES WERE PLACED INSIDE CHILD REPOSITORIES !!")
            out.append("     Those repositories are now dirty. Nothing was committed and")
            out.append("     nothing was pushed — review and commit them yourself:")
        else:
            out.append("  !! FILES WOULD BE PLACED INSIDE CHILD REPOSITORIES !!")
            out.append("     Applying would leave those repositories dirty. Nothing would be")
            out.append("     committed and nothing pushed — the commit stays your decision:")
        for action in child_writes:
            out.append(f"       {action.path}")
        out.append("")

    unchanged = [a for a in plan.actions if a.kind == "exists" and a.path.is_file()]
    if unchanged:
        out.append(f"  LEFT UNCHANGED ({len(unchanged)})")
        for action in unchanged:
            out.append(f"    {action.path}")
        out.append("")

    if plan.discrepancies:
        out.append("  DISCREPANCIES (reported, never auto-corrected)")
        for line in plan.discrepancies:
            out.append(f"    {line}")
        out.append("")

    out.append(f"  {mode}")
    if applied is None:
        out.append("  Re-run with --apply to make these changes.")
    out.append("")
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or apply this toolkit's workspace initialization."
    )
    parser.add_argument("--product", help="product id (required when creating workspace.json)")
    parser.add_argument("--name", help="human-readable product name")
    parser.add_argument("--data-dir", help=f"data root (default: ../{DEFAULT_DATA_DIRNAME})")
    parser.add_argument("--toolkit-root", type=Path, help="override the toolkit root (tests)")
    parser.add_argument("--config", type=Path, help="an explicit workspace.json path")
    parser.add_argument("--apply", action="store_true", help="write the plan")
    parser.add_argument("--json", action="store_true", help="machine-readable plan")
    args = parser.parse_args()

    root = args.toolkit_root or Path(__file__).resolve().parent.parent
    try:
        plan = build_plan(root, args.product, args.name, args.data_dir, args.config)
        applied = apply_plan(plan) if args.apply else None
    except (InitError, workspace_config.WorkspaceError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(
            {
                "toolkit_root": str(plan.toolkit_root),
                "father": str(plan.father),
                "config_path": str(plan.config_path),
                "config_exists": plan.config_exists,
                "product": {"id": plan.product_id, "name": plan.product_name},
                "data_root": str(plan.data_root),
                "repositories": [
                    {"alias": alias, "path": str(path)} for alias, path in plan.repositories
                ],
                "ignored": [
                    {"path": str(path), "reason": why} for path, why in plan.ignored
                ],
                "would_create": [str(a.path) for a in plan.writes],
                "created": [str(a.path) for a in (applied or [])],
                "discrepancies": plan.discrepancies,
                "applied": applied is not None,
            },
            indent=2,
        ))
    else:
        sys.stdout.write(render(plan, applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
