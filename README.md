# claude-limit-watch

Resume Claude Code sessions that are stuck on the five-hour usage limit.

One Python file, standard library only. Verified on macOS with Claude Code sessions started by the
Cursor / VS Code extension.

## Why

Long-running Claude Code sessions stop when the account's five-hour limit is hit. Sessions launched by the
Cursor / VS Code extension run in stream-json print mode, where Claude Code's own `autoContinueAtUsageLimit`
does not apply, so someone has to come back after the reset and type "continue". This tool does that: it
watches the live sessions on the machine, spots the ones whose last record is a request rejected with
`error: "rate_limit"`, waits for the server-provided reset time, and nudges the session through a throw-away
`claude -p` sender that runs on a credential outside the blocked subscription.

Polling only reads local files. The sender turn is the only thing that costs money: one Claude Haiku turn per
nudge, about $0.02 to $0.10.

## How it works

1. List live sessions from `~/.claude/sessions/<pid>.json` and find each transcript under
   `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`.
2. Classify the last dialogue record: `rate_limited(reset HH:MM)`, `user_turn`, `assistant_done`,
   `api_error` or `unknown`. Only `rate_limited` with a known reset time is ever acted on.
3. Wait until the reset time plus one minute, then send one cross-session message from a temporary
   `claude -p` process whose permission mode mirrors the receiver (delivery requires the two modes to be in
   the same class). A second attempt flips the class five minutes later; there is no third.
4. Confirm the nudge id shows up in the receiver's transcript and record the attempt under
   `sessionId:resetsAt`, so a session is nudged at most twice per limit window.

Guarantees covered by the built-in selftest: never sends on `unknown`, never sends while the receiver is busy
or has already continued, idempotent per limit window, the credential is never written to the log, and only
one `run` loop can hold the lock (`already running (pid N)` is the expected output of a duplicate start).

## Requirements

| Item | Requirement |
|---|---|
| OS | macOS (verified). The watch loop should also work on Linux but is untested there; `launchd-plist` is macOS only. Windows is not supported (uses `fcntl` and `ps`). |
| Python | 3.9 or newer, no third-party packages. Selftest verified on 3.9, 3.12 and 3.13. |
| Claude Code | 2.1.270 or newer: cross-session messaging and `~/.claude/sessions/<pid>.json` are required. |
| User | Run as the same user who runs the sessions; it reads `~/.claude` (or `$CLAUDE_CONFIG_DIR`). |
| Sender credential | One of `OPENROUTER_API_KEY` (default model `anthropic/claude-haiku-4.5`), `ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN` with optional `ANTHROPIC_BASE_URL`. It must not be the blocked subscription. |
| `claude` binary | The sender reuses the target session's own binary (same version), else `$CLAUDE_LIMIT_WATCH_CLAUDE_BIN`, else `claude` on `PATH`. |

## Install

Option A, copy the file:

```bash
git clone https://github.com/0xKT/claude-limit-watch.git
install -m 755 claude-limit-watch/claude_limit_watch.py ~/.local/bin/claude-limit-watch
claude-limit-watch selftest
```

Option B, as a Claude Code skill (Claude performs the setup with you):

```bash
git clone https://github.com/0xKT/claude-limit-watch.git ~/.claude/skills/claude-limit-watch
```

Then type `/claude-limit-watch` in Claude Code. The skill checks prerequisites, installs the command, tells you
how to store the credential without pasting it into the chat, registers sessions, runs the chain test and
starts the loop. See [SKILL.md](SKILL.md).

## Quick start

```bash
umask 077; printf 'OPENROUTER_API_KEY=sk-or-v1-...\n' > ~/.claude/limit-watch.env

claude-limit-watch status                      # live sessions and what the loop would do now
claude-limit-watch watch --all                 # or: watch <name> "<resume message>"
claude-limit-watch run --once --dry-run        # one pass, logs only, never sends
claude-limit-watch send <name> "limit-watch chain test, please ignore"   # one paid turn, run while NOT limited

nohup claude-limit-watch run >> ~/.claude/limit-watch.log 2>&1 &
tail -f ~/.claude/limit-watch.log
```

Optional, survive reboots (macOS):

