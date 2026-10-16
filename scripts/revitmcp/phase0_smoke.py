"""Phase 0 of docs/plans/SKILLOPT_INTEGRATION_PLAN.md: prove the harness before any training.

  python scripts/revitmcp/phase0_smoke.py --cycles 20 [--no-task] [--keep-revit]

  1. starts a Revit 2027 (never any other year) with the dialog watcher scoped to its PID;
  2. runs N reset/teardown cycles on a scratch copy of fixtures/2027/BLANK.rvt and checks each leaves only the anchor document open;
  3. runs ONE real task through `claude -p` against the revit-mcp MCP server, scores it from Revit's own state, and
     prints the stream-derived trace summary, seconds per task and the subscription window utilisation.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from skillopt.envs.revitmcp.claude_runner import resolve_claude_exe, run_task, write_mcp_config  # noqa: E402
from skillopt.envs.revitmcp.session import RevitProcess, RevitSession, UnsafeSession  # noqa: E402

WORKSPACE = REPO.parent
FIXTURE_DIR = WORKSPACE / "revit-mcp-commandset" / "tests" / "RevitMCPCommandSet.Tests" / "fixtures" / "2027"
SKILL = WORKSPACE / "revit-mcp" / ".agents" / "skills" / "revit-mcp-skill" / "SKILL.md"
SERVER_JS = WORKSPACE / "revit-mcp" / "build" / "index.js"

TASK_VIEW_NAME = "Phase0 Smoke Plan"
TASK_PROMPT = (
    f"In the open Revit model, create a floor plan view named '{TASK_VIEW_NAME}' for the lowest level. "
    "Reply 'done' when it exists."
)


def view_exists(client, name: str) -> tuple[bool, object]:
    views = client.call("get_all_views")
    return name in json.dumps(views), views


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=20)
    ap.add_argument("--no-task", action="store_true")
    ap.add_argument("--no-skill", action="store_true", help="run the task without the operator skill (the floor)")
    ap.add_argument("--keep-revit", action="store_true")
    ap.add_argument("--model", default="haiku")
    args = ap.parse_args()

    if not SERVER_JS.exists():
        print(f"build revit-mcp first: {SERVER_JS} is missing")
        return 2

    revit = RevitProcess(WORKSPACE)
    scratch = Path(tempfile.mkdtemp(prefix="revitmcp_scratch_"))
    session = None
    rc = 1
    try:
        t0 = time.time()
        port = revit.start()
        print(f"Revit 2027 pid {revit.proc.pid} listening on {port} after {time.time() - t0:.0f}s")
        session = RevitSession(port, FIXTURE_DIR / "BLANK.rvt", scratch, FIXTURE_DIR)
        info = session.check_safe()
        print(f"safety check passed: {info.get('revit_version_name')}")
        session.start()

        times = []
        for i in range(1, args.cycles + 1):
            t = time.time()
            session.reset()
            session.teardown()
            times.append(time.time() - t)
            leftover = session.documents()
            if session.dirty or [d.get('title') for d in leftover] != [session.anchor.stem]:
                print(f"cycle {i}: NOT CLEAN dirty={session.dirty} open={[d.get('title') for d in leftover]}")
                return 1
        if times:
            print(f"{args.cycles} reset/teardown cycles clean; {sum(times) / len(times):.1f}s per cycle (max {max(times):.1f}s)")

        if args.no_task:
            rc = 0
            return rc

        mcp_config = write_mcp_config(scratch / "mcp.json", SERVER_JS, port)
        skill_text = None if args.no_skill else SKILL.read_text(encoding="utf-8")
        session.reset()
        before_ok, _ = view_exists(session.client, TASK_VIEW_NAME)
        trace = run_task(
            prompt=TASK_PROMPT,
            skill_text=skill_text,
            mcp_config=mcp_config,
            claude_exe=resolve_claude_exe(),
            model=args.model,
        )
        after_ok, views = view_exists(session.client, TASK_VIEW_NAME)
        print(f"task: view existed before={before_ok}, after={after_ok}")
        print(
            f"trace: {trace.turns} turns, {trace.tool_calls} tool calls, {trace.tool_errors} errors, "
            f"{trace.duration_s:.1f}s, cost ${trace.cost_usd:.4f}, stop={trace.stop_reason}, rc={trace.returncode}"
        )
        print(f"isolation: {trace.init_tools} tools, skills={trace.init_skills}, memory={trace.init_memory_paths}")
        print(f"subscription window: five_hour={trace.five_hour_utilization} seven_day={trace.seven_day_utilization}")
        for step in trace.conversation:
            if step["type"] == "tool_call":
                print(f"  CALL {step['name'].replace('mcp__revit-mcp__', '')} {json.dumps(step['input'])[:140]}")
            elif step["type"] == "tool_result":
                print(f"  {'ERR ' if step['is_error'] else 'OK  '} {json.dumps(step['content'])[:140]}")
        print(f"final: {trace.final_text[:200]}")
        if trace.stderr.strip():
            print(f"stderr: {trace.stderr.strip()[:300]}")
        session.teardown()
        rc = 0 if (after_ok and not before_ok) else 1
        print("RESULT:", "PASS" if rc == 0 else "FAIL")
        return rc
    except UnsafeSession as exc:
        print(f"REFUSED: {exc}")
        return 3
    finally:
        if session is not None:
            try:
                session.teardown()
            except Exception as exc:  # noqa: BLE001 - best effort on the way out
                print(f"teardown on exit failed: {exc}")
            session.close()
        if not args.keep_revit:
            revit.stop()
            time.sleep(2)
        for p in scratch.glob("*"):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            scratch.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
