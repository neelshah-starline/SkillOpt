"""Runs RevitMCP tasks end to end: Revit session management, per-task reset, model or oracle execution, scoring."""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .claude_runner import RolloutTrace, resolve_claude_exe, run_task, write_mcp_config
from .revit_client import RevitRpcError
from .session import RevitProcess, RevitSession
from .verifier import Verdict, run_calls, snapshot, verify

REPO = Path(__file__).resolve().parents[3]
WORKSPACE = REPO.parent
FIXTURE_DIR = WORKSPACE / "revit-mcp-commandset" / "tests" / "RevitMCPCommandSet.Tests" / "fixtures" / "2027"
SERVER_JS = WORKSPACE / "revit-mcp" / "build" / "index.js"
TASK_DIR = Path(__file__).resolve().parent / "tasks"
# A Revit that has opened and closed this many documents is restarted: memory grows over long runs.
RECYCLE_EVERY = 40


def load_tasks(category: str, splits: set[str] | None = None, ids: set[str] | None = None) -> list[dict]:
    tasks = yaml.safe_load((TASK_DIR / f"{category}.yaml").read_text(encoding="utf-8"))
    out = []
    for t in tasks:
        if splits and t["split"] not in splits:
            continue
        if ids and t["id"] not in ids:
            continue
        out.append(t)
    return out


@dataclass
class TaskResult:
    id: str
    mode: str
    hard: int
    soft: float
    detail: str
    turns: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    duration_s: float = 0.0
    cost_usd: float = 0.0
    five_hour: float | None = None
    stop: str = ""
    pre_pass: bool | None = None
    conversation: list = field(default_factory=list)


class Harness:
    """Owns one Revit 2027 and runs tasks on it. Use as a context manager."""

    def __init__(self, model: str = "haiku", max_turns: int = 24):
        self.model = model
        self.max_turns = max_turns
        self.revit = RevitProcess(WORKSPACE)
        self.session: RevitSession | None = None
        self.scratch = Path(tempfile.mkdtemp(prefix="revitmcp_scratch_"))
        self.mcp_config: Path | None = None
        self.claude = resolve_claude_exe()
        self.done_since_start = 0

    def __enter__(self) -> "Harness":
        self._start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop()

    def _start(self) -> None:
        port = self.revit.start()
        self.session = RevitSession(port, FIXTURE_DIR / "BLANK.rvt", self.scratch, FIXTURE_DIR)
        self.session.start()
        self.mcp_config = write_mcp_config(self.scratch / "mcp.json", SERVER_JS, port)
        self.done_since_start = 0

    def _stop(self) -> None:
        if self.session is not None:
            self.session.close()
        self.revit.stop()
        time.sleep(2)
        for p in self.scratch.glob("*"):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def _restart(self) -> None:
        if self.session is not None:
            self.session.close()
        self.revit.stop()
        time.sleep(3)
        self._start()

    def run(self, task: dict, mode: str, skill_text: str | None) -> TaskResult:
        """mode: 'model' (claude -p with the skill), 'oracle' (the task's reference solution), 'none' (checks only)."""
        assert self.session is not None
        if self.session.dirty or not self.revit.alive() or self.done_since_start >= RECYCLE_EVERY:
            self._restart()
        s = self.session
        s.reset()
        try:
            run_calls(s.client, task.get("setup", []))
            snap = snapshot(s.client)
            pre = verify(s.client, snap, [c for c in task["checks"] if c["kind"] != "answer"])
            trace = RolloutTrace()
            if mode == "oracle":
                run_calls(s.client, task["oracle"])
                trace.final_text = task.get("oracle_answer", "")
            elif mode == "model":
                trace = run_task(
                    prompt=task["prompt"],
                    skill_text=skill_text,
                    mcp_config=self.mcp_config,
                    claude_exe=self.claude,
                    model=self.model,
                    max_turns=int(task.get("max_turns", self.max_turns)),
                )
            verdict: Verdict = verify(s.client, snap, task["checks"], trace.final_text)
            return TaskResult(
                id=task["id"],
                mode=mode,
                hard=verdict.hard,
                soft=verdict.soft,
                detail=verdict.summary(),
                turns=trace.turns,
                tool_calls=trace.tool_calls,
                tool_errors=trace.tool_errors,
                duration_s=trace.duration_s,
                cost_usd=trace.cost_usd,
                five_hour=trace.five_hour_utilization,
                stop=trace.stop_reason,
                pre_pass=bool(pre.hard),
                conversation=trace.conversation,
            )
        except (RevitRpcError, ConnectionError, OSError) as exc:
            s.dirty = True
            return TaskResult(id=task["id"], mode=mode, hard=0, soft=0.0, detail=f"harness error: {exc!r}")
        finally:
            s.teardown()
            self.done_since_start += 1


def write_results(path: Path, results: list[TaskResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in results], indent=1, ensure_ascii=False), encoding="utf-8")
