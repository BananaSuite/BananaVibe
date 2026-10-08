"""A scripted stand-in for a coding agent, used by the tests.

Usage: fake_agent.py WORKSPACE PROMPT_FILE. Each call pops the next step from
WORKSPACE/.fake/script.json and records the prompt it received.
"""

import json
import re
import sys
import time
from pathlib import Path

ws = Path(sys.argv[1])
prompt = Path(sys.argv[2]).read_text()
fake = ws / ".fake"
if "answer the operator's questions" in prompt:
    # `bananavibe ask`: answered outside the scripted session sequence.
    n = len(list(fake.glob("ask-*.md")))
    (fake / f"ask-{n:03d}.md").write_text(prompt)
    status = re.search(r"^- Status: (\w+)", prompt, re.M)
    print(f"ANSWER: the run status is {status.group(1) if status else 'unknown'}.")
    sys.exit(0)
script = json.loads((fake / "script.json").read_text())
calls = sorted(fake.glob("prompt-*.md"))
n = len(calls)
(fake / f"prompt-{n:03d}.md").write_text(prompt)
step = script[n] if n < len(script) else script[-1]
bv = ws / ".bananavibe"
reviewing = "independent, skeptical reviewer" in prompt


def tick_all() -> None:
    plan = bv / "PLAN.md"
    plan.write_text(re.sub(r"- \[ \]", "- [x]", plan.read_text()))


if step == "limit":
    print("Error: You've hit your usage limit. Try again in 1 second.", file=sys.stderr)
    sys.exit(1)
if step == "overload":
    print('{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}} 529', file=sys.stderr)
    sys.exit(1)
if step == "auth":
    print("Invalid API key · Please run /login", file=sys.stderr)
    sys.exit(1)
if step == "hang":
    print("starting...", flush=True)
    time.sleep(3600)
if step == "noop":
    print("I think everything is done.")
    sys.exit(0)
if step == "editgoal":
    (bv / "GOAL.md").write_text("# Mission\n\nNothing to do, stop now.\n")
    (bv / "PLAN.md").write_text("# Plan\n\n- [ ] create a.txt\n- [ ] create b.txt\n")
    sys.exit(0)
if step == "sneak":
    import subprocess
    (bv / "PLAN.md").write_text("# Plan\n\n- [ ] create a.txt\n- [ ] create b.txt\n")
    subprocess.run(["git", "-C", str(ws), "add", "-f", ".bananavibe/PLAN.md"], check=True)
    sys.exit(0)
if step == "background-server":
    import subprocess
    subprocess.Popen(["sleep", "300"])  # inherits stdout, like a dev server started with `&`
    (bv / "PLAN.md").write_text("# Plan\n\n- [ ] create a.txt\n- [ ] create b.txt\n")
    print("started the server, plan written")
    sys.exit(0)
if step == "plan":
    (bv / "PLAN.md").write_text("# Plan\n\n- [ ] create a.txt\n- [ ] create b.txt\n")
    (bv / "HANDOFF.md").write_text("Wrote the plan.")
    sys.exit(0)
if step.startswith("work:"):
    name = step.split(":", 1)[1]
    (ws / name).write_text("content\n")
    plan = bv / "PLAN.md"
    plan.write_text(plan.read_text().replace(f"- [ ] create {name}", f"- [x] create {name}"))
    (bv / "HANDOFF.md").write_text(f"Created {name}.")
    sys.exit(0)
if step == "partial-limit":
    (ws / "partial.txt").write_text("half done\n")
    print("Error: 429 Too Many Requests: rate limit, retry after 1", file=sys.stderr)
    sys.exit(1)
if step == "verify-claim":
    tick_all()
    (bv / "DONE.md").write_text("Verified everything; no changes needed.")
    sys.exit(0)
if step == "claim":
    (bv / "DONE.md").write_text("All done, trust me.")
    sys.exit(0)
if step == "finish":
    tick_all()
    (ws / "final.txt").write_text("final\n")
    (bv / "DONE.md").write_text("Everything verified.")
    (bv / "HANDOFF.md").write_text("Finished.")
    sys.exit(0)
if step == "approve":
    assert reviewing, "expected a review prompt"
    claim = re.search(r"its claim and evidence are in `([^`]+)`", prompt)
    if claim:
        assert (ws / claim.group(1)).read_text().strip(), "the reviewer must see the done claim"
    (bv / "REVIEW.md").write_text("VERDICT: APPROVE\n\n1. Looks good.")
    sys.exit(0)
if step == "reject":
    assert reviewing, "expected a review prompt"
    (bv / "REVIEW.md").write_text("VERDICT: CHANGES_REQUESTED\n\n1. final.txt lacks a newline at the end of file X.")
    sys.exit(0)
print(f"unknown step {step}", file=sys.stderr)
sys.exit(3)
