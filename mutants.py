"""Mutant bank for gearbox.py: every mutant must turn at least one test red.
Copies this folder to a temporary one, applies ONE change, runs the suite and reports.

    python3 mutants.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

SRC = os.path.dirname(os.path.abspath(__file__))
MUTANTS = [
    ("nested_handoff_not_omitted", "return head.startswith(MARK) or any(m in head for m in FOREIGN_MARKS)", "return False"),
    ("no_size_ceiling", "if len(text) <= max_chars:", "if True:"),
    ("agy_user_wrong_field", 'pb_text(payload, "19.2")', 'pb_text(payload, "19.3")'),
    ("agy_assistant_reads_thinking", 'pb_text(payload, "20.1")', 'pb_text(payload, "20.3")'),
    ("claude_includes_subagents", 'if d.get("isSidechain") or d.get("isMeta"):', 'if d.get("isMeta"):'),
    ("codex_ignores_folder", "        if not _covers(scwd, cwd):\n            continue\n        sid = pl.get(\"id\")", "        sid = pl.get(\"id\")"),
    ("state_takes_first_reply", "last = turns[-1] if turns else []", "last = turns[0] if turns else []"),
    ("state_keeps_the_head_not_the_tail", "    for f in reversed(frags):", "    for f in frags:"),
    ("rc3_becomes_rc0", 'print("The sessions found have no readable messages. Nothing launched.", file=sys.stderr)\n            return 3', 'print("", file=sys.stderr)\n            return 0'),
    ("rc3_becomes_rc0_with_session", 'print(f"Session {s.tool} {s.id} exists but has no readable messages. Nothing launched.", file=sys.stderr)\n            return 3', 'print("", file=sys.stderr)\n            return 0'),
    ("zombie_counts_as_alive", 'return bool(state) and not state.startswith("Z")', "return True"),
    ("agy_launched_without_-i", 'return ["agy", "-i", prompt]', 'return ["agy", prompt]'),
    ("loop_drops_handoff", "prompt = build_handoff(s, answer)", "prompt = None"),
    ("loop_launches_blind", 'cmd = target_command(current, prompt or (PRIME if current != "claude" else None))', "cmd = target_command(current, None)"),
    ("loop_ignores_enter", "if answer not in TOOLS:\n                return 0", 'if answer == "quit":\n                return 0'),
    ("loop_ignores_switch_note", "        answer = _take_next()\n        if answer in TOOLS:", "        answer = None\n        if answer in TOOLS:"),
    ("host_not_closed", "    os.kill(host_pid, signal.SIGTERM)\n    for _ in range(30):", "    return\n    for _ in range(30):"),
    ("host_detected_by_any_token", "if os.path.basename(toks[0]) in INTERPRETERS and len(toks) > 1 and not toks[1].startswith(\"-\"):", "if len(toks) > 1:"),
    ("host_detected_by_prefix", 'RE_TOOL_NAME = re.compile(r"^(claude|codex|agy)(?:-bin|', 'RE_TOOL_NAME = re.compile(r"^(claude|codex|agy)(?:-.*|-bin|'),
    ("host_npm_install_missed", 'PATH_HINTS = (("claude", "@anthropic-ai/claude-code/"), ("claude", "/claude-code/cli.js"), ("codex", "@openai/codex/"))', "PATH_HINTS = ()"),
    ("loop_child_of_another_tool_accepted", "return (want, pid) if t in (None, want) else None", "return (want, pid)"),
    ("sigkill_without_recheck", "    if _proc(host_pid) == identity:", "    if True:"),
    ("loop_first_handoff_dropped", "return loop(a.loop, cwd, first_prompt=first)", "return loop(a.loop, cwd, first_prompt=None)"),
    ("clean_launch_without_switch_rule", 'prompt or (PRIME if current != "claude" else None)', "prompt"),
    ("handoff_without_switch_rule", "            SWITCH_RULE,\n        ]", "        ]"),
    ("redaction_off", 'text = redact("\\n".join(parts))', 'text = "\\n".join(parts)'),
    ("fence_off", 'return re.sub(r"(?m)^([ \\t]{0,3})#", r"\\1\\\\#", text)', "return text"),
    ("long_path_accepted", "return isinstance(p, str) and 0 < len(p) <= 300 and \"\\n\" not in p", "return isinstance(p, str) and len(p) > 0"),
    ("handoff_world_readable", "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600", "os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644"),
    ("handoffs_never_purged", "_purge(glob.glob(os.path.join(d, \"*.md\")), keep=KEEP_HANDOFFS)", "pass"),
    ("next_note_follows_symlink", "fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)", "fd = os.open(path, os.O_RDONLY)"),
    ("scope_ignores_parent_folders", "        parent = os.path.dirname(p)\n        if p == home or parent == p:\n            return out", "        return out"),
    ("session_prefix_searches_everywhere", "    if a.session:\n        s = _find(scope, a.source, a.session)", "    if a.session:\n        s = _find(None, a.source, a.session)"),
    ("agy_db_workspace_ignored", "scwd = ws.get(sid) or _agy_workspace_from_db(p)", 'scwd = ws.get(sid) or ""'),
    ("previous_reply_dropped", 'previous = turns[-2] if len(turns) > 1 and len("\\n".join(last)) < 300 else []', "previous = []"),
    ("task_not_taken_from_handoff", "mm = RE_TASK.search(t)", "mm = None"),
    ("switch_lines_carried", 'users = [m for m in s.msgs if m.role == "user" and not _is_switch_line(m.text)]', 'users = [m for m in s.msgs if m.role == "user"]'),
    ("max_chars_floor_off", "    if n < MIN_CHARS:", "    if False:"),
    ("corrupt_db_crashes", '        s.msgs, s.error = [], f"{type(e).__name__}: {e}"\n        return s', "        raise"),
    ("unreadable_newest_stops_the_switch", "        print(f\"Skipping {s.tool} {s.short}: {s.error or 'no readable messages'}.\", file=err)\n    return None", "        return None\n    return None"),
    ("missing_executable_kills_the_loop", "        print(f\"Executable `{cmd[0]}` could not start: {e.strerror}.\", file=err)\n        return False", "        raise"),
    ("usage_error_rc2", "raise SystemExit(4)", "raise SystemExit(2)"),
    ("recycled_pid_alive", "return any(t in live for t in tokens)", "return True"),
    ("bg_shell_line_not_wrapped", '        cmd = ["sh", "-c", cmd[0]]', "        pass"),
    ("unidentified_host_in_loop_launches_anyway", '    if os.environ.get("GEARBOX_LOOP") and not a.dry_run:', "    if False:"),
]


def main():
    with open(os.path.join(SRC, "gearbox.py"), encoding="utf-8") as fh:
        source = fh.read()
    red = 0
    for name, old, new in MUTANTS:
        if old not in source:
            print(f"?? {name}: pattern not found in the code (mutant not applied)")
            continue
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(SRC, os.path.join(d, "m"), ignore=shutil.ignore_patterns("__pycache__", ".git"))
            with open(os.path.join(d, "m", "gearbox.py"), "w", encoding="utf-8") as fh:
                fh.write(source.replace(old, new, 1))
            r = subprocess.run([sys.executable, "-m", "unittest", "test_gearbox"], cwd=os.path.join(d, "m"),
                               capture_output=True, text=True, timeout=600)
            failures = re.findall(r"^(?:FAIL|ERROR): (\S+)", r.stdout + r.stderr, re.M)
            if failures:
                red += 1
                print(f"OK {name}: caught by {', '.join(sorted(set(failures))[:3])}")
            else:
                print(f"XX {name}: NO test catches it")
    print(f"\n{red}/{len(MUTANTS)} mutants caught")
    return 0 if red == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(main())
