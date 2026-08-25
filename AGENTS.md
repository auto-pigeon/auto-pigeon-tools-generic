<!-- graft:start -->
## Graft — repo context graph

This repo is indexed in `graft/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `graft map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `graft ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `graft grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `graft skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `graft callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `graft/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `graft ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `graft build` (deterministic,
no API key, $0).
<!-- graft:end -->

# auto-pigeon-toolkit — agent instructions

This repository is a **reusable, configurable agent-workflow toolkit**. It is
cloned once per product and sits beside that product's repositories. It contains
no product code.

Read `README.md` for the layout and the configuration contract, and `WORKFLOW.md`
for the prompt/handoff lifecycle. `bootstrap/` holds the prompt that commissioned
this repository, preserved verbatim.

## The one rule that shapes everything here

**Every repository and path lookup comes from `workspace.json`, through
`scripts/workspace_config.py`, and from nowhere else.**

This toolkit was extracted from `auto-pigeon-tools`, a single-product workflow
repository. What made that one unable to serve a second product was not its
design — the runners, the queue model and the completion rules are its design,
and they are kept — but three hardcoded things: a shared manifest living inside
one product repository, a ladder of fixed data-directory locations, and three
hand-maintained alias tables that could disagree with each other.

So when you change anything here:

- **Never add a second alias table.** If a shell script needs to know which
  repositories exist, it asks `workspace_config.py --print aliases`.
- **Never derive a repository path from this directory's location.** Repositories
  are configured, not adjacent. `<parent>/<name>` is exactly the assumption that
  had to be removed.
- **Never hardcode a product's name, a data directory, a port, a domain, a
  container or a service.** `tests/test_no_product_topology.py` enforces this on
  every test run, over executable code and over the whole repository, with a
  narrow documented allowlist.
- **Never add a competing configuration format.** One file, one schema, one
  precedence rule.

## Layout

```text
init.sh                  configure this checkout for one product
run-agent.sh             one prompt, one repository
run-sequence.sh          whole queues, unattended
scripts/
  workspace_config.py    THE topology helper — read this first
  workspace_init.py      the initialization planner and applier
  resolve_next_prompt.py which prompt is next, and why not
  agent_task.py          handoff writing and validation
  sequence_plan.py       the read-only dependency-aware planner
  execution_history.py   the read-only execution-time report
  execution_time.py      timing measurement and projection
  branch_policy.py       the main-branch preflight and completion gate
  dirty_baseline.py      what was already dirty, and whether it survived
  prompt_frontmatter.py  the ONE frontmatter decoder
  stream_progress.py     headless progress rendering
tests/                   unit tests, integration tests, the topology guard
```

## Implementation constraints

- Bash and Python 3, following the split the source toolkit proved: shell drives
  processes and terminals, Python parses and decides.
- **Python standard library only.** No package manager, no runtime service, no
  Docker, no GitHub CLI, no JavaScript, no native PowerShell.
- Linux, macOS and WSL.
- `git -C <path>`, never an assumed current directory.
- **Quote every path.** Paths containing spaces are tested and must keep working.
- No `eval` on generated commands.
- Machine-owned JSON and state files are written atomically (write-then-rename).
- Never weaken the branch, dirty-worktree or completion safety rules.
- Never read credentials out of a configured repository.

## Before you commit

```bash
./tests/run-tests.sh
```

Python unit tests, `bash -n` on every script, ShellCheck when it is available
(reported as *skipped*, never as passed, when it is not), and shell integration
that drives both runners end to end against a stub agent in a temporary
workspace. No test may use a real sibling repository as a fixture, and no test
may start a paid agent.
