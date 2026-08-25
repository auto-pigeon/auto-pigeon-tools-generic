# The prompt workflow

How work reaches an agent, how it comes back, and what "finished" means. This is
the toolkit's lifecycle; a product may add rules on top of it in its own
`AGENTS.md`, but it may not weaken these.

---

## 1. The queue IS the folder

A prompt's state is **where its file is**, not what any other file says about it:

```text
<data_root>/LLM/prompts/<alias>/            outstanding — this IS the queue
<data_root>/LLM/prompts/<alias>/done/       finished, never selected again
<data_root>/LLM/prompts/<alias>/blocked/    attempted and genuinely stuck
```

"What is next" is therefore: **the oldest file directly in the queue folder, by
filename sort, whose declared prerequisites are all in `done/`.** No handoff is
read to answer that question.

That rule is stable enough to hand to an agent in prose — "that folder is your
queue" — without it being able to misjudge the order. The alternative, inferring
what is finished by cross-referencing handoff statuses, is what once made a
queue's prompts 22–24 unreachable the moment 25 existed.

`<alias>` is the repository's alias from `workspace.json`. There is no second
place that names a queue folder.

## 2. Prompt frontmatter

A prompt may declare, in YAML frontmatter:

```yaml
---
task_id: 20260825_01_Add-Session-Endpoint
repo: api
mutation_targets:
  - api
  - web
requires:
  - 20260824_07_Earlier-Task            # same repository
  - repo: web                           # another repository
    prompt: 20260824_09_Other-Task.md
---
```

| Key | Read by | Meaning |
| --- | --- | --- |
| `task_id` | the planner | a stable identity, so the same task in two queues is a duplicate |
| `repo` / `repository` | the branch policy | the owning repository |
| `mutation_targets` / `touches` | the branch policy, the planner | every repository this prompt may change |
| `requires` | the resolver | prerequisites, checked recursively against `done/` |

A prompt written before this convention still works: prerequisites named in a
`## Prerequisite` prose section are parsed as a fallback.

**All of it goes through one decoder** (`scripts/prompt_frontmatter.py`).
Frontmatter that cannot be decoded is a **refusal**, never an empty list — a
prompt whose dependencies are unreadable is a prompt whose dependencies are
unknown, and scheduling on an unknown prerequisite is exactly the failure this
rule exists to remove.

**An undeclared mutation scope means "could touch anything"**, never "touches
nothing". It costs the prompt its parallel lane in the planner; it never buys it
a weaker check.

## 3. Handoffs

Every prompt gets exactly one handoff, at
`<data_root>/LLM/handoffs/<alias>/<same-filename>.md`, written through:

```bash
python3 scripts/agent_task.py checkpoint --repo-root <path> \
  --prompt <prompt.md> --status in_progress|complete|partial|blocked|failed
```

Its machine-owned header pins the prompt's canonical path and SHA-256, the
status, the runner's execution timing, and a checkpoint timestamp per
checkpoint. Everything below the `<!-- HANDOFF_BODY -->` marker is the agent's
own account and is preserved across re-checkpoints.

**Always pass `--prompt`.** Without it the default target is the *newest* prompt
in the folder, which is the wrong handoff whenever more than one is outstanding.

**The recorded `prompt_path` never changes when the prompt moves.** A handoff
written before the move into `done/` keeps identifying the prompt the same way
afterwards.

### A blocked handoff must say what the block costs

```yaml
status: blocked
block:
  severity: local | dependent | catastrophic
  reason: short_identifier
  summary: one human sentence
  can_continue_unrelated: true
  blocks_prompts:
    - 20260825_04_Some-Prompt
  blocks_repositories:
    - web
```

`severity` is the only field the runner branches on:

- **`local`** — nothing else is known to depend on this. Defer this prompt's
  dependants (computed, not trusted) and carry on.
- **`dependent`** — the same, plus further casualties the agent has named.
- **`catastrophic`** — *ultima ratio*. The workspace itself cannot be trusted, so
  the whole run stops. A missing fixture is not this. A half-written
  cross-repository contract is.

The agent's list can only **widen** the deterministic dependency closure the
resolver computes. It can never narrow it: an agent that forgets a dependant must
not be able to let the runner start work on it.

## 4. Finishing a prompt

In order, as the task's own last steps:

1. commit the work — **by pathspec**, never `git add -A`, when anything else in
   the tree was already dirty;
2. write the handoff with a truthful terminal status;
3. **move the prompt file into `done/`** (or into `blocked/`, with a `block:`
   statement, if it genuinely cannot finish);
4. leave every repository you changed on `main`, clean, with your commits
   reachable from `main`.

**Moving the file is what marks a prompt complete.** A prompt still in the queue
folder is still outstanding, whatever its handoff says. A prompt still in the
queue whose handoff already claims `complete` for that exact file is reported as
an inconsistency and refused — it is never silently re-run.

## 5. The branch rule

A prompt is not finished until every repository it mutates has its commits
reachable from `main` (or `$AUTOKIT_TARGET_BRANCH`).

- **Before** a prompt starts, the runner puts its declared mutation targets on
  the target branch when it can do so without leaving work behind — and refuses,
  naming the repository, the branch and the commits, when it cannot.
- **After** the agent stops, the gate is computed from **refs**, not timestamps:
  anything reachable from the new refs but not from the target branch is work
  this attempt stranded.
- On failure, one bounded reconciliation session is offered. It may fast-forward
  or merge. It may not reset, rebase, force-delete a branch, drop a stash, or
  push.

Nothing in this toolkit ever resets, rebases, force-deletes, stashes or pushes.
When reconciliation is ambiguous it reports and refuses: a wrong guess about
somebody's history is unrecoverable, and a refusal is not.

## 6. Context rollover

Under `run-sequence.sh` with Claude, each prompt gets a **fresh session**. If
Claude Code is about to auto-compact:

```text
PreCompact hook fires → marker written, compaction BLOCKED
  → the attempt is stopped, the work is checkpoint-committed
  → a fresh session resumes the SAME prompt from the prompt file,
    the in_progress handoff and that commit
```

Up to `--max-context-rollovers` times (default 5); exceeding it stops the run
rather than spawning sessions forever. Nothing estimates remaining context,
watches a percentage or parses a warning string — the hook is the only signal.

## 7. Dirty worktrees

The default is refusal. `--allow-dirty` records a baseline of exactly which paths
were already dirty, instructs the agent to preserve them, and fails the prompt if
any of that work is committed, deleted or reverted.

It **cannot** separate two edits to the same hunk. If prompt work lands inside a
hunk that was already dirty, the run stops rather than guess. That is a recorded
risk, not isolation.

The trap worth naming: `git commit` with no pathspec commits the **whole index**,
so a pre-existing staged change is swept in even when you only added your own
file. Commit by pathspec.

## 8. Execution time

`execution_seconds` is **supervised agent wall clock**: the sum of attempt
durations for one prompt, measured by the runner at process boundaries. It is not
CPU time, not tokens, not billed time. Gaps between attempts and between
invocations are excluded — nothing is running in them.

The runner measures; the handoff records. Attempt records under the run directory
are the ledger, and the handoff field is a projection of them, so writing it twice
produces the same number as writing it once. A handoff written before timing
existed reports `unknown`, which is counted and listed but never added to a total
as zero.

## 9. What the operator sees

```bash
run-sequence.sh --extract-sequence          what could run, in a safe order
run-sequence.sh --extract-sequence --format shell    just the commands
run-sequence.sh --history                   what every prompt actually cost
run-sequence.sh --repos                     the configured repositories
```

Both reports are read-only and refuse to share a command line with anything that
executes, so "did this command change anything" is answered by reading the flags.

The planner does not decide selection itself — it calls the same resolver the
runner calls, telling it to *assume* the steps already emitted are in `done/`. A
planner that disagreed with the runner about what runs next would be worse than
no planner: a confident wrong answer.

`--queue` narrows what the report **counts and prints** — the state summary, the
cycles, the unreadable-frontmatter list — to the queues asked about plus whatever
they transitively require. It never narrows the dependency graph: the commonest
reason a queue will not advance is a prompt in another one, and the report has to
be able to name it. So a one-repository report counts that repository's prompts
and no one else's, while still explaining a deferral with *"requires
`<other>/<prompt>`, which is not in `done/`"*.

An empty queue and a queue nothing in which can start are reported as the
different things they are: *"every queue inspected is empty"* versus *"no queued
prompt is currently schedulable"*.
