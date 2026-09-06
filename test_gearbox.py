"""Tests for `gearbox`. They run with HOME pointing at a temporary folder holding synthetic sessions
of the three CLIs, plus negative controls: a folder with no sessions and a session with no readable messages.

    python3 -m unittest -v test_gearbox
"""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gearbox  # noqa: E402


# ----------------------------------------------------------------------------- test protobuf encoder

def _vint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb(fields: dict) -> bytes:
    """{number: value} → bytes. value: str, bytes (nested message), int, or a list of them."""
    out = bytearray()
    for f, vals in fields.items():
        for v in (vals if isinstance(vals, list) else [vals]):
            if isinstance(v, int):
                out += _vint((f << 3) | 0) + _vint(v)
            else:
                b = v.encode("utf-8") if isinstance(v, str) else v
                out += _vint((f << 3) | 2) + _vint(len(b)) + b
    return bytes(out)


# ----------------------------------------------------------------------------- fixtures

CWD = os.path.realpath("/tmp/gearbox-test-project")   # os.getcwd() returns the real path; on macOS /tmp is a symlink
FOREIGN_HANDOFF = "I'm continuing a coding session from **Claude Code** ...\n\n## Session Handoff Context\nlots of text"


def write_claude(home, sid="aaaa1111-0000-0000-0000-000000000000", messages=None, mtime=None):
    d = os.path.join(home, ".claude", "projects", gearbox.re.sub(r"[^A-Za-z0-9]", "-", CWD))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{sid}.jsonl")
    base = {"cwd": CWD, "sessionId": sid, "isSidechain": False}
    lines = messages if messages is not None else [
        {**base, "type": "user", "message": {"role": "user", "content": "Fix the revenue validator"}},
        {**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Looking at the engine."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls engines/"}},
        ]}},
        {**base, "type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "a.py"}]}},
        {**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/tmp/gearbox-test-project/engines/a.py", "old_string": "x", "new_string": "y"}},
        ]}},
        {**base, "type": "user", "isSidechain": True, "message": {"role": "user", "content": "I AM A SUBAGENT: must not appear"}},
        {**base, "type": "user", "message": {"role": "user", "content": FOREIGN_HANDOFF}},
        {**base, "type": "user", "message": {"role": "user", "content": "<local-command-stdout>noise</local-command-stdout>"}},
        {**base, "type": "user", "message": {"role": "user", "content": "Now run the tests"}},
        {**base, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "FINAL STATE: 3 tests green, push pending."},
        ]}},
    ]
    with open(p, "w", encoding="utf-8") as fh:
        for l in lines:
            fh.write(json.dumps(l, ensure_ascii=False) + "\n")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def write_codex(home, sid="bbbb2222-0000-0000-0000-000000000000", cwd=CWD, mtime=None):
    d = os.path.join(home, ".codex", "sessions", "2026", "09", "05")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"rollout-2026-09-05T10-00-00-{sid}.jsonl")
    lines = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": cwd}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<skills_instructions>noise"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context><cwd>x</cwd></environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Generate the weekly report"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "zzz"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["bash", "-lc", "make report"]})}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "arguments": json.dumps({"input": "*** Begin Patch\n*** Update File: report/week.md\n+hello\n*** End Patch"})}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Report generated at report/week.md."}]}},
    ]
    with open(p, "w", encoding="utf-8") as fh:
        for l in lines:
            fh.write(json.dumps(l, ensure_ascii=False) + "\n")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def write_agy(home, sid="cccc3333-0000-0000-0000-000000000000", steps=None, mtime=None, workspace=CWD):
    d = os.path.join(home, ".gemini", "antigravity-cli", "conversations")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(home, ".gemini", "antigravity-cli", "history.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"display": "x", "workspace": workspace, "conversationId": sid}) + "\n")
    p = os.path.join(d, f"{sid}.db")
    con = sqlite3.connect(p)
    con.execute("create table steps (idx integer, step_type integer, status integer, step_payload blob)")
    if steps is None:
        steps = [
            (0, gearbox.AGY_USER, pb({19: pb({2: "Review the payroll transfer"})})),
            (1, gearbox.AGY_ASSISTANT, pb({20: pb({7: pb({2: "run_command", 3: json.dumps({"CommandLine": "pytest -q"})})})})),
            (2, gearbox.AGY_ASSISTANT, pb({20: pb({7: pb({2: "write_to_file", 3: json.dumps({"TargetFile": "/tmp/gearbox-test-project/payroll.py"})})})})),
            (3, gearbox.AGY_ASSISTANT, pb({20: pb({1: "Payroll reconciled to the cent.", 3: "internal thinking"})})),
        ]
    con.executemany("insert into steps values (?,?,3,?)", steps)
    con.commit()
    con.close()
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self._home_prev = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        gearbox.GEARBOX_DIR = os.path.join(self.home, ".gearbox")
        self._cwd_prev = os.getcwd()
        os.makedirs(CWD, exist_ok=True)
        os.chdir(CWD)

    def tearDown(self):
        os.chdir(self._cwd_prev)
        if self._home_prev is not None:
            os.environ["HOME"] = self._home_prev
        self.tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = gearbox.main(list(args))
        return rc, out.getvalue(), err.getvalue()


class Parsers(Base):
    def test_claude(self):
        write_claude(self.home)
        s = gearbox.parse_claude(gearbox.claude_sessions(CWD)[0])
        roles = [m.role for m in s.msgs]
        self.assertEqual(roles.count("user"), 3)               # task, foreign handoff, "run the tests"
        self.assertNotIn("I AM A SUBAGENT", gearbox.dump(s))
        self.assertNotIn("noise", gearbox.dump(s))
        self.assertEqual(gearbox.files_touched(s.msgs), ["/tmp/gearbox-test-project/engines/a.py"])
        self.assertEqual(gearbox.recent_commands(s.msgs), ["ls engines/"])

    def test_codex(self):
        write_codex(self.home)
        s = gearbox.parse_codex(gearbox.codex_sessions(CWD)[0])
        self.assertEqual([m.text for m in s.msgs if m.role == "user"], ["Generate the weekly report"])
        self.assertEqual([m.text for m in s.msgs if m.role == "assistant"], ["Report generated at report/week.md."])
        self.assertEqual(gearbox.files_touched(s.msgs), ["report/week.md"])
        self.assertEqual(gearbox.recent_commands(s.msgs), ["bash -lc make report"])

    def test_codex_filters_by_folder(self):
        write_codex(self.home, cwd="/another/folder")
        self.assertEqual(gearbox.codex_sessions(CWD), [])
        self.assertEqual(len(gearbox.codex_sessions(None)), 1)

    def test_agy(self):
        write_agy(self.home)
        s = gearbox.parse_agy(gearbox.agy_sessions(CWD)[0])
        self.assertEqual([m.text for m in s.msgs if m.role == "user"], ["Review the payroll transfer"])
        self.assertEqual([m.text for m in s.msgs if m.role == "assistant"], ["Payroll reconciled to the cent."])
        self.assertNotIn("internal thinking", gearbox.dump(s))
        self.assertEqual(gearbox.files_touched(s.msgs), ["/tmp/gearbox-test-project/payroll.py"])
        self.assertEqual(gearbox.recent_commands(s.msgs), ["pytest -q"])

    def test_agy_multibyte_text(self):
        write_agy(self.home, steps=[(0, gearbox.AGY_USER, pb({19: pb({2: "Añade la columna «Año» — ¿sí?"})}))])
        s = gearbox.parse_agy(gearbox.agy_sessions(CWD)[0])
        self.assertEqual(s.msgs[0].text, "Añade la columna «Año» — ¿sí?")


class Handoff(Base):
    def test_content(self):
        write_claude(self.home)
        s = gearbox.parse_claude(gearbox.claude_sessions(CWD)[0])
        t = gearbox.build_handoff(s, "codex")
        self.assertTrue(t.startswith(gearbox.MARK))
        self.assertIn("Fix the revenue validator", t)                 # task = first real message
        self.assertIn("FINAL STATE: 3 tests green", t)                # state = last reply
        self.assertIn("/tmp/gearbox-test-project/engines/a.py", t)
        self.assertIn("`ls engines/`", t)
        self.assertIn("gearbox --read claude aaaa1111", t)
        self.assertIn("Target: codex", t)

    def test_nested_handoff_is_omitted(self):
        write_claude(self.home)
        s = gearbox.parse_claude(gearbox.claude_sessions(CWD)[0])
        t = gearbox.build_handoff(s, "agy")
        self.assertNotIn("Session Handoff Context", t)
        self.assertIn("[previous handoff omitted]", t)
        # the task can never be a handoff, even when it is the first message
        write_claude(self.home, sid="dddd4444-0000-0000-0000-000000000000", messages=[
            {"cwd": CWD, "type": "user", "message": {"role": "user", "content": FOREIGN_HANDOFF}},
            {"cwd": CWD, "type": "user", "message": {"role": "user", "content": "The real task"}},
        ])
        s2 = gearbox.parse_claude([x for x in gearbox.claude_sessions(CWD) if x.id.startswith("dddd")][0])
        self.assertIn("## Task\nThe real task", gearbox.build_handoff(s2, "codex"))

    def test_legacy_spanish_handoff_is_omitted(self):
        write_claude(self.home, messages=[
            {"cwd": CWD, "type": "user", "message": {"role": "user", "content": "# Traspaso de sesión\nOrigen: agy\n\n## Encargo\nold"}},
            {"cwd": CWD, "type": "user", "message": {"role": "user", "content": "The real task"}},
        ])
        t = gearbox.build_handoff(gearbox.parse_claude(gearbox.claude_sessions(CWD)[0]), "codex")
        self.assertIn("## Task\nThe real task", t)
        self.assertNotIn("Traspaso", t)

    def test_size_ceiling(self):
        big = "word " * 20000
        write_claude(self.home, messages=[
            {"cwd": CWD, "type": "user", "message": {"role": "user", "content": big}},
            {"cwd": CWD, "type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": big}]}},
        ] + [
            {"cwd": CWD, "type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": f"/tmp/f{i}.py", "content": ""}}]}} for i in range(15)
        ])
        s = gearbox.parse_claude(gearbox.claude_sessions(CWD)[0])
        t = gearbox.build_handoff(s, "codex")
        self.assertLessEqual(len(t), gearbox.MAX_CHARS)
        self.assertIn("## Current state", t)                          # trimming keeps the fixed sections
        self.assertIn("## How to continue", t)
        t2 = gearbox.build_handoff(s, "codex", max_chars=3000)
        self.assertLessEqual(len(t2), 3000)

    def test_no_assistant_reply(self):
        write_claude(self.home, messages=[{"cwd": CWD, "type": "user", "message": {"role": "user", "content": "Just asked"}}])
        s = gearbox.parse_claude(gearbox.claude_sessions(CWD)[0])
        self.assertIn("has no assistant reply", gearbox.build_handoff(s, "agy"))


