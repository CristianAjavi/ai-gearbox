#!/usr/bin/env python3
"""gearbox: move work in progress from one AI CLI to another without losing context or processes.

Supported CLIs: claude (Claude Code), codex (Codex CLI), agy (Antigravity CLI).
Each one keeps its own login. What travels is a compact handoff: the task, the current
state, the latest messages, the files touched and the recent commands, plus the path of
the full session in case the target needs the detail.

Usage:
  gearbox codex                   from the latest session in this folder to Codex
  gearbox agy --from claude       pin the source CLI
  gearbox claude --session 7cdc   a specific session, by id prefix
  gearbox codex --dry-run         print the handoff and launch nothing
  gearbox --list                  sessions in this folder, newest first
  gearbox --read agy 7cdc140e     dump a full session as readable text
  gearbox --bg <cmd...>           start a process that survives switching CLIs
  gearbox --jobs                  list background processes and their state
  gearbox --loop claude           one tab all day: when you exit a CLI it asks which
                                  one to switch to and opens it with the handoff
  gearbox --loop codex --from agy the loop's first launch already carries a handoff

Inside a loop you can also switch without leaving the chat: run `gearbox codex` from
within the CLI (`!gearbox codex` in Claude Code; in Codex or agy ask the assistant to run
it). gearbox recognizes the CLI it runs inside, closes it and the loop opens the next one.

Exit codes: 0 ok · 2 no session · 3 session has no readable messages · 4 usage.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field

TOOLS = ("claude", "codex", "agy")
MAX_CHARS = 9000            # ≈ 2,200 tokens; the handoff ceiling
MARK = "# Session handoff"
FOREIGN_MARKS = ("# Traspaso de sesión", "I'm continuing a coding session", "## Session Handoff Context")
GEARBOX_DIR = os.path.join(os.path.expanduser("~"), ".gearbox")


@dataclass
class Msg:
    role: str                       # user | assistant | tool
    text: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)


@dataclass
class Session:
    tool: str
    id: str
    path: str
    cwd: str
    mtime: float
    msgs: list = field(default_factory=list)

    @property
    def short(self) -> str:
        return self.id[:8]


# ----------------------------------------------------------------------------- helpers

def _home() -> str:
    return os.path.expanduser("~")


def _is_handoff(text: str) -> bool:
    head = text.lstrip()[:400]
    return head.startswith(MARK) or any(m in head for m in FOREIGN_MARKS)


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def _cut(text: str, n: int) -> str:
    text = _clean(text)
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _json_or_empty(s):
    if isinstance(s, dict):
        return s
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ----------------------------------------------------------------------------- minimal protobuf (agy)

def _varint(b: bytes, i: int):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def pb_fields(b: bytes) -> dict:
    """{field_number: [values]} for the top level of a protobuf message.
    Length-delimited values stay as bytes; the caller decides whether they are text or a message."""
    out: dict = {}
    i = 0
    n = len(b)
    while i < n:
        tag, i = _varint(b, i)
        f, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        elif wt == 2:
            ln, i = _varint(b, i)
            v, i = b[i:i + ln], i + ln
        else:                       # groups: not expected; stop without guessing
            break
        out.setdefault(f, []).append(v)
    return out


def pb_path(b: bytes, path: str) -> list:
    """Values (bytes) at path '20.7.2': walks nested messages by field number."""
    parts = [int(p) for p in path.split(".")]
    current = [b]
    for p in parts:
        nxt = []
        for m in current:
            try:
                nxt.extend(v for v in pb_fields(m).get(p, []) if isinstance(v, bytes))
            except Exception:
                continue
        current = nxt
        if not current:
            return []
    return current


def pb_text(b: bytes, path: str) -> str:
    for v in pb_path(b, path):
        try:
            t = v.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if t and all(ord(c) >= 32 or c in "\n\t\r" for c in t):
            return t
    return ""


# ----------------------------------------------------------------------------- parsers

def _claude_project_dir(cwd: str) -> str:
    return os.path.join(_home(), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))


def claude_sessions(cwd: str | None) -> list:
    dirs = [_claude_project_dir(cwd)] if cwd else glob.glob(os.path.join(_home(), ".claude", "projects", "*"))
    out = []
    for d in dirs:
        for p in glob.glob(os.path.join(d, "*.jsonl")):
            sid = os.path.basename(p)[:-6]
            out.append(Session("claude", sid, p, cwd or "", os.path.getmtime(p)))
    return out


def parse_claude(s: Session) -> Session:
    msgs = []
    with open(s.path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("isSidechain") or d.get("isMeta"):
                continue
            if not s.cwd and d.get("cwd"):
                s.cwd = d["cwd"]
            t = d.get("type")
            content = (d.get("message") or {}).get("content")
            if t == "user":
                if isinstance(content, str):
                    if not content.lstrip().startswith("<"):
                        msgs.append(Msg("user", content))
                elif isinstance(content, list):
                    for blk in content:
                        if blk.get("type") == "text" and not blk.get("text", "").lstrip().startswith("<"):
                            msgs.append(Msg("user", blk["text"]))
            elif t == "assistant" and isinstance(content, list):
                for blk in content:
                    if blk.get("type") == "text" and blk.get("text", "").strip():
                        msgs.append(Msg("assistant", blk["text"]))
                    elif blk.get("type") == "tool_use":
                        msgs.append(Msg("tool", tool=blk.get("name", ""), args=blk.get("input") or {}))
    s.msgs = msgs
    return s


def codex_sessions(cwd: str | None) -> list:
    out = []
    for p in glob.glob(os.path.join(_home(), ".codex", "sessions", "**", "rollout-*.jsonl"), recursive=True):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                meta = json.loads(fh.readline())
        except Exception:
            continue
        pl = meta.get("payload") or {}
        scwd = pl.get("cwd", "")
        if cwd and scwd != cwd:
            continue
        sid = pl.get("id") or pl.get("session_id") or os.path.basename(p)
        out.append(Session("codex", sid, p, scwd, os.path.getmtime(p)))
    return out


def parse_codex(s: Session) -> Session:
    msgs = []
    with open(s.path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") != "response_item":
                continue
            pl = d.get("payload") or {}
            kind = pl.get("type")
            if kind == "message":
                role = pl.get("role")
                if role not in ("user", "assistant"):
                    continue
                for c in pl.get("content") or []:
                    txt = c.get("text", "")
                    if not txt.strip() or txt.lstrip().startswith("<"):
                        continue
                    msgs.append(Msg(role, txt))
            elif kind == "function_call":
                msgs.append(Msg("tool", tool=pl.get("name", ""), args=_json_or_empty(pl.get("arguments", "")) or {"raw": pl.get("arguments", "")}))
            elif kind == "custom_tool_call":
                msgs.append(Msg("tool", tool=pl.get("name", ""), args={"raw": pl.get("input", "")}))
    s.msgs = msgs
    return s


def _agy_dir() -> str:
    return os.path.join(_home(), ".gemini", "antigravity-cli")


def _agy_workspaces() -> dict:
    """conversationId -> workspace, read from the CLI's history.jsonl."""
    m = {}
    p = os.path.join(_agy_dir(), "history.jsonl")
    if not os.path.exists(p):
        return m
    with open(p, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("conversationId"):
                m[d["conversationId"]] = d.get("workspace", "")
    return m


def agy_sessions(cwd: str | None) -> list:
    ws = _agy_workspaces()
    out = []
    for p in glob.glob(os.path.join(_agy_dir(), "conversations", "*.db")):
        sid = os.path.basename(p)[:-3]
        scwd = ws.get(sid, "")
        if cwd and scwd != cwd:
            continue
        out.append(Session("agy", sid, p, scwd, os.path.getmtime(p)))
    return out


AGY_USER, AGY_ASSISTANT = 14, 15


def parse_agy(s: Session) -> Session:
    msgs = []
    con = sqlite3.connect(f"file:{s.path}?mode=ro", uri=True)
    try:
        rows = con.execute("select idx, step_type, step_payload from steps order by idx").fetchall()
    finally:
        con.close()
    for _idx, st, payload in rows:
        if not payload:
            continue
        if st == AGY_USER:
            t = pb_text(payload, "19.2")
            if t.strip():
                msgs.append(Msg("user", t))
        elif st == AGY_ASSISTANT:
            t = pb_text(payload, "20.1")
            if t.strip():
                msgs.append(Msg("assistant", t))
            name = pb_text(payload, "20.7.2")
            if name:
                msgs.append(Msg("tool", tool=name, args=_json_or_empty(pb_text(payload, "20.7.3"))))
    s.msgs = msgs
    return s


LISTERS = {"claude": claude_sessions, "codex": codex_sessions, "agy": agy_sessions}
PARSERS = {"claude": parse_claude, "codex": parse_codex, "agy": parse_agy}


def list_sessions(cwd: str | None, source: str | None = None) -> list:
    out = []
    for t in TOOLS:
        if source and t != source:
            continue
        out.extend(LISTERS[t](cwd))
    return sorted(out, key=lambda s: s.mtime, reverse=True)


# ----------------------------------------------------------------------------- state extraction

RE_PATCH = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (.+)")


def files_touched(msgs: list) -> list:
    seen, out = set(), []

    def add(p):
        if p and p not in seen:
            seen.add(p)
            out.append(p)

    for m in msgs:
        if m.role != "tool":
            continue
        a = m.args or {}
        if m.tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            add(a.get("file_path") or a.get("notebook_path"))
        elif m.tool in ("write_to_file", "replace_file_content", "multi_replace_file_content", "edit_file", "create_file"):
            add(a.get("TargetFile") or a.get("AbsolutePath") or a.get("target_file") or a.get("path"))
        elif m.tool == "apply_patch":
            for p in RE_PATCH.findall(a.get("input") or a.get("raw") or json.dumps(a)):
                add(p.strip())
    return out


def recent_commands(msgs: list, n: int = 5) -> list:
    out = []
    for m in msgs:
        if m.role != "tool":
            continue
        a = m.args or {}
        c = ""
        if m.tool == "Bash":
            c = a.get("command", "")
        elif m.tool in ("shell", "exec_command", "shell_command", "run_command", "exec"):
            c = a.get("command") or a.get("cmd") or a.get("CommandLine") or a.get("raw") or ""
            if isinstance(c, list):
                c = " ".join(c)
        if c:
            out.append(c.strip().splitlines()[0])
    return out[-n:]


def build_handoff(s: Session, target: str, max_chars: int = MAX_CHARS) -> str:
    users = [m for m in s.msgs if m.role == "user"]
    assistant = [m for m in s.msgs if m.role == "assistant"]
    task = next((m.text for m in users if not _is_handoff(m.text)), "")
    latest = [("[previous handoff omitted]" if _is_handoff(m.text) else m.text) for m in users[-6:]]
    state = assistant[-1].text if assistant else "(the source session has no assistant reply: resume from the user messages)"
    files = files_touched(s.msgs)[-15:]
    commands = recent_commands(s.msgs)
    when = dt.datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M")

    budget = {"state": 1800, "task": 700, "latest": 240, "cmd": 120}
    while True:
        parts = [
            f"{MARK}",
            f"Source: {s.tool} · session {s.id} · {when}",
            f"Full session file: {s.path}",
            f"Working directory: {s.cwd or os.getcwd()} · Target: {target}",
            "",
            "## Task",
            _cut(task, budget["task"]) or "(no readable opening message)",
            "",
            "## Current state (last reply of the source assistant)",
            _cut(state, budget["state"]),
            "",
            "## Latest user messages",
        ]
        parts += [f"- {_cut(u, budget['latest'])}" for u in latest] or ["- (none)"]
        if files:
            parts += ["", "## Files touched"] + [f"- {p}" for p in files]
        if commands:
            parts += ["", "## Recent commands"] + [f"- `{_cut(c, budget['cmd'])}`" for c in commands]
        parts += [
            "",
            "## How to continue",
            "Continue from the current state. Do not redo what is done or re-ask what is answered.",
            f"If a detail is missing, read the full session with `gearbox --read {s.tool} {s.id}` before asking.",
            "Long-running processes go through `gearbox --bg <cmd>` so they survive the next switch.",
        ]
        text = "\n".join(parts)
        if len(text) <= max_chars:
            return text
        # trim order: commands, files, latest messages, state, task
        if commands:
            commands = []
        elif files:
            files = []
        elif budget["latest"] > 80:
            budget["latest"] = 80
        elif budget["state"] > 400:
            budget["state"] = max(400, budget["state"] // 2)
        elif budget["task"] > 200:
            budget["task"] = 200
        else:
            return text[:max_chars]


def dump(s: Session) -> str:
    out = [f"# Session {s.tool} {s.id}", f"File: {s.path}", f"Directory: {s.cwd}", ""]
    for m in s.msgs:
        if m.role == "tool":
            a = json.dumps(m.args, ensure_ascii=False)
            out.append(f"**[tool {m.tool}]** {a[:300]}")
        else:
            out.append(f"**{m.role}:**\n{m.text.strip()}")
        out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------------- launching and jobs

def target_command(target: str, prompt: str | None) -> list:
    if target not in TOOLS:
        raise ValueError(target)
    if not prompt:
        return [target]
    if target == "agy":
        return ["agy", "-i", prompt]
    return [target, prompt]


def save_handoff(text: str, source: str, target: str) -> str:
    d = os.path.join(GEARBOX_DIR, "handoffs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{dt.datetime.now():%Y-%m-%d-%H%M%S}-{source}-to-{target}.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


def start_job(cmd: list, cwd: str) -> dict:
    d = os.path.join(GEARBOX_DIR, "jobs")
    os.makedirs(d, exist_ok=True)
    ts = f"{dt.datetime.now():%Y-%m-%d-%H%M%S}"
    log = os.path.join(d, f"{ts}.log")
    with open(log, "ab") as fh:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    rec = {"pid": p.pid, "cmd": cmd, "cwd": cwd, "log": log, "started": ts}
    with open(os.path.join(d, f"{ts}.json"), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False)
    return rec


def _alive(pid: int) -> bool:
    """Truly alive: neither finished nor a zombie. Our own children get reaped right here."""
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return True
    return bool(state) and not state.startswith("Z")


def list_jobs() -> list:
    d = os.path.join(GEARBOX_DIR, "jobs")
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            continue
        rec["alive"] = _alive(int(rec["pid"]))
        out.append(rec)
    return out


INTERPRETERS = {"sh", "bash", "zsh", "dash", "node", "python", "python3", "env"}


def _proc(pid: int):
    """(ppid, comm, command) of a process, or None if it does not exist."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    parts = out.split(None, 2)
    if len(parts) < 2:
        return None
    return int(parts[0]), parts[1], (parts[2] if len(parts) > 2 else "")


def _tool_of(comm: str, command: str) -> str | None:
    """Which CLI a process is, judged by its executable name; a script run through an interpreter counts too."""
    names = [os.path.basename(comm)]
    toks = command.split()
    if toks:
        names.append(os.path.basename(toks[0]))
        if os.path.basename(toks[0]) in INTERPRETERS and len(toks) > 1 and not toks[1].startswith("-"):
            names.append(os.path.basename(toks[1]))
    for n in names:
        for t in TOOLS:
            if n == t or n.startswith(t + "-") or n.startswith(t + "."):
                return t
    return None


def host_process(start_pid: int | None = None):
    """(tool, pid) of the AI CLI this process runs inside, walking up the parent chain; None outside any.
    GEARBOX_IGNORE_HOST=1 disables the lookup (the test suite runs inside a CLI itself)."""
    if os.environ.get("GEARBOX_IGNORE_HOST"):
        return None
    pid = start_pid or os.getppid()
    for _ in range(12):
        if pid <= 1:
            return None
        info = _proc(pid)
        if not info:
            return None
        ppid, comm, command = info
        t = _tool_of(comm, command)
        if t:
            return t, pid
        pid = ppid
    return None


def _next_path() -> str:
    return os.path.join(GEARBOX_DIR, "next")


def request_switch(target: str, host_tool: str, host_pid: int, err=sys.stderr) -> None:
    """From inside a CLI run by the loop: leave a note for the loop and close the host CLI."""
    os.makedirs(GEARBOX_DIR, exist_ok=True)
    with open(_next_path(), "w", encoding="utf-8") as fh:
        fh.write(target)
    print(f"Switching {host_tool} → {target}: closing {host_tool}, the loop opens {target} with the handoff.", file=err)
    os.kill(host_pid, signal.SIGTERM)
    for _ in range(30):
        if not _alive(host_pid):
            return
        time.sleep(0.1)
    os.kill(host_pid, signal.SIGKILL)


def _take_next() -> str | None:
    p = _next_path()
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        nxt = fh.read().strip().lower()
    os.remove(p)
    return nxt


def _run_cli(cmd: list, cwd: str, env: dict) -> None:
    """Run a CLI in the foreground. Ctrl+C belongs to the CLI, not to the loop; the terminal is
    put back in order afterwards in case the CLI was closed from inside."""
    p = subprocess.Popen(cmd, cwd=cwd, env=env)
    while True:
        try:
            p.wait()
            break
        except KeyboardInterrupt:
            continue
    if sys.stdin.isatty():
        subprocess.run(["stty", "sane"], stdin=sys.stdin, check=False)


def loop(start: str, cwd: str, ask=input, run=_run_cli, err=sys.stderr, first_prompt: str | None = None) -> int:
    """One tab: launch a CLI and, when it closes, open the next one with the handoff of the session
    just closed. The next CLI comes from `gearbox <cli>` run inside the chat, or from the question
    asked on exit. Enter, `quit` or Ctrl+C at that question end the loop."""
    current, prompt = start, first_prompt
    _take_next()                                   # a stale note must not decide the first switch
    while True:
        run(target_command(current, prompt), cwd=cwd, env={**os.environ, "GEARBOX_LOOP": "1", "GEARBOX_HOST": current})
        answer = _take_next()
        if answer in TOOLS:
            print(f"[gearbox] switch requested from inside {current}: → {answer}", file=err)
        else:
            try:
                answer = ask(f"[gearbox] you left {current}. Switch to? (claude/codex/agy · Enter = quit) ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("", file=err)
                return 0
            if answer not in TOOLS:
                return 0
        prompt = None
        s = _find(cwd, current, None)
        if s:
            PARSERS[current](s)
            if s.msgs:
                prompt = build_handoff(s, answer)
                p = save_handoff(prompt, current, answer)
                print(f"Handoff {current} → {answer}: {len(prompt)} chars (≈{len(prompt)//4} tokens), saved at {p}", file=err)
        if prompt is None:
            print(f"No readable {current} session in this folder: {answer} starts without a handoff.", file=err)
        current = answer


# ----------------------------------------------------------------------------- CLI

def _find(cwd: str, source: str | None, session: str | None) -> Session | None:
    cand = list_sessions(cwd if not session else None, source)
    if session:
        cand = [s for s in cand if s.id.startswith(session)]
    return cand[0] if cand else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gearbox", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", choices=TOOLS, help="CLI to switch to")
    ap.add_argument("--from", dest="source", choices=TOOLS, help="source CLI (default: the latest session in this folder)")
    ap.add_argument("--session", help="id prefix of the source session")
    ap.add_argument("--dry-run", action="store_true", help="print the handoff and launch nothing")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS, help=f"handoff ceiling (default {MAX_CHARS})")
    ap.add_argument("--list", action="store_true", help="sessions in this folder")
    ap.add_argument("--all", action="store_true", help="with --list: every folder")
    ap.add_argument("--read", nargs=2, metavar=("CLI", "ID"), help="dump a full session")
    ap.add_argument("--bg", nargs=argparse.REMAINDER, help="start a process that survives switching CLIs")
    ap.add_argument("--jobs", action="store_true", help="list background processes")
    ap.add_argument("--loop", choices=TOOLS, metavar="CLI", help="one tab: when you exit a CLI, ask which one to switch to and open it with the handoff")
    a = ap.parse_args(argv)
    cwd = os.getcwd()

    if a.loop:
        first = None
        if a.source or a.session:
            s = _find(cwd, a.source, a.session)
            if not s:
                print("No source session" + (f" from {a.source}" if a.source else "") + " in this folder.", file=sys.stderr)
                return 2
            PARSERS[s.tool](s)
            if s.msgs and s.tool != a.loop:
                first = build_handoff(s, a.loop, a.max_chars)
                p = save_handoff(first, s.tool, a.loop)
                print(f"Handoff {s.tool} → {a.loop}: {len(first)} chars (≈{len(first)//4} tokens), saved at {p}", file=sys.stderr)
        return loop(a.loop, cwd, first_prompt=first)

    if a.jobs:
        recs = list_jobs()
        if not recs:
            print("No background jobs recorded.")
        for r in recs:
            print(f"{'ALIVE' if r['alive'] else 'DONE '} pid {r['pid']:>6}  {r['started']}  {' '.join(r['cmd'])[:70]}  → {r['log']}")
        return 0

    if a.bg is not None:
        if not a.bg:
            print("gearbox --bg <command...>", file=sys.stderr)
            return 4
        r = start_job(a.bg, cwd)
        print(f"Started pid {r['pid']} · log {r['log']}")
        return 0

    if a.list:
        ses = list_sessions(None if a.all else cwd)
        if not ses:
            print("No sessions" + ("" if a.all else " in this folder") + ".")
            return 2
        for s in ses[:30]:
            print(f"{s.tool:6} {s.short}  {dt.datetime.fromtimestamp(s.mtime):%Y-%m-%d %H:%M}  {s.cwd}")
        return 0

    if a.read:
        tool, sid = a.read
        if tool not in TOOLS:
            print(f"Unknown CLI: {tool}", file=sys.stderr)
            return 4
        s = _find(cwd, tool, sid)
        if not s:
            print(f"No {tool} session starting with {sid}.", file=sys.stderr)
            return 2
        PARSERS[tool](s)
        if not s.msgs:
            print(f"Session {s.id} exists but has no readable messages.", file=sys.stderr)
            return 3
        print(dump(s))
        return 0

    if not a.target:
        ap.print_help()
        return 4

    host = None if a.dry_run else host_process()
    if host:
        host_tool, host_pid = host
        if host_tool == a.target:
            print(f"You are already inside {host_tool}.", file=sys.stderr)
            return 4
        if os.environ.get("GEARBOX_LOOP"):
            request_switch(a.target, host_tool, host_pid)
            return 0
        print(f"You are inside {host_tool}, outside a gearbox loop. Exit {host_tool} and run "
              f"`gearbox {a.target} --from {host_tool}`, or start next time with `gearbox --loop {host_tool}`.", file=sys.stderr)
        return 4

    s = _find(cwd, a.source, a.session)
    if not s:
        print("No source session" + (f" from {a.source}" if a.source else "") + " in this folder. Try `gearbox --list --all`.", file=sys.stderr)
        return 2
    PARSERS[s.tool](s)
    if not s.msgs:
        print(f"Session {s.tool} {s.id} exists but has no readable messages. Nothing launched.", file=sys.stderr)
        return 3
    if s.tool == a.target and not a.dry_run:
        print(f"The latest session is already {s.tool}. To pick it up, use its own resume.", file=sys.stderr)
        return 4
    text = build_handoff(s, a.target, a.max_chars)
    if a.dry_run:
        print(text)
        sys.stdout.flush()
        print(f"\n[{len(text)} chars ≈ {len(text)//4} tokens]", file=sys.stderr)
        return 0
    p = save_handoff(text, s.tool, a.target)
    print(f"Handoff {s.tool} → {a.target}: {len(text)} chars (≈{len(text)//4} tokens), saved at {p}", file=sys.stderr)
    cmd = target_command(a.target, text)
    try:
        os.execvp(cmd[0], cmd)
    except FileNotFoundError:
        print(f"Executable `{cmd[0]}` not found in PATH.", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
