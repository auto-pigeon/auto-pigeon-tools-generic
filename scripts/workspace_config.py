#!/usr/bin/env python3
"""The ONE implementation of "which repositories exist, what are they called, and
where does operational data live".

This replaces the source toolkit's `workspace_paths.py`, which answered the same
three questions by walking to a shared manifest inside a specific product
repository, and by climbing a ladder of an environment variable and two fixed
sibling directory names. Both of those encoded one product's layout: the manifest
named that product's repositories, and the ladder named that product's data
directory. Neither survives a second product.

Here the answer is a single file at the toolkit repository's root:

    workspace.json

and there is deliberately no second format, no fallback manifest, no discovery by
marker file and no inherited default. A toolkit checkout with no `workspace.json`
does not guess — it says to run `./init.sh`.

THREE THINGS IT ANSWERS
  TOPOLOGY   which repositories this product has and what each is called.
             `repositories: [{alias, path}]`. Paths resolve RELATIVE TO
             workspace.json; an absolute path is accepted when explicitly
             configured.
  DATA ROOT  `data_root` — prompts, handoffs, run state, reports. One machine's
             disk, unversioned, potentially large, and the operator's to back up.
  AGENTS     which agent commands are enabled and what they are called.

IDENTITY, AND SYMLINKS
  A repository's identity is its RESOLVED path — `Path.resolve()`, which follows
  every symlink. Two aliases pointing at the same checkout through different
  symlinks are the SAME repository and are refused as a duplicate, because a
  runner that thought they were two would take a branch snapshot of one and
  verify the other. The unresolved path is what gets printed in errors, because
  that is what the operator wrote.

ENVIRONMENT OVERRIDES, AND THE PRECEDENCE RULE
  Exactly one, and it is documented in README.md:

      AUTOKIT_WORKSPACE_CONFIG   path to the workspace.json to use

  Precedence, highest first:
      1. an explicit --config/`config_path=` argument
      2. $AUTOKIT_WORKSPACE_CONFIG
      3. <toolkit repository root>/workspace.json

  There is no environment override for the data root or for any individual
  repository path. One file decides the layout; a second way to be wrong about a
  path is not a feature.

USAGE
    workspace_config.py --print data-root
    workspace_config.py --print aliases          # ALIAS<TAB>PATH, one per line
    workspace_config.py --print repositories     # canonical aliases, config order
    workspace_config.py --print config           # the resolved config as JSON
    workspace_config.py --print toolkit-root
    workspace_config.py --path <alias>
    workspace_config.py --alias <alias>          # canonical spelling of an alias
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "auto-pigeon-toolkit-workspace/1.0"
CONFIG_BASENAME = "workspace.json"
CONFIG_ENV = "AUTOKIT_WORKSPACE_CONFIG"

#: Everything the toolkit owns under the data root. `LLM/` is inherited from the
#: source toolkit's layout unchanged, because the prompt/handoff tree is the part
#: agents and operators already know how to read.
DATA_SUBDIRS = (
    "LLM/prompts",
    "LLM/handoffs",
    "LLM/backlog",
    "LLM/runs",
    "LLM/reports",
    "LLM/fixtures",
    "LLM/generated",
)

DEFAULT_AGENTS = {
    "claude": {"enabled": True, "command": "claude"},
    "codex": {"enabled": True, "command": "codex"},
}


class WorkspaceError(RuntimeError):
    """Anything that makes the configuration unusable. Never a fallback."""


@dataclass(frozen=True)
class Repository:
    alias: str          #: canonical spelling, as written in workspace.json
    path: Path          #: as written, resolved against workspace.json's directory
    real: Path          #: fully resolved — the identity used for duplicate checks

    @property
    def exists(self) -> bool:
        return self.path.is_dir()

    @property
    def is_git(self) -> bool:
        return (self.path / ".git").exists()


@dataclass(frozen=True)
class Workspace:
    config_path: Path
    toolkit_root: Path
    product_id: str
    product_name: str
    data_root: Path
    repositories: tuple[Repository, ...]
    agents: dict[str, dict[str, Any]]

    # -- alias lookup, case-insensitive, one table -------------------------
    def find(self, alias: str) -> Repository | None:
        wanted = alias.strip().lower()
        for repo in self.repositories:
            if repo.alias.lower() == wanted:
                return repo
        return None

    def require(self, alias: str) -> Repository:
        repo = self.find(alias)
        if repo is None:
            known = ", ".join(repo.alias for repo in self.repositories) or "(none configured)"
            raise WorkspaceError(
                f"unknown repository alias {alias!r}. "
                f"{self.config_path} configures: {known}"
            )
        if not repo.exists:
            raise WorkspaceError(
                f"repository {repo.alias!r} is configured at {repo.path} but that "
                "directory does not exist. Fix its `path` in "
                f"{self.config_path}, or clone it there."
            )
        return repo

    # -- the canonical path derivations, and there are no others -----------
    def prompt_dir(self, alias: str) -> Path:
        return self.data_root / "LLM" / "prompts" / self.require(alias).alias

    def handoff_dir(self, alias: str) -> Path:
        return self.data_root / "LLM" / "handoffs" / self.require(alias).alias

    def run_state_root(self) -> Path:
        return self.data_root / ".run-sequence"

    def agent_command(self, name: str) -> str:
        entry = self.agents.get(name)
        if entry is None:
            raise WorkspaceError(
                f"agent {name!r} is not configured in {self.config_path}. "
                f"Configured: {', '.join(sorted(self.agents)) or '(none)'}"
            )
        if not entry.get("enabled", True):
            raise WorkspaceError(
                f"agent {name!r} is disabled in {self.config_path}. "
                'Set `"enabled": true` to use it.'
            )
        command = entry.get("command") or name
        return str(command)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "config_path": str(self.config_path),
            "toolkit_root": str(self.toolkit_root),
            "product": {"id": self.product_id, "name": self.product_name},
            "data_root": str(self.data_root),
            "repositories": [
                {
                    "alias": repo.alias,
                    "path": str(repo.path),
                    "exists": repo.exists,
                    "is_git": repo.is_git,
                }
                for repo in self.repositories
            ],
            "agents": self.agents,
        }


# ---------------------------------------------------------------------------
# Locating and reading the one configuration file
# ---------------------------------------------------------------------------
def toolkit_root(start: Path | None = None) -> Path:
    """This toolkit checkout's root — the directory holding scripts/."""
    here = (start or Path(__file__)).resolve()
    return here.parent.parent if here.is_file() else here


