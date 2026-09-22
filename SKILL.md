---
name: claude-limit-watch
description: Install, check or remove claude-limit-watch, the watcher that resumes Claude Code sessions stuck on the five-hour usage limit. Use when the user wants sessions to continue automatically after a usage limit, or asks about the state of the watcher.
argument-hint: "[install|status|test|uninstall]"
disable-model-invocation: true
---

# claude-limit-watch

The tool is `${CLAUDE_SKILL_DIR}/claude_limit_watch.py`; the full description is in
`${CLAUDE_SKILL_DIR}/README.md`. Read the README before the first run.

Sub-command from `$ARGUMENTS`: `install` (default when empty), `status`, `test`, `uninstall`.

## Rules

- Never ask the user to paste an API key into the chat, and never read or print
  `~/.claude/limit-watch.env`. Hand the user a command to run in their own terminal, then check only
  that the file exists with mode 600 (`stat -f '%Lp' ~/.claude/limit-watch.env` must print `600`).
- Steps that cost money (`send`) or start a background process (`run`, `launchctl`) need an explicit
  yes from the user in this conversation, each time.
- Do not change permission settings, `CLAUDE.md` or Claude Code config to get a step through. If a
  command is denied, show the exact command and let the user run it.
- Report every command and its output verbatim; do not summarise a check as "passed".

## install

1. Preflight, read-only. Run and report:
   - `uname -s` (macOS is the verified platform; on Linux say the loop is untested there),
   - `python3 --version` (3.9 or newer),
   - `ls ~/.claude/sessions/ 2>/dev/null | head` (must list `<pid>.json` files, `.key` files may sit beside
     them; if there is no `.json`, Claude Code is older than 2.1.270 or no session is running),
   - `command -v claude-limit-watch` (already installed?), and whether `~/.local/bin` is on `PATH`.
2. Install the command and prove it works:
   ```bash
   install -m 755 "${CLAUDE_SKILL_DIR}/claude_limit_watch.py" ~/.local/bin/claude-limit-watch
   claude-limit-watch selftest
   claude-limit-watch status
   ```
   `selftest` must end with `17/17 fixtures passed, 6/6 mutations caught`.
3. Credential. Ask which credential the user wants to spend on nudges (OpenRouter is the cheapest verified
   route; an Anthropic API key also works). Then give them this to run themselves, with their key filled in:
   ```bash
   umask 077; printf 'OPENROUTER_API_KEY=sk-or-v1-...\n' > ~/.claude/limit-watch.env
   ```
   Wait for them to confirm, then verify with `stat -f '%Lp' ~/.claude/limit-watch.env` only.
4. Register sessions: `claude-limit-watch watch --all`, or `watch <name> "<message>"` for specific ones.
   Show `claude-limit-watch list`. Offer to replace the default resume message if the user works in another
   language.
5. Dry run, free and side-effect free:
   ```bash
   claude-limit-watch run --once --dry-run
   ```
   Expected: a `limit-watch started (pid N ...)` line, then only state changes; a session that is due prints
   `-> would nudge (dry run)`. If it prints `already running (pid N)`, a loop is already up: report that and
   skip steps 6 and 7.
6. Chain test, only after an explicit yes (it costs one Haiku turn, a few cents, and the account must not be
   limited right now). Pick a live session name from `status`, then:
   ```bash
   claude-limit-watch send <name> "limit-watch chain test, please ignore"
   ```
   The output must contain `delivered`; the message appears in that session.
7. Start the loop, only after an explicit yes:
   ```bash
   nohup claude-limit-watch run >> ~/.claude/limit-watch.log 2>&1 &
   sleep 2; tail -3 ~/.claude/limit-watch.log
   ```
   The log must show `limit-watch started (pid N ...)`. If it shows `already running (pid N)`, a loop is
   already up and nothing else is needed. Offer the launchd variant from the README for reboot persistence.
8. Close with: the pid, the log path, the heartbeat cadence (`alive: ...` every 30 min), the cost per nudge,
   and that after the first real limit event `claude-limit-watch list` shows the attempt record.

## status

Run and report verbatim:

```bash
pgrep -fl 'claude-limit-watch run'
claude-limit-watch status
claude-limit-watch list
tail -20 ~/.claude/limit-watch.log
```

Explain any `unknown(transcript not found)` line: a session that just started or an idle tab that never
sent a turn; it is never nudged.

## test

The chain test from install step 6, with the same explicit-yes rule.

## uninstall

Only after an explicit yes. Stop the loop, remove the launchd agent if present, remove the command and the
state files. Leave `~/.claude/limit-watch.env` in place unless the user separately confirms deleting it.

```bash
pkill -f 'claude-limit-watch run' || true
launchctl bootout gui/$(id -u)/com.user.claude-limit-watch 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.user.claude-limit-watch.plist
rm -f ~/.local/bin/claude-limit-watch ~/.claude/limit-watch.json ~/.claude/limit-watch.lock
```
