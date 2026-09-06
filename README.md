# ai-gearbox

Shift work in progress from one AI CLI to another without losing context or processes.
It understands three CLIs, each with its own license: `claude` (Claude Code), `codex` (Codex CLI)
and `agy` (Google's Antigravity CLI).

It is not a single terminal with the three providers inside. Anthropic bars subscription use from
third-party interfaces and Google forbids using Antigravity outside its own products. So each official
CLI keeps talking to its provider, and `gearbox` only moves the work between them.

## Usage

```bash
gearbox codex                  # from the latest session in this folder to Codex
gearbox agy --from claude      # pin the source CLI
gearbox claude --session 7cdc  # a specific session, by id prefix
gearbox codex --dry-run        # print the handoff and launch nothing
gearbox --list                 # sessions in this folder, newest first
gearbox --list --all           # every folder
gearbox --read agy 7cdc140e    # dump a full session as readable text
gearbox --bg make render       # a process that survives switching CLIs
gearbox --jobs                 # background processes and their logs
gearbox --loop claude          # one tab all day (see below)
```

## One tab

`gearbox --loop claude` opens Claude Code and keeps the tab. From there you switch in two ways:

- **From inside the chat**: type `gearbox codex` inside the CLI. In Claude Code use `!gearbox codex`;
  Codex and agy have no shell prefix, so the loop tells them on launch, and every handoff repeats it, that
  a bare `gearbox <cli>` is a command to run at once. gearbox recognizes the CLI it runs inside, closes it,
  and the loop opens Codex in the same tab with the handoff of the session just closed.
- **On exit**: leave the CLI with `/exit` or Ctrl+C and answer the question `Switch to?`.

Enter or `quit` at that question ends the loop. `gearbox --loop codex --from agy` starts the loop with a
handoff already on board. Outside a loop, `gearbox codex` typed inside a CLI does not switch: the CLI owns
the terminal, so gearbox tells you to exit it first.

Typing `gearbox codex` into the chat as plain text does nothing: the assistant reads it as a message.

## What travels

A handoff of at most 9,000 characters, about 2,200 tokens, with:

- the task, the user's first real message;
- the current state, the source assistant's last reply;
- the user's last six messages;
- the files touched and the recent commands;
- the path of the full session, so the target can read the detail with `gearbox --read` if it needs to.

A previous handoff found inside the conversation is replaced by `[previous handoff omitted]`,
so successive switches do not nest. Every handoff is saved under `~/.gearbox/handoffs/`.

## What does not travel

The literal conversation, the permissions, the hooks and the agents of the source CLI. The switch
happens between replies, never in the middle of one.

## Where it reads from

| CLI | Sessions |
|---|---|
| claude | `~/.claude/projects/<folder>/<id>.jsonl` |
| codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| agy | `~/.gemini/antigravity-cli/conversations/<id>.db` (SQLite with protobuf steps) |

## Install and test

```bash
./install.sh                   # links ~/.local/bin/gearbox and runs the tests
python3 -m unittest -v test_gearbox
python3 mutants.py             # mutant bank: every mutant must turn at least one test red
```

It only needs the system's Python 3. The tests create synthetic sessions of the three formats in a
temporary folder and include negative controls: a folder with no sessions, an unreadable session,
broken protobuf and a fake target executable.
