#!/usr/bin/env bash
# run-sequence.sh — work prompt queues unattended.
#
#   ./run-sequence.sh --help
#
# THE SYNTAX LIVES IN print_usage() BELOW AND NOWHERE ELSE. This comment used to
# carry its own copy of the grammar and duly went stale; what follows now is only
# the reasoning a reader of the code needs, which `--help` deliberately does not
# repeat.
#
# THE DEFAULT IS A DRAIN OF EVERY QUEUE. `./run-sequence.sh` with no arguments
# discovers every repository in workspace.json that has a prompt queue
# and works all of them. One repository's queue is `--queue <repo>`, which is
# what a bare repository alias used to mean and still does, as a compatibility
# alias. `--drain` is an explicit spelling of the default and shares its code
# path exactly — there is one drain implementation in this file.
#
# ============================================================================
# CONTEXT SAFETY — what changed, and why it is the default
# ============================================================================
#
# The old bare mode handed a repo's WHOLE queue to ONE Claude session. Context
# accumulated across prompt 1, 2, 3, 4..., and somewhere in a long unattended
# run Claude Code would auto-compact — lossily replacing the conversation with
# a summary and carrying on. A real queue ran roughly 4h30m that way and
# the work came back with a cluster of mistakes whose common shape was a
# degraded memory of requirements and earlier decisions.
#
# So the default is now the opposite:
#
#   prompt 1  -> fresh claude #1 -> complete -> handoff + commit + done/
#   prompt 2  -> fresh claude #2 -> complete
#   prompt 3  -> fresh claude #3 -> Claude Code asks to auto-compact
#                                     |
#                                     PreCompact hook fires
#                                     marker written, compaction BLOCKED
#                                     attempt stopped, work checkpoint-committed
#                                     |
#                 fresh claude #4 -> SAME prompt 3, resumed from prompt +
#                                    in_progress handoff + checkpoint commit
#                                 -> complete
#   prompt 4  -> fresh claude #5 -> ...
#
# ONE FRESH SESSION PER PROMPT IS THE PRIMARY PROTECTION. Rollover is the
# backstop for a single prompt too big for one window; a ten-prompt queue no
# longer accumulates ten prompts of history whether or not compaction was ever
# going to fire. State crosses prompts through the repository, its Git history
# and the canonical handoff — never through conversation memory.
#
# THE SIGNAL IS `PreCompact`, AND NOTHING ELSE. Claude Code fires that hook
# before an automatic (and a manual `/compact`) compaction, and a command hook
# that answers {"decision":"block"} stops it. This script registers such a
# hook, per session, through `claude --settings`. Nothing here estimates
# remaining context, watches a percentage, parses a spinner, or reads an
# English warning string: the queue stays safe even when Claude has no useful
# introspection into its own context usage. Do not add a threshold back — the
# repository CLAUDE.md files had one at 95% and it was removed on purpose.
#
# ============================================================================
# THE UI IS THE REAL ONE — and `Stop` is what buys that
# ============================================================================
#
# `./run-sequence.sh api` shows the NATIVE Claude Code interactive TUI: the same
# tool cards, diffs, spinners, syntax highlighting, status line, colours and
# scrolling as typing `claude` yourself. Claude's output is not piped, parsed,
# filtered, tee'd or re-rendered by anything here, and the default TTY path uses
# no `-p`, no `--output-format stream-json` and no scripts/stream_progress.py.
#
# That used to be impossible to combine with an unattended queue, for one
# reason: THE INTERACTIVE CLI DOES NOT EXIT AT THE END OF A TURN. It still
# doesn't. It gained no automatic-exit feature and no flag for one. What changed
# is that the runner no longer needs it to:
#
#   Claude finishes its turn
#     -> Stop hook fires (registered per attempt, alongside PreCompact)
#     -> a runner-owned turn-stopped marker is written; the stop is ALLOWED
#     -> the background watcher sees the marker and closes the now-idle TUI
#     -> run-sequence checks the FILESYSTEM for completion and moves on
#
# The Stop hook decides nothing about completion. It records that a turn ended.
# Whether the PROMPT is finished is still, and only, the question the filesystem
# answers: the prompt moved into done/, a matching handoff exists, the worktree
# is clean. A turn that ended with the prompt still in the queue folder is the
# STUCK condition and stops the run — Claude asking the operator a question, or
# stopping early, or forgetting the done/ move all land there, and none of them
# advance the queue or restart forever.
#
# WHEN THERE IS NO TERMINAL — CI, a redirected stdout, an automation harness —
# there is nothing to render into, so the run falls back to the headless
# `-p` + stream_progress.py path and says so in one line. Context safety is
# identical in both: same PreCompact hook, same block, same checkpoint, same
# fresh-session rollover.
#
# WHERE THE STATE LIVES: <data_root>/.run-sequence/<run-id>/ — a rollover
# record per attempt, a state.json for the run, and the ephemeral settings
# files, which are deleted again when the run ends. Never inside a source
# repository, never in the prompt or handoff folders.
#
# WHICH MODES ARE CONTEXT-SAFE
#   claude, bare repo queue        context-safe (the default), native TUI
#   claude, explicit .md list      context-safe, native TUI
#   claude, --drain                context-safe (the same path as the default)
#   claude, no TTY anywhere        context-safe, headless streamed progress
#   claude, --single-session       NOT context-safe: one context for the whole
#                                  queue, native TUI, no rollover
#   claude, "verbatim message"     NOT context-safe: interactive, unchanged
#   codex, every mode              unchanged; no Claude hooks are ever injected
#
# ============================================================================
#
# --SINGLE-SESSION MODE hands the repo's whole queue to a single interactive
# session: it builds an instruction naming the repo's real prompt/handoff
# folders and telling the agent to work every prompt still in the queue folder,
# in ascending order, committing after each, moving each finished one into
# done/, and stopping only when blocked. One session means the TUI's formatting
# survives the whole run and context carries from one prompt to the next.
#
# That last part is exactly what makes it unsafe for a long queue. It is kept
# because somebody may still want ONE conversation carrying two or three short
# related prompts, and because `--agent codex` has no PreCompact lifecycle to
# hook. It is no longer kept for its rendering: the default has the same native
# TUI, so the only thing --single-session still buys is shared context, which is
# the very thing that makes it unsafe.
#
# What single-session gives up: prompt selection is the agent's reading of the
# folder rather than resolve_next_prompt.py's. Under the done/ model that costs
# far less than it used to. The old message had to explain, in prose, how to
# cross-reference every prompt against a handoff's status field and how to
# order the result -- inference on inference, and inferring order from
# timestamps and numbering is exactly how one queue's prompts 22-24 became unreachable
# once 25 existed (WORKFLOW.md). Now the folder itself carries the answer:
# everything in it is outstanding, oldest first, and an agent cannot misjudge
# "what is done" because nothing done is in front of it.
#
# THE CONTEXT-SAFE LOOP keeps the deterministic path --drain always had:
# resolve_next_prompt.py picks each next prompt -- oldest still in the queue
# folder, prerequisite chain checked recursively against done/ -- and
# run-agent.sh runs it by EXPLICIT filename, one agent process per prompt. The
# loop regains control through the Stop hook and the watcher, not by making the
# agent print instead of render.
#
# Each iteration re-resolves from scratch rather than just incrementing, so a
# prerequisite that only became satisfied mid-run is picked up immediately, and
# a prompt that ran but didn't actually complete is caught (see "stuck"
# handling below) instead of silently repeating forever.
#
# EXPLICIT-LIST MODE (a repo name followed by one or more .md filenames)
# runs exactly that list, in that order, via run-agent.sh's own explicit
# selection -- for when you want to hand-pick the exact sequence rather
# than trust automatic resolution (e.g. you know some are safe to run out
# of their written order). One repo per invocation in this mode.
#
# The context-safe loop and explicit-list mode used to force run-agent.sh's
# `claude` branch into -p/--print, purely so the process would exit when a
# prompt finished. That is what flattened the operator's screen into
# `[00:03] · Bash …` progress lines, and it is gone from the TTY path: the
# attempt runs as the terminal's foreground job in the native TUI, and the
# watcher closes it on the Stop marker. On the Claude path AGENT_PRINT=1 is now
# set in exactly one place, supervise_attempt_headless, and only when there is
# no terminal. (run_prompt_plain still sets it for codex, where run-agent.sh's
# codex branch has never read it — left alone rather than "tidied", because
# codex behaviour is out of scope here.)
#
# The run stops on `blocked`, on a resolver `error`, on a prompt that ran but
# was not moved into done/ afterward (stuck), on a nonzero agent exit that was
# not a context rollover, on a dirty worktree where a prompt was about to
# start, on an observed compaction boundary, on Ctrl-C, and on exceeding
# --max-context-rollovers -- printed loudly, the run halts rather than moving
# to the next repo, since these usually need a human decision before anything
# downstream can safely proceed. `idle` (queue genuinely empty) is quiet and
# moves on.
#
# CTRL-C keeps its native Claude meaning first: SIGINT reaches the whole
# foreground process group, so the running turn is interrupted the way it is in
# a hand-typed session. This script's INT trap is deferred by bash until the
# attempt returns and then runs before anything else, so the sequence stops
# cleanly at 130 and the interrupt is never re-read as a rollover, a Stop or a
# failure worth retrying. Nothing here ever runs `pkill claude`: only the one
# pid this run started is ever signalled.

set -euo pipefail

# ============================================================================
# INTERNAL HOOK MODES — these run BEFORE any normal CLI parsing
# ============================================================================
# Claude Code invokes this script back as a PreCompact/PostCompact/Stop command
# hook, with the event JSON on stdin. Keeping it in this one production script
# (rather than generating a throwaway hook script) is deliberate: the marker
# format and the block decision are the same contract the supervisor below
# reads, and a contract with one implementation cannot drift.
#
# The hook's whole job is: RECORD SIGNAL, DECIDE NOTHING ELSE, RETURN. It runs
# no tests, summarises nothing, touches no source, moves no prompt, decides
# nothing about completion, kills no parent, and sources nothing out of its own
# input. Git mutation and the completion decision belong to the supervisor,
# after the attempt has stopped.
#
# PreCompact additionally answers {"decision":"block"}. Stop NEVER does: it
# records that the interactive turn ended and ALLOWS the stop. Blocking a Stop
# to keep a turn alive is what `stop_hook_active` and CLAUDE_CODE_STOP_HOOK_BLOCK_CAP
# exist to cap, and the queue has no reason to want it — whether the PROMPT is
# finished is a filesystem question (done/ + handoff + clean tree), never a
# question about this turn.
if [[ "${1:-}" == "--internal-precompact-hook" || "${1:-}" == "--internal-postcompact-hook" \
   || "${1:-}" == "--internal-stop-hook" ]]; then
  hook_kind="$1"
  hook_marker="${2:-}"
  hook_payload=$(cat 2>/dev/null || true)
  hook_write_status=0

  if [[ -n "$hook_marker" ]]; then
    set +e
    HOOK_MARKER="$hook_marker" HOOK_PAYLOAD="$hook_payload" HOOK_KIND="$hook_kind" python3 - <<'PY'
import datetime, json, os, pathlib, tempfile

marker = pathlib.Path(os.environ["HOOK_MARKER"])
raw = os.environ.get("HOOK_PAYLOAD", "")
kind = os.environ.get("HOOK_KIND", "")
try:
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        data = {}
except (ValueError, TypeError):
    data = {}

trigger = data.get("trigger") or "unknown"
if kind == "--internal-postcompact-hook":
    schema, reason = "run-sequence.compact-breach/1", "%s_postcompact" % trigger
elif kind == "--internal-stop-hook":
    schema, reason = "run-sequence.turn-stopped/1", "agent_stop"
else:
    schema, reason = "run-sequence.rollover/1", "%s_precompact" % trigger

record = {
    "schema": schema,
    "reason": reason,
    "trigger": trigger,
    "hook_event_name": data.get("hook_event_name") or "",
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "session_id": data.get("session_id") or "",
    "transcript_path": data.get("transcript_path") or "",
    "cwd": data.get("cwd") or "",
}
if kind == "--internal-stop-hook":
    # The Stop payload also carries `last_assistant_message`. That is transcript
    # content under another name and is deliberately NOT copied here: the marker
    # is a runner-owned signal, and the next session must not re-import the
    # conversation this rollover model exists to reset. `stop_hook_active` is a
    # bool about the hook, not about the conversation, so it stays.
    record["stop_hook_active"] = bool(data.get("stop_hook_active"))
    record.pop("trigger", None)

# Atomic. A half-written marker caught by the supervisor's 200 ms poll would be
# a rollover with no session id, or a JSON parse error in the middle of a run.
# The transcript PATH is recorded for emergency forensics; the transcript
# itself is never copied here — the next agent must not re-ingest it.
marker.parent.mkdir(parents=True, exist_ok=True)
fd, staged = tempfile.mkstemp(dir=str(marker.parent), prefix=".rollover-", suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2, sort_keys=True)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
os.replace(staged, marker)
PY
    hook_write_status=$?
    set -e
    if [[ $hook_write_status -ne 0 ]]; then
      # Last-ditch shell fallback. A marker the supervisor can see matters more
      # than a complete one: without one, the block below leaves a session
      # running on uncompacted until it dies of context — loud, but wasteful.
      # The schema must still match the EVENT, or the supervisor would read a
      # finished turn as a context rollover and checkpoint-commit for nothing.
      hook_fallback_schema="run-sequence.rollover/1"
      hook_fallback_reason="precompact"
      case "$hook_kind" in
        --internal-postcompact-hook)
          hook_fallback_schema="run-sequence.compact-breach/1"; hook_fallback_reason="postcompact" ;;
        --internal-stop-hook)
          hook_fallback_schema="run-sequence.turn-stopped/1"; hook_fallback_reason="agent_stop" ;;
      esac
      {
        printf '{"schema":"%s","reason":"%s",' "$hook_fallback_schema" "$hook_fallback_reason"
        printf '"trigger":"unknown","timestamp":"%s",' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '"session_id":"","transcript_path":"","cwd":"","marker_write_degraded":true}\n'
      } >"$hook_marker.partial" 2>/dev/null \
        && mv "$hook_marker.partial" "$hook_marker" 2>/dev/null || true
    fi
  fi

  if [[ "$hook_kind" == "--internal-precompact-hook" ]]; then
    # {"decision":"block"} on stdout is what claude 2.1.x reads back from a
    # command hook (a bare exit 2 is the equivalent; JSON carries a reason, so
    # it is preferred). A blocked hook's output is deliberately NOT replayed to
    # the model as extra instructions, which is what makes this a control
    # signal rather than a prompt.
    if [[ $hook_write_status -ne 0 ]]; then
      printf '%s\n' '{"decision":"block","reason":"run-sequence context rollover requested (marker write degraded)"}'
    else
      printf '%s\n' '{"decision":"block","reason":"run-sequence context rollover requested"}'
    fi
  fi
  exit 0
fi

# ============================================================================

# ----------------------------------------------------------------------------
# Terminal state and watcher shutdown — defined HERE, above the traps
# ----------------------------------------------------------------------------
# Both traps below can fire on an early `exit` — an unknown flag, a refused
# combination of flags — which happens long before the supervision section is
# parsed. A trap that calls a function defined further down the file dies with
# "command not found" and replaces the script's real exit status with 127.
# These three have no dependencies of their own, so they live up here.
#
# Claude Code cleans up after itself on SIGTERM — observed on 2.1.239 emitting
# `?1049l` (leave the alternate screen), `?25h` (show the cursor), mouse
# tracking off, bracketed paste off and a scroll-region reset, then exiting 143.
# So the normal Stop and rollover paths already hand back a healthy terminal.
# This is the belt for the paths where it never gets the chance: the SIGKILL
# escalation, a crash, a rollover that had to be forced. A failed attempt must
# not leave the operator with a hidden cursor and no echo.
saved_tty_state=""
# Only an attempt can wreck a terminal, so only an attempt earns the repair.
# Without this, `--help` and every refused flag combination emitted a screen
# reset on their way out through the EXIT trap.
terminal_touched=false

save_terminal_state() {
  saved_tty_state=""
  terminal_touched=true
  if [[ -t 0 ]]; then
    saved_tty_state=$(stty -g 2>/dev/null || true)
  fi
}

restore_terminal_state() {
  # Only ever on a real terminal, so a redirected or piped run never picks up a
  # stray escape sequence in its log.
  if [[ "$terminal_touched" == true && -t 1 ]]; then
    printf '\033[?1049l\033[?25h\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?2004l\033[m'
  fi
  if [[ -n "$saved_tty_state" && -t 0 ]]; then
    stty "$saved_tty_state" 2>/dev/null || true
  fi
  saved_tty_state=""
}

stop_attempt_watcher() {
  local pid=$1
  [[ -n "$pid" ]] || return 0
  kill -TERM "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  return 0
}

# The pid and process group of the attempt currently being supervised, so an
# interrupt stops the agent this script started instead of orphaning it. The
# headless path fills the first two; the TTY path fills the pidfile, because
# there the pid is published by the attempt itself rather than by `$!`.
current_attempt_pid=""
current_attempt_pgid=""
current_attempt_pidfile=""
current_watcher_pid=""

# Ctrl-C during a long unattended run should say so plainly, take the running
# attempt down with it, and stop the whole run, rather than letting the loop
# advance and report a confusing downstream symptom.
#
# ON THE TTY PATH THIS TRAP IS DEFERRED, and that is deliberate rather than a
# limitation. SIGINT reaches the whole foreground process group, so Claude gets
# it directly and interrupts its turn exactly as it does in a hand-typed
# session — the first Ctrl-C does not tear the queue down mid-thought. bash
# holds this handler until the foreground attempt returns and then runs it
# BEFORE the next command, so by the time any rollover, Stop or failure logic
# could look at the attempt, the run has already stopped at 130. An interrupt is
# therefore never mistaken for one of those (observed; see the handoff).
on_interrupt() {
  echo
  echo "run-sequence.sh: interrupted — stopping."
  stop_attempt_watcher "$current_watcher_pid"
  current_watcher_pid=""
  if [[ -n "$current_attempt_pid" ]]; then
    terminate_attempt "$current_attempt_pgid" "$current_attempt_pid" "interrupt"
  elif [[ -n "$current_attempt_pidfile" && -s "$current_attempt_pidfile" ]]; then
    local tty_pid
    tty_pid=$(tr -dc '0-9' <"$current_attempt_pidfile" 2>/dev/null || true)
    if attempt_alive "$tty_pid"; then
      echo "  stopping attempt pid $tty_pid (interrupt): SIGTERM"
      terminate_pid_silently "$tty_pid"
    fi
  fi
  restore_terminal_state
  exit 130
}
trap on_interrupt INT

run_settings_dir=""
cleanup_run_state() {
  # The per-attempt settings files are ephemeral and must not outlive the run.
  # The breadcrumbs beside them deliberately do.
  stop_attempt_watcher "$current_watcher_pid"
  current_watcher_pid=""
  restore_terminal_state
  if [[ -n "$run_settings_dir" && -d "$run_settings_dir" ]]; then
    rm -rf "$run_settings_dir"
  fi
}
trap cleanup_run_state EXIT

