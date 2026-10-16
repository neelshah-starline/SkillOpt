"""Harvest real RevitMCP sessions into a durable ledger and report on them.

  python scripts/revitmcp/session_ledger.py harvest            # add new/updated episodes from ~/.claude/projects
  python scripts/revitmcp/session_ledger.py report             # outcomes and the most common failures
  python scripts/revitmcp/session_ledger.py evidence [--limit 30] [--out file.md]
                                                               # failed / corrected / recovered episodes as a reflection digest

Run `harvest` whenever you like (it is idempotent); run it before Claude Code prunes old transcripts. The ledger lives at
~/.revitmcp/ledger/episodes.jsonl by default (outside the repository: it holds your prompts), or --ledger PATH.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from skillopt.envs.revitmcp import session_ledger as sl  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["harvest", "report", "evidence"])
    ap.add_argument("--ledger", default=str(sl.DEFAULT_LEDGER))
    ap.add_argument("--projects", default=str(sl.DEFAULT_PROJECTS))
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    ledger = Path(args.ledger)

    if args.command == "harvest":
        new, updated = sl.harvest(Path(args.projects), ledger)
        print(f"ledger {ledger}: {new} new, {updated} updated, {len(sl.load_ledger(ledger))} total")
        return 0

    episodes = sorted(sl.load_ledger(ledger).values(), key=lambda e: e["started_at"])
    if args.command == "report":
        print(sl.summarize(episodes))
        return 0

    text = sl.evidence_markdown(episodes, args.limit)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
