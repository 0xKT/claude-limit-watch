#!/usr/bin/env python3
"""Resume Claude Code sessions that are stuck on a usage limit.

Watches the live Claude Code sessions on this machine, detects the ones whose last
dialogue record is a request rejected with ``error: "rate_limit"``, waits for the
server-provided ``resetsAt`` and then nudges the session through a throw-away
``claude -p`` sender that runs on a credential outside the blocked subscription.
Polling only reads local files; the sender turn is the only thing that costs money.
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

GRACE_S = 60
STALE_S = 48 * 3600
MAX_ATTEMPTS = 2
RETRY_SPACING_S = 300
PRUNE_S = 7 * 86400
HEARTBEAT_S = 1800
TAIL_START = 256 * 1024
SENDER_TIMEOUT_S = 180
DIALOGUE_TYPES = ("assistant", "user")
DEFAULT_MESSAGE = (
    "Continue the task you were working on when the limit was reached; "
    "do not repeat work that is already complete."
)
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
DEFAULT_MODEL_VARS = (
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
)
SCRUB_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
SENDER_FIXED_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
PASSTHROUGH_VARS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "OPENROUTER_API_KEY",
    "CLAUDE_LIMIT_WATCH_MODEL",
)
BYPASS_MODE = "bypassPermissions"
MIRROR_MODE = "auto"
VALID_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
MARKER_RE = re.compile(r"\[auto-nudge ([0-9a-f]{8})\]")
PERMISSION_MODE_RE = re.compile(rb'"permissionMode":\s*"([^"]+)"')
RESET_TEXT_RE = re.compile(r"resets (\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", re.I)


# ---------------------------------------------------------------- paths


class Paths:
    def __init__(self, claude_dir: Optional[str] = None, state_dir: Optional[str] = None):
        env = os.environ
        self.claude_dir = Path(
            claude_dir
            or env.get("CLAUDE_LIMIT_WATCH_CLAUDE_DIR")
            or env.get("CLAUDE_CONFIG_DIR")
            or Path.home() / ".claude"
        ).expanduser()
        self.state_dir = Path(
            state_dir or env.get("CLAUDE_LIMIT_WATCH_STATE_DIR") or self.claude_dir
        ).expanduser()

    @property
    def sessions_dir(self) -> Path:
        return self.claude_dir / "sessions"

    @property
    def projects_dir(self) -> Path:
        return self.claude_dir / "projects"

    @property
    def registry(self) -> Path:
        return self.state_dir / "limit-watch.json"

    @property
    def lock(self) -> Path:
        return self.state_dir / "limit-watch.lock"

    @property
    def env_file(self) -> Path:
        return self.state_dir / "limit-watch.env"

    @property
    def sender_cwd(self) -> Path:
        return self.state_dir / "limit-watch-sender"

    def transcript(self, session: "Session") -> Optional[Path]:
        slug = re.sub(r"[^A-Za-z0-9]", "-", session.cwd or "")
        direct = self.projects_dir / slug / f"{session.session_id}.jsonl"
        if direct.exists():
            return direct
        hits = glob.glob(str(self.projects_dir / "*" / f"{session.session_id}.jsonl"))
        return Path(hits[0]) if hits else None


# ------------------------------------------------------------- sessions


@dataclass
class Session:
    pid: int
    session_id: str
    name: str
    cwd: str
    status: str
    status_updated_at: Optional[float]
    socket: Optional[str]

    @classmethod
    def from_file(cls, path: Path) -> Optional["Session"]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "sessionId" not in data or "pid" not in data:
            return None
        updated = data.get("statusUpdatedAt")
        return cls(
            pid=int(data["pid"]),
            session_id=str(data["sessionId"]),
            name=str(data.get("name") or ""),
            cwd=str(data.get("cwd") or ""),
            status=str(data.get("status") or ""),
            status_updated_at=updated / 1000 if isinstance(updated, (int, float)) else None,
            socket=data.get("messagingSocketPath"),
        )

    @property
    def id8(self) -> str:
        return self.session_id[:8]

    @property
    def label(self) -> str:
        return self.name or self.id8


def load_sessions(paths: Paths) -> list[Session]:
    out = []
    for p in sorted(paths.sessions_dir.glob("*.json")):
        s = Session.from_file(p)
        if s is not None:
            out.append(s)
    return out


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_live(session: Session) -> bool:
    return pid_alive(session.pid) and bool(session.socket) and os.path.exists(session.socket)


def resolve_session(token: str, sessions: list[Session]) -> Session:
    exact = [s for s in sessions if s.session_id == token]
    if len(exact) == 1:
        return exact[0]
    named = [s for s in sessions if s.name == token]
    if len(named) == 1:
        return named[0]
    prefixed = [s for s in sessions if s.session_id.startswith(token)]
    if len(prefixed) == 1:
        return prefixed[0]
    candidates = named or prefixed
    if candidates:
        listing = ", ".join(f"{s.label} [{s.id8}]" for s in candidates)
        raise LookupError(f"ambiguous session {token!r}: {listing}")
    raise LookupError(f"no live session matches {token!r}")


# ----------------------------------------------------------- transcripts


def text_of(rec: dict) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "") for b in content if isinstance(b, dict)
        )
    return ""


def parse_ts(value) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_dialogue(rec: dict) -> bool:
    return rec.get("type") in DIALOGUE_TYPES and rec.get("isSidechain") is not True


def is_rate_limit(rec: dict) -> bool:
    return rec.get("isApiErrorMessage") is True and rec.get("error") == "rate_limit"


def pick_dialogue(records_newest_first: list[dict]) -> Optional[dict]:
    return next((r for r in records_newest_first if is_dialogue(r)), None)


@dataclass
class Scan:
    dialogue: Optional[dict]
    receiver_mode: Optional[str]
    markers: frozenset
    window: int


def scan_tail(path: Path) -> Scan:
    size = path.stat().st_size
    window = TAIL_START
    while True:
        start = max(0, size - window)
        with path.open("rb") as fh:
            fh.seek(start)
            lines = fh.read(size - start).split(b"\n")
        if start > 0:
            lines = lines[1:]
        records = []
        for raw in reversed(lines):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
        dialogue = pick_dialogue(records)
        if dialogue is None and start > 0:
            window *= 4
            continue
        markers = set()
        for rec in records:
            if rec.get("type") == "user":
                markers.update(MARKER_RE.findall(text_of(rec)))
        return Scan(dialogue, receiver_mode(path, size), frozenset(markers), window)


def receiver_mode(path: Path, size: int) -> Optional[str]:
    """Last user.permissionMode in the file; found by raw search so a long tool-heavy tail is cheap."""
    window = TAIL_START
    while True:
        start = max(0, size - window)
        with path.open("rb") as fh:
            fh.seek(start)
            buf = fh.read(size - start)
        hits = PERMISSION_MODE_RE.findall(buf)
        if hits:
            return hits[-1].decode("utf-8", "replace")
        if start == 0:
            return None
        window *= 4


def reset_from_text(text: str, record_ts: Optional[float]) -> Optional[int]:
    m = RESET_TEXT_RE.search(text)
    if not m or record_ts is None:
        return None
    hour, minute, ampm, zone = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower(), m.group(4)
    try:
        tz = ZoneInfo(zone.strip())
    except Exception:
        return None
    hour = hour % 12 + (12 if ampm == "pm" else 0)
    at = datetime.fromtimestamp(record_ts, tz)
    candidate = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < at:
        candidate += timedelta(days=1)
    return int(candidate.timestamp())


def extract_reset(rec: dict, record_ts: Optional[float]) -> tuple[Optional[int], Optional[str], str]:
    quota = rec.get("quotaLimits") or {}
    resets = quota.get("resetsAt")
    if isinstance(resets, (int, float)) and not isinstance(resets, bool):
        return int(resets), quota.get("rateLimitType"), "quotaLimits"
    derived = reset_from_text(text_of(rec), record_ts)
    return derived, quota.get("rateLimitType"), "text" if derived else "none"


@dataclass
class Verdict:
    kind: str
    record_ts: Optional[float] = None
    resets_at: Optional[int] = None
    rate_limit_type: Optional[str] = None
    stop_reason: Optional[str] = None
    detail: str = ""
    receiver_mode: Optional[str] = None
    markers: frozenset = field(default_factory=frozenset)

    def describe(self) -> str:
        if self.kind == "RATE_LIMITED":
            when = fmt_local(self.resets_at) if self.resets_at else "reset unknown"
            return f"rate_limited({self.rate_limit_type or '?'}, reset {when})"
        if self.kind == "ASSISTANT_DONE":
            return f"assistant_done({self.stop_reason})"
        if self.kind == "API_ERROR":
            return f"api_error({self.detail})"
        if self.kind == "UNKNOWN":
            return f"unknown({self.detail})"
        return self.kind.lower()


def classify(path: Optional[Path]) -> Verdict:
    if path is None or not path.exists():
        return Verdict("UNKNOWN", detail="transcript not found")
    try:
        scan = scan_tail(path)
    except OSError as exc:
        return Verdict("UNKNOWN", detail=f"unreadable transcript: {exc}")
    common = dict(receiver_mode=scan.receiver_mode, markers=scan.markers)
    rec = scan.dialogue
    if rec is None:
        return Verdict("UNKNOWN", detail="no dialogue record found", **common)
    ts = parse_ts(rec.get("timestamp"))
    kind = rec.get("type")
    if kind == "assistant":
        if rec.get("isApiErrorMessage") is True:
            if is_rate_limit(rec):
                resets_at, rl_type, source = extract_reset(rec, ts)
                return Verdict(
                    "RATE_LIMITED", ts, resets_at, rl_type, detail=f"reset from {source}", **common
                )
            return Verdict("API_ERROR", ts, detail=str(rec.get("error")), **common)
        stop = (rec.get("message") or {}).get("stop_reason")
        return Verdict("ASSISTANT_DONE", ts, stop_reason=stop, **common)
    return Verdict("USER_TURN", ts, **common)


# -------------------------------------------------------------- registry


def default_registry() -> dict:
    return {
        "version": 1,
        "watch_all": False,
        "default_message": DEFAULT_MESSAGE,
        "sessions": {},
        "attempts": {},
    }


def load_registry(path: Path) -> dict:
    reg = default_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return reg
    except (OSError, ValueError) as exc:
        raise SystemExit(f"registry {path} is unreadable: {exc}")
    if isinstance(data, dict):
        for key in reg:
            if key in data:
                reg[key] = data[key]
    reg["attempts"] = {
        k: v for k, v in dict(reg["attempts"]).items()
        if isinstance(v, dict) and isinstance(v.get("resets_at"), (int, float))
    }
    return reg


def save_registry(path: Path, reg: dict, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    reg["attempts"] = {
        k: v
        for k, v in reg["attempts"].items()
        if isinstance(v, dict) and now - float(v.get("resets_at") or 0) <= PRUNE_S
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".limit-watch.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def attempt_key(session_id: str, resets_at: int) -> str:
    return f"{session_id}:{resets_at}"


def key8(session_id: str, resets_at: int) -> str:
    return hashlib.sha1(attempt_key(session_id, resets_at).encode()).hexdigest()[:8]


def counts_toward_cap(attempt: dict) -> bool:
    # Failed sends count too; kept as its own function so the selftest can mutate it.
    return True


def attempts_exhausted(entry: Optional[dict]) -> bool:
    if not entry:
        return False
    return sum(1 for a in entry.get("attempts", []) if counts_toward_cap(a)) >= MAX_ATTEMPTS


def record_attempt(reg: dict, session: Session, resets_at: int, mode: str, outcome: "Outcome", now: float) -> dict:
    key = attempt_key(session.session_id, resets_at)
    entry = reg["attempts"].setdefault(
        key,
        {"session_id": session.session_id, "name": session.name, "resets_at": resets_at,
         "confirmed": False, "attempts": []},
    )
    entry["attempts"].append(
        {
            "at": iso(now),
            "mode": mode,
            "outcome": outcome.outcome,
            "msg_id": outcome.msg_id,
            "cost_usd": outcome.cost_usd,
            "detail": outcome.detail[:200],
        }
    )
    return entry


# -------------------------------------------------------------- decision


def reset_passed(now: float, resets_at: int) -> bool:
    return now >= resets_at + GRACE_S


def busy_blocks(session: Session, record_ts: Optional[float]) -> Optional[bool]:
    """True: busy after the limit; False: not busy; None: busy but stale."""
    if session.status != "busy":
        return False
    if record_ts is None or session.status_updated_at is None:
        return None
    return True if session.status_updated_at >= record_ts else None


def env_sender_mode() -> Optional[str]:
    value = os.environ.get("CLAUDE_LIMIT_WATCH_SENDER_MODE") or None
    if value and value not in VALID_MODES:
        print(f"warning: ignoring CLAUDE_LIMIT_WATCH_SENDER_MODE={value!r}; valid: {', '.join(VALID_MODES)}", file=sys.stderr)
        return None
    return value


def sender_mode_for(receiver_mode: Optional[str], attempt_no: int, override: Optional[str]) -> str:
    if override:
        return override
    mode = BYPASS_MODE if receiver_mode == BYPASS_MODE else MIRROR_MODE
    if attempt_no >= 2:
        mode = MIRROR_MODE if mode == BYPASS_MODE else BYPASS_MODE
    return mode


@dataclass
class Decision:
    action: str  # send | wait | skip
    reason: str
    mode: Optional[str] = None
    attempt_no: int = 0

    def __str__(self) -> str:
        extra = f" ({self.mode}, attempt {self.attempt_no})" if self.action == "send" else ""
        return f"{self.action}: {self.reason}{extra}"


def decide(
    *,
    registered: bool,
    live: bool,
    session: Session,
    verdict: Verdict,
    entry: Optional[dict],
    now: float,
    credential_ok: bool,
    allow_subscription: bool,
    mode_override: Optional[str],
) -> Decision:
    if not registered:
        return Decision("skip", "not-registered")
    if not live:
        return Decision("skip", "not-live")
    if verdict.kind != "RATE_LIMITED":
        return Decision("skip", verdict.kind.lower())
    if verdict.resets_at is None:
        return Decision("skip", "rate-limited-unknown-reset")
    if now - verdict.resets_at > STALE_S:
        return Decision("skip", "stale")
    if not reset_passed(now, verdict.resets_at):
        return Decision("wait", f"not-yet (until {fmt_local(verdict.resets_at + GRACE_S)})")
    busy = busy_blocks(session, verdict.record_ts)
    if busy is True:
        return Decision("skip", "busy")
    if attempts_exhausted(entry):
        return Decision("skip", "exhausted")
    prior = (entry or {}).get("attempts", [])
    if prior:
        last_at = parse_ts(prior[-1].get("at")) or 0
        if now - last_at < RETRY_SPACING_S:
            return Decision("wait", "retry-spacing")
    if not credential_ok and not allow_subscription:
        return Decision("skip", "no-credential")
    attempt_no = len(prior) + 1
    reason = "busy-stale-ignored" if busy is None else "ok"
    return Decision("send", reason, sender_mode_for(verdict.receiver_mode, attempt_no, mode_override), attempt_no)


# ---------------------------------------------------------------- sender


@dataclass
class SenderConfig:
    env: dict
    model: str
    credential: Optional[str]
    warnings: list = field(default_factory=list)


def load_env_file(path: Path) -> tuple[dict, list]:
    values: dict = {}
    warnings: list = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values, warnings
    except OSError as exc:
        return values, [f"cannot read {path}: {exc}"]
    if os.stat(path).st_mode & 0o077:
        warnings.append(f"{path} is readable by others; chmod 600 it")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values, warnings


def build_sender_config(process_env: dict, file_env: dict, file_warnings: Optional[list] = None) -> SenderConfig:
    source = {k: process_env[k] for k in PASSTHROUGH_VARS if process_env.get(k)}
    source.update(file_env)
    env = {k: v for k, v in process_env.items() if not k.startswith(SCRUB_PREFIXES)}
    env.update(SENDER_FIXED_ENV)
    env.update({k: v for k, v in source.items() if k not in PASSTHROUGH_VARS})
    model = source.get("CLAUDE_LIMIT_WATCH_MODEL") or ""
    token, key, orkey = (source.get(k, "") for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"))
    if token or key:
        credential = "ANTHROPIC_AUTH_TOKEN" if token else "ANTHROPIC_API_KEY"
        for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            if source.get(k):
                env[k] = source[k]
        model = model or "haiku"
    elif orkey:
        credential = "OPENROUTER_API_KEY"
        env["ANTHROPIC_BASE_URL"] = OPENROUTER_BASE_URL
        env["ANTHROPIC_AUTH_TOKEN"] = orkey
        env["ANTHROPIC_API_KEY"] = ""
        model = model or OPENROUTER_DEFAULT_MODEL
        for var in DEFAULT_MODEL_VARS:
            env.setdefault(var, model)
    else:
        credential = None
        model = model or "haiku"
    return SenderConfig(env, model, credential, list(file_warnings or []))


def pick_binary(session: Optional[Session]) -> Optional[str]:
    explicit = os.environ.get("CLAUDE_LIMIT_WATCH_CLAUDE_BIN")
    if explicit:
        return explicit
    if session is not None:
        try:
            out = subprocess.run(  # noqa: S603
                ["/bin/ps", "-o", "command=", "-p", str(session.pid)],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        head = out.split(" ", 1)[0] if out else ""
        if head and os.path.isabs(head) and os.access(head, os.X_OK):
            return head
    return shutil.which("claude")


def build_body(marker: str, resets_at: int, message: str) -> str:
    return (
        f"[auto-nudge {marker}] This session was cut off by a usage limit, which reset at "
        f"{fmt_local(resets_at)}.\nPicking up where you stopped:\n\n{message}\n\n"
        "(If that work is in fact already finished, ignore this message and do not reply -- "
        "it was sent automatically and may be a false alarm.)"
    )


def build_prompt(session: Session, body: str) -> str:
    return (
        "Use the SendMessage tool exactly once to send the message between the <<< >>> markers "
        f'verbatim to the local Claude session named "{session.name}" (session id {session.session_id}). '
        f"Do nothing else, then stop.\n<<<\n{body}\n>>>"
    )


def sender_argv(binary: str, mode: str, model: str, prompt: str) -> list[str]:
    return [
        binary, "-p", "--permission-mode", mode, "--tools", "SendMessage", "--strict-mcp-config",
        "--no-session-persistence", "--max-turns", "3", "--max-budget-usd", "0.50",
        "--output-format", "stream-json", "--verbose", "--model", model, prompt,
    ]


@dataclass
class Outcome:
    outcome: str  # delivered | failed | error
    msg_id: Optional[str] = None
    cost_usd: Optional[float] = None
    detail: str = ""


def parse_sender_stream(stdout: str) -> Outcome:
    delivered = False
    msg_id = None
    cost = None
    is_error = False
    last_tool_text = ""
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                text = content if isinstance(content, str) else "\n".join(
                    str(b.get("text") or "") for b in (content or []) if isinstance(b, dict)
                )
                last_tool_text = text
                try:
                    payload = json.loads(text)
                except ValueError:
                    continue
                if isinstance(payload, dict) and payload.get("success") is True:
                    delivered = True
                    msg_id = payload.get("msg_id")
        elif event.get("type") == "result":
            cost = event.get("total_cost_usd")
            is_error = bool(event.get("is_error"))
    if delivered and not is_error:
        return Outcome("delivered", msg_id, cost, last_tool_text[:200])
    return Outcome("failed", msg_id, cost, last_tool_text[:200] or "no successful SendMessage result")


def run_sender(session: Session, mode: str, body: str, cfg: SenderConfig, paths: Paths) -> Outcome:
    binary = pick_binary(session)
    if not binary:
        return Outcome("error", detail="no claude binary found")
    paths.sender_cwd.mkdir(parents=True, exist_ok=True)
    argv = sender_argv(binary, mode, cfg.model, build_prompt(session, body))
    try:
        proc = subprocess.run(  # noqa: S603
            argv, cwd=str(paths.sender_cwd), env=cfg.env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=SENDER_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired:
        return Outcome("failed", detail=f"sender timed out after {SENDER_TIMEOUT_S}s")
    except OSError as exc:
        return Outcome("error", detail=f"cannot start sender: {exc}")
    outcome = parse_sender_stream(proc.stdout)
    if proc.returncode != 0 and outcome.outcome == "delivered":
        outcome = Outcome("failed", outcome.msg_id, outcome.cost_usd, f"exit {proc.returncode}")
    if outcome.outcome != "delivered" and proc.stderr.strip():
        outcome.detail = (outcome.detail + " | " + proc.stderr.strip().splitlines()[-1])[:200]
    outcome.detail = f"{outcome.detail} [{os.path.basename(os.path.dirname(binary)) or binary}]"[:200]
    return outcome


# ------------------------------------------------------------------ loop


def fmt_local(ts: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Logger:
    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.last: dict = {}

    def line(self, text: str) -> None:
        print(time.strftime("%H:%M:%S"), text, file=self.out, flush=True)

    def state(self, key: str, reason: str, text: str) -> None:
        if self.last.get(key) != reason:
            self.last[key] = reason
            self.line(text)


@dataclass
class Event:
    session: Session
    verdict: Verdict
    decision: Decision
    outcome: Optional[Outcome] = None


SendFn = Callable[[Session, str, str, SenderConfig, Paths], Outcome]


def cycle(
    paths: Paths,
    *,
    now: Optional[float] = None,
    send_fn: SendFn = run_sender,
    allow_subscription: bool = False,
    dry_run: bool = False,
    mode_override: Optional[str] = None,
    logger: Optional[Logger] = None,
    process_env: Optional[dict] = None,
) -> list[Event]:
    now = time.time() if now is None else now
    logger = logger or Logger()
    reg = load_registry(paths.registry)
    file_env, warnings = load_env_file(paths.env_file)
    cfg = build_sender_config(process_env if process_env is not None else dict(os.environ), file_env, warnings)
    for warning in cfg.warnings:
        logger.state("env-file", warning, f"warning: {warning}")
    events: list[Event] = []
    changed = False
    for session in load_sessions(paths):
        registered = session.session_id in reg["sessions"] or bool(reg["watch_all"])
        if not registered:
            continue
        live = is_live(session)
        verdict = classify(paths.transcript(session)) if live else Verdict("UNKNOWN", detail="not-live")
        for key, entry in reg["attempts"].items():
            resets = entry.get("resets_at")
            if (
                isinstance(resets, (int, float))
                and entry.get("session_id") == session.session_id
                and not entry.get("confirmed")
                and key8(session.session_id, int(resets)) in verdict.markers
            ):
                entry["confirmed"] = True
                changed = True
                logger.line(f"{session.label}: nudge {key8(session.session_id, int(entry['resets_at']))} confirmed in transcript")
        entry = reg["attempts"].get(attempt_key(session.session_id, verdict.resets_at)) if verdict.resets_at else None
        decision = decide(
            registered=registered, live=live, session=session, verdict=verdict, entry=entry, now=now,
            credential_ok=cfg.credential is not None, allow_subscription=allow_subscription,
            mode_override=mode_override,
        )
        event = Event(session, verdict, decision)
        events.append(event)
        if decision.action == "send" or decision.reason in ("assistant_done", "user_turn"):
            logger.last.pop(session.session_id, None)
        else:
            state = verdict.describe() if verdict.kind in ("UNKNOWN", "API_ERROR") else decision.reason
            logger.state(session.session_id, state, f"{session.label}: {verdict.describe()} -> {decision}")
        if decision.reason == "no-credential":
            logger.state(
                "credential", "missing",
                f"blocked: no sender credential; put OPENROUTER_API_KEY=... (or ANTHROPIC_AUTH_TOKEN / "
                f"ANTHROPIC_API_KEY) in {paths.env_file}",
            )
        if decision.action != "send" or verdict.resets_at is None:
            continue
        logger.line(
            f"{session.label}: nudging (reset {fmt_local(verdict.resets_at)}, {decision.reason}, "
            f"attempt {decision.attempt_no}, {decision.mode}, via {pick_binary(session) or '?'})"
        )
        if dry_run:
            logger.line("  -> would nudge (dry run)")
            continue
        message = reg["sessions"].get(session.session_id, {}).get("message") or reg["default_message"]
        body = build_body(key8(session.session_id, verdict.resets_at), verdict.resets_at, message)
        outcome = send_fn(session, decision.mode, body, cfg, paths)
        event.outcome = outcome
        record_attempt(reg, session, verdict.resets_at, decision.mode, outcome, now)
        changed = True
        if outcome.outcome == "delivered":
            cost = f"${outcome.cost_usd:.3f}" if outcome.cost_usd is not None else "cost ?"
            logger.line(f"  -> delivered (msg_id {outcome.msg_id}, {cost}, {decision.mode})")
        else:
            logger.line(f"  -> {outcome.outcome}: {outcome.detail}")
    if changed:
        save_registry(paths.registry, reg, now)
    return events


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = fh.read().strip() or "unknown"
        fh.close()
        raise SystemExit(f"already running (pid {holder})")
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


# ------------------------------------------------------------------- CLI


def live_sessions(paths: Paths) -> list[Session]:
    return [s for s in load_sessions(paths) if is_live(s)]


def cmd_watch(args, paths: Paths) -> int:
    reg = load_registry(paths.registry)
    if args.all:
        reg["watch_all"] = True
        message = args.message or args.session
        if message:
            reg["default_message"] = message
        save_registry(paths.registry, reg)
        print(f"watching every live session; default message: {reg['default_message']!r}")
        return 0
    if not args.session:
        print("watch: give a session (id / id prefix / name) or --all", file=sys.stderr)
        return 2
    try:
        session = resolve_session(args.session, live_sessions(paths))
    except LookupError as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return 2
    reg["sessions"][session.session_id] = {
        "name": session.name,
        "message": args.message or reg["default_message"],
        "added_at": iso(time.time()),
    }
    save_registry(paths.registry, reg)
    print(f"watching {session.label} [{session.id8}]")
    return 0


def cmd_unwatch(args, paths: Paths) -> int:
    reg = load_registry(paths.registry)
    if args.all:
        reg["watch_all"] = False
        reg["sessions"] = {}
        save_registry(paths.registry, reg)
        print("cleared all registrations")
        return 0
    if not args.session:
        print("unwatch: give a session or --all", file=sys.stderr)
        return 2
    known = [
        Session(0, sid, meta.get("name", ""), "", "", None, None) for sid, meta in reg["sessions"].items()
    ]
    try:
        session = resolve_session(args.session, known or live_sessions(paths))
    except LookupError as exc:
        print(f"unwatch: {exc}", file=sys.stderr)
        return 2
    if reg["sessions"].pop(session.session_id, None) is None:
        print(f"unwatch: {session.label} was not registered", file=sys.stderr)
        return 2
    save_registry(paths.registry, reg)
    print(f"stopped watching {session.label} [{session.id8}]")
    return 0


def cmd_list(args, paths: Paths) -> int:
    reg = load_registry(paths.registry)
    print(f"watch_all: {reg['watch_all']}")
    print(f"default_message: {reg['default_message']}")
    live = {s.session_id: s for s in live_sessions(paths)}
    print(f"registered sessions: {len(reg['sessions'])}")
    for sid, meta in reg["sessions"].items():
        state = "live" if sid in live else "gone"
        print(f"  {meta.get('name') or sid[:8]:<18} {sid[:8]}  {state:<4}  {meta.get('message', '')[:70]}")
    print(f"attempt records: {len(reg['attempts'])}")
    for key, entry in sorted(reg["attempts"].items(), key=lambda kv: kv[1].get("resets_at", 0)):
        outcomes = ",".join(a.get("outcome", "?") for a in entry.get("attempts", []))
        print(
            f"  {entry.get('name') or entry.get('session_id', '')[:8]:<18} reset {fmt_local(entry.get('resets_at'))}"
            f"  attempts [{outcomes}]  confirmed={entry.get('confirmed', False)}"
        )
    return 0


def cmd_status(args, paths: Paths) -> int:
    reg = load_registry(paths.registry)
    file_env, warnings = load_env_file(paths.env_file)
    cfg = build_sender_config(dict(os.environ), file_env, warnings)
    now = time.time()
    print(f"credential: {cfg.credential or 'none'}  model: {cfg.model}  watch_all: {reg['watch_all']}")
    for warning in cfg.warnings:
        print(f"warning: {warning}")
    header = f"{'NAME':<18}{'ID':<10}{'PID':<7}{'MODE':<9}{'STATUS':<7}{'VERDICT':<44}{'TRIES':<6}DECISION"
    print(header)
    for session in load_sessions(paths):
        live = is_live(session)
        verdict = classify(paths.transcript(session)) if live else Verdict("UNKNOWN", detail="not-live")
        registered = session.session_id in reg["sessions"] or bool(reg["watch_all"])
        entry = reg["attempts"].get(attempt_key(session.session_id, verdict.resets_at)) if verdict.resets_at else None
        decision = decide(
            registered=registered, live=live, session=session, verdict=verdict, entry=entry, now=now,
            credential_ok=cfg.credential is not None, allow_subscription=False, mode_override=None,
        )
        tries = len((entry or {}).get("attempts", []))
        print(
            f"{session.label[:17]:<18}{session.id8:<10}{session.pid:<7}{(verdict.receiver_mode or '?')[:8]:<9}"
            f"{session.status[:6]:<7}{verdict.describe()[:43]:<44}{tries:<6}{decision}"
        )
    return 0


def cmd_classify(args, paths: Paths) -> int:
    target = Path(args.target).expanduser()
    if target.exists():
        path: Optional[Path] = target
        label = target.name
    else:
        try:
            session = resolve_session(args.target, load_sessions(paths))
        except LookupError as exc:
            print(f"classify: {exc}", file=sys.stderr)
            return 2
        path = paths.transcript(session)
        label = session.label
    verdict = classify(path)
    print(f"{label}: {verdict.describe()}")
    print(f"  kind={verdict.kind} record_ts={iso(verdict.record_ts) if verdict.record_ts else None}")
    print(f"  resets_at={verdict.resets_at} ({fmt_local(verdict.resets_at) if verdict.resets_at else '-'}) type={verdict.rate_limit_type}")
    print(f"  stop_reason={verdict.stop_reason} detail={verdict.detail!r} receiver_mode={verdict.receiver_mode}")
    print(f"  markers={sorted(verdict.markers)}")
    return 0


def cmd_send(args, paths: Paths) -> int:
    try:
        session = resolve_session(args.session, live_sessions(paths))
    except LookupError as exc:
        print(f"send: {exc}", file=sys.stderr)
        return 2
    file_env, warnings = load_env_file(paths.env_file)
    cfg = build_sender_config(dict(os.environ), file_env, warnings)
    for warning in cfg.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if cfg.credential is None and not args.allow_subscription:
        print(
            f"send: no sender credential; put OPENROUTER_API_KEY=... in {paths.env_file} "
            "or pass --allow-subscription for a chain test",
            file=sys.stderr,
        )
        return 3
    override = args.mode or env_sender_mode()
    mode = override or sender_mode_for(classify(paths.transcript(session)).receiver_mode, 1, None)
    print(f"sending to {session.label} [{session.id8}] via {cfg.credential or 'subscription'} / {cfg.model} / {mode}")
    outcome = run_sender(session, mode, args.message, cfg, paths)
    cost = f"${outcome.cost_usd:.3f}" if outcome.cost_usd is not None else "cost ?"
    print(f"  -> {outcome.outcome} (msg_id {outcome.msg_id}, {cost}) {outcome.detail}")
    return 0 if outcome.outcome == "delivered" else 1


def cmd_run(args, paths: Paths) -> int:
    lock = acquire_lock(paths.lock)
    logger = Logger()
    logger.line(f"limit-watch started (pid {os.getpid()}, interval {args.interval}s, state {paths.state_dir})")
    next_heartbeat = time.time() + HEARTBEAT_S
    try:
        while True:
            try:
                events = cycle(
                    paths, allow_subscription=args.allow_subscription, dry_run=args.dry_run,
                    mode_override=args.mode or env_sender_mode(),
                    logger=logger,
                )
            except Exception as exc:  # keep the watcher alive; the next cycle retries
                logger.line(f"cycle error: {exc!r}")
                events = []
            if args.once:
                return 0
            if time.time() >= next_heartbeat:
                logger.line(f"alive: {len(live_sessions(paths))} live, {len(events)} watched")
                next_heartbeat = time.time() + HEARTBEAT_S
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.line("stopped")
        return 0
    finally:
        lock.close()


def cmd_launchd_plist(args, paths: Paths) -> int:
    script = os.path.abspath(__file__)
    log_path = paths.claude_dir / "limit-watch.log"
    print(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.claude-limit-watch</string>
  <key>ProgramArguments</key>
  <array><string>{sys.executable}</string><string>{script}</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{os.environ.get('PATH', '/usr/bin:/bin')}</string></dict>
</dict>
</plist>"""
    )
    return 0


