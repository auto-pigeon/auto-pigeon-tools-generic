---
task_id: 20260825_01_Bootstrap-Configurable-Auto-Pigeon-Toolkit
priority: bootstrap
mutation_targets:
  - auto-pigeon-toolkit-generic
---

# Bootstrap a configurable Auto-Pigeon Toolkit from AUT

## Goal

Populate this new repository as a reusable, configurable version of Auto-Pigeon Tools’ agent-workflow machinery.

This is not a new orchestration platform and not a rewrite.

Use the existing sibling `auto-pigeon-tools` repository as the implementation source. Extract and generalize its proven agent workflow:

- `run-agent.sh`;
- `run-sequence.sh`;
- prompt queue resolution;
- dependency resolution;
- Claude and Codex execution;
- context rollover/checkpoints;
- dirty-worktree protection and `--allow-dirty`;
- handoff generation and validation;
- execution timing and history;
- sequence extraction;
- branch/commit completion checks;
- run logs.

Remove the hardcoded Auto-Pigeon repository topology and data paths. Replace them with one local `workspace.json`.

The finished repository will be cloned once per product as a sibling of that product’s repositories.

Do not modify `auto-pigeon-tools` or any other sibling repository.

## Intended layout

A product may consist of multiple repositories cloned into one father directory:

```text
/father/
├── auto-pigeon-toolkit/
├── workspace-data/
├── repo-one/
├── repo-two/
└── repo-three/
```

`auto-pigeon-toolkit` is the product’s workflow/control repository.

`workspace-data` is unversioned operational data comparable to Auto-Pigeon’s current `/mapper`. It may grow to several gigabytes.

Only immediate sibling directories are repository-discovery candidates.

## Source repository

Locate the existing AUT source in this order:

1. `$AUT_SOURCE_REPO`, when set;
2. `../auto-pigeon-tools`;
3. another immediate sibling whose Git remote or repository metadata identifies it as `auto-pigeon-tools`.

If no unambiguous source exists, stop before implementation and report the expected path.

Read before editing:

- AUT `AGENTS.md`;
- AUT `README.md`;
- `run-agent.sh`;
- `run-sequence.sh`;
- the canonical workspace/topology helpers;
- prompt and handoff readers;
- `resolve_next_prompt.py`;
- `agent_task.py`;
- context-rollover machinery;
- dirty baseline handling;
- sequence planning;
- execution history/timing;
- branch and completion policy;
- their tests.

Record which source commit was inspected.

Do not blindly copy the entire AUT repository.

## Scope boundary

### Include and generalize

Include the reusable agent-workflow components:

- repository discovery and alias resolution;
- prompt queues;
- handoffs;
- `run-agent.sh`;
- `run-sequence.sh`;
- Claude and Codex modes;
- dependency-aware prompt selection;
- `--extract-sequence`;
- `--history`;
- execution-time recording;
- dirty-worktree refusal and override;
- checkpoint/context rollover;
- completion and commit verification;
- run-state and concise logs;
- generic tests and documentation.

### Exclude

Do not copy or generalize Auto-Pigeon product operations:

- `launch-aup.sh`;
- `teardown-aup.sh`;
- AUP/AUB/AUC/AUE/AUG launchers;
- Docker Compose stacks;
- Gatus configuration;
- component health dashboards;
- Auto-Pigeon environment contracts;
- AUE scheduling/health;
- APMap staging;
- PocketBase data handling;
- backup scripts specific to `/mapper` or `/mapper-code`;
- hardcoded AUP/AUB/AUC/AUE/AUG/AULIBS aliases;
- hardcoded ports, domains, container names or service topology.

If a generic runner module currently depends on a product-specific helper, separate the narrow generic behavior instead of importing the product stack.

## Configuration contract

Create one repository-root configuration file:

```text
workspace.json
```

Example:

```json
{
  "schema": "auto-pigeon-toolkit-workspace/1.0",
  "product": {
    "id": "example-product",
    "name": "Example Product"
  },
  "data_root": "../workspace-data",
  "repositories": [
    {
      "alias": "api",
      "path": "../example-api"
    },
    {
      "alias": "web",
      "path": "../example-web"
    },
    {
      "alias": "automation",
      "path": "../example-automation"
    }
  ],
  "agents": {
    "claude": {
      "enabled": true,
      "command": "claude"
    },
    "codex": {
      "enabled": true,
      "command": "codex"
    }
  }
}
```

Requirements:

- paths are resolved relative to `workspace.json`;
- absolute paths are accepted when explicitly configured;
- aliases are unique and case-insensitive for lookup;
- repository paths must resolve to distinct Git worktrees;
- the toolkit repository itself is not a mutation target unless explicitly represented;
- unknown aliases fail clearly;
- duplicate paths or aliases fail clearly;
- missing repositories fail with the configured path;
- missing or malformed configuration never falls back to Auto-Pigeon assumptions;
- no source repository is required to contain the data directory;
- environment overrides, if retained, must be documented and subordinate to one clear precedence rule.

