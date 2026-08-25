# auto-pigeon-toolkit

A configurable, multi-repository **agent workflow toolkit**. Clone it once per
product, point it at that product's repositories, and drive prompt queues through
Claude or Codex — one fresh agent session per prompt, with dependency-aware
selection, context rollover, dirty-worktree protection, handoffs, execution
timing and a main-branch completion gate.

It contains no product code and no product operations. It is the control
repository for a workspace, and its entire notion of "which repositories exist"
lives in one file: **`workspace.json`**.

---

## Quick start

```bash
git clone <url> auto-pigeon-toolkit
cd auto-pigeon-toolkit

./init.sh --product example --name "Example Product"           # plan only — writes nothing
./init.sh --product example --name "Example Product" --apply   # write it

$EDITOR workspace.json     # rename an alias, drop a repository, move the data root

run-agent.sh api first-task.md
run-sequence.sh --queue web second-task.md
run-sequence.sh --extract-sequence
run-sequence.sh --history
```

`./init.sh` without `--apply` prints exactly what it would do and creates
nothing, so the first command above is always safe to run.

---

## Directory topology

A product is one or more repositories cloned into a single **father** directory,
with this toolkit and an unversioned data directory beside them:

```text
/father/
├── auto-pigeon-toolkit/     this repository — workflow, runners, workspace.json
├── workspace-data/          operational data (unversioned, can grow to GBs)
├── example-api/             ┐
├── example-web/             ├─ the product's repositories
└── example-worker/          ┘
```

Only **immediate siblings** are discovery candidates during initialization.
Repositories may afterwards live anywhere: `workspace.json` accepts absolute
paths, so a checkout on another volume works exactly as well as a sibling.

### The data directory

```text
workspace-data/
├── LLM/
│   ├── prompts/
│   │   └── <alias>/          the queue: every .md here is outstanding work
│   │       ├── done/         finished — never selected again
│   │       └── blocked/      attempted and genuinely stuck
│   ├── handoffs/
│   │   └── <alias>/          one handoff per prompt, same filename
│   ├── backlog/
│   ├── runs/
│   ├── reports/
│   ├── fixtures/
│   └── generated/
└── .run-sequence/<run-id>/   runner state: rollover records, timing, baselines
```

**This directory is not version-controlled, and backing it up is the operator's
responsibility.** It holds every prompt, every handoff, every run log and every
generated artifact this workspace has produced. Nothing in this toolkit backs it
up, replicates it, or prunes it.

`runs/`, `reports/`, `fixtures/` and `generated/` are never recursively scanned
during ordinary status or initialization; `--history` reads handoff metadata and
per-run timing ledgers, not run logs. Machine-owned JSON and state files are
written atomically (write-then-rename). Ordinary initialization and repair never
delete data.

---

## Configuration

One file, at this repository's root:

```json
{
  "schema": "auto-pigeon-toolkit-workspace/1.0",
  "product": {
    "id": "example-product",
    "name": "Example Product"
  },
  "data_root": "../workspace-data",
  "repositories": [
    { "alias": "api",        "path": "../example-api" },
    { "alias": "web",        "path": "../example-web" },
    { "alias": "automation", "path": "../example-automation" }
  ],
  "agents": {
    "claude": { "enabled": true, "command": "claude" },
    "codex":  { "enabled": true, "command": "codex" }
  }
}
```

| Field | Meaning |
| --- | --- |
| `schema` | `auto-pigeon-toolkit-workspace/1.0`. An unrecognised schema is refused, not guessed at. |
| `product.id` | Required. Identifies the product in seeds and reports. |
| `product.name` | Human-readable. Defaults to the id. |
| `data_root` | Required. Owns prompts, handoffs, run state and reports. |
| `repositories[].alias` | The repository's name **and** its queue and handoff folder name. Unique, case-insensitive, no path separators. |
| `repositories[].path` | Where the checkout is. |
| `agents.<name>.enabled` | A disabled agent refuses before anything is resolved. |
| `agents.<name>.command` | The executable to run. Point it at a wrapper if you need to. |

### Path rules

- **Relative paths resolve from the directory containing `workspace.json`** —
  never from the current working directory. Every runner is invoked from
  somewhere different, and a cwd-relative repository path would mean a different
  repository per caller.
- **Absolute paths are accepted** and stored as written.
- `~` expands.
- Paths containing spaces work throughout.

### How aliases work

An alias is the repository's name everywhere: on the command line
(`run-agent.sh api`), in a prompt's `requires:` and `mutation_targets:`, in the
run summary, and as the folder name for its queue (`LLM/prompts/api/`) and its
handoffs (`LLM/handoffs/api/`).

- **Lookup is case-insensitive.** `api`, `API` and `Api` all resolve.
- **The configured spelling is what gets printed and what names the folders.**
- **Aliases must be unique** case-insensitively, and **paths must resolve to
  distinct Git worktrees** — two aliases reaching one checkout through different
  symlinks are refused as duplicates.
- **An unknown alias fails loudly**, naming everything the configuration does
  have. There is no fallback table anywhere in this toolkit.

### Identity, and symlinks