def config_path(explicit: Path | None = None, *, start: Path | None = None) -> Path:
    """Where the configuration is, by the one precedence rule.

    explicit argument > $AUTOKIT_WORKSPACE_CONFIG > <toolkit root>/workspace.json
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = os.environ.get(CONFIG_ENV, "").strip()
    if from_env:
        return Path(from_env).expanduser()
    return toolkit_root(start) / CONFIG_BASENAME


def _resolve_configured_path(raw: str, base: Path) -> Path:
    """A configured path, resolved the one documented way.

    Relative paths resolve against the DIRECTORY CONTAINING workspace.json, never
    against the current working directory — every runner here is invoked from
    somewhere different and a cwd-relative repository path would mean a different
    repository per caller. `~` expands. An absolute path is taken as written.
    """
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    # os.path.normpath rather than resolve(): this keeps the operator's spelling
    # (including a symlinked parent they chose deliberately) while removing the
    # `../` segments that make an error message unreadable. `real` below carries
    # the fully resolved identity.
    return Path(os.path.normpath(str(candidate)))


def load(explicit: Path | None = None, *, start: Path | None = None) -> Workspace:
    """Read, validate and return the workspace. Never falls back to a default."""
    path = config_path(explicit, start=start)
    if not path.is_file():
        raise WorkspaceError(
            f"no workspace configuration at {path}.\n"
            "  This toolkit has not been initialized for a product yet. Run:\n"
            "      ./init.sh --product <id> --name \"<Name>\"          # plan only\n"
            "      ./init.sh --product <id> --name \"<Name>\" --apply  # write it\n"
            f"  Or point {CONFIG_ENV} at an existing configuration."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise WorkspaceError(f"cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise WorkspaceError(
            f"{path} is not valid JSON: line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    if not isinstance(raw, dict):
        raise WorkspaceError(f"{path} must contain a JSON object, not {type(raw).__name__}")

    schema = raw.get("schema")
    if schema is not None and not str(schema).startswith("auto-pigeon-toolkit-workspace/"):
        raise WorkspaceError(
            f"{path} declares schema {schema!r}, which this toolkit does not implement. "
            f"Expected {SCHEMA}."
        )

    base = path.parent

    product = raw.get("product") or {}
    if not isinstance(product, dict):
        raise WorkspaceError(f"{path}: `product` must be an object")
    product_id = str(product.get("id") or "").strip()
    if not product_id:
        raise WorkspaceError(f"{path}: `product.id` is required and must be a non-empty string")
    product_name = str(product.get("name") or product_id).strip()

    raw_data_root = raw.get("data_root")
    if not isinstance(raw_data_root, str) or not raw_data_root.strip():
        raise WorkspaceError(
            f"{path}: `data_root` is required — the directory that owns prompts, "
            "handoffs, run state and reports."
        )
    data_root = _resolve_configured_path(raw_data_root.strip(), base)

    entries = raw.get("repositories")
    if not isinstance(entries, list):
        raise WorkspaceError(f"{path}: `repositories` must be a list")

    repositories: list[Repository] = []
    seen_alias: dict[str, str] = {}
    seen_path: dict[str, str] = {}
    for index, entry in enumerate(entries):
        where = f"{path}: repositories[{index}]"
        if not isinstance(entry, dict):
            raise WorkspaceError(f"{where} must be an object with `alias` and `path`")
        alias = entry.get("alias")
        raw_path = entry.get("path")
        if not isinstance(alias, str) or not alias.strip():
            raise WorkspaceError(f"{where} is missing a non-empty `alias`")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise WorkspaceError(f"{where} ({alias}) is missing a non-empty `path`")
        alias = alias.strip()
        if "/" in alias or os.sep in alias or alias in (".", ".."):
            # The alias is also a DIRECTORY NAME under the data root — it names the
            # prompt and handoff folders. A separator in it would silently write
            # outside the queue it claims to be.
            raise WorkspaceError(
                f"{where}: alias {alias!r} may not contain a path separator — an alias "
                "also names this repository's prompt and handoff folders."
            )
        lowered = alias.lower()
        if lowered in seen_alias:
            raise WorkspaceError(
                f"{path}: duplicate repository alias {alias!r} (already used by "
                f"{seen_alias[lowered]!r}). Aliases are compared case-insensitively "
                "and must be unique."
            )
        seen_alias[lowered] = alias

        resolved = _resolve_configured_path(raw_path.strip(), base)
        # Identity is the fully resolved path: two aliases reaching one checkout
        # through different symlinks are one repository, and a runner must not
        # snapshot one and verify the other.
        try:
            real = resolved.resolve()
        except OSError:
            real = resolved
        key = str(real)
        if key in seen_path:
            raise WorkspaceError(
                f"{path}: repositories {seen_path[key]!r} and {alias!r} resolve to the "
                f"same worktree ({real}). Each alias must be a distinct Git worktree."
            )
        seen_path[key] = alias
        repositories.append(Repository(alias=alias, path=resolved, real=real))

    agents_raw = raw.get("agents")
    agents: dict[str, dict[str, Any]] = {}
    if agents_raw is None:
        agents = {name: dict(value) for name, value in DEFAULT_AGENTS.items()}
    elif not isinstance(agents_raw, dict):
        raise WorkspaceError(f"{path}: `agents` must be an object")
    else:
        for name, value in agents_raw.items():
            if not isinstance(value, dict):
                raise WorkspaceError(f"{path}: agents.{name} must be an object")
            agents[str(name)] = {
                "enabled": bool(value.get("enabled", True)),
                "command": str(value.get("command") or name),
            }

    return Workspace(
        config_path=path.resolve(),
        toolkit_root=toolkit_root(start),
        product_id=product_id,
        product_name=product_name,
        data_root=data_root,
        repositories=tuple(repositories),
        agents=agents,
    )


def require_repositories(workspace: Workspace) -> None:
    """Refuse a configuration whose repositories are not all present.

    Called by anything that is about to WORK a repository. Deliberately not part
    of `load`: `init.sh` and the read-only reports have to be able to read a
    configuration that describes a checkout somebody has not cloned yet, and say
    so precisely.
    """
    missing = [repo for repo in workspace.repositories if not repo.exists]
    if missing:
        lines = "\n".join(f"    {repo.alias}: {repo.path}" for repo in missing)
        raise WorkspaceError(
            f"{len(missing)} configured repository/ies do not exist:\n{lines}\n"
            f"  Configured in {workspace.config_path}."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve the toolkit workspace configuration.")
    parser.add_argument("--config", type=Path, help="an explicit workspace.json")
    parser.add_argument(
        "--print",
        dest="what",
        choices=("data-root", "aliases", "repositories", "config", "toolkit-root",
                 "product-id", "product-name", "agent-command"),
    )
    parser.add_argument("--path", metavar="ALIAS", help="print one repository's absolute path")
    parser.add_argument("--alias", metavar="ALIAS", help="print an alias's canonical spelling")
    parser.add_argument("--agent", metavar="NAME", help="with --print agent-command")
    args = parser.parse_args()

    if not args.what and not args.path and not args.alias:
        parser.error("say what to print: --print ..., --path ALIAS or --alias ALIAS")

    try:
        workspace = load(args.config)
        if args.path:
            print(workspace.require(args.path).path)
            return 0
        if args.alias:
            print(workspace.require(args.alias).alias)
            return 0
        if args.what == "data-root":
            print(workspace.data_root)
        elif args.what == "toolkit-root":
            print(workspace.toolkit_root)
        elif args.what == "product-id":
            print(workspace.product_id)
        elif args.what == "product-name":
            print(workspace.product_name)
        elif args.what == "aliases":
            # ALIAS<TAB>PATH — what the shell runners build their one alias table
            # from. A tab, so a path containing spaces survives `IFS=$'\t' read`.
            for repo in workspace.repositories:
                print(f"{repo.alias}\t{repo.path}")
        elif args.what == "repositories":
            for repo in workspace.repositories:
                print(repo.alias)
        elif args.what == "agent-command":
            if not args.agent:
                parser.error("--print agent-command needs --agent NAME")
            print(workspace.agent_command(args.agent))
        else:
            print(json.dumps(workspace.to_json(), indent=2, sort_keys=True))
    except WorkspaceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