# -------------------------------------------------------------- selftest


class SelfTestError(AssertionError):
    pass


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise SelfTestError(msg)


class Fixture:
    """Synthetic ~/.claude plus state dir inside a temp directory."""

    def __init__(self, tmp: str):
        self.root = Path(tmp)
        self.paths = Paths(claude_dir=str(self.root / "claude"), state_dir=str(self.root / "state"))
        self.paths.sessions_dir.mkdir(parents=True)
        self.paths.projects_dir.mkdir(parents=True)
        self.paths.state_dir.mkdir(parents=True)
        self.sent: list = []
        self.log = Logger(out=open(os.devnull, "w"))
        self.now = 1_800_000_000.0
        self.counter = 0

    def session(self, name: str, *, status: str = "idle", status_updated_at: Optional[float] = None,
                pid: Optional[int] = None, socket: bool = True) -> Session:
        self.counter += 1
        sid = f"{self.counter:08x}-0000-4000-8000-{self.counter:012x}"
        pid = pid or os.getpid()
        sock = self.root / f"{name}.sock"
        if socket:
            sock.write_text("")
        cwd = str(self.root / "work")
        data = {
            "pid": pid, "sessionId": sid, "cwd": cwd, "name": name, "status": status,
            "statusUpdatedAt": int((status_updated_at or self.now) * 1000), "messagingSocketPath": str(sock),
        }
        (self.paths.sessions_dir / f"{name}.json").write_text(json.dumps(data))
        return Session.from_file(self.paths.sessions_dir / f"{name}.json")

    def transcript(self, session: Session, records: list, noise_bytes: int = 0) -> Path:
        slug = re.sub(r"[^A-Za-z0-9]", "-", session.cwd)
        path = self.paths.projects_dir / slug / f"{session.session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
            written = 0
            while written < noise_bytes:
                line = json.dumps({"type": "attachment", "sessionId": session.session_id, "attachment": {"blob": "x" * 5000}}) + "\n"
                fh.write(line)
                written += len(line)
        return path

    def register(self, session: Optional[Session] = None, *, watch_all: bool = False, message: Optional[str] = None,
                 attempts: Optional[dict] = None) -> dict:
        reg = load_registry(self.paths.registry)
        reg["watch_all"] = watch_all
        if session is not None:
            reg["sessions"][session.session_id] = {"name": session.name, "message": message or reg["default_message"], "added_at": iso(self.now)}
        if attempts:
            reg["attempts"].update(attempts)
        save_registry(self.paths.registry, reg, self.now)
        return reg

    def fake_send(self, outcome: str = "delivered"):
        def send(session, mode, body, cfg, paths):
            self.sent.append({"session": session.name, "mode": mode, "body": body, "model": cfg.model, "credential": cfg.credential})
            return Outcome(outcome, "msg-1" if outcome == "delivered" else None, 0.02, "fake")
        return send

    def run(self, **kw) -> list[Event]:
        kw.setdefault("process_env", {"PATH": "/usr/bin", "OPENROUTER_API_KEY": "sk-or-test"})
        kw.setdefault("send_fn", self.fake_send())
        return cycle(self.paths, now=self.now, logger=self.log, **kw)


