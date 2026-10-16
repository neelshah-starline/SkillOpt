"""Run RevitMCP tasks and print a score table.

  python scripts/revitmcp/run_tasks.py --category views_sheets --mode oracle            # validate the task set
  python scripts/revitmcp/run_tasks.py --category views_sheets --split test --skill seed # baseline with the hand-written skill
  python scripts/revitmcp/run_tasks.py --category views_sheets --split test --skill none # the no-skill floor

--mode oracle applies each task's reference solution instead of the model and requires: the checks FAIL before it
(the task is not already done) and PASS after it (the task is solvable and the checks are satisfiable).
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from skillopt.envs.revitmcp.harness import WORKSPACE, Harness, load_tasks, write_results  # noqa: E402

SEED_SKILL = WORKSPACE / "revit-mcp" / ".agents" / "skills" / "revit-mcp-skill" / "SKILL.md"
FAMILY_SKILL = WORKSPACE / "revit-mcp" / ".agents" / "skills" / "revit-family-authoring" / "SKILL.md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="views_sheets")
    ap.add_argument("--split", default="", help="comma list of train,val,test (default all)")
    ap.add_argument("--ids", default="")
    ap.add_argument("--mode", choices=["model", "oracle"], default="model")
    ap.add_argument("--skill", default="seed", help="seed | none | path to a skill markdown file")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default="")
    ap.add_argument("--max-window", type=float, default=0.70, help="stop when the five-hour window passes this")
    args = ap.parse_args()

    tasks = load_tasks(
        args.category,
        {s for s in args.split.split(",") if s} or None,
        {s for s in args.ids.split(",") if s} or None,
    )
    if not tasks:
        print("no tasks selected")
        return 2
    skill_text = None
    if args.mode == "model" and args.skill != "none":
        if args.skill == "seed":
            skill_text = SEED_SKILL.read_text(encoding="utf-8")
            if args.category == "family_editor":
                # The family skill defers to revit-mcp-skill for per-tool contracts, so a client gets both.
                skill_text += "\n\n" + FAMILY_SKILL.read_text(encoding="utf-8")
        else:
            skill_text = Path(args.skill).read_text(encoding="utf-8")

    answer_tasks = {t["id"] for t in tasks if any(c["kind"] == "answer" for c in t["checks"])}

    def valid(r) -> bool:
        # An answer-only task is "not already done" by construction (the reply is not in Revit's state).
        return r.hard == 1 and (r.pre_pass is False or r.id in answer_tasks)

    results = []
    stopped_early = False
    with Harness(model=args.model) as h:
        for rep in range(args.repeat):
            for task in tasks:
                r = h.run(task, args.mode, skill_text)
                results.append(r)
                flag = "PASS" if r.hard else "FAIL"
                if args.mode == "oracle":
                    ok = valid(r)
                    flag = "OK  " if ok else "BAD "
                print(
                    f"{flag} {r.id:<8} soft={r.soft:.2f} turns={r.turns} calls={r.tool_calls} err={r.tool_errors} "
                    f"{r.duration_s:.0f}s pre_pass={r.pre_pass} | {r.detail if not r.hard or args.mode == 'oracle' else ''}",
                    flush=True,
                )
                if r.five_hour is not None and r.five_hour >= args.max_window:
                    print(f"STOP: five-hour window at {r.five_hour:.0%} (limit {args.max_window:.0%})")
                    stopped_early = True
                    break
            if stopped_early:
                break

    out = Path(args.out) if args.out else REPO / "outputs" / "revitmcp" / f"{args.category}_{args.mode}_{datetime.now():%Y%m%d_%H%M%S}.json"
    write_results(out, results)
    hard = [r.hard for r in results]
    print(
        f"\n{args.mode} {args.category} skill={args.skill if args.mode == 'model' else '-'}: "
        f"hard {sum(hard)}/{len(hard)} = {statistics.mean(hard):.0%}, soft {statistics.mean(r.soft for r in results):.2f}, "
        f"calls/task {statistics.mean(r.tool_calls for r in results):.1f}, errors/task {statistics.mean(r.tool_errors for r in results):.2f}, "
        f"cost ${sum(r.cost_usd for r in results):.2f} -> {out}"
    )
    if args.mode == "oracle":
        bad = [r.id for r in results if not valid(r)]
        print("oracle validation:", "ALL TASKS VALID" if not bad else f"INVALID: {bad}")
        return 0 if not bad else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