```bash
claude-limit-watch launchd-plist > ~/Library/LaunchAgents/com.user.claude-limit-watch.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.claude-limit-watch.plist
```

## Commands

| Command | Purpose |
|---|---|
| `watch <name> [message]`, `watch --all [message]` | register sessions for nudging, optionally with a custom resume message |
| `unwatch <name>`, `unwatch --all` | remove registrations |
| `list` | registrations and attempt records |
| `status` | live sessions, verdicts and the decision the loop would take now |
| `classify <session or transcript path>` | classify one transcript |
| `send <name> <message> [--mode M] [--allow-subscription]` | send a message now (chain test) |
| `run [--interval N] [--once] [--dry-run] [--mode M] [--allow-subscription]` | the watch loop: 60 s poll, 30 min heartbeat, single instance |
| `selftest` | built-in fixtures plus mutation checks |
| `launchd-plist` | print a LaunchAgent plist for `run` |

## Configuration

Credential file `~/.claude/limit-watch.env`, one `KEY=value` per line (`export` prefix and quotes are
accepted). Values here override the process environment.

| Key | Meaning |
|---|---|
| `OPENROUTER_API_KEY` | send through OpenRouter, model `anthropic/claude-haiku-4.5` unless overridden |
| `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` | send through the Anthropic API or a gateway; `ANTHROPIC_BASE_URL` optional |
| `CLAUDE_LIMIT_WATCH_MODEL` | model for the sender turn |

Process environment variables:

| Variable | Meaning |
|---|---|
| `CLAUDE_LIMIT_WATCH_CLAUDE_DIR` | Claude Code config dir; default `$CLAUDE_CONFIG_DIR` or `~/.claude` |
| `CLAUDE_LIMIT_WATCH_STATE_DIR` | where the registry, lock and env file live; default is the config dir |
| `CLAUDE_LIMIT_WATCH_CLAUDE_BIN` | force a specific `claude` binary for the sender |
| `CLAUDE_LIMIT_WATCH_SENDER_MODE` | force the sender's permission mode instead of mirroring the receiver |

## Files

| Path | Role |
|---|---|
| `~/.claude/limit-watch.env` | sender credential, mode 600 |
| `~/.claude/limit-watch.json` | registrations and attempt records |
| `~/.claude/limit-watch.lock` | single-instance lock |
| `~/.claude/limit-watch.log` | loop log, when started as shown above |

## Operating notes

- The loop polls every 60 s and prints `alive: N live, N watched` every 30 min; anything else in the log is
  a state change worth reading.
- `run --once --dry-run` makes one pass and exits; a session that is due gets `-> would nudge (dry run)`
  instead of a message. Use `status` for the per-session view.
- Only one loop can hold `limit-watch.lock`; a second `run` prints `already running (pid N)` and exits 1
  without touching anything. Point `CLAUDE_LIMIT_WATCH_STATE_DIR` elsewhere to experiment beside a live loop.
- `--mode` forces the sender's permission mode instead of mirroring the receiver. `--allow-subscription`
  lets `send` or `run` proceed without a separate credential; it can only work while the account is not
  limited, so it is for chain tests, not for the loop.
- `unknown(transcript not found)` appears for a session that just started (its transcript is not on disk
  yet) or an idle tab that never sent a turn. Such sessions are never nudged.
- A session is nudged at most twice per limit window, five minutes apart. Attempt records are pruned after
  seven days; transcripts idle for more than 48 hours are treated as stale and skipped.
- The sender runs with telemetry and auto-update disabled and receives only the credential variables; the
  secret never reaches the log.
- A closed tab cannot be resumed: the session process has to be alive.

## Field record

First real event, 2026-09-22: a session was rejected at 02:00 (`rateLimitType: five_hour`, reset 04:30) and
sat idle for two and a half hours. The loop nudged at 04:31:49, the message landed at 04:31:57, the session
resumed its task at 04:32:15 and kept working. Cost of the nudge: $0.094.

## Development

```bash
python3 claude_limit_watch.py selftest        # 17 fixtures, 6 mutation checks
```

The repository is four files: `claude_limit_watch.py` (the tool), `README.md`, `SKILL.md` and `LICENSE` (MIT).
The design follows an internal design note by a colleague; the implementation was rebuilt from that note with
Claude Code.