# ============================================================================
# THE ONE HELP FUNCTION
# ============================================================================
# Everything that prints usage — `--help`, an unknown flag, a refused
# combination — goes through this. There is no second copy of the syntax in a
# header comment, in the README, or in an error branch: the AUT-04 prompt asked
# for one usage function per script precisely because three copies of a grammar
# are three chances to document a flag that no longer exists.
print_usage() {
  cat <<'HELPTEXT'
run-sequence.sh — work prompt queues unattended, one FRESH agent session per
prompt, with automatic pre-compaction rollover and a main-branch completion gate.

USAGE
  run-sequence.sh [options]                     DEFAULT: drain every eligible queue
  run-sequence.sh [options] --queue <repo>      one repository's queue
  run-sequence.sh [options] --queue <repo> <prompt.md>...   an explicit ordered list
  run-sequence.sh --help
  run-sequence.sh --repos

MODES
  run-sequence.sh
      THE DEFAULT, and the normal unattended invocation. Discovers every
      repository in workspace.json that has a prompt queue, resolves
      dependencies, and runs everything eligible in a safe order. A prompt that
      blocks defers only the prompts that actually depend on it; unrelated work
      keeps going. The whole run stops only for a catastrophic block.

  run-sequence.sh --drain
      An explicit spelling of the default. Same flag, same code path — kept so
      existing habits and scripts keep working.

  run-sequence.sh --queue api
      THE OLD DEFAULT, now explicit: process only that repository's eligible
      queue and leave every other repository alone. Repeatable (--queue api
      --queue web), and the bare positional form `run-sequence.sh api` is a
      compatibility alias for exactly this. A bare repository alias never means
      "drain everything".

  run-sequence.sh --queue api prompt1.md prompt2.md
      Run exactly that list, in that order, skipping automatic resolution — for
      when you know a sequence is safe out of its written order. One repository
      per invocation. `run-sequence.sh api prompt1.md prompt2.md` still works.

  run-sequence.sh --single-session --queue api
      THE OLD one-context mode: the repository's whole queue handed to a single
      agent session. NOT context-safe — context accumulates across every prompt,
      which is the accumulation the default exists to avoid. It requires an
      explicit --queue: there is no single-session drain of the whole workspace.

  run-sequence.sh --queue api "execute 29, 30, 31, 32"
      Your own message, passed to one interactive session unchanged. Also not
      context-safe.

  run-sequence.sh --extract-sequence
      READ-ONLY. Inspect every queue, handoff, dependency and mutation
      declaration, then print the commands that would work through them in a
      safe order. Starts no agent, moves no prompt, writes no handoff, commits
      nothing and changes no queue state. Narrow it with --queue, and get just
      the shell with --format shell.

  run-sequence.sh --history
      READ-ONLY. What every prompt actually cost: per-prompt execution time,
      attempts, and the totals. Execution time is supervised agent WALL CLOCK —
      not CPU time, not tokens, not billed time — and the total is a SUM, so two
      prompts run in parallel each contribute their full duration.

OPTIONS
  --queue <repo>              add one repository's queue (repeatable)
  --drain                     explicit alias for the default drain
  --agent claude|codex        which agent to drive (default: claude)
  --max N                     stop after N prompts per repository
  --max-context-rollovers N   context rollovers allowed per prompt (default 5);
                              exceeding it stops the run rather than spawning
                              sessions forever
  --single-session            one agent context for a whole queue (see above)
  --allow-dirty               start even when a relevant repository already has
                              uncommitted work. The DEFAULT IS STILL TO REFUSE;
                              this records a baseline of what was already dirty,
                              tells the agent to leave it alone, and fails the
                              prompt if any of it is committed, deleted or
                              edited. It CANNOT separate overlapping edits to the
                              same hunk — that case stops the run. It relaxes
                              nothing else: dependencies, the branch gate, the
                              rollover limit and every other check are unchanged.
  --accept-dirty              a compatibility alias for --allow-dirty
  --extract-sequence          READ-ONLY dependency-aware planner (see MODES)
  --history                   READ-ONLY execution-time report (see MODES)
  --format human|shell|json   output format for --extract-sequence and --history.
                              Default human. `shell` is extraction-only and prints
                              nothing but executable commands; `json` is stable
                              structured data.
  --dry-run                   print the plan — repositories, queues, next prompt,
                              worktree state — and run nothing
  --repos                     list the configured repository aliases and their
                              paths, then exit. No side effects.
  -h, --help                  this text, on stdout, exit 0, no side effects

REPOSITORY ALIASES (case-insensitive)
  Every alias comes from this toolkit's workspace.json, and there is no second
  table anywhere in this script. `run-sequence.sh --repos` lists the ones this
  workspace configures, with their paths.

WHAT THE DEFAULT ACTUALLY DOES, PROMPT BY PROMPT
  * ONE FRESH agent session per prompt. State crosses prompts through the
    repository, its Git history and the canonical handoff — never through
    conversation memory.
  * The NATIVE Claude Code TUI whenever there is a terminal on stdin and stdout.
    With no terminal (CI, a pipe, a log file) it falls back to streamed progress
    and says so on the first line. Context safety is identical either way.
  * CONTEXT ROLLOVER before compaction: Claude Code's PreCompact hook is
    intercepted and BLOCKED, the work is checkpoint-committed, and a fresh
    session resumes the SAME prompt from the prompt, the in_progress handoff and
    that commit. Up to --max-context-rollovers times.
  * THE MAIN-BRANCH GATE. A prompt starts only when its declared mutation
    targets are on `main` and, when they are not, only after this script has
    moved them there without leaving any work behind. It ends only when every
    repository the prompt actually changed is back on `main`, clean, with its
    commits reachable from `main`. Work found on a feature branch buys ONE
    bounded remediation session; nothing here ever resets, rebases,
    force-deletes a branch, or pushes.
  * COMPLETION IS A FILESYSTEM FACT: the prompt file moved into done/, a handoff
    pinned to that exact prompt, a clean worktree. Nothing else counts.

WHEN A PROMPT BLOCKS
  It stays blocked — nothing converts a block into a completion. What follows it
  is decided in two layers:
    1. the DETERMINISTIC floor — every queued prompt that transitively requires
       the blocked one is DEFERRED, in every repository, whether or not the
       handoff remembered to say so;
    2. the agent's own impact statement in the blocked handoff (`block:` with a
       severity, a reason and optional blocks_prompts / blocks_repositories),
       which can only WIDEN that set.
  DEFERRED means "not in THIS run". No file is moved, no handoff is invented,
  nothing is marked complete, and the next run picks the prompt up normally.
  Unrelated prompts keep running. Only `severity: catastrophic` stops everything.

  The run summary distinguishes COMPLETE, BLOCKED, DEFERRED and NOT REACHED.

STATE, AND STOPPING
  Runtime state:  <data_root>/.run-sequence/<run-id>/ — a rollover record per
                  attempt, a state.json for the run, and ephemeral settings files
                  that are deleted when the run ends. Never inside a repository.
  Ctrl-C:         reaches the running turn first, exactly as in a hand-typed
                  session, and then stops the whole run at 130. Nothing here ever
                  runs `pkill claude`; only the pid this run started is signalled.

ENVIRONMENT
  AUTOKIT_WORKSPACE_CONFIG   the workspace.json to use, when it is not the one at
                         this toolkit's root. The ONE path override there is;
                         README.md states the precedence rule.
  AUTOKIT_TARGET_BRANCH      the branch completion requires (default: main)
  AUTOKIT_BRANCH_POLICY=off  skip the branch preflight and completion gate entirely

EXAMPLES
  run-sequence.sh                      drain every eligible queue (the default)
  run-sequence.sh --drain --max 5      the same, at most 5 prompts per repository
  run-sequence.sh --queue api          only that repository's queue
  run-sequence.sh --queue api --queue web      api's queue, then web's
  run-sequence.sh api                  compatibility alias for --queue api
  run-sequence.sh --dry-run            what a drain would do, without doing it
  run-sequence.sh --queue api 20260803_29_Some-Prompt.md
  run-sequence.sh --agent codex --queue web
  run-sequence.sh --allow-dirty --queue api prompt.md
  run-sequence.sh --extract-sequence
  run-sequence.sh --extract-sequence --queue api --format shell
  run-sequence.sh --history
  run-sequence.sh --history --queue api

READ-ONLY MODES, AND WHAT THEY PROMISE
  --extract-sequence and --history mutate NOTHING: no agent, no prompt moved, no
  handoff written, no commit, no queue state. Both refuse to combine with each
  other or with anything that executes.
  A parallel lane is printed only for prompts proven independent — no dependency
  either way, disjoint DECLARED mutation targets, different prompt/handoff
  folders. An undeclared mutation scope is treated as "could touch anything".
  A handoff written before execution timing existed reports `unknown`, which is
  counted and listed but never added to a total as zero.

SEE ALSO
  run-agent.sh --help    drive a single prompt for a single repository
HELPTEXT
}

usage() {
  print_usage >&2
  exit 2
}

agent="claude"
max_tasks=""
max_context_rollovers=5
positional=()
queue_targets=()

drain=false
dry_run=false
single_session=false

# `--allow-dirty` is the canonical spelling; `--accept-dirty` is the alias the
# operator asked for and behaves identically. One variable, so no code path can
# ever see the two spellings differently.
allow_dirty=false
extract_sequence=false
history_mode=false
output_format=""

# TARGET BRANCH = main, frozen by HITL on 20260823. Overridable for an unusual
# checkout; never defaulted away from. `off` disables the preflight and the
# completion gate together — they are one policy and half of it is worse than
# neither, because a preflight with no gate promises a guarantee it cannot keep.
target_branch="${AUT_TARGET_BRANCH:-main}"
branch_policy="${AUT_BRANCH_POLICY:-on}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) print_usage; exit 0 ;;
    # WHAT THIS WORKSPACE IS CONFIGURED WITH, and nothing else: no queue is discovered, no prompt
    # resolved, no run state written, no agent started. The source toolkit's `--version` reported
    # each product component's build; that is a product operation and left with the rest of them.
    # `readlink -f` because this may be invoked through a symlink, and `dirname` on a symlink gives
    # the LINK's directory rather than this repository's.
    --repos)
      exec python3 "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/scripts/workspace_config.py" --print aliases ;;
    --agent) [[ $# -ge 2 ]] || usage; agent=$2; shift 2 ;;
    --max) [[ $# -ge 2 ]] || usage; max_tasks=$2; shift 2 ;;
    --max-context-rollovers) [[ $# -ge 2 ]] || usage; max_context_rollovers=$2; shift 2 ;;
    --queue) [[ $# -ge 2 ]] || usage; queue_targets+=("$2"); shift 2 ;;
    --drain) drain=true; shift ;;
    --single-session) single_session=true; shift ;;
    --allow-dirty|--accept-dirty) allow_dirty=true; shift ;;
    --extract-sequence) extract_sequence=true; shift ;;
    --history) history_mode=true; shift ;;
    --format) [[ $# -ge 2 ]] || usage; output_format=$2; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    -*) echo "unknown flag: $1" >&2; echo "" >&2; usage ;;
    *) positional+=("$1"); shift ;;
  esac
done

[[ "$max_context_rollovers" =~ ^[0-9]+$ ]] || {
  echo "error: --max-context-rollovers takes a non-negative integer, got '$max_context_rollovers'" >&2
  exit 2
}

if [[ "$single_session" == true && "$drain" == true ]]; then
  echo "error: --single-session and --drain are opposites — one context for the" >&2
  echo "       whole queue versus one agent process per prompt. Pick one." >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# THE READ-ONLY MODES, AND WHAT THEY REFUSE TO SHARE A COMMAND LINE WITH
# ---------------------------------------------------------------------------
# `--extract-sequence` and `--history` inspect and print. The rule that keeps
# that promise cheap to verify is that they cannot be combined with anything
# that executes, and cannot be combined with each other: a single invocation is
# either entirely read-only or entirely not, so "did this command change
# anything" is answered by reading the flags rather than the code.
if [[ "$extract_sequence" == true && "$history_mode" == true ]]; then
  echo "error: --extract-sequence and --history are two different reports. Ask for one." >&2
  exit 2
fi
if [[ "$extract_sequence" == true || "$history_mode" == true ]]; then
  if [[ "$single_session" == true ]]; then
    echo "error: --single-session runs agents; --extract-sequence and --history run none." >&2
    exit 2
  fi
fi
if [[ -n "$output_format" ]]; then
  case "$output_format" in
    human|shell|json) ;;
    *) echo "error: --format takes human, shell or json, got '$output_format'" >&2; exit 2 ;;
  esac
  if [[ "$extract_sequence" == false && "$history_mode" == false ]]; then
    echo "error: --format only means something for --extract-sequence or --history." >&2
    exit 2
  fi
  if [[ "$history_mode" == true && "$output_format" == "shell" ]]; then
    echo "error: --format shell prints commands to RUN a plan, so it belongs to" >&2
    echo "       --extract-sequence. --history has no commands to print; use human or json." >&2
    exit 2
  fi
fi
[[ -n "$output_format" ]] || output_format=human

# tools_dir is where this script (and its scripts/ subfolder) actually live: the
# toolkit checkout. Repository paths are NOT derived from it — every one of them
# comes from workspace.json, so a repository may sit anywhere the operator put
# it. The source toolkit assumed `<parent of tools_dir>/<repo-directory>`, which
# is the assumption that made it serve exactly one product.
#
# `readlink -f` first, because this may be invoked through a symlink and
# `dirname` on a symlink gives the *link's* directory, not this file's.
# self_path is the same resolution, kept whole: it is what the generated
# PreCompact hook command has to name, and a hook that named the symlink would
# break the moment the symlink moved.
self_path=$(readlink -f "${BASH_SOURCE[0]}")
tools_dir=$(cd "$(dirname "$self_path")" && pwd)
run_agent="$tools_dir/run-agent.sh"
resolver="$tools_dir/scripts/resolve_next_prompt.py"
config_tool="$tools_dir/scripts/workspace_config.py"

command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found on PATH" >&2; exit 2; }

[[ -f "$resolver" ]] || { echo "missing resolver: $resolver" >&2; exit 2; }
[[ -f "$config_tool" ]] || { echo "missing workspace resolver: $config_tool" >&2; exit 2; }

# ============================================================================
# THE ONE ALIAS TABLE — read from workspace.json, never maintained here
# ============================================================================
# The source toolkit kept a hand-written `repo_dirs` map in this file, a second
# in run-agent.sh and a third in branch_policy.py. Three tables is three chances
# to know about a repository the workspace does not have, or to miss one it does.
#
# `repo_paths` is keyed by the LOWERCASED alias, because lookup is documented as
# case-insensitive; `repo_alias` gives back the configured spelling, which is
# what every message, every queue folder and every handoff folder uses. Reading a
# tab-separated stream is what keeps a path containing spaces intact.
declare -A repo_paths=()
declare -A repo_alias=()
declare -a configured_aliases=()
while IFS=$'\t' read -r _alias _path; do
  [[ -n "$_alias" ]] || continue
  repo_paths["${_alias,,}"]="$_path"
  repo_alias["${_alias,,}"]="$_alias"
  configured_aliases+=("$_alias")