def _rec(kind: str, ts: float, sid: str, **extra) -> dict:
    rec = {"type": kind, "timestamp": iso(ts), "sessionId": sid, "uuid": f"u-{ts}", "isSidechain": False}
    rec.update(extra)
    return rec


def _rate_limit(ts: float, sid: str, resets_at: Optional[int], text: str = "usage limit") -> dict:
    rec = _rec(
        "assistant", ts, sid, isApiErrorMessage=True, apiErrorStatus=429, error="rate_limit",
        message={"role": "assistant", "model": "<synthetic>", "stop_reason": "stop_sequence", "content": [{"type": "text", "text": text}]},
    )
    if resets_at is not None:
        rec["quotaLimits"] = {"status": "rejected", "resetsAt": resets_at, "rateLimitType": "five_hour"}
    return rec


def _assistant(ts: float, sid: str, stop_reason: str = "end_turn") -> dict:
    return _rec("assistant", ts, sid, message={"role": "assistant", "stop_reason": stop_reason, "content": [{"type": "text", "text": "done"}]})


def _user(ts: float, sid: str, text: str = "hello", mode: str = "auto", **extra) -> dict:
    return _rec("user", ts, sid, permissionMode=mode, message={"role": "user", "content": [{"type": "text", "text": text}]}, **extra)