Do not support multiple competing configuration formats in v1.

## Data layout

The configured `data_root` owns operational data:

```text
workspace-data/
└── LLM/
    ├── prompts/
    │   ├── <repository-alias>/
    │   ├── done/
    │   └── blocked/
    ├── handoffs/
    │   └── <repository-alias>/
    ├── backlog/
    ├── runs/
    ├── reports/
    ├── fixtures/
    └── generated/
```

Preserve the current AUT queue/handoff structure when it is already more precise than this illustrative tree. There must still be exactly one canonical path derivation.

Large-data rules:

- do not recursively scan `runs`, `reports`, `fixtures` or `generated` during ordinary status or initialization;
- history reads handoff metadata, not complete run logs;
- initialization never hashes or inventories existing data content;
- writes of machine-owned JSON/state files are atomic;
- ordinary initialization or repair never deletes data;
- documentation must state that the unversioned data directory requires operator backup.

## Initialization

Add:

```text
init.sh
```

Interface:

```bash
./init.sh [--product ID] [--name NAME] [--data-dir PATH]
./init.sh [options] --apply
```

### Default: read-only plan

Without `--apply`, `init.sh` must:

1. resolve the toolkit repository and its father;
2. inspect immediate sibling directories only;
3. identify siblings whose root contains `.git`;
4. exclude the toolkit itself;
5. exclude the proposed/configured data directory;
6. ignore ordinary non-Git directories;
7. ignore nested Git repositories below an immediate sibling;
8. propose deterministic aliases based on repository basenames;
9. inventory whether relevant seed documents already exist;
10. print the exact configuration and filesystem changes it would make;
11. write nothing.

Do not start an agent, initialize Git repositories or create directories in planning mode.

### Apply

With `--apply`, initialization may:

- create `workspace.json`;
- create the configured data directories;
- create missing toolkit-level seed documents;
- create missing child-repository seed documents;
- leave existing content byte-for-byte unchanged.

It must not:

- overwrite an existing `workspace.json`;
- silently replace repository aliases chosen by the operator;
- overwrite or merge an existing `AGENTS.md`;
- overwrite or merge an existing `DESIGN.md`;
- overwrite or merge an existing `UI-DESIGN.md`;
- commit changes in child repositories;
- push anything;
- modify Git remotes;
- scan beyond immediate sibling roots.

If `workspace.json` already exists, use it as authoritative and report discrepancies rather than regenerating it.

A second identical `--apply` must be a no-op.

## Seed documents

At toolkit level, create missing deterministic seeds for:

```text
AGENTS.md
DESIGN.md
UI-DESIGN.md
WORKFLOW.md
README.md
```

Seeds may contain:

- product ID/name;
- configured repositories and paths;
- links to existing repository documents;
- the workflow/data separation;
- explicit empty sections and TODO markers.

They must not invent:

- repository responsibilities;
- service relationships;
- API contracts;
- architecture;
- deployment topology;
- UI behavior.

For each child repository:

- if `AGENTS.md` is missing, create a short pointer to the toolkit’s `AGENTS.md`, `WORKFLOW.md` and relevant workspace documentation;
- if `DESIGN.md` is missing, create a minimal repository-design seed;
- preserve every existing document byte-for-byte;
- do not create `UI-DESIGN.md` in every repository merely because the workspace has one.

Creating missing files inside child repositories is allowed only with `--apply`. Report every created file prominently because those repositories become dirty.

Do not commit child-repository changes.

## Preserve the existing runner interface

Keep existing operator commands wherever they are generic:

```bash
run-agent.sh <alias> <prompt.md>

run-sequence.sh --queue <alias> <prompt.md>...

run-sequence.sh --extract-sequence
run-sequence.sh --history
```

Also preserve currently implemented generic options, including:

- Claude and Codex selection;
- dry-run behavior;
- context-safe execution;
- dirty-worktree default refusal;
- `--allow-dirty` and compatible alias;
- explicit queue execution;
- dependency-aware drain;
- sequence extraction formats;
- history formats;
- timing/checkpoint fields.

Do not rename everything behind a new CLI in this task.

Every repository/path lookup must now come from `workspace.json` and shared topology helpers. Shell scripts must not maintain a second alias table.

## Remove hardcoded product assumptions

Audit all included source and tests for:

```text
/home/bario
/mapper
/mapper-code
auto-pigeon
auto-pigeon-backend
auto-pigeon-collaboration
auto-pigeon-extractor
auto-pigeon-gallery
auto-pigeon-libraries
AUP
AUB
AUC
AUE
AUG
AULIBS
PB
```

The repository’s own brand `auto-pigeon-toolkit` and documentation explaining its origin are allowed.

Examples and fixtures may use neutral names such as:

```text
example-api
example-web
example-worker
api
web
worker
```

No executable topology may depend on an Auto-Pigeon name.

Add a regression test with a narrow allowlist so hardcoded product topology cannot re-enter generic execution code.

## Implementation constraints

- Support Linux, macOS and WSL.
- Use Bash and Python, following the proven AUT split.
- Do not add JavaScript.
- Prefer Python’s standard library.
- Do not add a package manager or runtime service.
- Do not require Docker.
- Do not require GitHub CLI.
- Do not implement native PowerShell.
- Preserve paths containing spaces.
- Resolve symlinks deliberately and document the identity rule.
- Use `git -C <path>` rather than assuming the current directory.
- Quote every shell path.
- Do not use `eval` to execute generated commands.
- Do not weaken branch, dirty-worktree or completion safety.
- Do not access credentials from sibling repositories.

## Tests

Create isolated temporary workspaces. Do not use the developer’s real sibling repositories as mutation fixtures.

Cover at least:

1. planning mode performs zero writes;
2. immediate sibling Git repositories are discovered;
3. nested repositories are ignored;
4. non-Git siblings are ignored;
5. the toolkit excludes itself;
6. the configured data directory is excluded;
7. deterministic aliases are proposed;
8. duplicate aliases fail;
9. duplicate repository paths fail;
10. relative paths resolve from `workspace.json`;
11. explicit absolute paths work;
12. paths containing spaces work;
13. malformed configuration fails visibly;
14. missing configuration instructs the operator to run `init.sh`;
15. `--apply` creates the configuration and data skeleton;
16. `--apply` creates only missing seed documents;
17. existing `AGENTS.md`, `DESIGN.md` and `UI-DESIGN.md` remain byte-identical;
18. a second `--apply` is a no-op;
19. child repositories are never committed automatically;
20. queue aliases come exclusively from configuration;
21. `run-agent.sh` resolves a configured repository;
22. explicit `run-sequence.sh --queue` works;
23. dependency-aware selection works across configured aliases;
24. dirty-worktree refusal remains the default;
25. dirty override behavior remains unchanged;
26. context rollover/checkpoint behavior remains unchanged;
27. timing and history work under the configured data root;
28. sequence extraction emits configured aliases;
29. Claude and Codex invocation paths remain supported;
30. generic executable code contains no hardcoded Auto-Pigeon topology.

Run:

- Python unit tests;
- shell integration tests;
- `bash -n` on every shell script;
- ShellCheck when available, reporting honestly if unavailable;
- one disposable end-to-end initialization;
- one disposable `run-agent.sh` smoke using a stub agent;
- one disposable multi-prompt sequence using a stub agent;
- sequence extraction and history against disposable handoffs.

Do not run a paid/real agent merely to prove process invocation.

## Documentation

The README must provide a start-to-finish example:

```bash
git clone <url> auto-pigeon-toolkit
cd auto-pigeon-toolkit

./init.sh --product example --name "Example Product"
./init.sh --product example --name "Example Product" --apply

$EDITOR workspace.json

run-agent.sh api first-task.md
run-sequence.sh --queue web second-task.md
run-sequence.sh --extract-sequence
run-sequence.sh --history
```

Document:

- directory topology;
- configuration schema;
- read-only versus apply behavior;
- how aliases work;
- where prompts, handoffs and logs live;
- unversioned-data backup responsibility;
- how existing documents are protected;
- Linux/macOS/WSL support;
- Claude/Codex prerequisites;
- what was deliberately excluded from AUT.

## Git and bootstrap behavior

This prompt is being executed inside the new repository:

```text
/mapper-code/auto-pigeon-toolkit-generic
```

The repository may initially contain only this prompt and Git metadata.

Populate the repository directly. Do not create another nested repository.

Preserve this prompt in:

```text
bootstrap/20260825_01_Bootstrap-Configurable-Auto-Pigeon-Toolkit.md
```

If it began at the repository root, move it there before the final commit.

Before committing:

- ensure the current branch is local `main`;
- ensure no sibling repository was modified;
- run all tests;
- run `git diff --check`;
- inspect `git status`;
- verify no caches, temporary repositories, run logs or fixture Git metadata are staged;
- verify no secrets or real product data are staged.

Create one final commit:

```text
feat: bootstrap configurable multi-repository agent toolkit
```

Do not push.

## Completion report

Report:

- source AUT commit inspected;
- included and excluded AUT components;
- final repository tree;
- `workspace.json` schema;
- initialization behavior;
- document non-overwrite behavior;
- exact supported commands;
- tests and results;
- any behavior intentionally not preserved;
- final commit;
- final `git status`;
- confirmation that no sibling repository changed.

Do not claim completion if the implementation still depends on AULIBS, `/mapper`, `/mapper-code`, or any fixed AUP/AUB/AUC/AUE/AUG topology.
