#!/usr/bin/env bash
set -euo pipefail

# ONE help function, and everything that prints usage goes through it. A header
# comment saying one syntax while --help says another is exactly the drift §18
# of the AUT-04 prompt forbids, so there is no second copy of this text anywhere
# in this file.
print_usage() {
  cat <<'HELPTEXT'
run-agent.sh — drive ONE prompt for ONE repository through claude or codex.

USAGE
  run-agent.sh [--agent claude|codex] <repo-alias> [selector] [agent arguments...]
  run-agent.sh --help
  run-agent.sh --version

REPOSITORY ALIASES (case-insensitive)
  Every alias comes from this toolkit's workspace.json, and there is no second
  table anywhere in this script. `run-agent.sh --repos` lists the ones this
  workspace configures, with their paths.

SELECTOR — what this session is asked to do. Pick one, or none.
  (nothing)            the next prompt scripts/resolve_next_prompt.py picks: the
                       OLDEST file still directly in the repo's queue folder
                       whose prerequisites are all in done/.
  <prompt>.md          that exact prompt, skipping automatic selection. The
                       FILENAME as it appears in the queue folder, not a path.
  "verbatim message"   sent to the agent unchanged: no resolver, no prompt
                       lookup, no wrapper instructions bolted on.
  --interactive        a plain session with nothing injected at all.
  --dry-run            resolve everything — repo root, data root, which prompt
                       would run, the exact message that would be sent — print it
                       as JSON and exit. Starts no agent and writes nothing, so
                       it is safe against the real, populated prompt queues.

OPTIONS
  --agent claude|codex which agent to drive (default: claude)
  --repos              list the configured aliases and their paths, then exit.
                       Read-only: nothing else is resolved or written.
  -h, --help           this text, on stdout, exit 0, nothing else happens
  Any other argument is passed straight through to the agent CLI, so
  `run-agent.sh aub --permission-mode plan` overrides the default auto mode.

ENVIRONMENT
  AGENT_PRINT=1        drive claude with -p and streamed progress instead of the
                       native TUI. Set automatically when stdout is not a
                       terminal (CI, a pipe, a log file).
  AGENT_STREAM=0       with AGENT_PRINT, go back to silent -p output.
  AUTOKIT_WORKSPACE_CONFIG  the workspace.json to use, when it is not the one at
                       this toolkit's root. The ONE path override there is.
  AUTOKIT_TARGET_BRANCH     the branch a prompt must be worked on (default: main)
  AUTOKIT_BRANCH_POLICY=off skip the branch preflight below entirely

THE MAIN-BRANCH RULE
  A prompt is not finished until every repository it mutates is back on main,
  clean, with its commits reachable from main. Before a prompt starts, this
  script puts that prompt's declared mutation targets onto main when it can do
  so without leaving work behind, and REFUSES — naming the repository, the
  branch and the commits — when it cannot. It never resets, stashes,
  force-deletes a branch, or pushes. run-sequence.sh additionally checks the
  same thing after the agent stops.

EXAMPLES
  run-agent.sh api
  run-agent.sh api 20260819_103_Add-Session-Endpoint.md
  run-agent.sh --agent codex web
  run-agent.sh web --dry-run
  run-agent.sh worker "execute 29, 30, 31, 32"
  run-agent.sh api --interactive

  The legacy form 'run-agent.sh <claude|codex> <repo> ...' still works.

SEE ALSO
  run-sequence.sh --help   drain whole queues, one fresh session per prompt
HELPTEXT
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
  print_usage
  exit 0
fi

# --repos: WHAT THIS WORKSPACE IS CONFIGURED WITH, and nothing else.
#
# No side effects: no prompt is resolved, no handoff read, no run state written, no agent started.
# Handled before every other argument so it keeps working in a workspace too broken to resolve
# anything — which is exactly when an operator needs to see the alias table.
#
# The source toolkit had a `--version` here reporting each product component's build. That is a
# product operation rather than a workflow one, and it left with the rest of them.
#
# `readlink -f` first: this may be invoked through a symlink, and `dirname` on a symlink gives the
# LINK's directory rather than this repository's.
if [[ ${1:-} == "--repos" ]]; then
  exec python3 "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/scripts/workspace_config.py" --print aliases
fi

usage() {
  print_usage >&2
  exit 2
}

# The agent is a flag with a default, matching run-sequence.sh's shape, rather
# than a required positional nobody ever varied. The old leading
# `claude`/`codex` positional is still accepted so every existing invocation,
# script and habit keeps working — no repo alias is spelled `claude` or
# `codex`, so the two forms can never be confused.
agent_name="claude"
if [[ ${1:-} == "--agent" ]]; then
  [[ $# -ge 2 ]] || usage
  agent_name=$2
  shift 2
elif [[ ${1:-} == --agent=* ]]; then
  agent_name="${1#--agent=}"
  shift
elif [[ ${1:-} == "claude" || ${1:-} == "codex" ]]; then
  agent_name=$1
  shift
fi

[[ $# -ge 1 ]] || usage
repo_name=$1
shift

case "$agent_name" in
  claude|codex) ;;
  *) usage ;;
esac

# tools_dir is where this script (and its scripts/ subfolder) actually live: the
# toolkit checkout. Repository paths are NOT derived from it — they come from
# workspace.json, which is why a repository may sit anywhere the operator put it
# rather than having to be a sibling of this directory. The source toolkit
# assumed `<workspace>/<repo-directory>` here, and that assumption is the whole
# reason it could serve exactly one product.
#
# `readlink -f` first, because this may be invoked through a symlink and
# `dirname` on a symlink gives the *link's* directory, not this file's.
tools_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
config_tool="$tools_dir/scripts/workspace_config.py"

command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found on PATH" >&2; exit 2; }
[[ -f "$config_tool" ]] || { echo "missing workspace resolver: $config_tool" >&2; exit 2; }

# ONE alias table, and it is the configuration's. Resolved here so an unknown
# alias fails naming the configured list rather than with a shrug, and so every
# path below is a quoted value that came from workspace.json.
repo_alias=$(python3 "$config_tool" --alias "$repo_name") || exit 2
repo_root=$(python3 "$config_tool" --path "$repo_name") || exit 2

router="$tools_dir/scripts/agent_task.py"
resolver="$tools_dir/scripts/resolve_next_prompt.py"

[[ -d "$repo_root" ]] || { echo "missing repository: $repo_root" >&2; exit 2; }
[[ -f "$router" ]] || { echo "missing task router: $router" >&2; exit 2; }
[[ -f "$resolver" ]] || { echo "missing prompt resolver: $resolver" >&2; exit 2; }

data_root=$(python3 "$config_tool" --print data-root) || exit 2
# The executable for this agent, from `agents.<name>.command`. A disabled agent
# fails here, before anything is resolved, rather than at exec time with a
# command-not-found nobody can trace back to the configuration.
agent_command=$(python3 "$config_tool" --print agent-command --agent "$agent_name") || exit 2
# Exported for the agent: every instruction this script writes addresses prompts
# and handoffs as $WORKSPACE_DATA_ROOT/LLM/..., so a path can be pasted straight
# out of the message into a shell.
export WORKSPACE_DATA_ROOT="$data_root"

# Identify this window at a glance. The alias IS the label — a second table
# mapping repositories to display names is one more thing that can disagree with
# workspace.json. Only sent when stdout is an actual terminal, so a pipe or a
# redirect does not pick up a stray escape sequence. Supported by essentially
# every terminal emulator (xterm, iTerm2, gnome-terminal, Windows Terminal,
# kitty, VS Code's integrated terminal); under tmux/screen it additionally needs
# `set -g set-titles on` in tmux.conf to pass through.
if [ -t 1 ]; then
  printf '\033]0;%s · %s\007' "$repo_alias" "$agent_name"
fi

# TARGET BRANCH = main. Overridable for an unusual checkout, never defaulted away
# from: it is the branch a prompt's commits must be reachable from before the
# prompt counts as finished.
target_branch="${AUTOKIT_TARGET_BRANCH:-main}"

interactive=false
if [[ ${1:-} == "--interactive" ]]; then
  interactive=true
  shift
fi

# --dry-run: resolve everything (repo paths, data root, which prompt would
# run) and print it, then exit WITHOUT invoking claude/codex and without
# writing anything. Safe to run against a real, populated prompt queue to
# verify resolution — this is the intended way to check this script against
# real prompts without actually executing any of them.
dry_run=false
if [[ "$interactive" == false && ${1:-} == "--dry-run" ]]; then
  dry_run=true
  shift
fi

# Explicit prompt selection: `run-agent.sh claude auc some-prompt.md` runs
# that specific prompt instead of letting the resolver pick the next one.
# Only recognized in non-interactive mode — interactive mode hands off to
# a raw shell with no injected prompt at all, so "which prompt" doesn't
# apply there. Detected by a bare (non-flag) argument ending in `.md`,
# consumed here so it never reaches the underlying agent CLI as a stray
# positional argument.
explicit_prompt_file=""
if [[ "$interactive" == false && "${1:-}" == *.md && "${1:-}" != -* ]]; then
  explicit_prompt_file=$1
  shift
fi

# Verbatim message: `run-agent.sh claude aue "execute 29, 30, 31, 32"` sends
# that text to the agent as-is, with no resolver, no prompt-file lookup and
# no wrapper instructions bolted on. This is the escape hatch for driving
# several prompts in ONE session, by hand: one session told "execute 29, 30,
# 31, 32" walks the list itself and keeps one context across all of them.
#
# It is no longer the way to get the TUI out of an auto-advancing run.
# run-sequence.sh's default queue is now both native-TUI and one fresh session
# per prompt — it supervises the interactive session with a Stop hook rather
# than making the agent print. See run-sequence.sh's header.
#
# Detected as a bare argument that is neither a flag nor a .md filename, so
# it can never collide with agent arguments (always `-`-prefixed) or with
# explicit prompt selection (always `.md`).
verbatim_message=""
if [[ "$interactive" == false && -z "$explicit_prompt_file" && -n "${1:-}" && "${1:-}" != -* ]]; then
  verbatim_message=$1
  shift
fi

if [[ "$interactive" == false && -n "$verbatim_message" ]]; then
  initial_prompt="$verbatim_message"
  if [[ "$dry_run" == true ]]; then
    python3 - "$repo_alias" "$repo_root" "$data_root" "$initial_prompt" <<'PY'
import json, sys
repo_alias, repo_root, data_root, initial_prompt = sys.argv[1:5]
print(json.dumps({
    "repo": repo_alias,
    "repo_root": repo_root,
    "data_root": data_root,
    "resolved_prompt": None,
    "selection": "verbatim message (passed through unchanged)",
    "initial_prompt": initial_prompt,
}, indent=2))
PY
    exit 0
  fi
elif [[ "$interactive" == false ]]; then
  # The queue folder is the configured alias. The source toolkit read this from
  # a per-repository configuration file committed inside each checkout, which
  # let a repository disagree with the workspace about which queue was its own.
  prompt_directory="$repo_alias"

  auto_selected=false
  if [[ -z "$explicit_prompt_file" ]]; then
    # Automatic mode: ask resolve_next_prompt.py for the next runnable
    # prompt — the OLDEST file still sitting directly in the repo's prompt
    # folder (finished ones have been moved into its done/ subfolder), with
    # its prerequisite chain checked recursively against done/ presence.
    # This is deliberately the same resolver run-sequence.sh already uses.
    # It is NOT the same as `agent_task.py status`/`session-start`, which
    # resolve to the newest prompt in the folder and silently skip
    # everything behind it the moment more than one prompt is queued —
    # exactly the failure mode WORKFLOW.md documents and this run-agent.sh
    # used to have here. See the toolkit's WORKFLOW.md.
    state=$(python3 "$resolver" --repo "$repo_alias" --data-root "$data_root" --json)
    action=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["action"])' <<<"$state")
    reason=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason",""))' <<<"$state")
    if [[ "$action" == "blocked" || "$action" == "idle" || "$action" == "error" ]]; then
      echo "$repo_name: $reason"
      exit 0
    fi
    prompt_path=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["prompt_path"])' <<<"$state")
    explicit_prompt_file=$(basename "$prompt_path")
    auto_selected=true
  fi

  prompt_queue_dir="$data_root/LLM/prompts/$prompt_directory"
  explicit_prompt_path="$prompt_queue_dir/$explicit_prompt_file"
  if [[ ! -f "$explicit_prompt_path" ]]; then
    # Distinguish "already finished" from "never existed" — under the done/
    # model an explicitly named prompt that has been completed is simply no
    # longer in the queue folder, and reporting that as "not in the prompt
    # folder" would read as a typo.
    #
    # AND NAME THE PATH THAT WAS ACTUALLY CHECKED. "that prompt is not in the
    # aub prompt folder" was reported by an operator looking straight at the
    # prompt in the folder, and they were right: a second, empty data directory
    # had displaced the real data root, so the script was truthfully describing
    # a folder nobody meant. One configured `data_root` is what retired that
    # failure; naming the resolved path is what makes the next one one look long. A claim about the filesystem the reader cannot
    # falsify from the terminal is worse than no claim — print the absolute
    # path, and the resolved root it hangs off, so the next such report takes
    # one look instead of a session.
    if [[ -f "$prompt_queue_dir/done/$explicit_prompt_file" ]]; then
      echo "$repo_name: $explicit_prompt_file is already complete (it is in $prompt_queue_dir/done/). Move it back out to re-run it."
    elif [[ -f "$prompt_queue_dir/blocked/$explicit_prompt_file" ]]; then
      echo "$repo_name: $explicit_prompt_file is parked as blocked (it is in $prompt_queue_dir/blocked/). It needs a rewrite or a human decision, then move it back into the queue folder."
    else
      echo "$repo_name: $explicit_prompt_file is not in $prompt_queue_dir/"
      echo "  (data root: $data_root — fix data_root in workspace.json if that is not the tree you meant)"
      if [[ ! -d "$prompt_queue_dir" ]]; then
        echo "  That queue folder does not exist at all, which usually means the data root above is wrong."
      fi
    fi
    exit 0
  fi

  # The closing instruction is the same in both branches on purpose: finishing
  # a prompt means writing its handoff AND moving the prompt file into done/.
  # That move is what marks it complete — nothing else does — so it cannot be
  # left implicit in the message that starts the task.
  # THE BRANCH RULE IS RUNNER-OWNED. It is stated here, once, in the message
  # every prompt is dispatched with, rather than copied into hundreds of prompt
  # files — which is also why no existing prompt needed editing when it was
  # frozen. It is stated to the agent AND checked afterwards by run-sequence.sh;
  # neither half is sufficient alone, because an instruction can be forgotten and
  # a check that arrives with no warning wastes a whole attempt.
  branch_rule="BRANCH POLICY (this comes from the runner, not from the prompt): work directly on $target_branch. Do not create or switch to a feature branch, and do not leave a detached HEAD. Before you declare this prompt complete: return every repository you changed to $target_branch, make sure the commits you made are reachable from $target_branch, and leave every working tree clean. Do not push; local $target_branch is the completion authority."
  finish_rule="When the task is genuinely finished, write its handoff at \$WORKSPACE_DATA_ROOT/LLM/handoffs/$prompt_directory/$explicit_prompt_file and then, as your last step, move the prompt file itself into \$WORKSPACE_DATA_ROOT/LLM/prompts/$prompt_directory/done/ (create that folder if needed). Moving it is what marks it complete — a prompt still in the queue folder is still outstanding. If it is genuinely blocked, record status blocked in the handoff and move the prompt into ../blocked/ instead, then stop. $branch_rule"
  if [[ "$auto_selected" == true ]]; then
    initial_prompt="Work on the prompt at \$WORKSPACE_DATA_ROOT/LLM/prompts/$prompt_directory/$explicit_prompt_file — it was selected automatically as the oldest outstanding prompt with satisfied prerequisites (scripts/resolve_next_prompt.py). Follow the normal task lifecycle from the toolkit's AGENTS.md and WORKFLOW.md: read the newest handoff first, and check whether a handoff for this prompt already exists (it may be a resume). $finish_rule"
  else
    initial_prompt="Work on the prompt at \$WORKSPACE_DATA_ROOT/LLM/prompts/$prompt_directory/$explicit_prompt_file specifically — it was explicitly selected for this run, skip the resolver's automatic oldest-outstanding selection. Otherwise follow the normal task lifecycle from the toolkit's AGENTS.md and WORKFLOW.md: check its prerequisites, and check whether a handoff for it already exists before starting. $finish_rule"
  fi

  # ---------------------------------------------------------------------
  # FRONTMATTER PREFLIGHT — read-only, and it runs before anything else looks
  # at this prompt's declarations
  # ---------------------------------------------------------------------------
  # `20260825_AUT-08`. Everything below reads the same frontmatter block: the
  # branch policy asks it which repositories may be written, and the resolver
  # asks it what has to be in `done/` first. A block the canonical decoder
  # cannot read has UNKNOWN answers to both, and the failure it replaces was
  # silent — an unreadable `requires:` list arriving as no requirements at all.
  # The error names the file and the line, so this is a one-line fix rather
  # than an investigation.
  if ! frontmatter_error=$(python3 "$tools_dir/scripts/prompt_frontmatter.py" \
                             lint "$explicit_prompt_path" 2>&1 >/dev/null); then
    echo "" >&2
    echo "$repo_name: refusing to start $explicit_prompt_file — its frontmatter" >&2
    echo "  cannot be decoded, so its prerequisites and mutation targets are unknown." >&2
    echo "$frontmatter_error" >&2
    exit 1
  fi

  # ---------------------------------------------------------------------
  # BRANCH PREFLIGHT — put the prompt's mutation targets on the target branch
  # ---------------------------------------------------------------------------
  # Read from the prompt's own frontmatter (`repo`, `mutation_targets`,
  # `touches`), and always including the repository this run is for, because a
  # prompt that declares nothing still mutates its own.
  #
  # `--apply` is a `git checkout` of an existing branch and nothing else. The
  # refusal cases — commits the target cannot reach, a dirty tree that would
  # have to be stashed to move — print the repository, the branch and the
  # commits and stop before an agent starts, which is the whole point: a prompt
  # begun on the wrong branch is far cheaper to stop than to reconcile.
  if [[ "${AUTOKIT_BRANCH_POLICY:-on}" != "off" && "$dry_run" == false ]]; then
    branch_targets=("--repo" "$repo_alias")
    while IFS= read -r declared_target; do
      [[ -n "$declared_target" && "$declared_target" != "$repo_alias" ]] || continue
      branch_targets+=("--repo" "$declared_target")
    done < <(python3 "$tools_dir/scripts/branch_policy.py" targets \
               --prompt "$explicit_prompt_path" 2>/dev/null || true)
    if ! python3 "$tools_dir/scripts/branch_policy.py" preflight \
         --target-branch "$target_branch" --apply \
         "${branch_targets[@]}"; then
      echo "" >&2
      echo "$repo_name: refusing to start $explicit_prompt_file — a mutation target is not on" >&2
      echo "  \`$target_branch\` and cannot be moved there without leaving work behind." >&2
      echo "  Nothing was reset, stashed or deleted. Land or delete that branch yourself," >&2
      echo "  or re-run with AUTOKIT_BRANCH_POLICY=off if you know better." >&2
      exit 1
    fi
  fi

  if [[ "$dry_run" == true ]]; then
    python3 - "$repo_alias" "$repo_root" "$data_root" "$prompt_directory" "$explicit_prompt_file" "$auto_selected" "$initial_prompt" <<'PY'
import json, sys
(repo_alias, repo_root, data_root, prompt_directory,
 prompt_file, auto_selected, initial_prompt) = sys.argv[1:8]
print(json.dumps({
    "repo": repo_alias,
    "repo_root": repo_root,
    "data_root": data_root,
    "resolved_prompt": "LLM/prompts/" + prompt_directory + "/" + prompt_file,
    "selection": "automatic (oldest incomplete, prerequisites satisfied)" if auto_selected == "true" else "explicit (given on the command line)",
    "initial_prompt": initial_prompt,
}, indent=2))
PY
    exit 0
  fi
fi

cd "$repo_root"
if [[ "$agent_name" == "claude" ]]; then
  # --permission-mode auto is the default here since repo-level
  # .claude/settings.json can't grant itself auto mode (Claude Code ignores
  # defaultMode: "auto" from a repository's own settings file) — the flag is
  # the only reliable way to make this the standing default across repos.
  # Only added when the caller hasn't already specified their own mode, so
  # `run-agent.sh claude aub --permission-mode plan` still overrides it.
  claude_args=("$@")
  has_permission_mode=false
  for arg in "${claude_args[@]}"; do
    case "$arg" in
      --permission-mode|--permission-mode=*) has_permission_mode=true ;;
    esac
  done
  if [[ "$has_permission_mode" == false ]]; then
    claude_args=(--permission-mode auto "${claude_args[@]}")
  fi

  if [[ "$interactive" == true ]]; then
    exec "$agent_command" "${claude_args[@]}"
  fi

  # LAUNCH MODE. The default is the real interactive TUI, pre-seeded with
  # $initial_prompt — same rendering you get from typing `claude` yourself
  # (syntax highlighting, tool cards, diffs, progress). `-p` throws all of
  # that away and emits flat text, which is unreadable for a real task.
  #
  # -p is therefore used ONLY when there is nothing to render into: a caller
  # that explicitly asks with AGENT_PRINT=1, or a stdout that is not a terminal
  # (a pipe, a log file, CI). It is NOT how unattended runs are driven any more.
  # run-sequence.sh's context-safe queue keeps the TUI and learns that a turn
  # ended from Claude Code's Stop hook instead; it sets AGENT_PRINT=1 only on
  # its own headless fallback, for the same no-terminal reason.
  #
  # The interactive CLI still does not exit by itself at the end of a turn, and
  # gained no flag to make it. Nothing below claims otherwise — what changed is
  # that a supervisor no longer needs it to.
  print_mode=false
  if [[ "${AGENT_PRINT:-0}" == "1" || ! -t 1 ]]; then
    print_mode=true
  fi

  if [[ "$print_mode" == false ]]; then
    exec "$agent_command" "${claude_args[@]}" "$initial_prompt"
  fi

  # -p/--print: non-interactive mode. Without it, `claude "$initial_prompt"`
  # opens an interactive session pre-seeded with that message and then waits
  # for more input -- it does not exit on its own when the task is done. Under
  # run-sequence.sh that waiting session is closed by its Stop-hook watcher;
  # a caller landing HERE has no terminal, so -p is simply the only mode left.
  # Deliberately NOT using --bare here: it skips locally-configured state,
  # which for a browser/subscription login includes the session credentials
  # themselves -- adding it caused "Not logged in" on every run. -p alone
  # is sufficient for the exit-on-completion behavior this needs.
  #
  # PROGRESS VISIBILITY. Plain `-p` prints absolutely nothing until the whole
  # task finishes, so a run that is working perfectly looks identical to a
  # hung one. That is not cosmetic: a healthy run was Ctrl-C'd 19 seconds
  # in precisely because the terminal showed nothing after the "task #1"
  # banner. Stream the transcript instead and render one line per assistant
  # message and tool call as they arrive. Set AGENT_STREAM=0 to get the old
  # silent behavior back.
  #
  # This cannot use `exec`, since the output goes through a formatter, so the
  # agent's own exit status is recovered from PIPESTATUS and re-raised --
  # run-sequence.sh relies on a nonzero status to tell an interrupted or
  # failed run apart from one that genuinely finished without a handoff.
  if [[ "${AGENT_STREAM:-1}" == "0" ]]; then
    exec "$agent_command" -p "${claude_args[@]}" "$initial_prompt"
  fi

  set +e
  "$agent_command" -p --verbose --output-format stream-json "${claude_args[@]}" "$initial_prompt" \
    | python3 "$tools_dir/scripts/stream_progress.py"
  agent_status=${PIPESTATUS[0]}
  set -e
  exit "$agent_status"
else
if [[ "$interactive" == true ]]; then
    exec "$agent_command" --add-dir "$repo_root" --add-dir "$data_root" "$@"
  fi
  # The repository being worked and the data root that holds its queue and its
  # handoffs. NOT a whole workspace directory: with repositories configurable to
  # arbitrary paths there is no single parent to hand over, and handing an agent
  # every sibling of one checkout was never something this needed to do.
  exec "$agent_command" --add-dir "$repo_root" --add-dir "$data_root" "$@" "$initial_prompt"
fi