A repository's identity is its **fully resolved path** (`Path.resolve()`, which
follows every symlink). That is what duplicate detection compares, and what
`agent_task.py` matches a `--repo-root` against. The path *as written* is what
error messages print, because that is what you typed.

### Environment overrides

Exactly one, and it is subordinate to an explicit flag:

```text
AUTOKIT_WORKSPACE_CONFIG    the workspace.json to use
```

Precedence, highest first:

1. an explicit `--config PATH` argument
2. `$AUTOKIT_WORKSPACE_CONFIG`
3. `<toolkit repository root>/workspace.json`

There is **no** environment override for the data root or for any individual
repository path. Two further variables affect policy rather than paths:

```text
AUTOKIT_TARGET_BRANCH       the branch completion requires (default: main)
AUTOKIT_BRANCH_POLICY=off   skip the branch preflight and completion gate
AGENT_PRINT=1               drive claude headless instead of the native TUI
AGENT_STREAM=0              with AGENT_PRINT, silence the streamed progress
```

### When configuration is missing or malformed

It never falls back to a default layout. A missing `workspace.json` tells you to
run `./init.sh`; a malformed one reports the JSON error with its position; a
missing repository reports the configured path. Guessing would be worse than
failing, because a runner that guessed wrong would work the wrong repository.

---

## Initialization

```bash
./init.sh [--product ID] [--name NAME] [--data-dir PATH] [--config PATH]
./init.sh [options] --apply
```

### Read-only by default

Without `--apply`, `init.sh`:

1. resolves this toolkit's directory and its father;
2. inspects **immediate sibling directories only**;
3. identifies siblings whose root contains `.git` (a directory or a file — a
   linked worktree and a submodule both count);
4. excludes the toolkit itself;
5. excludes the proposed or configured data directory;
6. ignores ordinary non-Git directories;
7. ignores Git repositories nested *below* an immediate sibling;
8. proposes a deterministic alias per repository, from its basename;
9. inventories which seed documents already exist;
10. prints the exact configuration and filesystem changes it would make;
11. **writes nothing.**

No directory is created, no repository initialized, no agent started.

### With `--apply`

It creates, and only when missing:

- `workspace.json`
- the data directory skeleton
- this repository's seed documents
- `AGENTS.md` and `DESIGN.md` inside each configured child repository

It will not overwrite an existing `workspace.json`, will not silently replace
aliases you chose, will not overwrite or merge an existing `AGENTS.md`,
`DESIGN.md` or `UI-DESIGN.md`, will not commit anything in a child repository,
will not push, will not modify a Git remote, and will not scan beyond immediate
sibling roots.

**A second identical `--apply` is a no-op.**

### If `workspace.json` already exists

It is **authoritative**. Its product, data root and repository list are used as
written. Discovery still runs, but only to report differences — a sibling that is
a Git repository but is not configured is listed under DISCREPANCIES, never added.

### How existing documents are protected

Every write is conditional on the target not existing. `init.sh` has no code path
that opens an existing file for writing, merges into one, or renames one out of
the way. Documents you wrote stay byte-for-byte identical, and the report lists
them under LEFT UNCHANGED so you can see that it saw them.

### Alias proposal

Lowercased, runs of non-alphanumeric characters collapsed to `-`, trimmed:
`Example API` → `example-api`, `my_worker` → `my-worker`. Collisions get `-2`,
`-3` suffixes in sorted order, so the same tree always proposes the same names.
Rename them in `workspace.json` afterwards if you prefer something shorter.

### Seed documents

Seeds may state the product id and name, the configured repositories and their
paths, the workflow/data split, and where a real document belongs. They may not
invent repository responsibilities, service relationships, API contracts,
architecture, deployment topology or UI behavior — so they are mostly headings
and `TODO` markers. A document that guesses is worse than one that is honestly
empty.

`UI-DESIGN.md` is seeded at toolkit level only, never into every child
repository: a workspace having a user interface does not mean every repository
in it does.

**Files created inside child repositories leave those repositories dirty.** They
are reported prominently. Committing them is your decision.

---

## Commands

### `run-agent.sh` — one prompt, one repository

```bash
run-agent.sh [--agent claude|codex] <alias> [selector] [agent arguments...]
run-agent.sh --repos
run-agent.sh --help
```

| Selector | Meaning |
| --- | --- |
| *(nothing)* | the next prompt the resolver picks: the oldest still directly in the queue folder whose prerequisites are all in `done/` |
| `<prompt>.md` | that exact prompt, skipping automatic selection |
| `"a message"` | sent to the agent unchanged — no resolver, no wrapper |
| `--interactive` | a plain session with nothing injected |
| `--dry-run` | resolve everything, print it as JSON, start nothing, write nothing |

The legacy form `run-agent.sh <claude|codex> <alias> ...` still works. Any other
argument passes straight through to the agent CLI.

### `run-sequence.sh` — whole queues, unattended

```bash
run-sequence.sh                                  drain every eligible queue
run-sequence.sh --queue <alias>                  one repository's queue
run-sequence.sh --queue <alias> <prompt.md>...   an explicit ordered list
run-sequence.sh --extract-sequence               READ-ONLY dependency-aware plan
run-sequence.sh --history                        READ-ONLY execution history
run-sequence.sh --repos
run-sequence.sh --help
```

