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
    ("codex_ignores_folder", "if cwd and scwd != cwd:", "if False:"),
    ("state_takes_first_reply", "state = assistant[-1].text if assistant", "state = assistant[0].text if assistant"),
    ("rc3_becomes_rc0", 'print(f"Session {s.tool} {s.id} exists but has no readable messages. Nothing launched.", file=sys.stderr)\n        return 3', 'print("", file=sys.stderr)\n        return 0'),
    ("zombie_counts_as_alive", 'return bool(state) and not state.startswith("Z")', "return True"),
    ("agy_launched_without_-i", 'return ["agy", "-i", prompt]', 'return ["agy", prompt]'),
    ("loop_drops_handoff", "prompt = build_handoff(s, answer)", "prompt = None"),
    ("loop_launches_blind", "run(target_command(current, prompt), cwd=cwd)", "run(target_command(current, None), cwd=cwd)"),
    ("loop_ignores_enter", "if answer not in TOOLS:\n            return 0", 'if answer == "quit":\n            return 0'),
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
                               capture_output=True, text=True, timeout=180)
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