def _noise(ts: float, sid: str) -> list:
    return [
        {"type": "queue-operation", "operation": "dequeue", "sessionId": sid, "timestamp": iso(ts)},
        {"type": "attachment", "sessionId": sid, "attachment": {"type": "x"}, "timestamp": iso(ts)},
        {"type": "ai-title", "aiTitle": "t", "sessionId": sid},
        {"type": "last-prompt", "lastPrompt": "p", "sessionId": sid},
        {"type": "brand-new-record-type", "sessionId": sid},
    ]


def _stuck_session(fx: Fixture, name: str = "stuck", *, resets_offset: int = -3600, status: str = "idle",
                   status_updated_at: Optional[float] = None, noise_bytes: int = 0, mode: str = "auto") -> tuple[Session, int]:
    resets_at = int(fx.now) + resets_offset
    s = fx.session(name, status=status, status_updated_at=status_updated_at)
    ts = fx.now - 7200
    fx.transcript(s, [_user(ts - 10, s.session_id, mode=mode), _assistant(ts - 5, s.session_id, "tool_use"),
                      _rate_limit(ts, s.session_id, resets_at)] + _noise(ts + 1, s.session_id), noise_bytes=noise_bytes)
    return s, resets_at


def _events_by_name(events: list[Event]) -> dict:
    return {e.session.name: e for e in events}