done < <(python3 "$config_tool" --print aliases) || exit 2
if (( ${#configured_aliases[@]} == 0 )); then
  echo "error: workspace.json configures no repositories. Add one, or run ./init.sh." >&2
  exit 2
fi

data_root=$(python3 "$config_tool" --print data-root) || exit 2
export WORKSPACE_DATA_ROOT="$data_root"


# ============================================================================
# MODE RESOLUTION — one grammar, decided once, before anything can act on it
# ============================================================================
# Three spellings reach the same place:
#
#   --queue api            the explicit per-repository mode
#   api                    the compatibility positional for it
#   (nothing at all)       DRAIN: every configured repository that has a
#                          prompt queue
#
# A bare repository alias is deliberately NOT a drain-everything trigger. It has
# meant "this repository's queue" for as long as this script has existed, and
# quietly widening it to the whole workspace would be the most expensive possible
# way to be helpful.
#
# `--drain` sets no target of its own. With repositories it is the old
# per-repository drain; with none it is the default. Either way the loop below is
# the only drain implementation in this file — there is no second one for the
# bare form to call.

declare -a repo_targets=()
declare -a rest_tokens=()

for queue_token in "${queue_targets[@]}"; do
  [[ -n "${repo_paths[${queue_token,,}]:-}" ]] || {
    echo "unknown repository alias: $queue_token" >&2
    echo "  workspace.json configures: ${configured_aliases[*]}" >&2
    exit 2
  }
  repo_targets+=("${repo_alias[${queue_token,,}]}")
done

# Leading positional repository aliases, and only leading ones: the first token
# that is not an alias begins the .md list or the verbatim message.
positional_index=0
while (( positional_index < ${#positional[@]} )) \
  && [[ -n "${repo_paths[${positional[$positional_index],,}]:-}" ]]; do
  repo_targets+=("${repo_alias[${positional[$positional_index],,}]}")
  positional_index=$((positional_index + 1))
done
if (( positional_index < ${#positional[@]} )); then
  rest_tokens=("${positional[@]:$positional_index}")
fi

explicit_repo_targets=false
(( ${#repo_targets[@]} > 0 )) && explicit_repo_targets=true

# What the read-only reports below narrow themselves with. Built from the
# EXPLICITLY named repositories only: with none named, the reports do their own
# discovery over the canonical topology, which is the same set the drain would
# have found and is what "inspect everything" has to mean.
# The CONFIGURED SPELLING of the alias, not whatever case the operator typed.
# Lookup is case-insensitive everywhere, but the reports match a queue folder,
# and a folder is named by the configured spelling. Passing the raw token through
# would make `--extract-sequence --queue API` match nothing and silently plan an
# empty queue, which is the worst possible answer: it looks like "nothing to do".
declare -a queue_report_args=()
if [[ "$explicit_repo_targets" == true ]]; then
  for queue_token in "${repo_targets[@]}"; do
    queue_report_args+=("--queue" "${repo_alias[${queue_token,,}]}")
  done
fi

# A read-only report describes the queues; it does not take a prompt list or a
# verbatim message, both of which are instructions to an agent. Checked here
# because this is the first point at which a repository alias has been told
# apart from everything else on the command line.
if [[ "$extract_sequence" == true || "$history_mode" == true ]] \
  && (( ${#rest_tokens[@]} > 0 )); then
  echo "error: a read-only report takes no prompt list and no verbatim message." >&2
  echo "       '${rest_tokens[0]}' is neither a repository alias nor a flag." >&2
  echo "       Narrow the report with --queue <repo>." >&2
  exit 2
fi

if [[ "$explicit_repo_targets" == false && ${#rest_tokens[@]} -gt 0 ]]; then
  echo "error: '${rest_tokens[0]}' is not a repository alias, and a prompt list or a" >&2
  echo "       verbatim message has to say which repository it belongs to." >&2
  echo "       Did you mean: $0 --queue <repo> ${rest_tokens[*]}" >&2
  echo "" >&2
  usage
fi

# `mapfile` rather than a subshell loop appending to the array: a `while read`
# fed by a pipe runs in a subshell, and the array it filled would be gone.
drain_all=false
if [[ "$explicit_repo_targets" == false ]]; then
  drain_all=true
  # A repository joins the drain only if it can actually be worked: a checkout on
  # disk and a queue folder for its alias under the data root. Everything else is
  # skipped SILENTLY — a workspace whose optional repository has not been cloned
  # is normal, and a drain that refused to start over it would be useless.
  #
  # This is the same rule sequence_plan.discover_repositories applies, so the
  # planner never advertises a queue the drain would skip.
  for _alias in "${configured_aliases[@]}"; do
    _root="${repo_paths[${_alias,,}]}"
    [[ -d "$_root" ]] || continue
    [[ -d "$data_root/LLM/prompts/$_alias" ]] || continue
    repo_targets+=("$_alias")
  done
  if (( ${#repo_targets[@]} == 0 )); then
    echo "run-sequence.sh: no configured repository has a prompt queue" >&2
    echo "  configured: ${configured_aliases[*]}" >&2
    echo "  queues:     $data_root/LLM/prompts/" >&2
    exit 2
  fi
fi

for queue_token in "${repo_targets[@]}"; do
  [[ -n "${repo_paths[${queue_token,,}]:-}" ]] || {
    echo "unknown repository alias: $queue_token" >&2
    echo "  workspace.json configures: ${configured_aliases[*]}" >&2
    exit 2
  }
done

if [[ "$single_session" == true && "$drain_all" == true ]]; then
  echo "error: --single-session has no whole-workspace form. One context for one" >&2
  echo "       repository's queue is already the mode this script documents as" >&2
  echo "       unsafe for a long queue; one context for EVERY repository's queue" >&2
  echo "       has never been defined or tested. Name the queue explicitly:" >&2
  echo "         $0 --single-session --queue <repo>" >&2
  exit 2
fi

# From here down `positional` means the resolved repository list, which is what
# every reader of it below (the run-state record, the loops) actually wanted.
positional=("${repo_targets[@]}")

# ============================================================================
# THE READ-ONLY REPORTS — and they exit before anything in this file can write
# ============================================================================
# Placed HERE deliberately, and the position is the guarantee: everything above
# it resolves paths and reads configuration, and everything below it can start
# an agent, claim a run directory, move a prompt or commit. A reader checking
# "can --history write anything" has to read this far and no further.
#
# `--queue` narrows both reports; with none, both inspect everything the
# canonical topology exposes. Neither takes an agent, a rollover budget or a
# branch policy, because neither runs anything for those to apply to.
if [[ "$extract_sequence" == true ]]; then
  exec python3 "$tools_dir/scripts/sequence_plan.py" \
    --format "$output_format" "${queue_report_args[@]}"
fi
if [[ "$history_mode" == true ]]; then
  exec python3 "$tools_dir/scripts/execution_history.py" \
    --format "$output_format" "${queue_report_args[@]}"
fi

# --- doctrine pillar 2: the characteristic times this loop is built on ---
# 0.2 s   the rollover-marker poll. Anywhere in 100-500 ms is fine; a longer
#         interval only costs how much further a doomed session runs.
# 5 s     the cooperative window after a marker appears. Claude Code CONTINUES
#         UNCOMPACTED rather than exiting when a PreCompact hook blocks, so
#         this window is usually spent in full; it exists so a session that
#         does wind itself up is never killed needlessly.
# 10 s    the SIGTERM grace given to the attempt's whole process group before
#         SIGKILL — the same grace a service group is normally given.
# 2 s     the settle after a NORMAL Stop marker, before the now-idle TUI is
#         closed. Short and bounded on purpose: the turn is already over and
#         the hook has already run, so this only lets the TUI finish drawing.
#         Measured against a real interactive session, the Stop marker landed
#         ~2 s after the turn ended and the session then sat waiting
#         indefinitely — there is nothing further to wait for.
# 10 s    the SIGTERM grace given to the attempt (whole process group headless,
#         one pid on the TTY path) before SIGKILL — the same grace a service
#         group is normally given.
# 30 s    how long the watcher will wait for the attempt to publish its pid
#         before giving up and simply watching nothing. Generous: run-agent.sh
#         resolves the workspace and the prompt through several python3 calls
#         first, and a slow machine must not lose its watcher over it.
POLL_INTERVAL=0.2
COOPERATIVE_EXIT_SECONDS=5
STOP_SETTLE_SECONDS=2
TERM_GRACE_SECONDS=10
ATTEMPT_PID_SECONDS=30
# Counted in polls rather than seconds: shell has no float comparison, and a
# tick count is the number the loops below actually need.
COOPERATIVE_EXIT_TICKS=$(awk -v s="$COOPERATIVE_EXIT_SECONDS" -v p="$POLL_INTERVAL" 'BEGIN { printf "%d", (s / p) + 0.5 }')
STOP_SETTLE_TICKS=$(awk -v s="$STOP_SETTLE_SECONDS" -v p="$POLL_INTERVAL" 'BEGIN { printf "%d", (s / p) + 0.5 }')
TERM_GRACE_TICKS=$(awk -v s="$TERM_GRACE_SECONDS" -v p="$POLL_INTERVAL" 'BEGIN { printf "%d", (s / p) + 0.5 }')
ATTEMPT_PID_TICKS=$(awk -v s="$ATTEMPT_PID_SECONDS" -v p="$POLL_INTERVAL" 'BEGIN { printf "%d", (s / p) + 0.5 }')

# ----------------------------------------------------------------------------
# NATIVE TUI OR HEADLESS — decided once, from the terminal, for the whole run
# ----------------------------------------------------------------------------
# The default is the native Claude Code TUI, and the only thing that can take it
# away is the absence of a terminal to render into.
#
# BOTH streams are tested, which is one stricter than run-agent.sh's own `! -t 1`
# at its line 285. run-agent.sh only has to decide whether it can WRITE the TUI;
# this script also has to know the TUI will have a keyboard. A run with a
# terminal on stdout but a redirected stdin would otherwise start an interactive
# session that reads EOF immediately. When this test says headless, the headless
# supervisor sets AGENT_PRINT=1, which run-agent.sh already honours — so there
# is one decision here and no second, divergent test downstream.
#
# `--agent codex` has no TUI lifecycle this script hooks and is untouched by
# any of it.
tui_mode=false
if [[ "$agent" == "claude" && -t 0 && -t 1 ]]; then
  tui_mode=true
fi

# --- detect mode: does anything after the repository aliases end in .md? ---
# `rest_tokens` is everything the mode resolution above could not read as a
# repository alias, so the question is only what SHAPE it has. A repo list can no
# longer be mistaken for a message here: the aliases were consumed before this
# ran, which is what the old `is the second token a known repo` test was for.
explicit_mode=false
for rest_token in "${rest_tokens[@]}"; do
  if [[ "$rest_token" == *.md ]]; then
    explicit_mode=true
    break
  fi
done

# --- verbatim mode: `run-sequence.sh --queue api "execute 29, 30, 31, 32"` ---
# One repo, one free-text message, one interactive session with the agent's
# full TUI formatting intact — the message goes through untouched and the
# agent works the list itself.
verbatim_mode=false
if [[ "$explicit_mode" == false && ${#rest_tokens[@]} -ge 1 ]]; then
  verbatim_mode=true
fi

if [[ "$verbatim_mode" == true ]]; then
  if (( ${#repo_targets[@]} != 1 )); then
    echo "error: a verbatim message runs in ONE repository's session; ${#repo_targets[@]} were named." >&2
    exit 2
  fi
  repo_name="${repo_targets[0]}"

  if (( ${#rest_tokens[@]} > 1 )); then
    echo "error: a free-text message must be quoted as a single argument:" >&2
    echo "         $0 --queue $repo_name \"${rest_tokens[*]}\"" >&2
    exit 2
  fi
  message="${rest_tokens[0]}"

  if [[ "$dry_run" == true ]]; then
    printf '%s\n' "$message"
    exit 0
  fi

  echo ""
  echo "==================== $repo_name: interactive session ===================="
  echo "message: $message"
  echo ""
  exec "$run_agent" --agent "$agent" "$repo_name" "$message"
fi

read_frontmatter_field() {
  # $1 = file, $2 = field name. One top-level scalar out of a HANDOFF header.
  #
  # Delegated to `scripts/prompt_frontmatter.py` since `20260825_AUT-08`: this
  # was the sixth hand parser in the repository and it read an INDENTED key as
  # a top-level one, so `block:`'s `severity` could arrive as though it were
  # `status`. `--handoff` is what keeps a ` #` inside an unquoted human
  # sentence from being stripped as a comment.
  python3 "$tools_dir/scripts/prompt_frontmatter.py" get "$1" "$2" --handoff 2>/dev/null || printf ''
}

sha256_of() {
  python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
}

repo_queue_dir_name() {
  # $1 = repository alias. The queue folder and the handoff folder are BOTH the
  # configured alias — one derivation, and no file inside a checkout that could
  # disagree with workspace.json about which queue belongs to it. This function
  # exists so the derivation is named rather than spelled out at nine call sites.
  printf '%s' "${repo_alias[${1,,}]:-$1}"
}

# Sets `resolved_repo_root` rather than printing it: an `exit` inside a
# command substitution only ends the SUBSHELL, so a printing version would
# announce "unknown repo" and then carry on with an empty path.
resolved_repo_root=""
resolve_repo_root() {
  # A configured alias whose checkout is absent must fail with "missing
  # repository", never "unknown alias": those are different problems and only one
  # of them is a typo.
  local repo_name=$1
  resolved_repo_root="${repo_paths[${repo_name,,}]:-}"
  [[ -n "$resolved_repo_root" ]] || {
    echo "unknown repository alias: $repo_name" >&2
    echo "  workspace.json configures: ${configured_aliases[*]}" >&2
    exit 2
  }
  [[ -d "$resolved_repo_root" ]] || { echo "missing repository: $resolved_repo_root" >&2; exit 2; }
}

# ============================================================================
# Git — the checkpoint policy for context rollover
# ============================================================================

git_is_repo() { git -C "$1" rev-parse --git-dir >/dev/null 2>&1; }
git_head() { git -C "$1" rev-parse HEAD 2>/dev/null || printf '%s' ""; }
git_dirty_paths() { git -C "$1" status --porcelain 2>/dev/null; }

require_clean_worktree() {
  # A context-safe prompt begins from a clean worktree, so that everything
  # uncommitted afterwards demonstrably belongs to this prompt's attempt and a
  # rollover's `git add -A` is safe. A dirty tree HERE is somebody else's work,
  # of unknown ownership: stop, name the paths, and never guess. This is
  # deliberately stricter than the old CLAUDE.md instruction to stage
  # everything, which could not tell one from the other.
  #
  # Returns 1 rather than exiting. A dirty repository is a REPOSITORY-level
  # problem — nothing further may be dispatched against it (§14: never hand the
  # same dirty repository to another agent) — but it says nothing about the
  # other six repositories a drain is working, and killing all of them over one
  # was the behaviour AUT-04 was written to end. The caller decides.
  local repo_root=$1 repo_name=$2 prompt_file=$3 when=$4 dirty
  git_is_repo "$repo_root" || return 0
  dirty=$(git_dirty_paths "$repo_root")
  [[ -z "$dirty" ]] && return 0
  echo ""
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "  $repo_name: the worktree is DIRTY $when $prompt_file — stopping."
  echo "  Repository: $repo_root"
  echo "  Uncommitted paths:"
  printf '%s\n' "$dirty" | sed 's/^/    /'
  echo ""
  echo "  Nothing here will stage or commit work whose ownership it does not"
  echo "  know, so no 'git add .' is run against an unknown baseline. Commit,"
  echo "  stash or discard the above yourself, then re-run."
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  return 1
}

# ============================================================================
# EXECUTION TIME — the runner is the authority, and this is the whole mechanism
# ============================================================================
#   execution time = the sum of supervised attempt wall-clock durations
#                    for one prompt
#
# Two timestamps per attempt, taken at lifecycle boundaries this loop already
# passes through. Nothing samples a process, counts a token or reads a clock
# inside the agent's turn, so the measurement costs two `python3` invocations per
# attempt and cannot perturb what it measures.
#
# It is NOT CPU time and NOT billed time. An attempt spending forty minutes
# waiting on a slow install costs forty minutes here, correctly, because that is
# forty minutes of an agent process being alive.
#
# The ledger under the run directory is the record; the handoff field is a
# PROJECTION of it, recomputed and replaced on every write. That is what makes
# refreshing the handoff three times per prompt safe: writing the number twice
# produces the same number, where accumulating it twice would double it.
timing_tool="$tools_dir/scripts/execution_time.py"

record_attempt_timing() {
  # $1 repo alias, $2 prompt file, $3 attempt, $4 started, $5 finished (may be
  # empty), $6 outcome. Written the instant it is known, so a crash costs the
  # current attempt's tail and nothing earlier.
  local repo_name=$1 prompt_file=$2 attempt=$3 started=$4 finished=$5 outcome=$6
  [[ -n "$run_dir" ]] || return 0
  local -a finished_arg=()
  [[ -n "$finished" ]] && finished_arg=(--finished-at "$finished")
  python3 "$timing_tool" record --run-dir "$run_dir" --repo "$repo_name" \
    --prompt "$prompt_file" --attempt "$attempt" --started-at "$started" \
    "${finished_arg[@]}" --outcome "$outcome" >/dev/null 2>&1 || true
}

apply_attempt_timing() {
  # $1 repo alias, $2 prompt file, $3 handoff path, $4 "final" for a completed
  # prompt. A handoff that does not exist yet is skipped rather than created:
  # agent_task.py owns creating one, and a timing writer that also created
  # handoffs would be a second author of the same file.
  local repo_name=$1 prompt_file=$2 handoff_path=$3 final=${4:-}
  [[ -n "$run_dir" && -f "$handoff_path" ]] || return 0
  local -a final_arg=()
  [[ "$final" == "final" ]] && final_arg=(--final)
  python3 "$timing_tool" apply --run-dir "$run_dir" --repo "$repo_name" \
    --prompt "$prompt_file" --handoff "$handoff_path" "${final_arg[@]}" >/dev/null 2>&1 || true
}

# ============================================================================
# --ALLOW-DIRTY: an explicit, recorded risk — never a claim of isolation
# ============================================================================
# The default refusal above is not bureaucracy. It is what makes the rollover's
# `git add -A` safe: with a clean start, everything uncommitted afterwards
# demonstrably belongs to this prompt's attempt. `--allow-dirty` gives that
# property up, on purpose, and everything here exists to make the loss VISIBLE
# rather than silent.
#
# What is bought: a baseline of exactly what was already dirty, an instruction to
# the agent to leave it alone, a checkpoint that stages only paths the baseline
# did not name, and a completion check that fails if any of that pre-existing
# work was committed, deleted or reverted.
#
# What is NOT bought, and must never be described as though it were: separation
# of two edits to the same hunk. If prompt work lands in a file that already had
# uncommitted changes, nothing here can say which line belongs to whom. That is
# reported as an OVERLAP and stops the run.
dirty_tool="$tools_dir/scripts/dirty_baseline.py"
declare -a dirty_baseline_files=()
dirty_baseline_repos=""
dirty_preservation=""
dirty_preservation_detail=""
declare -A dirty_run_repos=()

prompt_repo_aliases() {
  # Every repository this prompt could write: the one it runs in, plus every
  # mutation target its frontmatter declares. Same source as the branch policy's
  # target list, so "which repositories does this prompt touch" has one answer.
  local repo_dir=$1 prompt_path=$2 token expecting=false
  while IFS= read -r token; do
    if [[ "$expecting" == true ]]; then
      printf '%s\n' "$token"
      expecting=false
      continue
    fi
    [[ "$token" == "--repo" ]] && expecting=true
  done < <(declared_branch_targets "$repo_dir" "$prompt_path")
}

baseline_field() {
  # One scalar out of a baseline file, without a second JSON parser in Bash.
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], "") or "")' \
    "$1" "$2" 2>/dev/null || true
}

capture_dirty_baselines() {
  # $1 repo alias, $2 repo dir name, $3 prompt file, $4 prompt path, $5 state dir.
  # Records a baseline for EVERY repository this prompt could write, not only the
  # one it runs in — §1.4's "must still be recorded" — and prints the honest
  # warning before the first agent process starts.
  local repo_name=$1 repo_dir=$2 prompt_file=$3 prompt_path=$4 state_dir=$5
  local stem="${prompt_file%.md}" target root out counts any=false declared=""
  dirty_baseline_files=()
  dirty_baseline_repos=""
  dirty_preservation=""
  dirty_preservation_detail=""
  mkdir -p "$state_dir"

  declared=$(prompt_repo_aliases "$repo_dir" "$prompt_path" | tr '\n' ' ')
  local -a reported=()
  while IFS= read -r target; do
    [[ -n "$target" ]] || continue
    root="${repo_paths[${target,,}]:-}"
    [[ -d "$root" ]] || continue
    out="$state_dir/$stem.$target.dirty-baseline.json"
    counts=$(python3 "$dirty_tool" capture --repo-root "$root" --repo "$target" \
      --out "$out" --prompt "$prompt_file" --run-id "$run_id" --attempt 1 2>/dev/null) || counts=""
    dirty_baseline_files+=("$out")
    if [[ "$(baseline_field "$out" dirty)" == "True" ]]; then
      any=true
      dirty_run_repos[$target]=1
      dirty_baseline_repos="${dirty_baseline_repos:+$dirty_baseline_repos }$target"
      reported+=("$target|$counts")
    fi
  done < <(prompt_repo_aliases "$repo_dir" "$prompt_path")

  [[ "$any" == true ]] || return 0

  echo ""
  echo "  --------------------------------------------------------------------"
  echo "  --allow-dirty: STARTING OVER PRE-EXISTING UNCOMMITTED WORK"
  local line target_name target_counts is_target
  for line in "${reported[@]}"; do
    target_name="${line%%|*}"
    target_counts="${line#*|}"
    if [[ " $declared " == *" $target_name "* ]]; then is_target=yes; else is_target=no; fi
    echo "    $target_name  $target_counts"
    echo "        declared mutation target of this prompt: $is_target"
  done
  echo ""
  echo "  A baseline of exactly these paths has been recorded under the run"
  echo "  directory, and completion will fail if any of it is committed, deleted"
  echo "  or reverted. But OVERLAPPING EDITS TO THE SAME HUNK CANNOT BE"
  echo "  MECHANICALLY SEPARATED: if this prompt's work lands in a file that is"
  echo "  already dirty, nothing here can tell whose change is whose, and the run"
  echo "  stops rather than guess. This is a recorded risk, not isolation."
  echo "  --------------------------------------------------------------------"
}

dirty_mode_note() {
  # The instruction the agent gets. It names the baseline paths for the
  # repositories it may write, because "preserve pre-existing work" is not
  # actionable without knowing which work that is.
  local baseline repo_label
  echo "PRE-EXISTING UNCOMMITTED WORK — PRESERVE IT"
  echo ""
  echo "This run started with pre-existing uncommitted work."
  echo ""
  echo "Preserve it."
  echo "Do not use git add -A, git add ., broad formatting or mechanical rewrites."
  echo "Stage only prompt-owned paths or hunks."
  echo "Do not commit pre-existing staged, unstaged or untracked work."
  echo "If prompt work overlaps a pre-existing dirty hunk and cannot be separated safely,"
  echo "stop blocked and identify the file and overlap."
  echo ""
  echo "ONE TRAP IN PARTICULAR: \`git commit\` with no pathspec commits the WHOLE INDEX,"
  echo "so a pre-existing STAGED change is swept in even when you only added your own"
  echo "file. Commit by pathspec — \`git commit -m \"...\" -- path/one path/two\` — or the"
  echo "runner will report the pre-existing work as absorbed and stop the run."
  echo ""
  echo "The paths that were ALREADY dirty when this attempt started, by repository:"
  for baseline in "${dirty_baseline_files[@]-}"; do
    [[ -f "$baseline" ]] || continue
    [[ "$(baseline_field "$baseline" dirty)" == "True" ]] || continue
    repo_label=$(baseline_field "$baseline" repository)
    echo ""
    echo "  $repo_label:"
    python3 -c '
import json, sys
for entry in json.load(open(sys.argv[1]))["entries"]:
    print("    [%s] %s" % (",".join(entry["kinds"]), entry["path"]))
' "$baseline" 2>/dev/null || true
  done
  echo ""
  echo "Anything NOT in that list is yours and may be committed normally."
  echo "Nothing above may be cleaned, stashed, discarded or committed by you."
}

compare_dirty_baselines() {
  # Sets dirty_preservation to preserved | changed | overlap_blocked, and
  # dirty_preservation_detail to a human sentence naming what moved.
  dirty_preservation="preserved"
  dirty_preservation_detail=""
  local baseline verdict detail
  for baseline in "${dirty_baseline_files[@]-}"; do
    [[ -f "$baseline" ]] || continue
    verdict=$(python3 "$dirty_tool" compare --baseline "$baseline" 2>/dev/null || echo changed)
    verdict=$(tr -d '[:space:]' <<<"$verdict")
    [[ "$verdict" == "preserved" ]] && continue
    detail=$(python3 "$dirty_tool" compare --baseline "$baseline" --json 2>/dev/null \
      | python3 -c '
import json, sys
report = json.load(sys.stdin)
parts = ["%s: %s (%s)" % (f["kind"], f["path"], f["detail"]) for f in report["findings"]]
print("%s -- %s" % (report.get("repository", "?"), "; ".join(parts[:8])))
' 2>/dev/null || true)
    dirty_preservation="$verdict"
    dirty_preservation_detail="$detail"
    # `changed` is the worse of the two and must not be overwritten by a later
    # repository reporting only an overlap.
    [[ "$verdict" == "changed" ]] && return 0
  done
  return 0
}

unexplained_new_paths() {
  # $1 = handoff path. Prints every path that is dirty NOW but was not in any
  # baseline and is not named anywhere in the handoff.
  #
  # "Explained" is deliberately mechanical: the path is NAMED in the part of the
  # handoff the agent wrote. A judgement about whether the explanation is a GOOD
  # one is not something a runner can make, and pretending otherwise would either
  # reject honest work or accept a sentence that says nothing. Naming the file is
  # the checkable part, and it is what lets the next reader tell this change from
  # the pre-existing ones.
  #
  # ONLY BELOW THE `HANDOFF_BODY` MARKER, and that is the whole difference between
  # a check and a formality: agent_task.py's machine-owned header already lists
  # every changed path under "Repository files changed", so searching the whole
  # file would find every path in it and pass unconditionally. The header is
  # written by tooling; an explanation has to come from the author.
  local handoff_path=$1 baseline path body
  [[ -f "$handoff_path" ]] || return 0
  body=$(awk 'found { print } /<!-- HANDOFF_BODY -->/ { found = 1 }' "$handoff_path")
  for baseline in "${dirty_baseline_files[@]-}"; do
    [[ -f "$baseline" ]] || continue
    while IFS= read -r path; do
      [[ -n "$path" ]] || continue
      grep -qF -- "$path" <<<"$body" && continue
      printf '%s\n' "$path"
    done < <(python3 "$dirty_tool" new-paths --baseline "$baseline" 2>/dev/null || true)
  done
}

report_dirty_preservation_failure() {
  local repo_name=$1 prompt_file=$2
  echo ""
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  if [[ "$dirty_preservation" == "overlap_blocked" ]]; then
    echo "  $repo_name: OVERLAP on $prompt_file — pre-existing work cannot be separated."
    echo "  A file that was ALREADY dirty when this prompt started was edited during"
    echo "  the attempt. Which change belongs to the prompt and which to the work"
    echo "  that was already there cannot be decided mechanically, so the run stops"
    echo "  here rather than commit a guess."
  else
    echo "  $repo_name: PRESERVATION FAILURE on $prompt_file."
    echo "  Pre-existing uncommitted work recorded before this prompt started has"
    echo "  been committed, deleted or reverted. --allow-dirty promises exactly one"
    echo "  thing — that this cannot happen unnoticed — so the run stops here."
  fi
  echo ""
  echo "  ${dirty_preservation_detail:-(no detail recorded)}"
  echo ""
  echo "  Baselines: ${dirty_baseline_files[*]-(none)}"
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
}

git_checkpoint() {
  # Called only AFTER the attempt has stopped — never from the hook, which
  # performs no Git mutation at all. Prints the checkpoint commit, or the empty
  # string when the repo is not a Git repository.
  #
  # A clean tree means the agent committed its own work as it went: record HEAD
  # and do NOT manufacture an empty commit. Earlier commits are never amended
  # or squashed — checkpoint commits are durable evidence of where a context
  # window ran out, and rewriting them destroys exactly that.
  # A FOURTH ARGUMENT CHANGES THE STAGING RULE, and this is the one place it
  # matters. With no baseline (the default, clean-start path) `git add -A` is
  # correct precisely because the tree was clean when the prompt began. With a
  # baseline — `--allow-dirty` — `git add -A` would sweep somebody else's
  # uncommitted work into a checkpoint commit under this prompt's message, which
  # is the single failure that mode exists to prevent. So only paths the baseline
  # did NOT record are staged, by explicit pathspec.
  local repo_root=$1 prompt_file=$2 attempt=$3 baseline=${4:-}
  git_is_repo "$repo_root" || { printf '%s' ""; return 0; }
  if [[ -n "$(git_dirty_paths "$repo_root")" ]]; then
    local staged_ok=true
    if [[ -n "$baseline" && -f "$baseline" ]]; then
      local -a own_paths=()
      mapfile -t own_paths < <(python3 "$dirty_tool" new-paths --baseline "$baseline" 2>/dev/null || true)
      if (( ${#own_paths[@]} == 0 )); then
        # Everything dirty was already dirty before this prompt started. There is
        # nothing of this attempt's to checkpoint, and staging any of it would be
        # exactly the mistake. Record HEAD and move on.
        printf '%s' "$(git_head "$repo_root")"
        return 0
      fi
      git -C "$repo_root" add -- "${own_paths[@]}" >/dev/null 2>&1 || staged_ok=false
    else
      git -C "$repo_root" add -A >/dev/null 2>&1 || staged_ok=false
    fi
    if [[ "$staged_ok" != true ]]; then
      echo "  warning: could not stage the rollover checkpoint in $repo_root" >&2
    elif ! git -C "$repo_root" diff --cached --quiet 2>/dev/null; then
      if ! git -C "$repo_root" commit -q \
        -m "checkpoint: context rollover while executing $prompt_file (attempt $attempt)" >/dev/null 2>&1; then
        echo "  warning: could not create the rollover checkpoint commit in $repo_root" >&2
        echo "           (is user.name/user.email configured there?)" >&2
      fi
    fi
  fi
  git_head "$repo_root"
}

# ============================================================================
# THE MAIN-BRANCH RULE
# ============================================================================
# Frozen by HITL on 20260823, out of an overnight drain that ended like this:
# one agent created a feature branch, finished its prompt there, every prompt
# after it inherited the branch, and a later cross-repository task committed one repository
# on the branch while its siblings committed on main. Nothing was lost. Nothing
# looked wrong. Only a human reading `git log` could tell.
#
# So a prompt is not finished until every repository it MUTATES is back on
# `main`, clean, with its commits reachable from `main`. Three moments enforce
# that, and all three call scripts/branch_policy.py rather than running git here:
#
#   BEFORE   preflight  — put the declared targets on main, or refuse and name
#                         the repository, the branch and the commits
#   AFTER    verify     — declared AND observed targets: on main, clean, nothing
#                         stranded off it
#   ON FAIL  remediate  — ONE bounded fresh session whose whole job is to land
#                         the work, non-destructively
#
# Nothing in this file resets, rebases, stashes, force-deletes a branch or
# pushes. When reconciliation is ambiguous the run says so and stops that
# repository; a wrong guess about somebody's history is not recoverable, and a
# refusal is.
branch_policy_script="$tools_dir/scripts/branch_policy.py"

declared_branch_targets() {
  # --repo arguments for one prompt: the repository it runs in, plus every
  # repository its frontmatter declares. Always the running repository, because
  # a prompt that declares nothing still mutates its own.
  local repo_dir=$1 prompt_path=$2 declared
  printf '%s\n%s\n' "--repo" "$repo_dir"
  [[ -f "$prompt_path" ]] || return 0
  while IFS= read -r declared; do
    [[ -n "$declared" && "$declared" != "$repo_dir" ]] || continue
    printf '%s\n%s\n' "--repo" "$declared"
  done < <(python3 "$branch_policy_script" targets \
             --prompt "$prompt_path" 2>/dev/null || true)
}

workspace_snapshot_args() {
  # Every CONFIGURED repository that is actually checked out. The snapshot is
  # deliberately WIDER than the prompt's declaration: an undeclared-but-mutated
  # repository has to be caught, and the only honest way to catch one is to have
  # recorded its refs beforehand.
  local alias
  for alias in "${configured_aliases[@]}"; do
    [[ -d "${repo_paths[${alias,,}]}" ]] || continue
    printf '%s\n%s\n' "--repo" "$alias"
  done
}

branch_preflight() {
  # 0 = every declared target is on the target branch (possibly after a
  # checkout this made). 1 = refused; the report has already been printed.
  local repo_name=$1 repo_dir=$2 prompt_path=$3
  [[ "$branch_policy" != "off" ]] || return 0
  local -a args=()
  mapfile -t args < <(declared_branch_targets "$repo_dir" "$prompt_path")
  local report status=0
  report=$(python3 "$branch_policy_script" preflight \
             --target-branch "$target_branch" --apply "${args[@]}" 2>&1) || status=$?
  if (( status == 0 )); then
    # Silent when there was nothing to do: a line per prompt saying "still on
    # main" is noise. A checkout is not — that is the runner changing the
    # repository under the operator, and it says so.
    if grep -q "checked-out" <<<"$report"; then
      echo "  branch preflight: moved a mutation target onto $target_branch"
      printf '%s\n' "$report" | sed 's/^/    /'
    fi
    return 0
  fi
  echo ""
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "  $repo_name: a mutation target is not on \`$target_branch\` and cannot be moved"
  echo "  there without leaving work behind. Not starting this prompt."
  printf '%s\n' "$report" | sed 's/^/  /'
  echo ""
  echo "  Nothing was reset, stashed, merged or deleted. Land or remove that branch"
  echo "  yourself, then re-run."
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  return 1
}

take_branch_snapshot() {
  # $1 = where to write it. Every checked-out repository's HEAD, branch tips and
  # dirty flag, so "what did this attempt create" is answerable afterwards from
  # refs rather than guessed from timestamps.
  local out=$1
  [[ "$branch_policy" != "off" ]] || return 0
  local -a args=()
  mapfile -t args < <(workspace_snapshot_args)
  (( ${#args[@]} > 0 )) || return 0
  python3 "$branch_policy_script" snapshot \
    --out "$out" "${args[@]}" >/dev/null 2>&1 || true
}

# Set by run_branch_gate for its caller.
branch_gate_ok=yes
branch_gate_remediable=no
branch_gate_off_target=""
branch_gate_drift=""

run_branch_gate() {
  # $1 repo_dir, $2 prompt_path, $3 snapshot file, $4 verdict file.
  # Prints the human report only when the gate FAILS or scope drifted: a passing
  # gate on seven repositories is seven lines nobody reads.
  branch_gate_ok=yes
  branch_gate_remediable=no
  branch_gate_off_target=""
  branch_gate_drift=""
  [[ "$branch_policy" != "off" ]] || return 0
  [[ -f "$3" ]] || return 0
  local -a args=()
  mapfile -t args < <(declared_branch_targets "$1" "$2")
  local report status=0 line key value
  # --allow-dirty relaxes exactly ONE of this gate's three checks — cleanliness —
  # because under that mode a dirty tree is the declared starting condition and
  # whether it is still the SAME dirt is decided by the baseline comparison
  # instead. Branch placement and stray commits stay fully enforced.
  local -a gate_dirty=()
  [[ "$allow_dirty" == true ]] && gate_dirty=(--allow-dirty)
  report=$(python3 "$branch_policy_script" verify \
             --target-branch "$target_branch" --snapshot "$3" \
             --verdict-file "$4" "${gate_dirty[@]}" "${args[@]}" 2>&1) || status=$?
  if [[ -f "$4" ]]; then
    while IFS='=' read -r key value; do
      case "$key" in
        BRANCH_GATE_OK) branch_gate_ok="$value" ;;
        BRANCH_GATE_REMEDIABLE) branch_gate_remediable="$value" ;;
        BRANCH_GATE_OFF_TARGET) branch_gate_off_target="$value" ;;
        BRANCH_GATE_DRIFT) branch_gate_drift="$value" ;;
      esac
    done <"$4"
  elif (( status != 0 )); then
    branch_gate_ok=no
  fi
  if [[ "$branch_gate_ok" != "yes" || -n "$branch_gate_drift" ]]; then
    printf '%s\n' "$report" | sed 's/^/  /'
  fi
  [[ "$branch_gate_ok" == "yes" ]]
}

branch_remediation_message() {
  # The instruction for the ONE bounded repair session. It is a verbatim message
  # rather than a prompt: nothing about the queue changes, no handoff is
  # selected for it, and run-agent.sh's branch preflight is deliberately not in
  # this path — being off `main` is the very condition this session exists to fix.
  local repo_name=$1 prompt_file=$2 report=$3
  cat <<MSG
BRANCH RECONCILIATION — a repair task, not a feature prompt.

The work for $prompt_file appears complete, and the worktree is clean, but
runner policy requires it to be on \`$target_branch\`. It is not. This is what
the completion gate found:

$report

Land the completed commits onto \`$target_branch\` using the safest
non-destructive method available:

  * inspect the graph first — \`git log --oneline --graph --all --decorate\`,
    \`git branch -vv\`, \`git status\`;
  * prefer a FAST-FORWARD of \`$target_branch\` when the branch is simply ahead;
  * a normal merge commit is acceptable when a fast-forward is genuinely not
    possible and the merge is unambiguous;
  * finish with every affected repository ON \`$target_branch\` and CLEAN.

You must NOT: \`reset --hard\`, force-delete or delete any branch, drop a stash,
rebase or otherwise rewrite history that is not yours, or push anything
anywhere. Do not touch any prompt file, and do not move anything into done/ or
blocked/ — that has already been decided.

Then update this prompt's canonical handoff to record the reconciliation you
performed: which repository, which branch, which commits, and by what Git
operation. Use:

  python3 scripts/agent_task.py checkpoint --repo-root <that repo> \\
    --prompt $prompt_file --status complete

If the reconciliation is genuinely ambiguous — divergent histories, a branch
carrying commits that are not this prompt's, anything that would need a
destructive guess — DO NOTHING DESTRUCTIVE. Leave the repository exactly as you
found it, say precisely what you found and why it cannot be landed safely, and
stop. Being refused is the correct outcome there; the runner will treat it as a
block rather than advance.
MSG
}

# ============================================================================
# Runtime state — $WORKSPACE_DATA_ROOT/.run-sequence/<run-id>/
# ============================================================================
# Deliberately outside every source repository, outside the prompt folder and
# outside the handoff folder: these are the RUNNER's breadcrumbs, not part of
# any repository's history and not part of the workflow's case law.

run_id=""
run_dir=""
declare -a rollover_log=()
sessions_started=0
prompts_completed=0
rollovers_total=0

init_run_state() {
  [[ -n "$run_dir" ]] && return 0
  run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  run_dir="$data_root/.run-sequence/$run_id"
  run_settings_dir="$run_dir/settings"
  mkdir -p "$run_settings_dir"
  # Say it once, plainly, and only when it is true. An operator who expected the
  # native TUI and got flat text deserves the reason on the first line rather
  # than a mystery — and the reason is never a flag they forgot.
  if [[ "$agent" == "claude" && "$tui_mode" == false ]]; then
    echo "run-sequence.sh: no usable terminal on stdin/stdout — running headless (streamed progress instead of the Claude TUI)."
  fi
  write_run_state "running"
}

write_run_state() {
  local phase=$1
  [[ -n "$run_dir" ]] || return 0
  RS_DIR="$run_dir" RS_ID="$run_id" RS_PHASE="$phase" RS_AGENT="$agent" \
  RS_REPOS="${positional[*]}" RS_MAX_ROLLOVERS="$max_context_rollovers" \
  RS_SESSIONS="$sessions_started" RS_COMPLETED="$prompts_completed" \
  RS_ROLLOVERS="$rollovers_total" RS_ALLOW_DIRTY="$allow_dirty" \
  RS_DIRTY_REPOS="${!dirty_run_repos[*]}" \
  RS_DIRTY_PRESERVATION="$dirty_preservation" python3 - <<'PY' || true
import datetime, json, os, pathlib, tempfile
directory = pathlib.Path(os.environ["RS_DIR"])
state = {
    "schema": "run-sequence.state/1",
    "run_id": os.environ["RS_ID"],
    "phase": os.environ["RS_PHASE"],
    "mode": "context-safe",
    "agent": os.environ["RS_AGENT"],
    "repos": os.environ["RS_REPOS"].split(),
    "max_context_rollovers": int(os.environ["RS_MAX_ROLLOVERS"]),
    "agent_sessions_started": int(os.environ["RS_SESSIONS"]),
    "prompts_completed": int(os.environ["RS_COMPLETED"]),
    "context_rollovers": int(os.environ["RS_ROLLOVERS"]),
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
# ONLY ON A DIRTY RUN. An ordinary clean run's state stays byte-for-byte what it
# always was, so nothing that reads this file has to learn three new keys to
# describe a mode it never used.
if os.environ.get("RS_ALLOW_DIRTY") == "true":
    state["allow_dirty"] = True
    state["dirty_baseline_repositories"] = sorted(
        entry for entry in os.environ.get("RS_DIRTY_REPOS", "").split() if entry
    )
    state["dirty_preservation"] = os.environ.get("RS_DIRTY_PRESERVATION") or "unknown"
directory.mkdir(parents=True, exist_ok=True)
fd, staged = tempfile.mkstemp(dir=str(directory), prefix=".state-", suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(state, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.replace(staged, directory / "state.json")
PY
}

merge_rollover_record() {
  # The hook wrote what only the hook knows: trigger, session id, transcript
  # path, cwd. The runner merges in what only the runner knows: run id, repo,
  # prompt, attempt, the commit the attempt started from, the checkpoint commit
  # it produced, and the agent's exit status. In place, atomically.
  local marker=$1
  shift
  RS_MARKER="$marker" python3 - "$@" <<'PY' || true
import json, os, pathlib, sys, tempfile
marker = pathlib.Path(os.environ["RS_MARKER"])
try:
    record = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        record = {}
except (OSError, ValueError):
    record = {}
for pair in sys.argv[1:]:
    key, _, value = pair.partition("=")
    if key:
        record[key] = value
marker.parent.mkdir(parents=True, exist_ok=True)
fd, staged = tempfile.mkstemp(dir=str(marker.parent), prefix=".rollover-", suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.replace(staged, marker)
PY
}

write_attempt_settings() {
  # An EPHEMERAL settings file, per attempt. `claude --settings` is ADDITIVE,
  # so ~/.claude/settings.json and every repository .claude/settings.json keep
  # applying untouched — this adds the hook, only to processes this script
  # starts, and only for this attempt. No permanent settings file is written,
  # read-modify-written, or backed up.
  #
  # Both triggers are registered: there is no reason for a context-safe
  # unattended session to compact in place, including when the agent decides to
  # type /compact itself. PostCompact is registered too, as the alarm: if a
  # compaction ever COMPLETES inside a supervised attempt the protection has
  # failed, and the run must stop saying so rather than pretend it worked.
  #
  # Stop is the THIRD hook, and it is what lets the queue keep the native TUI.
  # The interactive CLI does not exit at the end of a turn and gained no flag to
  # make it — but Stop fires when the main agent has finished responding, so the
  # supervisor learns the turn ended without having to read Claude's output.
  # Verified firing interactively on claude 2.1.239: once per turn, with the TUI
  # still the foreground job afterwards. It carries no matcher — Stop has no
  # trigger variants to distinguish — and it NEVER blocks.
  local settings_file=$1 marker=$2 breach=$3 stop=$4
  RS_SETTINGS="$settings_file" RS_SELF="$self_path" \
  RS_MARKER="$marker" RS_BREACH="$breach" RS_STOP="$stop" python3 - <<'PY'
import json, os, pathlib, shlex

self_path = shlex.quote(os.environ["RS_SELF"])
pre = "%s --internal-precompact-hook %s" % (self_path, shlex.quote(os.environ["RS_MARKER"]))
post = "%s --internal-postcompact-hook %s" % (self_path, shlex.quote(os.environ["RS_BREACH"]))
stop = "%s --internal-stop-hook %s" % (self_path, shlex.quote(os.environ["RS_STOP"]))


def entry(trigger, command):
    return {"matcher": trigger, "hooks": [{"type": "command", "command": command, "timeout": 15}]}


def bare(command):
    return {"hooks": [{"type": "command", "command": command, "timeout": 15}]}


settings = {
    "hooks": {
        "PreCompact": [entry("auto", pre), entry("manual", pre)],
        "PostCompact": [entry("auto", post), entry("manual", post)],
        "Stop": [bare(stop)],
    }
}
path = pathlib.Path(os.environ["RS_SETTINGS"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

# ============================================================================
# THE RUN LEDGER — COMPLETE, BLOCKED, DEFERRED, NOT REACHED
# ============================================================================
# A drain that stops at the first blocked prompt wastes a night. A drain that
# carries on regardless corrupts things. What makes the difference is knowing
# WHICH of the remaining prompts a block actually costs — and saying so at the
# end, in four categories that mean four different things:
#
#   COMPLETE     ran, moved into done/, handoff written, gate passed
#   BLOCKED      attempted and genuinely stuck. Its file is in blocked/ (the
#                agent put it there) or still queued (the runner refused to
#                accept it). Either way it stays available for repair.
#   DEFERRED     NOT ATTEMPTED IN THIS RUN, because something it depends on
#                blocked. The file is untouched, no handoff is invented, nothing
#                is marked complete, and the next run picks it up normally.
#   NOT REACHED  still queued when the run ended: --max, or a repository that
#                had already become unsafe.
#
# Deferral is per-run state and lives ONLY in these arrays and in the resolver's
# `--skip`. Nothing here writes it to disk, which is the whole reason a later
# drain can simply run the prompt.
#
# A DEFERRAL HAS A KIND, and the difference decides whether a later pass may
# reconsider it:
#   prereq  the resolver could not select it yet — a prerequisite was not in
#           done/. If something completes later in this run, that may no longer
#           be true, so it is retried.
#   block   a prompt actually blocked, or a handoff named it. Nothing this run
#           can do will change that, so it STAYS deferred. Getting this wrong is
#           not academic: the first version cleared every deferral on the retry
#           pass and duly ran a prompt whose blocked prerequisite had told it not
#           to.
declare -a outcome_complete=()
declare -a outcome_blocked=()
declare -a deferred_order=()
declare -A deferred_reason=()
declare -A deferred_kind=()
declare -A repo_unsafe=()
declare -A skip_stems=()
declare -A repo_prompt_dir=()
declare -A alias_of_app=()
first_failure_status=0
catastrophic_reason=""
prompts_blocked=0
prompts_deferred=0

resolver_skip_args=()
build_resolver_skip_args() {
  # `--skip <stem>` for every prompt deferred in this repository so far. The
  # resolver, not this script, decides what that means — there is exactly one
  # implementation of `requires:` in this workspace and it is Python.
  local repo_name=$1 stem
  resolver_skip_args=()
  [[ -n "${skip_stems[$repo_name]:-}" ]] || return 0
  while IFS= read -r stem; do
    [[ -n "$stem" ]] || continue
    resolver_skip_args+=("--skip" "$stem")
  done <<<"${skip_stems[$repo_name]}"
}

already_deferred() {
  local repo_name=$1 stem=$2
  [[ -n "${skip_stems[$repo_name]:-}" ]] || return 1
  grep -qxF "$stem" <<<"${skip_stems[$repo_name]}"
}

defer_prompt() {
  # $1 repo alias (may be empty when the prompt is in an app this run is not
  # draining — it is still recorded, so the summary is truthful), $2 label to
  # show, $3 stem, $4 reason, $5 kind (prereq|block).
  local repo_name=$1 label=$2 stem=$3 reason=$4 kind=${5:-prereq}
  [[ -n "${deferred_reason[$label]:-}" ]] && return 0
  if [[ -n "$repo_name" ]] && ! already_deferred "$repo_name" "$stem"; then
    skip_stems[$repo_name]="${skip_stems[$repo_name]:+${skip_stems[$repo_name]}$'\n'}$stem"
  fi
  deferred_order+=("$label")
  deferred_reason[$label]="$reason"
  deferred_kind[$label]="$kind"
  prompts_deferred=$((prompts_deferred + 1))
  echo "  DEFERRED  $label — $reason"
}

mark_repo_unsafe() {
  local repo_name=$1 reason=$2
  [[ -n "${repo_unsafe[$repo_name]:-}" ]] && return 0
  repo_unsafe[$repo_name]="$reason"
  echo ""
  echo "  $repo_name is now UNSAFE for the rest of this run: $reason"
  echo "  No further prompt will be dispatched against it. Other repositories are"
  echo "  unaffected unless they depend on its work."
}

record_blocked() {
  # $1 label, $2 severity, $3 reason
  outcome_blocked+=("$1|$2|$3")
  prompts_blocked=$((prompts_blocked + 1))
}

# ---------------------------------------------------------------------------
# What a block costs the rest of the queue
# ---------------------------------------------------------------------------
apply_block_impact() {
  # $1 repo alias, $2 prompt_directory, $3 prompt file, $4 handoff path.
  #
  # TWO LAYERS, and the order matters. The deterministic dependency closure is
  # the FLOOR: every queued prompt that transitively requires this one is
  # deferred whether or not any handoff remembered it. The agent's own `block:`
  # statement can only WIDEN that — never narrow it — because an LLM that
  # forgets a dependant must not be able to let the runner start work on it.
  local repo_name=$1 prompt_directory=$2 prompt_file=$3 handoff_path=$4
  local stem="${prompt_file%.md}"
  local severity="local" reason="" summary="" impact_json=""

  if [[ -f "$handoff_path" ]]; then
    impact_json=$(python3 "$tools_dir/scripts/agent_task.py" block-info \
      --repo-root "$resolved_repo_root" --prompt "$prompt_file" --json 2>/dev/null || echo '{}')
  fi
  [[ -n "$impact_json" ]] || impact_json='{}'
  severity=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("severity") or "local")' "$impact_json")
  reason=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("reason") or "unspecified")' "$impact_json")
  summary=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("summary") or "")' "$impact_json")

  echo ""
  echo "  BLOCKED   $prompt_directory/$prompt_file"
  echo "            severity: $severity   reason: $reason"
  [[ -n "$summary" ]] && echo "            $summary"
  record_blocked "$prompt_directory/$prompt_file" "$severity" "$reason"

  if [[ "$severity" == "catastrophic" ]]; then
    catastrophic_reason="$repo_name/$prompt_file — $reason: ${summary:-no summary recorded}"
    return 0
  fi

  # The blocked prompt itself must never be selected again in this run. Its file
  # is normally in blocked/ already, but a prompt the RUNNER refused (a failed
  # branch reconciliation, say) is still in the queue folder.
  defer_or_skip_self "$repo_name" "$stem"

  # LAYER 1 — the deterministic closure, across every app queue.
  local dependents
  dependents=$(python3 "$resolver" --data-root "$data_root" \
    --dependents-of "$prompt_directory/$stem" --json 2>/dev/null || echo '{"dependents":[]}')
  local line app prompt_name dep_stem dep_alias
  while IFS=$'\t' read -r app prompt_name dep_stem; do
    [[ -n "$prompt_name" ]] || continue
    dep_alias="${alias_of_app[$app]:-}"
    defer_prompt "$dep_alias" "$app/$prompt_name" "$dep_stem" \
      "requires $prompt_file, which blocked" block
  done < <(python3 -c '
import json, sys
data = json.loads(sys.argv[1])
for entry in data.get("dependents", []):
    print("%s\t%s\t%s" % (entry["app"], entry["prompt"], entry["stem"]))
' "$dependents")

  # LAYER 2 — what the agent said, which widens layer 1 and never replaces it.
  local declared_stem declared_repo
  while IFS= read -r declared_stem; do
    [[ -n "$declared_stem" ]] || continue
    declared_stem="${declared_stem%.md}"
    defer_declared_stem "$declared_stem" "$prompt_file"
  done < <(python3 -c '
import json, sys
for value in json.loads(sys.argv[1]).get("blocks_prompts", []):
    print(value)
' "$impact_json")

  while IFS= read -r declared_repo; do
    [[ -n "$declared_repo" ]] || continue
    local blocked_alias="${alias_of_app[$declared_repo]:-}"
    if [[ -n "$blocked_alias" ]]; then
      mark_repo_unsafe "$blocked_alias" \
        "$prompt_file reported that it blocks all work in $declared_repo"
    fi
  done < <(python3 -c '
import json, sys
for value in json.loads(sys.argv[1]).get("blocks_repositories", []):
    print(value)
' "$impact_json")
}

defer_or_skip_self() {
  local repo_name=$1 stem=$2
  already_deferred "$repo_name" "$stem" && return 0
  skip_stems[$repo_name]="${skip_stems[$repo_name]:+${skip_stems[$repo_name]}$'\n'}$stem"
}

defer_declared_stem() {
  # An agent-named casualty. It may live in any of this run's queues, so it is
  # looked up rather than assumed to be in the same repository.
  local stem=$1 because=$2 candidate app alias_name
  for alias_name in "${repo_targets[@]}"; do
    app="${repo_prompt_dir[$alias_name]:-}"
    [[ -n "$app" ]] || continue
    for candidate in "$data_root/LLM/prompts/$app/$stem"*.md; do
      [[ -f "$candidate" ]] || continue
      defer_prompt "$alias_name" "$app/$(basename "$candidate")" \
        "$(basename "$candidate" .md)" \
        "$because's handoff names it as blocked by this failure" block
      return 0
    done
  done
  # Named but not queued anywhere in this run: record it so the operator sees the
  # claim, without pretending it affected anything here.
  echo "  note: the blocked handoff names $stem, which is not in any queue this run is draining."
}

# ---------------------------------------------------------------------------
# What the agent is told about the queue behind it
# ---------------------------------------------------------------------------
following_queue_note() {
  # $1 = repo alias, $2 = prompt file currently being dispatched.
  # A compact tail listing what this run intends to attempt next, and what to do
  # about it if this prompt has to block. It is information, not permission: the
  # agent is told in the same breath never to touch another prompt file.
  local current_repo=$1 current_prompt=$2
  local -a following=()
  local alias_name app queued base label count=0
  for alias_name in "$current_repo" "${repo_targets[@]}"; do
    [[ -n "${repo_unsafe[$alias_name]:-}" ]] && continue
    app="${repo_prompt_dir[$alias_name]:-}"
    [[ -n "$app" ]] || continue
    for queued in "$data_root/LLM/prompts/$app/"*.md; do
      [[ -f "$queued" ]] || continue
      base=$(basename "$queued")
      [[ "$alias_name" == "$current_repo" && "$base" == "$current_prompt" ]] && continue
      already_deferred "$alias_name" "${base%.md}" && continue
      label="$app/$base"
      case " ${following[*]-} " in *" $label "*) continue ;; esac
      following+=("$label")
      count=$((count + 1))
      (( count >= 40 )) && break 2
    done
  done

  echo "FOLLOWING QUEUED PROMPTS"
  echo ""
  if (( ${#following[@]} == 0 )); then
    echo "Nothing else is queued in this run. You are the last prompt."
  else
    echo "This run intends to attempt these after you, in roughly this order:"
    local index=1 entry
    for entry in "${following[@]}"; do
      printf '  %d. %s\n' "$index" "$entry"
      index=$((index + 1))
    done
  fi
  cat <<'NOTE'

IF YOU MUST MARK THIS PROMPT BLOCKED, assess that list first.

  1. Decide which of those prompts are CAUSALLY affected by whatever blocked
     you — not which ones are merely nearby.
  2. Record the assessment in this prompt's handoff:

       python3 scripts/agent_task.py checkpoint --repo-root <repo> \
         --prompt <this-prompt.md> --status blocked \
         --block-severity local|dependent|catastrophic \
         --block-reason <short_identifier> \
         --block-summary "<one sentence a human can act on>" \
         [--blocks-prompt <exact-prompt-stem>]... \
         [--blocks-repository <repository-directory>]...

  3. PREFER LETTING UNRELATED WORK CONTINUE. `local` is the right severity when
     nothing else is known to depend on you, and it is the common case.
     `dependent` when you can name specific casualties.
  4. `catastrophic` STOPS THE ENTIRE RUN and is the ultima ratio: repository
     history that cannot be trusted, a shared contract left half-written across
     repositories, a corruption risk. A missing fixture, an unavailable service
     or one failed optional smoke is NOT catastrophic.

The runner independently computes the transitive dependency closure of your
prompt and defers everything that requires it, so you do not need to list those.
Your list only WIDENS that set.

Do not edit, move, rename or delete any other prompt file. You are assessing
this run's queue, not changing it.
NOTE
}

# ============================================================================
# The agent-facing notes injected per attempt
# ============================================================================
# These go in through `claude --append-system-prompt`, which run-agent.sh
# already forwards to the CLI. run-agent.sh keeps sole ownership of the initial
# task instruction; this only adds what is true of THIS attempt.

context_safe_note() {
  cat <<'NOTE'
CONTEXT-SAFE RUN

This session was started by run-sequence.sh and handles exactly ONE prompt. It
will not be given a second one: when this prompt is finished the process exits
and the next prompt starts in a brand-new session with no memory of this one.

You do not own context-window management. run-sequence.sh does, through Claude
Code's PreCompact lifecycle hook: automatic compaction is intercepted, your
work is checkpoint-committed, and a fresh session resumes this same prompt.
Do not estimate your remaining context, do not watch for a percentage, do not
run /compact, and do not stop to ask for a session reset.

What that costs you: keep the canonical handoff's HANDOFF_BODY reasonably
current — after a completed milestone, a decision worth keeping, or a test run
that produced a result — rather than writing it all in the last five minutes.
If this session is replaced mid-prompt, the handoff and the Git history are
what the next one has. Do not write the handoff after every tool call; useful
recovery state is the goal, not constant I/O.
NOTE
}

rollover_resume_note() {
  # $1 attempt, $2 max attempts, $3 repo, $4 prompt path, $5 handoff path,
  # $6 start commit, $7 breadcrumb path
  local attempt=$1 attempts=$2 repo=$3 prompt_path=$4 handoff_path=$5 start_commit=$6 breadcrumb=$7
  local range="the commits made since this prompt began"
  [[ -n "$start_commit" ]] && range="git log --oneline $start_commit..HEAD  (and git diff $start_commit..HEAD)"
  cat <<NOTE
CONTEXT ROLLOVER RESUME

You are a fresh Claude session continuing the SAME prompt. This is attempt
$attempt of $attempts on $repo. The previous session was not finished and was
not wrong: Claude Code asked to compact its context, run-sequence.sh blocked
the compaction, checkpointed the work in Git and replaced the process. Nothing
about the task changed.

Do not restart the prompt blindly.

Read, in this order:
  1. the prompt:            $prompt_path
  2. its canonical handoff, status in_progress: $handoff_path
  3. the Git history since the prompt began:
       $range
  4. the latest context-rollover breadcrumb: $breadcrumb

Then determine, requirement by requirement, what is already complete and what
remains. Preserve completed correct work — re-doing it is the main way a
rollover turns into lost time. Continue only the unfinished work.

Do NOT read the previous session's transcript unless the handoff and the Git
state are genuinely insufficient. The transcript path is recorded in the
breadcrumb for emergencies only; reading it back re-imports the very context
this rollover exists to reset.

Everything else about the task is unchanged: final completion still requires
the canonical handoff, the prompt moves into done/ only when it is genuinely
complete, and into blocked/ if it is genuinely blocked.

$(context_safe_note)
NOTE
}

# ============================================================================
# Process supervision
# ============================================================================

attempt_alive() {
  # A reaped-but-not-waited child still answers `kill -0`, so a zombie would
  # look alive for the whole SIGTERM grace and then be "escalated" to SIGKILL.
  local pid=$1 state
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  state=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
  [[ "$state" == Z* ]] && return 1
  return 0
}

group_live_count() {
  # How many non-zombie processes are still in the attempt's process group.
  # The HEADLESS path runs `claude ... | python3 stream_progress.py`, so an
  # attempt is a bash, a claude and a python — the whole group has to go, and
  # only the group: never `pkill claude`, which would take down an unrelated
  # session. The TTY path has no group of its own to kill (see below) and
  # signals one pid instead.
  local pgid=$1
  [[ -n "$pgid" ]] || { echo 0; return 0; }
  ps -e -o pgid=,stat= 2>/dev/null | awk -v g="$pgid" '$1 == g && $2 !~ /^Z/ { n++ } END { print n + 0 }'
}

terminate_attempt() {
  local pgid=$1 pid=$2 why=$3 waited=0
  if [[ -n "$pgid" ]]; then
    echo "  stopping attempt process group $pgid ($why): SIGTERM"
    kill -TERM -"$pgid" 2>/dev/null || true
  else
    echo "  stopping attempt pid $pid ($why): SIGTERM"
    kill -TERM "$pid" 2>/dev/null || true
  fi

  while (( waited < TERM_GRACE_TICKS )); do
    if [[ -n "$pgid" ]]; then
      [[ "$(group_live_count "$pgid")" == "0" ]] && break
    else
      attempt_alive "$pid" || break
    fi
    sleep "$POLL_INTERVAL"
    waited=$((waited + 1))
  done

  local still=1
  if [[ -n "$pgid" ]]; then
    [[ "$(group_live_count "$pgid")" == "0" ]] && still=0
  else
    attempt_alive "$pid" || still=0
  fi
  if (( still )); then
    echo "  attempt still alive after ${TERM_GRACE_SECONDS}s of SIGTERM — SIGKILL"
    if [[ -n "$pgid" ]]; then
      kill -KILL -"$pgid" 2>/dev/null || true
    else
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
}

terminate_pid_silently() {
  # The TTY path's terminator. Same escalation as terminate_attempt, by PID
  # only, and SILENT: the Claude TUI owns the terminal while this runs and a
  # stray line of ours would be drawn straight through its rendering.
  #
  # By PID and never by group, because on the TTY path the attempt deliberately
  # SHARES run-sequence.sh's process group — that is what makes it the
  # terminal's foreground job. `kill -TERM -$pgid` there would kill this script
  # too. There is exactly one pid to signal because the attempt subshell
  # `exec`s all the way down to claude.
  local pid=$1 waited=0
  [[ -n "$pid" ]] || return 0
  kill -TERM "$pid" 2>/dev/null || true
  while (( waited < TERM_GRACE_TICKS )); do
    attempt_alive "$pid" || return 0
    sleep "$POLL_INTERVAL"
    waited=$((waited + 1))
  done
  if attempt_alive "$pid"; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  return 0
}

# Outputs of supervise_attempt, read by run_prompt_context_safe.
attempt_status=0
attempt_rolled_over=false
attempt_compact_breach=false

attempt_watcher() {
  # THE BACKGROUND HALF of the TTY topology. It watches runner-owned marker
  # FILES — never Claude's output, which it cannot see and must not parse — and
  # its only power is to signal the one pid the attempt published.
  #
  # It prints NOTHING. The TUI is drawing on this terminal; a line from here
  # would land in the middle of it. Everything it decides is written to the
  # verdict file and reported by the foreground half after the TUI is gone.
  #
  # `set +e` first: this runs as a background subshell that inherits `set -e`,
  # and a polling loop whose body ends in a failing test would silently end the
  # watcher rather than the iteration.
  set +e
  local pidfile=$1 marker=$2 breach=$3 stop=$4 verdict=$5
  local pid="" reason="" waited=0 spins=0 grace_ticks

  while [[ -z "$pid" ]]; do
    if [[ -s "$pidfile" ]]; then
      pid=$(tr -dc '0-9' <"$pidfile" 2>/dev/null)
    fi
    if [[ -n "$pid" ]]; then
      break
    fi
    spins=$((spins + 1))
    if (( spins > ATTEMPT_PID_TICKS )); then
      return 0
    fi
    sleep "$POLL_INTERVAL"
  done

  while true; do
    if [[ -f "$marker" ]]; then reason="rollover"; break; fi
    if [[ -f "$breach" ]]; then reason="breach"; break; fi
    if [[ -f "$stop" ]]; then reason="stop"; break; fi
    if ! attempt_alive "$pid"; then
      # The agent returned on its own; nothing to close.
      return 0
    fi
    sleep "$POLL_INTERVAL"
  done

  printf '%s\n' "$reason" >"$verdict.partial" 2>/dev/null
  mv "$verdict.partial" "$verdict" 2>/dev/null

  # A NORMAL STOP gets a short settle, not the cooperative window: the turn is
  # already over and the hook has already run, so this is only the moment the
  # TUI needs to finish drawing. A rollover or a breach gets the longer window,
  # because blocking a compaction does not end a Claude session — it makes it
  # carry on uncompacted — so the kill there is the normal path, not the
  # exceptional one.
  grace_ticks=$COOPERATIVE_EXIT_TICKS
  if [[ "$reason" == "stop" ]]; then
    grace_ticks=$STOP_SETTLE_TICKS
  fi
  waited=0
  while (( waited < grace_ticks )); do
    attempt_alive "$pid" || return 0
    sleep "$POLL_INTERVAL"
    waited=$((waited + 1))
  done
  attempt_alive "$pid" || return 0

  # Close ONLY this supervised attempt. Not a process group, not a name match,
  # not every claude on the machine: the one pid this attempt published.
  terminate_pid_silently "$pid"
  return 0
}

supervise_attempt_tty() {
  # THE DEFAULT. The attempt is the terminal's FOREGROUND JOB and renders
  # Claude Code's own interactive TUI — the same tool cards, diffs, spinners,
  # syntax highlighting, status line and scrolling the operator gets from typing
  # `claude` themselves. Nothing here reads, parses, filters, re-renders or
  # tees that output; run-sequence.sh does not see it at all.
  #
  #   run-sequence.sh                       ← the terminal's foreground pgroup
  #   ├── attempt_watcher &                 ← polls marker FILES, prints nothing
  #   └── ( echo $BASHPID >pidfile; exec run-agent.sh … )   ← FOREGROUND
  #           └── exec claude …             ← real controlling TTY
  #
  # Three properties this shape buys, each of which a more elaborate one loses:
  #
  #   * ONE PID all the way down. The subshell publishes its own pid and then
  #     `exec`s, and run-agent.sh's TUI branch `exec`s claude, so the pid in the
  #     file IS claude's. No pgid guessing and no pipeline to hunt through.
  #   * THE REAL TERMINAL. No `set -m`, no setsid, no tmux, no screen, no pty
  #     helper. The attempt stays in this script's process group, which is
  #     already the terminal's foreground group, so claude gets the controlling
  #     TTY, SIGWINCH and native keyboard handling for free. Putting it in its
  #     own group is exactly what would earn it a SIGTTIN the moment the TUI
  #     touched the terminal.
  #   * CTRL-C KEEPS ITS NATIVE MEANING. SIGINT reaches the whole foreground
  #     group, so Claude interrupts its turn the way it always does. bash defers
  #     this script's INT trap until the foreground attempt returns and then
  #     runs it FIRST, so an interrupt can never be misread downstream as a
  #     rollover, a Stop or a retryable failure.
  local repo_name=$1 prompt_file=$2 settings_file=$3 marker=$4 breach=$5 stop=$6 note=$7
  local stem="${marker%.rollover.json}"
  local pidfile="$stem.pid" verdict="$stem.verdict"
  local watcher_pid verdict_reason=""

  rm -f "$pidfile" "$verdict" "$verdict.partial"

  save_terminal_state
  attempt_watcher "$pidfile" "$marker" "$breach" "$stop" "$verdict" &
  watcher_pid=$!
  current_watcher_pid="$watcher_pid"
  current_attempt_pidfile="$pidfile"

  sessions_started=$((sessions_started + 1))

  set +e
  (
    printf '%s\n' "$BASHPID" >"$pidfile"
    exec "$run_agent" --agent "$agent" "$repo_name" "$prompt_file" \
      --settings "$settings_file" --append-system-prompt "$note"
  )
  attempt_status=$?
  set -e

  stop_attempt_watcher "$watcher_pid"
  current_watcher_pid=""
  current_attempt_pidfile=""
  restore_terminal_state

  if [[ -f "$marker" ]]; then attempt_rolled_over=true; fi
  if [[ -f "$breach" ]]; then attempt_compact_breach=true; fi

  if [[ -f "$verdict" ]]; then
    verdict_reason=$(tr -d '[:space:]' <"$verdict" 2>/dev/null || true)
  fi

  # CLOSING AN IDLE TUI IS NOT A FAILURE. The turn ended, the hook said so, the
  # watcher shut the waiting session — that is this design working, and claude
  # exits 143 for it. Normalise that one case, and only when the watcher says it
  # is the one that acted; a genuine nonzero exit is still a genuine failure,
  # and whether the PROMPT is finished is still decided by verify_completion
  # reading the filesystem, never by this status.
  if [[ "$verdict_reason" == "stop" && "$attempt_status" -eq 143 ]]; then
    attempt_status=0
  fi

  rm -f "$pidfile" "$verdict"
}

supervise_attempt_headless() {
  # NO USABLE TTY: CI, a redirected stdout, an automation harness. There is no
  # terminal to render into, so this keeps the pre-existing `-p` +
  # stream_progress.py path exactly as it was, including its own process-group
  # supervision. Context safety is unchanged here — the same PreCompact hook,
  # the same rollover, the same checkpoint.
  local repo_name=$1 prompt_file=$2 settings_file=$3 marker=$4 breach=$5 note=$6
  local status_file="${marker%.json}.exit"

  rm -f "$status_file" "$status_file.partial"

  # Job control puts the background job in its OWN process group, so the
  # attempt can be signalled without signalling run-sequence.sh — and so a
  # terminal Ctrl-C reaches this script rather than the agent. No setsid, no
  # tmux, no daemon: `set -m` is portable and needs nothing installed. This is
  # safe HERE precisely because there is no TUI to hand a terminal to; the TTY
  # path must not do it.
  #
  # The wrapper subshell records the agent's exit status in a file because a
  # child that has exited but not been waited for still answers `kill -0`;
  # a file that appears is an unambiguous "the agent returned".
  set -m
  (
    set +e
    AGENT_PRINT=1 "$run_agent" --agent "$agent" "$repo_name" "$prompt_file" \
      --settings "$settings_file" --append-system-prompt "$note"
    printf '%s\n' "$?" >"$status_file.partial"
    mv "$status_file.partial" "$status_file"
  ) &
  local child_pid=$!
  set +m

  # Read the process group back rather than assuming it — `set -m` makes the
  # job its own leader, but the value that matters for kill(2) is the one the
  # kernel has, not the one this script expected.
  local pgid observed self_pgid
  pgid="$child_pid"
  observed=$(ps -o pgid= -p "$child_pid" 2>/dev/null | tr -d ' ' || true)
  [[ -n "$observed" ]] && pgid="$observed"
  self_pgid=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ' || true)
  if [[ -n "$self_pgid" && "$pgid" == "$self_pgid" ]]; then
    echo "  warning: the attempt shares run-sequence.sh's process group ($pgid);" >&2
    echo "           it will be signalled by pid only, never by group." >&2
    pgid=""
  fi
  current_attempt_pid="$child_pid"
  current_attempt_pgid="$pgid"

  sessions_started=$((sessions_started + 1))

  while true; do
    if [[ -f "$marker" ]]; then attempt_rolled_over=true; break; fi
    if [[ -f "$breach" ]]; then attempt_compact_breach=true; break; fi
    if [[ -f "$status_file" ]]; then break; fi
    attempt_alive "$child_pid" || break
    sleep "$POLL_INTERVAL"
  done

  # The agent may have exited in the same instant the marker landed; the marker
  # is the more important of the two facts, so re-check it after the loop.
  [[ -f "$marker" ]] && attempt_rolled_over=true
  [[ -f "$breach" ]] && attempt_compact_breach=true

  if [[ "$attempt_rolled_over" == true || "$attempt_compact_breach" == true ]]; then
    # Give it a chance to wind itself up first — blocking a compaction does not
    # end a Claude session, it makes it carry on uncompacted, so this window is
    # usually spent in full and the kill below is the normal path, not the
    # exceptional one.
    local waited=0
    while [[ ! -f "$status_file" ]] && attempt_alive "$child_pid" \
      && (( waited < COOPERATIVE_EXIT_TICKS )); do
      sleep "$POLL_INTERVAL"
      waited=$((waited + 1))
    done
    if [[ ! -f "$status_file" ]] && attempt_alive "$child_pid"; then
      terminate_attempt "$pgid" "$child_pid" "context rollover"
    fi
  fi

  local waited_status=0
  set +e
  wait "$child_pid" 2>/dev/null
  waited_status=$?
  set -e
  current_attempt_pid=""
  current_attempt_pgid=""

  if [[ -f "$status_file" ]]; then
    attempt_status=$(tr -dc '0-9' <"$status_file")
    [[ -n "$attempt_status" ]] || attempt_status=0
  else
    attempt_status=$waited_status
  fi
  rm -f "$status_file"

  # In `-p` mode the agent exits by itself at the end of its turn, so the Stop
  # marker is recorded but never acted on: there is no waiting TUI to close.
}

supervise_attempt() {
  # $1 repo_name, $2 prompt_file, $3 settings file, $4 marker, $5 breach file,
  # $6 stop marker, $7 the --append-system-prompt note
  local repo_name=$1 prompt_file=$2 settings_file=$3 marker=$4 breach=$5 stop=$6 note=$7

  attempt_status=0
  attempt_rolled_over=false
  attempt_compact_breach=false

  if [[ "$tui_mode" == true ]]; then
    supervise_attempt_tty "$repo_name" "$prompt_file" "$settings_file" \
      "$marker" "$breach" "$stop" "$note"
  else
    supervise_attempt_headless "$repo_name" "$prompt_file" "$settings_file" \
      "$marker" "$breach" "$note"
  fi
}

# ============================================================================
# Handoff maintenance around a rollover
# ============================================================================

ensure_in_progress_handoff() {
  # Before the FIRST attempt, and again after every rollover. A rollover is not
  # a partial, not a failure and not a completion — it is the same task, still
  # in progress, in a different process. agent_task.py owns the machine header
  # (repository identity, prompt path and SHA, changed-file inventory) and
  # PRESERVES everything after <!-- HANDOFF_BODY -->, so the agent's own words
  # survive untouched. Reimplementing that header in Bash is exactly what this
  # call exists to avoid.
  local repo_root=$1 prompt_file=$2
  if ! python3 "$tools_dir/scripts/agent_task.py" checkpoint \
    --repo-root "$repo_root" --prompt "$prompt_file" --status in_progress >/dev/null 2>&1; then
    echo "  warning: could not checkpoint an in_progress handoff for $prompt_file" >&2
    echo "           (agent_task.py checkpoint failed; the agent must still write one)" >&2
    return 0
  fi
}

append_rollover_note() {
  # A SMALL machine-generated note, appended at the very end of the handoff so
  # it cannot corrupt agent-authored content, and repeated rollovers extend the
  # same section rather than duplicating it. It states facts only: attempt,
  # commit, reason. It never fabricates a summary of what was completed — the
  # next agent derives that from the prompt, the body above, the Git history
  # and the files, which are all first-hand.
  local handoff=$1 attempt=$2 checkpoint=$3 reason=$4
  [[ -f "$handoff" ]] || return 0
  RS_HANDOFF="$handoff" RS_ATTEMPT="$attempt" RS_COMMIT="$checkpoint" RS_REASON="$reason" \
  python3 - <<'PY' || true
import datetime, os, pathlib
handoff = pathlib.Path(os.environ["RS_HANDOFF"])
heading = "## Context rollover checkpoints (machine-generated)"
text = handoff.read_text(encoding="utf-8").rstrip("\n")
if heading not in text:
    text += "\n\n" + heading + "\n"
line = "\n- attempt %s — checkpoint commit `%s` — reason: %s — %s" % (
    os.environ["RS_ATTEMPT"],
    os.environ["RS_COMMIT"] or "(no Git checkpoint)",
    os.environ["RS_REASON"],
    datetime.datetime.now(datetime.timezone.utc).isoformat(),
)
handoff.write_text(text + line + "\n", encoding="utf-8")
PY
}

# ============================================================================
# Completion verification — unchanged rules, one addition
# ============================================================================

completion_verdict=""
completion_detail=""

verify_completion() {
  # Looks at the FILESYSTEM, not at the agent's exit status: the prompt file
  # must have moved into done/, and its handoff must exist and pin this exact
  # prompt. The one addition for the context-safe path is the worktree check at
  # the end — a prompt is not finished while its own changes are uncommitted.
  #
  # SETS `completion_verdict` INSTEAD OF EXITING, because the four outcomes are
  # not equally bad and the caller is what knows the difference:
  #   complete  everything held
  #   blocked   the agent parked it in blocked/ — it said why, so the queue can
  #             reason about the damage
  #   stuck     the turn ended with the prompt still queued. NO impact statement
  #             exists, so the runner cannot know what is safe in that
  #             repository and must treat the whole repository as unsafe.
  #   mismatch  moved to done/ with a missing or wrong handoff — a hole in the
  #             record, and equally a reason to stop touching that repository.
  local repo_name=$1 repo_root=$2 prompt_file=$3 prompt_directory=$4 handoff_directory=$5
  completion_verdict=""
  completion_detail=""
  local prompt_dir="$data_root/LLM/prompts/$prompt_directory"
  local handoff_path="$data_root/LLM/handoffs/$handoff_directory/$prompt_file"
  local status got_prompt_path got_sha want_sha want_prompt_path

  if [[ -f "$prompt_dir/blocked/$prompt_file" ]]; then
    status=$(read_frontmatter_field "$handoff_path" "status" 2>/dev/null || true)
    echo ""
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "  $repo_name: $prompt_file was parked as BLOCKED — stopping the run."
    echo "  It now sits in prompts/$prompt_directory/blocked/ and needs a rewrite"
    echo "  or a human decision before anything downstream of it can proceed."
    echo "  Handoff: $handoff_path${status:+ (status: $status)}"
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    completion_verdict="blocked"
    completion_detail="parked in blocked/"
    return 0
  fi

  if [[ ! -f "$prompt_dir/done/$prompt_file" ]]; then
    status=$(read_frontmatter_field "$handoff_path" "status" 2>/dev/null || true)
    echo ""
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "  $repo_name: $prompt_file is still in the queue folder — not complete."
    echo "  A finished prompt is moved into prompts/$prompt_directory/done/;"
    echo "  this one was not, so it did not finish. Stopping the run here."
    if [[ -f "$handoff_path" ]]; then
      echo "  Handoff: $handoff_path${status:+ (status: $status)}"
    else
      echo "  No handoff was written either (expected $handoff_path)."
    fi
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    completion_verdict="stuck"
    completion_detail="the turn ended with the prompt still in the queue folder"
    return 0
  fi

  # SECONDARY: the handoff no longer gates selection, but it is still the
  # audit trail and is still mandatory. A prompt that moved to done/ without
  # one — or with one pinned to different text — is a hole in the record.
  if [[ ! -f "$handoff_path" ]]; then
    echo ""
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "  $repo_name: $prompt_file was moved to done/ but no handoff was written."
    echo "  Expected: $handoff_path"
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    completion_verdict="mismatch"
    completion_detail="moved into done/ with no handoff"
    return 0
  fi

  got_prompt_path=$(read_frontmatter_field "$handoff_path" "prompt_path")
  got_sha=$(read_frontmatter_field "$handoff_path" "prompt_sha256")
  want_sha=$(sha256_of "$prompt_dir/done/$prompt_file")
  # prompt_path stays the queue-relative identity even after the move — see
  # canonical_prompt_path() in scripts/agent_task.py.
  want_prompt_path="LLM/prompts/$prompt_directory/$prompt_file"

  if [[ "$got_prompt_path" != "$want_prompt_path" || "$got_sha" != "$want_sha" ]]; then
    echo ""
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "  $repo_name: handoff at $handoff_path does not match $prompt_file"
    echo "  (prompt_path/prompt_sha256 mismatch — stale or wrong handoff). Stopping."
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    completion_verdict="mismatch"
    completion_detail="the handoff does not pin this prompt"
    return 0
  fi

  if [[ "$agent" == "claude" ]]; then
    if [[ "$allow_dirty" == true ]]; then
      # THE CLEAN-WORKTREE RULE DOES NOT APPLY, AND IS NOT SIMPLY DROPPED. A
      # prompt may finish with the repository still dirty — with the SAME dirt it
      # started with. What replaces the check is the harder question: is every
      # recorded pre-existing path still exactly where and what it was, and is
      # everything else the agent left uncommitted explained in the handoff?
      compare_dirty_baselines
      if [[ "$dirty_preservation" != "preserved" ]]; then
        report_dirty_preservation_failure "$repo_name" "$prompt_file"
        completion_verdict="mismatch"
        completion_detail="pre-existing uncommitted work was not preserved ($dirty_preservation)"
        return 0
      fi
      local -a unexplained=()
      mapfile -t unexplained < <(unexplained_new_paths "$handoff_path")
      if (( ${#unexplained[@]} > 0 )); then
        echo ""
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        echo "  $repo_name: $prompt_file left uncommitted changes that are NEITHER part"
        echo "  of the recorded pre-existing baseline NOR mentioned in the handoff:"
        printf '    %s\n' "${unexplained[@]}"
        echo ""
        echo "  Under --allow-dirty a still-dirty repository is not a failure by itself,"
        echo "  but a NEW uncommitted path has to be either committed or explained —"
        echo "  otherwise the next reader cannot tell it from the work that was already"
        echo "  there, which is the one distinction this whole mode exists to keep."
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        completion_verdict="mismatch"
        completion_detail="new uncommitted paths were neither committed nor explained in the handoff"
        return 0
      fi
    elif ! require_clean_worktree "$repo_root" "$repo_name" "$prompt_file" "after completing"; then
      completion_verdict="mismatch"
      completion_detail="the worktree is dirty after completion"
      return 0
    fi
  fi

  completion_verdict="complete"
  echo "$repo_name: $prompt_file — complete (moved to done/)."
}

report_agent_failure() {
  local repo_name=$1 prompt_file=$2 status=$3
  echo ""
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  if [[ $status -eq 130 ]]; then
    echo "  $repo_name: $prompt_file was INTERRUPTED (Ctrl-C) — stopping."
    echo "  Nothing is wrong with the queue; the task simply did not finish."
  else
    echo "  $repo_name: the agent exited $status on $prompt_file — stopping."
    echo "  No context-rollover marker was written, so this is a real failure,"
    echo "  not a context window running out."
  fi
  echo "  Re-run it with: ./run-agent.sh --agent $agent $repo_name $prompt_file"
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  # An interrupt is the operator's decision and ends everything immediately;
  # anything else is recorded so the run can exit with the agent's own status
  # later, after the summary. The first failure is the one that gets to set it —
  # a later, less informative status must not overwrite the one that explains
  # the run.
  if [[ $status -eq 130 ]]; then
    exit 130
  fi
  (( first_failure_status == 0 )) && first_failure_status=$status
  return 0
}

# ============================================================================
# One prompt, one or more attempts
# ============================================================================

run_prompt_plain() {
  # The codex path, and any agent without a PreCompact lifecycle: one process,
  # no hooks, no supervision, exactly as before. AGENT_PRINT=1 below is inert —
  # run-agent.sh's codex branch has never read it — and is left exactly as it
  # was rather than tidied away, because codex behaviour is not in scope.
  local repo_name=$1 prompt_file=$2 status=0
  attempt_outcome=""
  set +e
  AGENT_PRINT=1 "$run_agent" --agent "$agent" "$repo_name" "$prompt_file"
  status=$?
  set -e
  sessions_started=$((sessions_started + 1))
  if [[ $status -ne 0 ]]; then
    report_agent_failure "$repo_name" "$prompt_file" "$status"
    attempt_outcome="failed"
  fi
}

# Set by run_prompt_context_safe: "" (ran normally, verify next), or one of
# dirty / failed / oversized / catastrophic.
attempt_outcome=""

run_prompt_context_safe() {
  local repo_name=$1 repo_root=$2 prompt_file=$3 prompt_directory=$4 handoff_directory=$5
  local repo_dir_name; repo_dir_name=$(repo_queue_dir_name "$repo_name")
  attempt_outcome=""
  local prompt_path="$data_root/LLM/prompts/$prompt_directory/$prompt_file"
  local handoff_path="$data_root/LLM/handoffs/$handoff_directory/$prompt_file"
  local repo_state_dir="$run_dir/$repo_name"
  local stem="${prompt_file%.md}"
  mkdir -p "$repo_state_dir"

  # THE DEFAULT IS UNCHANGED, and that is the point of the branch rather than a
  # softened version of the old check: without --allow-dirty this refuses exactly
  # as it always has. With it, the refusal is replaced by a RECORD — not removed.
  local own_baseline=""
  if [[ "$allow_dirty" == true ]]; then
    capture_dirty_baselines "$repo_name" "$repo_dir_name" "$prompt_file" \
      "$prompt_path" "$repo_state_dir"
    own_baseline="$repo_state_dir/$stem.$repo_dir_name.dirty-baseline.json"
    [[ -f "$own_baseline" ]] || own_baseline=""
  elif ! require_clean_worktree "$repo_root" "$repo_name" "$prompt_file" "before starting"; then
    attempt_outcome="dirty"
    return 0
  fi
  local start_commit
  start_commit=$(git_head "$repo_root")

  local attempts=$((max_context_rollovers + 1))
  local attempt=1
  declare -a prompt_checkpoints=()

  while (( attempt <= attempts )); do
    ensure_in_progress_handoff "$repo_root" "$prompt_file"

    local marker="$repo_state_dir/$stem.attempt-$attempt.rollover.json"
    local breach="$repo_state_dir/$stem.attempt-$attempt.compact-breach.json"
    local stop="$repo_state_dir/$stem.attempt-$attempt.stop.json"
    local settings="$run_settings_dir/${repo_name}.$stem.attempt-$attempt.settings.json"
    rm -f "$marker" "$breach" "$stop"
    write_attempt_settings "$settings" "$marker" "$breach" "$stop"

    local note
    if (( attempt == 1 )); then
      note=$(printf '%s\n\n%s\n' "$(context_safe_note)" "$(following_queue_note "$repo_name" "$prompt_file")")
    else
      note=$(printf '%s\n\n%s\n' \
        "$(rollover_resume_note "$attempt" "$attempts" "$repo_name" \
          "$prompt_path" "$handoff_path" "$start_commit" \
          "$repo_state_dir/$stem.attempt-$((attempt - 1)).rollover.json")" \
        "$(following_queue_note "$repo_name" "$prompt_file")")
      echo "  attempt $attempt/$attempts — fresh Claude session, SAME prompt, resuming from the handoff and the checkpoint commit."
    fi
    # Every attempt, not only the first: a fresh session after a rollover knows
    # nothing the previous one was told, and "leave that file alone" is the piece
    # of context it least affords to lose.
    if [[ "$allow_dirty" == true && -n "$dirty_baseline_repos" ]]; then
      note=$(printf '%s\n\n%s\n' "$note" "$(dirty_mode_note)")
    fi

    # TIMING: two timestamps per attempt, at the lifecycle boundary, and nothing
    # else. No polling loop, no sampling, no clock reading inside the agent's
    # turn — the runner already knows when it started a process and when that
    # process stopped, which is the entire measurement.
    local attempt_started attempt_finished attempt_label
    attempt_started=$(python3 "$timing_tool" now)
    # Recorded with no finish FIRST. If run-sequence.sh itself dies here, the
    # ledger still says an attempt started and when — which is what turns a
    # crash into a `checkpoint_estimate` rather than a silently missing hour.
    record_attempt_timing "$repo_name" "$prompt_file" "$attempt" "$attempt_started" "" running
    supervise_attempt "$repo_name" "$prompt_file" "$settings" "$marker" "$breach" "$stop" "$note"
    attempt_finished=$(python3 "$timing_tool" now)
    rm -f "$settings"

    attempt_label=ran
    if [[ "$attempt_compact_breach" == true ]]; then
      attempt_label=compact_breach
    elif [[ "$attempt_rolled_over" == true ]]; then
      attempt_label=rollover
    elif [[ $attempt_status -ne 0 ]]; then
      attempt_label=failed
    fi
    # Replaces the started-only record for this attempt number; the ledger holds
    # one row per attempt, so this cannot add the duration a second time.
    record_attempt_timing "$repo_name" "$prompt_file" "$attempt" \
      "$attempt_started" "$attempt_finished" "$attempt_label"
    apply_attempt_timing "$repo_name" "$prompt_file" "$handoff_path"

    if [[ "$attempt_compact_breach" == true ]]; then
      echo ""
      echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
      echo "  $repo_name: a CONTEXT COMPACTION COMPLETED during $prompt_file."
      echo "  PostCompact fired inside a supervised attempt, which means the"
      echo "  PreCompact block did not hold and this session's memory of the task"
      echo "  has already been summarised away. The safety mechanism failed;"
      echo "  stopping rather than pretending it worked."
      echo "  Breadcrumb: $breach"
      echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
      write_run_state "compact-breach"
      # CATASTROPHIC by definition: the protection this whole design rests on
      # did not hold, so nothing downstream of it can be trusted to have been
      # done with an intact memory of its requirements.
      catastrophic_reason="$repo_name/$prompt_file — a context compaction COMPLETED inside a supervised attempt"
      attempt_outcome="catastrophic"
      return 0
    fi

    if [[ "$attempt_rolled_over" == true ]]; then
      local reason checkpoint
      reason=$(python3 -c '
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("reason", "precompact"))
except Exception:
    print("precompact")
' "$marker")
      echo ""
      echo "  $repo_name: context rollover on $prompt_file (attempt $attempt, $reason)."
      checkpoint=$(git_checkpoint "$repo_root" "$prompt_file" "$attempt" "$own_baseline")
      ensure_in_progress_handoff "$repo_root" "$prompt_file"
      append_rollover_note "$handoff_path" "$attempt" "$checkpoint" "$reason"
      merge_rollover_record "$marker" \
        "run_id=$run_id" "repo=$repo_name" "prompt=$prompt_file" \
        "attempt=$attempt" "attempt_start_commit=$start_commit" \
        "checkpoint_commit=$checkpoint" "agent_exit_status=$attempt_status"
      rollovers_total=$((rollovers_total + 1))
      prompt_checkpoints+=("$checkpoint")
      rollover_log+=("  $repo_name  $prompt_file  attempt $attempt → checkpoint ${checkpoint:0:12}")
      echo "  checkpoint commit: ${checkpoint:-(none; no Git repository)}"
      echo "  handoff refreshed at status in_progress: $handoff_path"
      write_run_state "running"
      attempt=$((attempt + 1))
      continue
    fi

    if [[ $attempt_status -ne 0 ]]; then
      report_agent_failure "$repo_name" "$prompt_file" "$attempt_status"
      attempt_outcome="failed"
      return 0
    fi
    break
  done

  if (( attempt > attempts )); then
    echo ""
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "  $repo_name: $prompt_file exceeded --max-context-rollovers"
    echo "  ($max_context_rollovers), so the run stops rather than spawning Claude"
    echo "  sessions forever. The prompt stays in the queue folder and its handoff"
    echo "  keeps whatever truthful status it has; nothing is marked complete."
    echo "  Checkpoint commits, oldest first:"
    local commit
    for commit in "${prompt_checkpoints[@]}"; do
      echo "    ${commit:-(none)}"
    done
    echo "  Breadcrumbs: $repo_state_dir"
    echo "  This prompt is probably too large for one context window: split it,"
    echo "  or raise --max-context-rollovers deliberately."
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    write_run_state "rollover-limit"
    # PROMPT-level, not repository-level: every attempt was checkpoint-committed
    # on the target branch and the tree is clean, so the repository is in a
    # perfectly good state — this one prompt is simply too big for one window.
    # It must not be selected again in this run, or the loop would spawn
    # sessions for it forever.
    attempt_outcome="oversized"
    return 0
  fi
}

# `prompt_outcome` is what the scheduler reads. One of:
#   complete      accepted, in done/, on the target branch, clean
#   blocked       this PROMPT cannot proceed; the queue can, minus its dependants
#   repo-unsafe   this REPOSITORY cannot proceed; other repositories can
#   catastrophic  nothing can proceed
prompt_outcome=""
prompt_outcome_detail=""

run_one_prompt() {
  # $1 = repo_name, $2 = prompt filename.
  local repo_name=$1 prompt_file=$2 repo_root prompt_directory handoff_directory
  resolve_repo_root "$repo_name"; repo_root="$resolved_repo_root"
  prompt_directory=$(repo_queue_dir_name "$repo_name")
  handoff_directory=$(repo_queue_dir_name "$repo_name")
  init_run_state

  prompt_outcome=""
  prompt_outcome_detail=""

  local repo_dir; repo_dir=$(repo_queue_dir_name "$repo_name")
  local prompt_path="$data_root/LLM/prompts/$prompt_directory/$prompt_file"
  local handoff_path="$data_root/LLM/handoffs/$handoff_directory/$prompt_file"
  local stem="${prompt_file%.md}"

  # 1. BEFORE — the declared mutation targets go onto the target branch, or this
  #    prompt never starts. A wrong branch is far cheaper to refuse than to
  #    reconcile, and it is a property of the REPOSITORY, so it stops the
  #    repository rather than only this prompt.
  if ! branch_preflight "$repo_name" "$repo_dir" "$prompt_path"; then
    prompt_outcome="repo-unsafe"
    prompt_outcome_detail="a mutation target is not on \`$target_branch\` and cannot be moved there safely"
    return 0
  fi

  # 2. The refs of every checked-out repository, so "what did this attempt
  #    create, and where did it put it" is answerable afterwards — including for
  #    a sibling the prompt never declared.
  local state_dir="$run_dir/$repo_name"
  local snapshot_file="$state_dir/$stem.branch-before.json"
  local verdict_file="$state_dir/$stem.branch-verdict.env"
  mkdir -p "$state_dir"
  take_branch_snapshot "$snapshot_file"

  if [[ "$agent" == "claude" ]]; then
    run_prompt_context_safe "$repo_name" "$repo_root" "$prompt_file" \
      "$prompt_directory" "$handoff_directory"
  else
    run_prompt_plain "$repo_name" "$prompt_file"
  fi

  case "$attempt_outcome" in
    catastrophic)
      prompt_outcome="catastrophic"
      prompt_outcome_detail="$catastrophic_reason"
      return 0 ;;
    dirty)
      prompt_outcome="repo-unsafe"
      prompt_outcome_detail="the worktree was dirty before this prompt could start"
      return 0 ;;
    failed)
      # A nonzero exit with no rollover marker leaves the repository in a state
      # nobody described: no handoff status to read, no impact statement, quite
      # possibly uncommitted work. Scope the damage to the repository.
      prompt_outcome="repo-unsafe"
      prompt_outcome_detail="the agent exited nonzero with no context-rollover marker"
      return 0 ;;
    oversized)
      prompt_outcome="blocked"
      prompt_outcome_detail="exceeded --max-context-rollovers ($max_context_rollovers)"
      return 0 ;;
  esac

  verify_completion "$repo_name" "$repo_root" "$prompt_file" \
    "$prompt_directory" "$handoff_directory"

  case "$completion_verdict" in
    stuck|mismatch)
      # No impact statement exists, so there is nothing to reason from about
      # what is still safe in this repository. Conservative by construction.
      prompt_outcome="repo-unsafe"
      prompt_outcome_detail="$completion_detail"
      return 0 ;;
  esac

  # 3. AFTER — the completion gate. It runs for a BLOCKED prompt too: §14 of the
  #    AUT-04 prompt is explicit that partial or checkpoint work a block leaves
  #    behind obeys the same rule, and that the next agent must never inherit a
  #    dirty repository.
  if ! run_branch_gate "$repo_dir" "$prompt_path" "$snapshot_file" "$verdict_file"; then
    if [[ "$completion_verdict" == "complete" && "$branch_gate_remediable" == "yes" ]]; then
      if remediate_branch "$repo_name" "$repo_root" "$repo_dir" "$prompt_file" \
           "$prompt_path" "$snapshot_file" "$verdict_file"; then
        prompt_outcome="complete"
        prompts_completed=$((prompts_completed + 1))
        return 0
      fi
    fi
    prompt_outcome="repo-unsafe"
    prompt_outcome_detail="work is not on \`$target_branch\` after the attempt (repositories: ${branch_gate_off_target:-unknown})"
    return 0
  fi

  if [[ -n "$branch_gate_drift" ]]; then
    # Not a refusal — the gate already proved those repositories are on the
    # target branch and clean. It is a REPORT, because a prompt that changed a
    # repository it never declared is worth an operator's attention even when
    # every check passed.
    echo "  scope drift: $prompt_file also changed $branch_gate_drift, which its frontmatter does not declare."
  fi

  if [[ "$completion_verdict" == "blocked" ]]; then
    # A blocked prompt's time was still consumed and is still recorded; what it
    # does NOT get is a completion timestamp, because it did not complete.
    apply_attempt_timing "$repo_name" "$prompt_file" "$handoff_path"
    prompt_outcome="blocked"
    prompt_outcome_detail="$completion_detail"
    return 0
  fi

  apply_attempt_timing "$repo_name" "$prompt_file" "$handoff_path" final
  prompt_outcome="complete"
  prompts_completed=$((prompts_completed + 1))
  write_run_state "running"
}

remediate_branch() {
  # ONE bounded repair session, per prompt, ever. Returns 0 when the gate passes
  # afterwards. Deliberately not a loop: a reconciliation that a fresh session
  # with the whole graph in front of it could not land is not going to be landed
  # by a second one, and repeating it is how an overnight queue turns into an
  # overnight retry.
  local repo_name=$1 repo_root=$2 repo_dir=$3 prompt_file=$4 prompt_path=$5
  local snapshot_file=$6 verdict_file=$7
  local stem="${prompt_file%.md}"
  local state_dir="$run_dir/$repo_name"

  echo ""
  echo "=========================================================================="
  # `branch_gate_off_target` is a list of REPOSITORIES, not of branches — the
  # gate reports which repositories failed, and the branch each is sitting on is
  # in the report above. Naming it as a branch here read as a bug.
  echo "  $repo_name: $prompt_file completed with work that is not on \`$target_branch\`."
  echo "  Off-target repositories: ${branch_gate_off_target:-(unknown)}"
  echo "  Starting ONE branch-reconciliation session."
  echo "  It may fast-forward or merge. It may not reset, delete, rebase or push."
  echo "=========================================================================="

  # mapfile, not word splitting on a command substitution. This was the ONE
  # branch-policy call site still splitting its target list on whitespace; every
  # other one reads it into an array. With configurable aliases, an alias
  # containing a space would silently have become two arguments there, one of
  # them not a repository.
  local -a verify_args=()
  mapfile -t verify_args < <(declared_branch_targets "$repo_dir" "$prompt_path")
  local report
  report=$(python3 "$branch_policy_script" verify \
    --target-branch "$target_branch" --snapshot "$snapshot_file" \
    "${verify_args[@]}" 2>&1 || true)

  local message
  message=$(branch_remediation_message "$repo_name" "$prompt_file" "$report")

  if [[ "$agent" != "claude" ]]; then
    echo "  --agent $agent has no supervised repair path here; not attempting one."
    return 1
  fi

  local marker="$state_dir/$stem.remediation.rollover.json"
  local breach="$state_dir/$stem.remediation.compact-breach.json"
  local stop="$state_dir/$stem.remediation.stop.json"
  local settings="$run_settings_dir/${repo_name}.$stem.remediation.settings.json"
  rm -f "$marker" "$breach" "$stop"
  write_attempt_settings "$settings" "$marker" "$breach" "$stop"

  supervise_attempt "$repo_name" "$message" "$settings" "$marker" "$breach" "$stop" \
    "$(context_safe_note)"
  rm -f "$settings"

  if [[ "$attempt_status" -ne 0 ]]; then
    echo "  the reconciliation session exited $attempt_status."
  fi

  if run_branch_gate "$repo_dir" "$prompt_path" "$snapshot_file" "$verdict_file"; then
    echo "  reconciled: $prompt_file's work is on \`$target_branch\` and the tree is clean."
    return 0
  fi
  echo ""
  echo "  reconciliation did NOT land the work on \`$target_branch\`. Nothing was"
  echo "  reset, deleted or pushed, and the branch still holds the commits."
  echo "  This is a block, not an advance."
  return 1
}

print_context_safe_plan() {
  local repo_name=$1 repo_root=$2 prompt_directory=$3 handoff_directory=$4
  shift 4
  local state next dirty
  echo ""
  echo "==================== $repo_name: context-safe queue (dry run) ===================="
  echo "  repository:            $repo_root"
  echo "  prompt queue:          $data_root/LLM/prompts/$prompt_directory/"
  echo "  handoffs:              $data_root/LLM/handoffs/$handoff_directory/"
  echo "  agent:                 $agent, one FRESH session per prompt"
  echo "  context rollover:      PreCompact hook → block → checkpoint → fresh session"
  echo "  max rollovers/prompt:  $max_context_rollovers"
  echo "  runtime state:         $data_root/.run-sequence/<run-id>/"
  if [[ $# -gt 0 ]]; then
    echo "  prompts (explicit):    $*"
  else
    state=$(python3 "$resolver" --repo "$repo_name" --data-root "$data_root" --json 2>/dev/null || echo '{"action":"error","reason":"resolver failed"}')
    next=$(python3 -c '
import json, sys
state = json.load(sys.stdin)
action = state.get("action")
if action == "run":
    print(state["prompt_path"])
else:
    print("%s: %s" % (action, state.get("reason", "")))
' <<<"$state")
    echo "  next prompt:           $next"
  fi
  if git_is_repo "$repo_root"; then
    dirty=$(git_dirty_paths "$repo_root")
    if [[ -n "$dirty" && "$allow_dirty" == true ]]; then
      echo "  worktree:              DIRTY — --allow-dirty, so a baseline of these paths"
      echo "                         would be recorded and they would have to survive:"
      printf '%s\n' "$dirty" | sed 's/^/                           /'
    elif [[ -n "$dirty" ]]; then
      echo "  worktree:              DIRTY — a context-safe prompt would REFUSE to start:"
      printf '%s\n' "$dirty" | sed 's/^/                           /'
    else
      echo "  worktree:              clean"
    fi
  else
    echo "  worktree:              not a Git repository (no checkpoint commits possible)"
  fi
  echo "  nothing was started, written or claimed."
}

single_session_queue_message() {
  local prompt_directory=$1 handoff_directory=$2
  printf '%s' "\$WORKSPACE_DATA_ROOT/LLM/prompts/$prompt_directory/ IS your queue: every .md file directly in that folder is outstanding work, and nothing else is. Finished prompts are not there — they have been moved into its done/ subfolder, and genuinely blocked ones into blocked/. Ignore both subfolders entirely; you never need to read a handoff to work out what is done. Execute every file in the queue folder in ascending filename order, starting with the oldest, one at a time, without skipping any. Work unsupervised: do not stop to ask for confirmation between prompts. For each prompt follow the normal task lifecycle from the toolkit's AGENTS.md and WORKFLOW.md, write its handoff at \$WORKSPACE_DATA_ROOT/LLM/handoffs/$handoff_directory/ with the same filename (if a handoff already exists saying in_progress or partial, resume rather than restart), commit, and then as your final step for that prompt move the prompt file into \$WORKSPACE_DATA_ROOT/LLM/prompts/$prompt_directory/done/ (create the folder if it does not exist). Moving the file is what marks a prompt complete; a prompt still in the queue folder is still outstanding. Then re-list the queue folder and take the next oldest. Stop only if you are genuinely blocked — record status blocked in that prompt's handoff, move the prompt file into \$WORKSPACE_DATA_ROOT/LLM/prompts/$prompt_directory/blocked/ instead of done/, say so, and stop there rather than moving on."
}

print_run_summary() {
  local line entry label detail severity
  echo ""
  if (( prompts_blocked > 0 || first_failure_status != 0 )); then
    echo "==================== run-sequence.sh finished ===================="
  else
    echo "==================== run-sequence.sh finished cleanly ===================="
  fi
  echo ""
  echo "  prompts completed:   $prompts_completed"
  echo "  prompts blocked:     $prompts_blocked"
  echo "  prompts deferred:    $prompts_deferred"
  echo "  agent sessions:      $sessions_started"
  echo "  context rollovers:   $rollovers_total"
  if [[ "$allow_dirty" == true ]]; then
    echo ""
    echo "  --allow-dirty was in effect."
    if (( ${#dirty_run_repos[@]} > 0 )); then
      echo "  dirty baseline repositories: ${!dirty_run_repos[*]}"
      echo "  dirty preservation:          ${dirty_preservation:-preserved}"
      [[ -n "$dirty_preservation_detail" ]] && echo "    $dirty_preservation_detail"
    else
      echo "  no relevant repository was actually dirty; nothing was relaxed."
    fi
    echo "  Overlapping edits to one hunk cannot be mechanically separated, and"
    echo "  nothing above claims they were."
  fi

  if (( ${#outcome_complete[@]} > 0 )); then
    echo ""
    echo "  COMPLETE"
    for entry in "${outcome_complete[@]}"; do
      echo "    $entry"
    done
  fi

  if (( ${#outcome_blocked[@]} > 0 )); then
    echo ""
    echo "  BLOCKED — attempted and genuinely stuck. Nothing about these was"
    echo "  converted into a completion; each stays available for repair."
    for entry in "${outcome_blocked[@]}"; do
      label="${entry%%|*}"
      severity="${entry#*|}"; severity="${severity%%|*}"
      detail="${entry##*|}"
      echo "    $label  [$severity] $detail"
    done
  fi

  if (( ${#deferred_order[@]} > 0 )); then
    echo ""
    echo "  DEFERRED — not attempted in THIS run. The files were not touched, no"
    echo "  handoff was written for them, and a later run picks them up normally."
    for entry in "${deferred_order[@]}"; do
      [[ -n "$entry" ]] || continue
      echo "    $entry"
      echo "        because: ${deferred_reason[$entry]}"
    done
  fi

  print_not_reached

  if (( ${#repo_unsafe[@]} > 0 )); then
    echo ""
    echo "  REPOSITORIES CLOSED FOR THIS RUN"
    local unsafe_repo
    for unsafe_repo in "${!repo_unsafe[@]}"; do
      echo "    $unsafe_repo — ${repo_unsafe[$unsafe_repo]}"
    done
  fi

  if (( ${#rollover_log[@]} > 0 )); then
    echo ""
    echo "  rollovers:"
    for line in "${rollover_log[@]}"; do
      echo "  $line"
    done
  fi
  if [[ -n "$run_dir" ]]; then
    echo ""
    echo "  runtime state: $run_dir"
  fi
}

print_not_reached() {
  # Everything still sitting in a queue this run was draining that is neither
  # complete (it would have left the folder), blocked, nor deferred. Usually
  # --max, or a repository that became unsafe partway through. Derived from the
  # filesystem at the end rather than tracked as it goes, so it cannot disagree
  # with what is actually there.
  local -a not_reached=()
  local alias_name app queued base label
  for alias_name in "${repo_targets[@]-}"; do
    [[ -n "$alias_name" ]] || continue
    app="${repo_prompt_dir[$alias_name]:-}"
    [[ -n "$app" ]] || continue
    for queued in "$data_root/LLM/prompts/$app/"*.md; do
      [[ -f "$queued" ]] || continue
      base=$(basename "$queued")
      label="$app/$base"
      [[ -n "${deferred_reason[$label]:-}" ]] && continue
      case " ${outcome_blocked[*]-} " in *" $label|"*) continue ;; esac
      not_reached+=("$label${repo_unsafe[$alias_name]:+ (${repo_unsafe[$alias_name]})}")
    done
  done
  (( ${#not_reached[@]} > 0 )) || return 0
  echo ""
  echo "  NOT REACHED — still queued when the run ended."
  for label in "${not_reached[@]}"; do
    echo "    $label"
  done
}

# ============================================================================
# EXPLICIT-LIST MODE
# ============================================================================

if [[ "$explicit_mode" == true ]]; then
  if (( ${#repo_targets[@]} != 1 )); then
    echo "error: an explicit prompt list runs against ONE repository; ${#repo_targets[@]} were named." >&2
    echo "       $0 --queue <repo> prompt1.md prompt2.md" >&2
    exit 2
  fi
  repo_name="${repo_targets[0]}"
  resolve_repo_root "$repo_name"; repo_root="$resolved_repo_root"
  prompt_files=("${rest_tokens[@]}")

  if [[ "$single_session" == true ]]; then
    echo "error: --single-session cannot run an explicit prompt list; an explicit list" >&2
    echo "       has always been one agent process per prompt." >&2
    exit 2
  fi

  if [[ "$dry_run" == true ]]; then
    prompt_directory=$(repo_queue_dir_name "$repo_name")
    handoff_directory=$(repo_queue_dir_name "$repo_name")
    if [[ "$agent" == "claude" ]]; then
      print_context_safe_plan "$repo_name" "$repo_root" "$prompt_directory" \
        "$handoff_directory" "${prompt_files[@]}"
    else
      echo ""
      echo "==================== $repo_name: explicit list (dry run, $agent) ===================="
      printf '  %s\n' "${prompt_files[@]}"
    fi
    exit 0
  fi

  echo ""
  echo "==================== $repo_name: explicit list (${#prompt_files[@]} prompts) ===================="

  # A HAND-PICKED ORDER STOPS AT THE FIRST FAILURE, and that is not the same
  # policy as the drain's on purpose. A drain infers its order and can honestly
  # reason about which of the remaining prompts a block costs; an explicit list
  # is somebody's assertion that THESE prompts run in THIS order, so the moment
  # one of them does not complete, the assertion behind the rest is void.
  prompt_directory=$(repo_queue_dir_name "$repo_name")
  repo_prompt_dir[$repo_name]="$prompt_directory"
  alias_of_app[$prompt_directory]="$repo_name"
  count=0
  for prompt_file in "${prompt_files[@]}"; do
    count=$((count + 1))
    echo ""
    echo "---- $repo_name: [$count/${#prompt_files[@]}] $prompt_file ----"
    run_one_prompt "$repo_name" "$prompt_file"
    if [[ "$prompt_outcome" == "complete" ]]; then
      outcome_complete+=("$prompt_directory/$prompt_file")
      continue
    fi
    record_blocked "$prompt_directory/$prompt_file" "${prompt_outcome:-unknown}" \
      "${prompt_outcome_detail:-did not complete}"
    echo ""
    echo "  $repo_name: $prompt_file did not complete, so the rest of this explicit"
    echo "  list is not attempted — the order you gave assumed it had."
    break
  done

  echo ""
  if (( prompts_blocked == 0 )); then
    echo "==================== $repo_name: all ${#prompt_files[@]} prompts complete ===================="
  fi
  print_run_summary
  if (( first_failure_status != 0 )); then
    exit "$first_failure_status"
  fi
  (( prompts_blocked > 0 )) && exit 1
  exit 0
fi

# ============================================================================
# --SINGLE-SESSION MODE: hand the whole queue to ONE agent session
# ============================================================================
# The old default. Kept, explicit, and documented as not context-safe: one
# conversation carries every prompt in the queue, which is readable and is
# exactly the accumulation the default now avoids.

if [[ "$single_session" == true ]]; then
  for repo_name in "${positional[@]}"; do
    resolve_repo_root "$repo_name"; repo_root="$resolved_repo_root"
    prompt_directory=$(repo_queue_dir_name "$repo_name")
    handoff_directory=$(repo_queue_dir_name "$repo_name")
    queue_message=$(single_session_queue_message "$prompt_directory" "$handoff_directory")

    if [[ "$dry_run" == true ]]; then
      printf '%s\n' "$queue_message"
      continue
    fi

    echo ""
    echo "==================== $repo_name ($prompt_directory): whole queue, one session ===================="
    echo ""
    echo "  NOT CONTEXT-SAFE: this hands the entire queue to a single $agent"
    echo "  context. For a long unattended queue, drop --single-session."
    echo ""
    "$run_agent" --agent "$agent" "$repo_name" "$queue_message"
  done
  exit 0
fi

# ============================================================================
# CODEX BARE MODE: unchanged — one session for the whole queue
# ============================================================================
# Context rollover is Claude Code's PreCompact lifecycle, and codex has no
# equivalent this script can hook, so nothing Claude-specific is ever pushed
# onto it. `--drain` still gives codex the deterministic per-prompt loop.

if [[ "$agent" != "claude" && "$drain" == false ]]; then
  for repo_name in "${positional[@]}"; do
    resolve_repo_root "$repo_name"; repo_root="$resolved_repo_root"
    prompt_directory=$(repo_queue_dir_name "$repo_name")
    handoff_directory=$(repo_queue_dir_name "$repo_name")
    queue_message=$(single_session_queue_message "$prompt_directory" "$handoff_directory")

    if [[ "$dry_run" == true ]]; then
      printf '%s\n' "$queue_message"
      continue
    fi

    echo ""
    echo "==================== $repo_name ($prompt_directory): whole queue, one session ===================="
    echo ""
    "$run_agent" --agent "$agent" "$repo_name" "$queue_message"
  done
  exit 0
fi

# ============================================================================
# THE DEFAULT: the context-safe queue
# ============================================================================
# ONE drain implementation, and every mode reaches it: bare `run-sequence.sh`
# (every configured repository), `--drain` (the same thing, spelled out),
# `--queue <repo>` and the positional `<repo>` compatibility form (a subset of
# repositories). The only difference between them is the contents of
# `repo_targets`, decided once at the top of this file.
#
# THE SHAPE OF THE LOOP: repositories in order, each drained as far as it can go,
# and then — only if something completed and something is still deferred — around
# again. The inner order is what `run-sequence.sh api web` has always meant (api's
# queue, then web's) and is deliberately unchanged. The outer pass is what makes a
# CROSS-REPOSITORY prerequisite work: web's prompt requires an api prompt that had
# not run yet when web was first visited, so web is revisited once api is done.
#
# NOTHING HERE DELETES, MOVES OR FABRICATES ANYTHING IN THE QUEUE. A deferred
# prompt is a stem in `skip_stems` and a line in the summary; on disk it is
# exactly where its author put it.

for repo_name in "${repo_targets[@]}"; do
  resolve_repo_root "$repo_name"; repo_root="$resolved_repo_root"
  prompt_directory=$(repo_queue_dir_name "$repo_name")
  repo_prompt_dir[$repo_name]="$prompt_directory"
  alias_of_app[$prompt_directory]="$repo_name"
done

if [[ "$dry_run" == true ]]; then
  if [[ "$drain_all" == true ]]; then
    echo ""
    echo "DEFAULT MODE: DRAIN (dry run)"
    echo "  repositories: ${repo_targets[*]}"
  fi
  for repo_name in "${repo_targets[@]}"; do
    resolve_repo_root "$repo_name"; repo_root="$resolved_repo_root"
    prompt_directory=$(repo_queue_dir_name "$repo_name")
    handoff_directory=$(repo_queue_dir_name "$repo_name")
    if [[ "$agent" == "claude" ]]; then
      print_context_safe_plan "$repo_name" "$repo_root" "$prompt_directory" "$handoff_directory"
    else
      echo ""
      echo "==================== $repo_name: deterministic drain (dry run, $agent) ===================="
      echo "  prompt queue: $data_root/LLM/prompts/$prompt_directory/"
    fi
  done
  exit 0
fi

init_run_state

# §15's no-argument safety: the bare form is powerful, so it says what it is
# about to do before it does it — and then proceeds, because the entire point is
# unattended operation. Ctrl-C remains the operator's stop.
if [[ "$drain_all" == true ]]; then
  eligible_total=0
  for repo_name in "${repo_targets[@]}"; do
    prompt_directory="${repo_prompt_dir[$repo_name]}"
    for queued_path in "$data_root/LLM/prompts/$prompt_directory/"*.md; do
      [[ -f "$queued_path" ]] && eligible_total=$((eligible_total + 1))
    done
  done
  echo ""
  echo "=========================================================================="
  echo "DEFAULT MODE: DRAIN"
  echo "  eligible prompts: $eligible_total"
  echo "  repositories:     ${repo_targets[*]}"
  echo "  agent:            $agent, one FRESH session per prompt"
  echo "  completion:       every mutation target back on \`$target_branch\`, clean"
  echo "  blocked policy:   defer what depends on it, continue the rest"
  echo "  state:            $run_dir"
  echo "=========================================================================="
fi

declare -A repo_counts=()
declare -A repo_last_candidate=()
declare -A repo_drained=()

while true; do
  pass_completed=0

  for repo_name in "${repo_targets[@]}"; do
    [[ -n "$catastrophic_reason" ]] && break
    [[ -n "${repo_unsafe[$repo_name]:-}" ]] && continue
    [[ -n "${repo_drained[$repo_name]:-}" ]] && continue

    resolve_repo_root "$repo_name"; repo_root="$resolved_repo_root"
    prompt_directory="${repo_prompt_dir[$repo_name]}"
    handoff_directory=$(repo_queue_dir_name "$repo_name")

    if [[ -z "${repo_counts[$repo_name]:-}" ]]; then
      repo_counts[$repo_name]=0
      echo ""
      if [[ "$agent" == "claude" ]]; then
        echo "==================== $repo_name ($prompt_directory): context-safe queue ===================="
        echo "  one fresh $agent session per prompt; automatic compaction is intercepted"
        echo "  and becomes a checkpoint + fresh session on the SAME prompt."
        echo "  up to $max_context_rollovers context rollovers per prompt · state in $run_dir"
      else
        echo "==================== draining $repo_name ($prompt_directory) ===================="
      fi
    fi

    while true; do
      [[ -n "$catastrophic_reason" ]] && break
      if [[ -n "${repo_unsafe[$repo_name]:-}" ]]; then
        break
      fi
      if [[ -n "$max_tasks" && "${repo_counts[$repo_name]}" -ge "$max_tasks" ]]; then
        echo "$repo_name: reached --max $max_tasks prompts, stopping this repo's loop."
        repo_drained[$repo_name]=max
        break
      fi

      build_resolver_skip_args "$repo_name"
      state=$(python3 "$resolver" --repo "$repo_name" --data-root "$data_root" \
        "${resolver_skip_args[@]}" --json)
      action=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])' <<<"$state")

      if [[ "$action" == "idle" ]]; then
        reason=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason",""))' <<<"$state")
        echo "$repo_name: $reason. Done."
        repo_drained[$repo_name]=idle
        break
      fi

      if [[ "$action" == "error" ]]; then
        reason=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason",""))' <<<"$state")
        mark_repo_unsafe "$repo_name" "the resolver could not read this queue: $reason"
        break
      fi

      if [[ "$action" == "blocked" ]]; then
        # THE RESOLVER'S OWN "blocked" IS NOT A PROMPT THAT FAILED. It means the
        # oldest selectable prompt has a prerequisite that is not in done/ —
        # unmet, parked in blocked/, or (the completed-but-not-moved guard) an
        # inconsistency needing a human. None of those is a reason to abandon the
        # prompts BEHIND it, so the candidate is deferred and the loop asks for
        # the next one. Before AUT-04 this line exited the whole run.
        reason=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason",""))' <<<"$state")
        candidate=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("candidate",""))' <<<"$state")
        if [[ -z "$candidate" ]]; then
          mark_repo_unsafe "$repo_name" "$reason"
          break
        fi
        defer_prompt "$repo_name" "$prompt_directory/$candidate" "${candidate%.md}" "$reason" prereq
        continue
      fi

      # action == "run"
      prompt_path=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["prompt_path"])' <<<"$state")
      prompt_file=$(basename "$prompt_path")

      if [[ "$prompt_file" == "${repo_last_candidate[$repo_name]:-}" ]]; then
        mark_repo_unsafe "$repo_name" \
          "$prompt_file was just run and the resolver still offers it — refusing to loop"
        break
      fi
      repo_last_candidate[$repo_name]="$prompt_file"

      repo_counts[$repo_name]=$(( ${repo_counts[$repo_name]} + 1 ))
      echo ""
      echo "---- $repo_name: prompt #${repo_counts[$repo_name]} — $prompt_file ----"
      run_one_prompt "$repo_name" "$prompt_file"

      case "$prompt_outcome" in
        complete)
          outcome_complete+=("$prompt_directory/$prompt_file")
          pass_completed=$((pass_completed + 1))
          ;;
        blocked)
          apply_block_impact "$repo_name" "$prompt_directory" "$prompt_file" \
            "$data_root/LLM/handoffs/$handoff_directory/$prompt_file"
          ;;
        repo-unsafe)
          record_blocked "$prompt_directory/$prompt_file" "repository" "$prompt_outcome_detail"
          mark_repo_unsafe "$repo_name" "$prompt_outcome_detail"
          ;;
        catastrophic)
          record_blocked "$prompt_directory/$prompt_file" "catastrophic" "$prompt_outcome_detail"
          ;;
      esac
      write_run_state "running"
    done
  done

  [[ -n "$catastrophic_reason" ]] && break
  # Another pass only buys something when this one COMPLETED work and something
  # is still deferred for a reason a completion could have changed — a
  # cross-repository prerequisite that was still queued the first time round.
  # Without both, a second pass would re-derive exactly the same answers.
  (( pass_completed > 0 )) || break
  retry_possible=false
  for deferred_label in "${deferred_order[@]-}"; do
    [[ -n "$deferred_label" ]] || continue
    [[ "${deferred_kind[$deferred_label]}" == "prereq" ]] && retry_possible=true
  done
  [[ "$retry_possible" == true ]] || break

  # Drop ONLY the prerequisite deferrals and ask the resolver again — it
  # re-derives everything from the filesystem, so nothing here has to model what
  # "satisfied" means. Block-caused deferrals survive untouched: no completion in
  # this run can unblock the prompt that caused them.
  declare -a kept_order=()
  for repo_name in "${repo_targets[@]}"; do
    skip_stems[$repo_name]=""
    unset 'repo_drained[$repo_name]'
    unset 'repo_last_candidate[$repo_name]'
  done
  prompts_deferred=0
  for deferred_label in "${deferred_order[@]-}"; do
    [[ -n "$deferred_label" ]] || continue
    if [[ "${deferred_kind[$deferred_label]}" == "prereq" ]]; then
      unset 'deferred_reason[$deferred_label]'
      unset 'deferred_kind[$deferred_label]'
      continue
    fi
    kept_order+=("$deferred_label")
    prompts_deferred=$((prompts_deferred + 1))
    deferred_app="${deferred_label%%/*}"
    deferred_alias="${alias_of_app[$deferred_app]:-}"
    if [[ -n "$deferred_alias" ]]; then
      deferred_stem="${deferred_label##*/}"
      deferred_stem="${deferred_stem%.md}"
      skip_stems[$deferred_alias]="${skip_stems[$deferred_alias]:+${skip_stems[$deferred_alias]}$'\n'}$deferred_stem"
    fi
  done
  deferred_order=("${kept_order[@]-}")
done

if [[ -n "$catastrophic_reason" ]]; then
  echo ""
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "  SEQUENCE STOPPED — CATASTROPHIC BLOCK"
  echo ""
  echo "  $catastrophic_reason"
  echo ""
  echo "  This is the ultima ratio, and it means continuing could not have"
  echo "  produced trustworthy results: a repository whose history cannot be"
  echo "  trusted, a shared contract left half-written across repositories, a"
  echo "  corruption risk, or a compaction that defeated the rollover guard."
  echo "  Every prompt not yet attempted is left exactly where it was."
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  write_run_state "catastrophic"
  print_run_summary
  exit 1
fi

write_run_state "finished"
print_run_summary

# 0 only when nothing blocked and no agent failed. A drain that deferred work is
# still a successful drain — deferral is this design working, not failing.
if (( first_failure_status != 0 )); then
  exit "$first_failure_status"
fi
(( prompts_blocked > 0 )) && exit 1
exit 0