class Selection(Base):
    def test_newest_across_clis(self):
        now = time.time()
        write_claude(self.home, mtime=now - 300)
        write_codex(self.home, mtime=now - 100)
        write_agy(self.home, mtime=now - 200)
        ses = gearbox.list_sessions(CWD)
        self.assertEqual([s.tool for s in ses], ["codex", "agy", "claude"])
        self.assertEqual(gearbox.list_sessions(CWD, "agy")[0].tool, "agy")

    def test_by_prefix(self):
        write_claude(self.home)
        write_agy(self.home)
        rc, out, _ = self.run_cli("codex", "--session", "cccc", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("Source: agy", out)

    def test_same_cli_does_not_launch(self):
        write_claude(self.home)
        rc, _, err = self.run_cli("claude")
        self.assertEqual(rc, 4)
        self.assertIn("already claude", err)


class NegativeControls(Base):
    def test_no_sessions_rc2(self):
        rc, _, err = self.run_cli("codex", "--dry-run")
        self.assertEqual(rc, 2)
        self.assertIn("No source session", err)
        rc, out, _ = self.run_cli("--list")
        self.assertEqual(rc, 2)

    def test_unreadable_session_rc3(self):
        write_agy(self.home, steps=[(0, 99, b"\x00\x01\x02")])
        rc, _, err = self.run_cli("claude", "--dry-run")
        self.assertEqual(rc, 3)
        self.assertIn("no readable messages", err)
        rc, _, err = self.run_cli("--read", "agy", "cccc")
        self.assertEqual(rc, 3)

    def test_no_target_rc4(self):
        rc, _, _ = self.run_cli()
        self.assertEqual(rc, 4)

    def test_broken_protobuf_does_not_crash(self):
        self.assertEqual(gearbox.pb_text(b"\xff\xff\xff", "1"), "")
        self.assertEqual(gearbox.pb_text(b"", "20.1"), "")


class Jobs(Base):
    def test_job_survives_and_is_listed(self):
        rc, out, _ = self.run_cli("--bg", "sh", "-c", "echo hello; sleep 1")
        self.assertEqual(rc, 0)
        self.assertIn("Started pid", out)
        rc, out, _ = self.run_cli("--jobs")
        self.assertIn("ALIVE", out)
        time.sleep(1.5)
        rc, out, _ = self.run_cli("--jobs")
        self.assertIn("DONE", out)
        log = [r["log"] for r in gearbox.list_jobs()][0]
        with open(log, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "hello")

    def test_bg_without_command_rc4(self):
        rc, _, _ = self.run_cli("--bg")
        self.assertEqual(rc, 4)

    def test_foreign_zombie_is_not_alive(self):
        """A zombie we did not parent: waitpid does not apply, kill(0) answers, only `ps` gives it away."""
        code = "import os,time\npid=os.fork()\nif pid==0: os._exit(0)\nprint(pid,flush=True)\ntime.sleep(5)"
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            zpid = int(p.stdout.readline())
            time.sleep(0.3)
            self.assertFalse(gearbox._alive(zpid))
            self.assertTrue(gearbox._alive(p.pid))
        finally:
            p.kill()
            p.wait()


class Launch(Base):
    def test_command_per_target(self):
        self.assertEqual(gearbox.target_command("claude", "P"), ["claude", "P"])
        self.assertEqual(gearbox.target_command("codex", "P"), ["codex", "P"])
        self.assertEqual(gearbox.target_command("agy", "P"), ["agy", "-i", "P"])
        self.assertEqual(gearbox.target_command("agy", None), ["agy"])
        self.assertEqual(gearbox.target_command("claude", ""), ["claude"])
        with self.assertRaises(ValueError):
            gearbox.target_command("cursor", "P")

    def test_launches_with_fake_executable(self):
        """With a fake `codex` on PATH, `gearbox codex` saves the handoff and hands it over whole."""
        write_claude(self.home)
        bindir = os.path.join(self.home, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "codex")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%s' \"$1\" > \"$HOME/received.md\"\n")
        os.chmod(fake, 0o755)
        env = {**os.environ, "PATH": bindir + os.pathsep + os.environ["PATH"], "HOME": self.home}
        r = subprocess.run([sys.executable, gearbox.__file__, "codex"], cwd=CWD, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Handoff claude → codex", r.stderr)
        with open(os.path.join(self.home, "received.md"), encoding="utf-8") as fh:
            received = fh.read()
        self.assertTrue(received.startswith(gearbox.MARK))
        self.assertIn("FINAL STATE", received)
        saved = os.listdir(os.path.join(self.home, ".gearbox", "handoffs"))
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].endswith("-claude-to-codex.md"))