def st_candidate_sends(fx: Fixture) -> None:
    s, resets_at = _stuck_session(fx)
    fx.register(s)
    ev = _events_by_name(fx.run())["stuck"]
    _check(ev.verdict.kind == "RATE_LIMITED" and ev.verdict.resets_at == resets_at, "verdict")
    _check(ev.decision.action == "send" and ev.decision.mode == "auto" and ev.decision.attempt_no == 1, str(ev.decision))
    _check(len(fx.sent) == 1 and f"[auto-nudge {key8(s.session_id, resets_at)}]" in fx.sent[0]["body"], "body marker")
    _check(DEFAULT_MESSAGE in fx.sent[0]["body"], "default message in body")
    reg = load_registry(fx.paths.registry)
    entry = reg["attempts"][attempt_key(s.session_id, resets_at)]
    _check(len(entry["attempts"]) == 1 and entry["attempts"][0]["outcome"] == "delivered", "attempt recorded")


def st_completed_after_limit_skips(fx: Fixture) -> None:
    s = fx.session("done")
    ts = fx.now - 7200
    fx.transcript(s, [_rate_limit(ts, s.session_id, int(fx.now) - 3600), _user(ts + 100, s.session_id),
                      _assistant(ts + 200, s.session_id, "end_turn")] + _noise(ts + 201, s.session_id))
    fx.register(s)
    ev = _events_by_name(fx.run())["done"]
    _check(ev.verdict.kind == "ASSISTANT_DONE" and ev.decision.action == "skip", f"{ev.verdict.kind} {ev.decision}")
    _check(not fx.sent, "must not send after completion")