| Option | Meaning |
| --- | --- |
| `--queue <alias>` | add one repository's queue (repeatable) |
| `--drain` | explicit spelling of the default |
| `--agent claude\|codex` | which agent to drive |
| `--max N` | stop after N prompts per repository |
| `--max-context-rollovers N` | rollovers allowed per prompt (default 5) |
| `--single-session` | one agent context for a whole queue — **not** context-safe |
| `--allow-dirty` / `--accept-dirty` | start over pre-existing uncommitted work |
| `--format human\|shell\|json` | output format for the two read-only reports |
| `--dry-run` | print the plan and run nothing |

**The default is one fresh agent session per prompt.** State crosses prompts
through the repository, its Git history and the canonical handoff — never
through conversation memory. When Claude Code is about to auto-compact, a
`PreCompact` hook intercepts it, the work is checkpoint-committed, and a fresh
session resumes the *same* prompt.

**A dirty worktree refuses by default.** `--allow-dirty` records a baseline of
exactly what was already dirty, tells the agent to leave it alone, and fails the
prompt if any of it is committed, deleted or reverted. It cannot separate
overlapping edits to the same hunk — that case stops the run. It relaxes nothing
else.

**Completion is a filesystem fact:** the prompt file moved into `done/`, a
handoff pinned to that exact prompt, a clean worktree, and every repository the
prompt changed back on `main` with its commits reachable from `main`.

The two read-only reports mutate nothing — no agent, no prompt moved, no handoff
written, no commit — and refuse to combine with each other or with anything that
executes.

---

## Prerequisites

- **Bash** and **Python 3** (standard library only). No package manager, no
  runtime service, no Docker, no GitHub CLI, no JavaScript.
- **Git**, invoked as `git -C <path>` throughout.
- **Linux, macOS and WSL** are supported. There is no native PowerShell
  implementation; under Windows, run this inside WSL.
- **An agent CLI**: `claude` (Claude Code) and/or `codex`, on `PATH` or named by
  absolute path in `agents.<name>.command`. Context rollover and the Stop-hook
  supervision are Claude Code features; `--agent codex` runs without them.
- **ShellCheck** is optional. `tests/run-tests.sh` uses it when it is installed
  (or when `$SHELLCHECK` points at one) and reports the stage as *skipped* —
  never as passed — when it is not.

---

## Tests

```bash
./tests/run-tests.sh
SHELLCHECK=/path/to/shellcheck ./tests/run-tests.sh
```

Python unit tests, `bash -n` on every shell script, ShellCheck when available,
and shell integration covering initialization and both runners end to end.

Every test builds its own workspace under a temporary directory and drives a
**stub agent** configured through `agents.<name>.command`. No test touches a real
sibling repository, and no test starts a paid agent — what is under test is the
runner, and paying a model to prove a process was started would prove nothing
extra.

---

## Where this came from, and what was left behind

This toolkit is extracted from `auto-pigeon-tools`, the single-product workflow
repository it grew up in. The commissioning prompt is preserved verbatim in
[`bootstrap/`](bootstrap/).

**Taken and generalized:** `run-agent.sh`, `run-sequence.sh`, prompt queue
resolution, dependency resolution, Claude and Codex execution, context
rollover/checkpoints, dirty-worktree protection and `--allow-dirty`, handoff
generation and validation, execution timing and history, sequence extraction,
branch and completion policy, run logs, and the one frontmatter decoder.

**Deliberately left behind** — these are product operations, not workflow:
product launchers and teardown scripts, Docker Compose stacks, health-monitoring
configuration and component dashboards, environment contracts, scheduling and
health checks, data-staging and database handling, backup scripts written for one
specific data root, and every hardcoded repository alias, port, domain, container
name and service topology.

**Replaced rather than copied:** the shared workspace manifest that lived inside
one product repository, the ladder of fixed data-directory locations, the
per-repository configuration file committed into every checkout, and the three
hand-maintained alias tables. All four are now `workspace.json`.

**Not preserved:** `--version` on both runners reported each product component's
build number and a pinned monitoring release. It is replaced by `--repos`, which
prints the configured aliases and their paths.

`tests/test_no_product_topology.py` enforces all of this on every test run: the
old product's repository names, data-root paths, aliases and helper filenames may
not appear in executable code — not in a string, not in a comment — and outside a
narrow documented allowlist they may not appear anywhere at all.

---

## Documents

| File | What it is |
| --- | --- |
| `README.md` | this file |
| `AGENTS.md` | instructions for agents working in *this* repository |
| `WORKFLOW.md` | the prompt/handoff lifecycle, in full |
| `workspace.json` | the one configuration file (created by `./init.sh --apply`) |
| `workspace.example.json` | a reference configuration to copy from |
| `bootstrap/` | the prompt that commissioned this repository |

`workspace.json` is **not** gitignored: it is one product's configuration and
belongs in that product's clone, in version control, where the team can see which
repositories the workspace has. This template ships without one.

## Licence

MIT — see [`LICENSE`](LICENSE).