class Loop(Base):
    def _run_loop(self, start, answers):
        launched = []
        answers = list(answers)

        def ask(_msg):
            r = answers.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r

        def run(cmd, cwd=None):
            launched.append((cmd, cwd))

        err = io.StringIO()
        rc = gearbox.loop(start, CWD, ask=ask, run=run, err=err)
        return rc, launched, err.getvalue()

    def test_switches_in_the_same_tab_with_handoff(self):
        write_claude(self.home)
        rc, launched, err = self._run_loop("claude", ["codex", ""])
        self.assertEqual(rc, 0)
        self.assertEqual(launched[0], (["claude"], CWD))              # the first one starts clean
        cmd, cwd = launched[1]
        self.assertEqual(cmd[0], "codex")
        self.assertTrue(cmd[1].startswith(gearbox.MARK))              # the second one gets the handoff
        self.assertIn("FINAL STATE", cmd[1])
        self.assertIn("Target: codex", cmd[1])
        self.assertEqual(len(launched), 2)                              # Enter = quit
        self.assertIn("Handoff claude → codex", err)
        self.assertEqual(len(os.listdir(os.path.join(self.home, ".gearbox", "handoffs"))), 1)

    def test_no_session_starts_without_handoff(self):
        rc, launched, err = self._run_loop("codex", ["agy", "quit"])
        self.assertEqual(rc, 0)
        self.assertEqual([c for c, _ in launched], [["codex"], ["agy"]])
        self.assertIn("starts without a handoff", err)

    def test_ctrl_c_quits(self):
        rc, launched, _ = self._run_loop("agy", [KeyboardInterrupt()])
        self.assertEqual(rc, 0)
        self.assertEqual(len(launched), 1)

    def test_answer_with_caps_and_spaces(self):
        write_agy(self.home)
        rc, launched, _ = self._run_loop("agy", ["  Claude ", ""])
        self.assertEqual(launched[1][0][0], "claude")
        self.assertIn("Source: agy", launched[1][0][1])

    def test_loop_from_main_with_fake_executable(self):
        """Real wiring: `gearbox --loop claude` runs the executable, and Enter ends with rc 0."""
        bindir = os.path.join(self.home, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "claude")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\necho started > \"$HOME/started.txt\"\n")
        os.chmod(fake, 0o755)
        env = {**os.environ, "PATH": bindir + os.pathsep + os.environ["PATH"], "HOME": self.home}
        r = subprocess.run([sys.executable, gearbox.__file__, "--loop", "claude"], cwd=CWD, env=env,
                           input="\n", capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.home, "started.txt")))
        self.assertIn("you left claude", r.stdout)


if __name__ == "__main__":
    unittest.main()