def st_user_and_tool_use_skip(fx: Fixture) -> None:
    a = fx.session("usr")
    fx.transcript(a, [_rate_limit(fx.now - 7200, a.session_id, int(fx.now) - 3600), _user(fx.now - 100, a.session_id)])
    b = fx.session("tool")
    fx.transcript(b, [_user(fx.now - 300, b.session_id), _assistant(fx.now - 100, b.session_id, "tool_use")])
    fx.register(a)
    fx.register(b)
    evs = _events_by_name(fx.run())
    _check(evs["usr"].verdict.kind == "USER_TURN" and evs["usr"].decision.action == "skip", "user last")
    _check(evs["tool"].verdict.kind == "ASSISTANT_DONE" and evs["tool"].decision.action == "skip", "tool_use last")
    _check(not fx.sent, "no sends")


def st_noise_and_sidechain(fx: Fixture) -> None:
    s, resets_at = _stuck_session(fx, "side")
    path = fx.paths.transcript(s)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant(fx.now - 10, s.session_id, "end_turn") | {"isSidechain": True}) + "\n")
        fh.write(json.dumps(_user(fx.now - 5, s.session_id) | {"isSidechain": True}) + "\n")
    fx.register(s)
    ev = _events_by_name(fx.run())["side"]
    _check(ev.verdict.kind == "RATE_LIMITED" and ev.decision.action == "send", f"sidechain ignored: {ev.decision}")


def st_deep_tail(fx: Fixture) -> None:
    s, resets_at = _stuck_session(fx, "deep", noise_bytes=TAIL_START * 2 + 12345)
    verdict = classify(fx.paths.transcript(s))
    _check(verdict.kind == "RATE_LIMITED" and verdict.resets_at == resets_at, f"deep tail verdict {verdict.describe()}")
    _check(verdict.receiver_mode == "auto", "receiver mode found in deep tail")
    far, _ = _stuck_session(fx, "farmode", mode=BYPASS_MODE, noise_bytes=TAIL_START + 50000)
    path = fx.paths.transcript(far)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "mode", "mode": "normal", "sessionId": far.session_id}) + "\n")
        fh.write(json.dumps(_rate_limit(fx.now - 3600, far.session_id, int(fx.now) - 1800)) + "\n")
    verdict = classify(path)
    _check(verdict.kind == "RATE_LIMITED" and verdict.receiver_mode == BYPASS_MODE, f"mode beyond window, mode record ignored: {verdict.receiver_mode}")
    fx.register(far)
    ev = _events_by_name(fx.run())["farmode"]
    _check(ev.decision.action == "send" and ev.decision.mode == BYPASS_MODE, f"mirrors bypass receiver: {ev.decision}")
    scan = scan_tail(fx.paths.transcript(s))
    _check(scan.window > TAIL_START, "window grew")


def st_api_error_and_text_fallback(fx: Fixture) -> None:
    a = fx.session("apierr")
    fx.transcript(a, [_user(fx.now - 200, a.session_id), _rec(
        "assistant", fx.now - 100, a.session_id, isApiErrorMessage=True, apiErrorStatus=400, error="unknown",
        message={"role": "assistant", "stop_reason": "stop_sequence", "content": [{"type": "text", "text": "API Error: 400"}]})])
    fx.register(a)
    ev = _events_by_name(fx.run())["apierr"]
    _check(ev.verdict.kind == "API_ERROR" and ev.decision.action == "skip" and not fx.sent, "api error skipped")
    b = fx.session("textreset")
    ts = fx.now - 7200
    fx.transcript(b, [_rate_limit(ts, b.session_id, None, text="limit · your session limit resets 9:30pm (Europe/London)")])
    verdict = classify(fx.paths.transcript(b))
    expected = datetime.fromtimestamp(ts, ZoneInfo("Europe/London")).replace(hour=21, minute=30, second=0, microsecond=0)
    if expected.timestamp() < ts:
        expected += timedelta(days=1)
    _check(verdict.kind == "RATE_LIMITED" and verdict.resets_at == int(expected.timestamp()), f"text fallback {verdict.resets_at} vs {int(expected.timestamp())}")
    c = fx.session("noreset")
    fx.transcript(c, [_rate_limit(ts, c.session_id, None, text="no time here")])
    fx.register(c)
    ev = _events_by_name(fx.run())["noreset"]
    _check(ev.decision.reason == "rate-limited-unknown-reset" and not fx.sent, "unknown reset skipped")


def st_timing_gates(fx: Fixture) -> None:
    early, _ = _stuck_session(fx, "early", resets_offset=-30)
    stale, _ = _stuck_session(fx, "stale", resets_offset=-(STALE_S + 10))
    edge, _ = _stuck_session(fx, "edge", resets_offset=-GRACE_S)
    fx.register(early)
    fx.register(stale)
    fx.register(edge)
    evs = _events_by_name(fx.run())
    _check(evs["early"].decision.action == "wait" and evs["early"].decision.reason.startswith("not-yet"), str(evs["early"].decision))
    _check(evs["stale"].decision.reason == "stale", str(evs["stale"].decision))
    _check(evs["edge"].decision.action == "send", f"exactly at grace boundary sends: {evs['edge'].decision}")
    _check([x["session"] for x in fx.sent] == ["edge"], "only edge sent")


def st_busy_gates(fx: Fixture) -> None:
    fresh, _ = _stuck_session(fx, "busyfresh", status="busy", status_updated_at=fx.now - 60)
    stale, _ = _stuck_session(fx, "busystale", status="busy", status_updated_at=fx.now - 7200 - 600)
    fx.register(fresh)
    fx.register(stale)
    evs = _events_by_name(fx.run())
    _check(evs["busyfresh"].decision.reason == "busy", str(evs["busyfresh"].decision))
    _check(evs["busystale"].decision.action == "send" and evs["busystale"].decision.reason == "busy-stale-ignored", str(evs["busystale"].decision))
    nots, _ = _stuck_session(fx, "busynots", status="busy")
    data = json.loads((fx.paths.sessions_dir / "busynots.json").read_text())
    del data["statusUpdatedAt"]
    (fx.paths.sessions_dir / "busynots.json").write_text(json.dumps(data))
    fx.register(nots)
    ev = _events_by_name(fx.run())["busynots"]
    _check(ev.decision.action == "send" and ev.decision.reason == "busy-stale-ignored", f"unverifiable busy: {ev.decision}")


def st_attempt_cap_and_spacing(fx: Fixture) -> None:
    s, resets_at = _stuck_session(fx, "capped")
    key = attempt_key(s.session_id, resets_at)
    two_failed = {key: {"session_id": s.session_id, "resets_at": resets_at, "confirmed": False, "attempts": [
        {"at": iso(fx.now - 4000), "mode": "auto", "outcome": "failed"},
        {"at": iso(fx.now - 3000), "mode": "bypassPermissions", "outcome": "failed"}]}}
    fx.register(s, attempts=two_failed)
    ev = _events_by_name(fx.run())["capped"]
    _check(ev.decision.reason == "exhausted" and not fx.sent, f"two failed attempts exhaust: {ev.decision}")
    one_recent = {key: {"session_id": s.session_id, "resets_at": resets_at, "confirmed": False, "attempts": [
        {"at": iso(fx.now - 60), "mode": "auto", "outcome": "failed"}]}}
    reg = load_registry(fx.paths.registry)
    reg["attempts"] = one_recent
    save_registry(fx.paths.registry, reg, fx.now)
    ev = _events_by_name(fx.run())["capped"]
    _check(ev.decision.reason == "retry-spacing" and not fx.sent, f"spacing: {ev.decision}")
    reg["attempts"][key]["attempts"][0]["at"] = iso(fx.now - RETRY_SPACING_S - 1)
    save_registry(fx.paths.registry, reg, fx.now)
    ev = _events_by_name(fx.run())["capped"]
    _check(ev.decision.action == "send" and ev.decision.attempt_no == 2 and ev.decision.mode == BYPASS_MODE, f"attempt 2 flips class: {ev.decision}")
    entry = load_registry(fx.paths.registry)["attempts"][key]
    _check(len(entry["attempts"]) == 2, "second attempt recorded")


def st_failed_send_counts(fx: Fixture) -> None:
    s, resets_at = _stuck_session(fx, "failing")
    fx.register(s)
    fx.run(send_fn=fx.fake_send("failed"))
    entry = load_registry(fx.paths.registry)["attempts"][attempt_key(s.session_id, resets_at)]
    _check(len(entry["attempts"]) == 1 and entry["attempts"][0]["outcome"] == "failed", "failure recorded")
    fx.now += RETRY_SPACING_S + 1
    fx.run(send_fn=fx.fake_send("failed"))
    fx.now += RETRY_SPACING_S + 1
    ev = _events_by_name(fx.run(send_fn=fx.fake_send("failed")))["failing"]
    _check(ev.decision.reason == "exhausted", f"failures count toward cap: {ev.decision}")
    _check(len(load_registry(fx.paths.registry)["attempts"][attempt_key(s.session_id, resets_at)]["attempts"]) == 2, "exactly two attempts")


def st_confirmed_marker(fx: Fixture) -> None:
    s, resets_at = _stuck_session(fx, "confirmed")
    marker = key8(s.session_id, resets_at)
    key = attempt_key(s.session_id, resets_at)
    fx.register(s, attempts={key: {"session_id": s.session_id, "resets_at": resets_at, "confirmed": False,
                                  "attempts": [{"at": iso(fx.now - 600), "mode": "auto", "outcome": "delivered"}]}})
    path = fx.paths.transcript(s)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_user(fx.now - 500, s.session_id, text=f'<cross-session-message from="uds:/tmp/x.sock"> [auto-nudge {marker}] hi', isMeta=True)) + "\n")
        fh.write(json.dumps(_assistant(fx.now - 400, s.session_id, "tool_use")) + "\n")
    ev = _events_by_name(fx.run())["confirmed"]
    _check(ev.decision.action == "skip" and not fx.sent, "resumed session not nudged")
    _check(load_registry(fx.paths.registry)["attempts"][key]["confirmed"] is True, "marker confirmed")


def st_credential_gate(fx: Fixture) -> None:
    s, resets_at = _stuck_session(fx, "nocred")
    fx.register(s)
    ev = _events_by_name(fx.run(process_env={"PATH": "/usr/bin"}))["nocred"]
    _check(ev.decision.reason == "no-credential" and not fx.sent, str(ev.decision))
    _check(attempt_key(s.session_id, resets_at) not in load_registry(fx.paths.registry)["attempts"], "no attempt consumed")
    ev = _events_by_name(fx.run(process_env={"PATH": "/usr/bin"}, allow_subscription=True))["nocred"]
    _check(ev.decision.action == "send" and fx.sent[-1]["credential"] is None, "subscription allowed explicitly")


def st_dry_run_and_watch_all(fx: Fixture) -> None:
    s, resets_at = _stuck_session(fx, "wall")
    fx.register(watch_all=True)
    ev = _events_by_name(fx.run(dry_run=True))["wall"]
    _check(ev.decision.action == "send" and not fx.sent, "dry run does not send")
    _check(not load_registry(fx.paths.registry)["attempts"], "dry run records nothing")
    fx.run()
    _check(len(fx.sent) == 1, "watch_all covers unregistered session")
    other = fx.session("unwatched")
    fx.transcript(other, [_rate_limit(fx.now - 7200, other.session_id, int(fx.now) - 3600)])
    fx.register(watch_all=False)
    fx.sent.clear()
    fx.run()
    _check(not fx.sent, "unregistered session ignored when watch_all is off")


def st_sender_config(fx: Fixture) -> None:
    base = {"PATH": "/usr/bin", "ANTHROPIC_BASE_URL": "https://leak.example", "CLAUDE_CODE_USE_BEDROCK": "1", "HOME": "/x"}
    secret = "sk-or-" + "secret"
    cfg = build_sender_config(base, {"OPENROUTER_API_KEY": secret})
    _check(cfg.credential == "OPENROUTER_API_KEY" and cfg.model == OPENROUTER_DEFAULT_MODEL, "openrouter mapping")
    _check(cfg.env["ANTHROPIC_BASE_URL"] == OPENROUTER_BASE_URL and cfg.env["ANTHROPIC_AUTH_TOKEN"] == secret, "openrouter env")
    _check(cfg.env["ANTHROPIC_API_KEY"] == "" and "CLAUDE_CODE_USE_BEDROCK" not in cfg.env, "scrubbed")
    _check(all(cfg.env[v] == OPENROUTER_DEFAULT_MODEL for v in DEFAULT_MODEL_VARS), "model vars")
    _check(cfg.env["DISABLE_TELEMETRY"] == "1" and cfg.env["HOME"] == "/x", "fixed env kept")
    cfg = build_sender_config({"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-x"}, {})
    _check(cfg.credential == "ANTHROPIC_API_KEY" and cfg.env["ANTHROPIC_API_KEY"] == "sk-ant-x" and cfg.model == "haiku", "api key passthrough")
    cfg = build_sender_config({"PATH": "/usr/bin"}, {"ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_BASE_URL": "https://gw", "CLAUDE_LIMIT_WATCH_MODEL": "m1"})
    _check(cfg.credential == "ANTHROPIC_AUTH_TOKEN" and cfg.env["ANTHROPIC_BASE_URL"] == "https://gw" and cfg.model == "m1", "token passthrough")
    cfg = build_sender_config({"PATH": "/usr/bin", "ANTHROPIC_API_KEY": ""}, {})
    _check(cfg.credential is None, "empty key is no credential")
    from_file = "fi" + "le"
    cfg = build_sender_config({"PATH": "/usr/bin", "OPENROUTER_API_KEY": "proc"}, {"OPENROUTER_API_KEY": from_file})
    _check(cfg.env["ANTHROPIC_AUTH_TOKEN"] == from_file, "env file overrides process env")
    env_path = fx.paths.env_file
    env_path.write_text('# comment\nexport OPENROUTER_API_KEY="sk-or-file"\nFOO=\'bar\'\nBROKEN LINE\n')
    os.chmod(env_path, 0o644)
    values, warnings = load_env_file(env_path)
    _check(values == {"OPENROUTER_API_KEY": "sk-or-file", "FOO": "bar"}, f"env parse {values}")
    _check(warnings and "chmod 600" in warnings[0], "permission warning")
    os.chmod(env_path, 0o600)
    _check(load_env_file(env_path)[1] == [], "no warning at 0600")
    s, _ = _stuck_session(fx, "secret")
    fx.register(s)
    buf = tempfile.SpooledTemporaryFile(mode="w+")
    fx.log = Logger(out=buf)
    fx.run(process_env={"PATH": "/usr/bin"})
    buf.seek(0)
    _check("sk-or-file" not in buf.read(), "secret never logged")
    _check(fx.sent and fx.sent[-1]["credential"] == "OPENROUTER_API_KEY", "env file credential used")


def st_sender_plumbing(fx: Fixture) -> None:
    s = fx.session("plumb")
    argv = sender_argv("/bin/claude", "auto", "haiku", "PROMPT")
    _check(argv[:5] == ["/bin/claude", "-p", "--permission-mode", "auto", "--tools"] and argv[-1] == "PROMPT", "argv shape")
    _check("--strict-mcp-config" in argv and "--no-session-persistence" in argv and "stream-json" in argv, "argv flags")
    _check(sender_mode_for(BYPASS_MODE, 1, None) == BYPASS_MODE and sender_mode_for("auto", 1, None) == "auto", "mirror")
    _check(sender_mode_for(None, 1, None) == "auto" and sender_mode_for(None, 2, None) == BYPASS_MODE, "flip")
    _check(sender_mode_for(BYPASS_MODE, 2, None) == "auto" and sender_mode_for("auto", 2, "plan") == "plan", "override")
    prompt = build_prompt(s, "BODY")
    _check(f'"{s.name}"' in prompt and s.session_id in prompt and "<<<\nBODY\n>>>" in prompt, "prompt")
    ok = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "SendMessage"}]}}),
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": json.dumps({"success": True, "message": "x -> y", "msg_id": "m-9"})}]}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0213}),
    ])
    out = parse_sender_stream(ok)
    _check(out.outcome == "delivered" and out.msg_id == "m-9" and out.cost_usd == 0.0213, f"parse ok {out}")
    bad = "\n".join([
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "No session named zzz"}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01}),
    ])
    out = parse_sender_stream(bad)
    _check(out.outcome == "failed" and "No session" in out.detail, f"parse failed {out}")
    _check(parse_sender_stream("garbage\n").outcome == "failed", "garbage stream fails")


def st_registry_and_resolution(fx: Fixture) -> None:
    a = fx.session("alpha")
    b = fx.session("beta")
    sessions = load_sessions(fx.paths)
    _check(resolve_session(a.session_id, sessions).name == "alpha", "by id")
    _check(resolve_session("beta", sessions).name == "beta", "by name")
    _check(resolve_session(b.session_id[:12], sessions).name == "beta", "by prefix")
    try:
        resolve_session("0000", sessions)
        _check(False, "ambiguous prefix accepted")
    except LookupError as exc:
        _check("ambiguous" in str(exc), str(exc))
    try:
        resolve_session("nope", sessions)
        _check(False, "unknown accepted")
    except LookupError:
        pass
    reg = load_registry(fx.paths.registry)
    reg["attempts"] = {
        "old": {"session_id": "x", "resets_at": int(fx.now) - PRUNE_S - 10, "confirmed": False, "attempts": []},
        "new": {"session_id": "y", "resets_at": int(fx.now) - 100, "confirmed": False, "attempts": []},
    }
    save_registry(fx.paths.registry, reg, fx.now)
    loaded = load_registry(fx.paths.registry)
    _check(set(loaded["attempts"]) == {"new"}, "pruned")
    before = fx.paths.registry.read_text()
    try:
        save_registry(fx.paths.registry, {**loaded, "attempts": {"bad": {"resets_at": int(fx.now), "attempts": [set()]}}}, fx.now)
        _check(False, "unserializable registry accepted")
    except TypeError:
        pass
    _check(fx.paths.registry.read_text() == before, "failed write leaves the old registry intact")
    fx.paths.registry.write_text(json.dumps({**loaded, "attempts": {"legacy": {"session_id": "z", "confirmed": False, "attempts": []}}}))
    _check(load_registry(fx.paths.registry)["attempts"] == {}, "malformed attempt entry dropped")
    _check(not [p for p in fx.paths.state_dir.iterdir() if p.name.startswith(".limit-watch.")], "temp file cleaned")
    dead = fx.session("dead", pid=2**22 - 1)
    _check(not is_live(dead), "dead pid not live")
    nosock = fx.session("nosock", socket=False)
    _check(not is_live(nosock), "missing socket not live")
    fx.transcript(dead, [_rate_limit(fx.now - 7200, dead.session_id, int(fx.now) - 3600)])
    fx.register(dead)
    ev = _events_by_name(fx.run())["dead"]
    _check(ev.decision.reason == "not-live" and not fx.sent, "dead session skipped")


def st_unknown_never_sends(fx: Fixture) -> None:
    fx.session("fresh")
    empty = fx.session("empty")
    fx.transcript(empty, [])
    junk = fx.session("junk")
    fx.transcript(junk, _noise(fx.now - 10, junk.session_id))
    fx.register(watch_all=True)
    buf = tempfile.SpooledTemporaryFile(mode="w+")
    fx.log = Logger(out=buf)
    evs = _events_by_name(fx.run())
    for name in ("fresh", "empty", "junk"):
        _check(evs[name].verdict.kind == "UNKNOWN" and evs[name].decision.action == "skip", f"{name}: {evs[name].decision}")
    _check(not fx.sent, "unknown verdict must never send")
    _check(not load_registry(fx.paths.registry)["attempts"], "unknown verdict records nothing")
    fx.run()
    buf.seek(0)
    lines = [ln for ln in buf.read().splitlines() if "unknown(" in ln]
    _check(len(lines) == 3, f"unknown logged once per state, got {len(lines)}")


SELFTESTS = [
    st_candidate_sends, st_completed_after_limit_skips, st_user_and_tool_use_skip, st_noise_and_sidechain,
    st_deep_tail, st_api_error_and_text_fallback, st_timing_gates, st_busy_gates, st_attempt_cap_and_spacing,
    st_failed_send_counts, st_confirmed_marker, st_credential_gate, st_dry_run_and_watch_all, st_sender_config,
    st_sender_plumbing, st_registry_and_resolution, st_unknown_never_sends,
]

MUTATIONS = [
    ("record-type filtering", "is_dialogue", lambda rec: rec.get("isSidechain") is not True, st_noise_and_sidechain),
    ("completion state", "pick_dialogue", lambda recs: next((r for r in recs if is_rate_limit(r)), None) or next((r for r in recs if is_dialogue(r)), None), st_completed_after_limit_skips),
    ("idempotent key", "attempts_exhausted", lambda entry: False, st_attempt_cap_and_spacing),
    ("busy gate", "busy_blocks", lambda session, ts: False, st_busy_gates),
    ("resetsAt timing", "reset_passed", lambda now, resets_at: True, st_timing_gates),
    ("failure counting", "counts_toward_cap", lambda attempt: attempt.get("outcome") == "delivered", st_failed_send_counts),
]


def _run_fixture(test) -> Optional[str]:
    with tempfile.TemporaryDirectory(prefix="limit-watch-selftest-") as tmp:
        try:
            test(Fixture(tmp))
        except SelfTestError as exc:
            return str(exc)
        except Exception as exc:  # a crash is a failure too
            return f"{type(exc).__name__}: {exc}"
    return None


def cmd_selftest(args, paths: Paths) -> int:
    failures = 0
    for test in SELFTESTS:
        err = _run_fixture(test)
        print(f"{'ok  ' if err is None else 'FAIL'} {test.__name__}{'' if err is None else ': ' + err}")
        failures += err is not None
    survived = 0
    for label, name, mutant, test in MUTATIONS:
        original = globals()[name]
        globals()[name] = mutant
        try:
            err = _run_fixture(test)
        finally:
            globals()[name] = original
        caught = err is not None
        print(f"{'ok  ' if caught else 'FAIL'} mutation[{label}] {'caught' if caught else 'SURVIVED'}")
        survived += not caught
    print(f"selftest: {len(SELFTESTS) - failures}/{len(SELFTESTS)} fixtures passed, "
          f"{len(MUTATIONS) - survived}/{len(MUTATIONS)} mutations caught")
    return 0 if failures == 0 and survived == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-limit-watch", description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("watch", help="register a session (or --all) for nudging")
    p.add_argument("session", nargs="?", help="session name as shown by status")
    p.add_argument("message", nargs="?", help="resume message for this registration")
    p.add_argument("--all", action="store_true", help="apply to every live session, now and later")
    p.set_defaults(func=cmd_watch)
    p = sub.add_parser("unwatch", help="remove a registration (or --all)")
    p.add_argument("session", nargs="?", help="session name as shown by status")
    p.add_argument("--all", action="store_true", help="apply to every live session, now and later")
    p.set_defaults(func=cmd_unwatch)
    sub.add_parser("list", help="show registrations and attempts").set_defaults(func=cmd_list)
    sub.add_parser("status", help="show live sessions, verdicts and decisions").set_defaults(func=cmd_status)
    p = sub.add_parser("classify", help="classify one session or transcript path")
    p.add_argument("target", help="session name or path to a transcript .jsonl")
    p.set_defaults(func=cmd_classify)
    p = sub.add_parser("send", help="send a message now (chain test)")
    p.add_argument("session", help="session name as shown by status")
    p.add_argument("message", help="message text to deliver")
    p.add_argument("--mode", choices=VALID_MODES, help="sender permission mode; default mirrors the receiver")
    p.add_argument("--allow-subscription", action="store_true", help="send even without a separate credential (fails while limited)")
    p.set_defaults(func=cmd_send)
    p = sub.add_parser("run", help="watch loop")
    p.add_argument("--interval", type=float, default=60, help="poll interval in seconds (default 60)")
    p.add_argument("--once", action="store_true", help="one pass, then exit")
    p.add_argument("--dry-run", action="store_true", help="decide and log, but never send")
    p.add_argument("--allow-subscription", action="store_true", help="send even without a separate credential (fails while limited)")
    p.add_argument("--mode", choices=VALID_MODES, help="sender permission mode; default mirrors the receiver")
    p.set_defaults(func=cmd_run)
    sub.add_parser("selftest", help="run the built-in tests").set_defaults(func=cmd_selftest)
    sub.add_parser("launchd-plist", help="print a LaunchAgent plist for `run`").set_defaults(func=cmd_launchd_plist)
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args, Paths())


if __name__ == "__main__":
    sys.exit(main())
