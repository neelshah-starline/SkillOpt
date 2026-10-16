"""A durable ledger of real RevitMCP usage, built from Claude Code transcripts.

Why this exists. SkillOpt-Sleep (skillopt_sleep/) already harvests Claude Code transcripts, but it keeps only the user's
prompts, the assistant's final text, tool NAMES and a few feedback phrases: it never reads a tool result, so it cannot see
that a Revit call failed, timed out, or was retried. Claude Code also prunes old transcripts. This module reads the
transcripts directly, keeps one compact record per user request ("episode") that used a revit-mcp tool, and appends it to
a ledger outside the repository, so what happened in real sessions survives and can feed reflection.

What an episode records (all local; arguments and results are truncated, nothing is sent anywhere):
  intent          the user's request
  calls           each revit-mcp tool call: name, arguments, is_error, errorCode, hint, short result
  counters        calls, errors, timeouts, repeated identical calls
  outcome         clean | recovered | failed | corrected | no_result (see classify)
  next_feedback   approval / disapproval phrases in the user's NEXT message (a correction is a failure signal)

Two sources, because not everyone uses Claude Code:
  * Claude Code transcripts (~/.claude/projects): rich (the user's request, the assistant's closing message, whether the
    next user message disapproved) but only for Claude Code.
  * The revit-mcp call log (~/.revitmcp/calls, written by the server itself for EVERY MCP client: Cline, Antigravity, Claude
    Desktop, any other LLM): objective (tool, arguments, outcome, errorCode, hint, duration) but it cannot see the user's
    request, so those episodes have no intent and no "corrected" signal. Consecutive calls of one server process are one
    episode until the client has been idle for IDLE_GAP_S seconds.
A Claude Code session appears in both; the call-log copy is dropped (the transcript episode is richer).

It is a harvester only: it never writes outside the ledger and never talks to Revit.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

TOOL_PREFIX = "mcp__revit-mcp__"
DEFAULT_LEDGER = Path.home() / ".revitmcp" / "ledger" / "episodes.jsonl"
DEFAULT_PROJECTS = Path.home() / ".claude" / "projects"
DEFAULT_CALLS = Path.home() / ".revitmcp" / "calls"
IDLE_GAP_S = 180
DEDUP_WINDOW_S = 15

# Same idea as skillopt_sleep.harvest: lexical cues in the user's next message. English only; extend as needed.
NEGATIVE = (
    "still broken", "still not", "still wrong", "doesn't work", "does not work", "not working", "that's wrong",
    "thats wrong", "incorrect", "wrong", "nope", "fix it", "didn't", "did not", "broken", "error again",
    "still failing", "not fixed", "revert", "undo",
)
POSITIVE = ("thanks", "thank you", "perfect", "great", "works now", "that works", "lgtm", "looks good", "nice")

_ERROR_CODE = re.compile(r'"errorCode"\s*:\s*"([A-Z_]+)"')
_HINT = re.compile(r'"hint"\s*:\s*"((?:[^"\\]|\\.)*)"')
_SUCCESS_FALSE = re.compile(r'"success"\s*:\s*false')


@dataclass
class Call:
    tool: str
    args: str
    is_error: bool
    error_code: str = ""
    hint: str = ""
    result: str = ""
    ts: str = ""
    ms: int = 0


@dataclass
class Episode:
    id: str
    session_id: str
    project: str
    started_at: str
    intent: str
    calls: list[Call] = field(default_factory=list)
    final_text: str = ""
    next_feedback: list[str] = field(default_factory=list)
    complete: bool = False
    n_calls: int = 0
    n_errors: int = 0
    n_timeouts: int = 0
    n_repeats: int = 0
    outcome: str = "no_result"
    source: str = ""
    client: str = ""
    origin: str = "transcript"  # transcript | call-log

    def to_json(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------------------------------------ parsing helpers
def _content_blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _is_human_prompt(record: dict) -> str | None:
    """The text of a real user request, or None for tool results, injected context and slash-command bodies."""
    if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
        return None
    blocks = _content_blocks(record.get("message", {}))
    if any(b.get("type") == "tool_result" for b in blocks):
        return None
    text = "\n".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text").strip()
    if not text or text.startswith(("<", "Caveat:", "[Request interrupted", "[Pasted text")):
        return None
    return text


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def _trim(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "..."


def _signals(text: str) -> list[str]:
    low = text.lower()
    return [f"neg:{p}" for p in NEGATIVE if p in low] + [f"pos:{p}" for p in POSITIVE if p in low]


def _make_call(tool_use: dict, result: dict | None) -> Call:
    text = _result_text(result) if result else ""
    flagged = bool(result and result.get("is_error")) or bool(_SUCCESS_FALSE.search(text))
    code = (_ERROR_CODE.search(text) or [None, ""])[1] if flagged else ""
    hint = (_HINT.search(text) or [None, ""])[1] if flagged else ""
    return Call(
        tool=str(tool_use.get("name", ""))[len(TOOL_PREFIX):],
        args=_trim(json.dumps(tool_use.get("input", {}), ensure_ascii=False), 300),
        is_error=flagged,
        error_code=code,
        hint=_trim(hint.replace('\\"', '"'), 200),
        result=_trim(text, 160) if flagged or not text else _trim(text, 100),
    )


def classify(ep: Episode) -> str:
    """clean: no failed call. recovered: failures, but the last call succeeded. failed: the last call failed (or the
    request ended in timeouts). corrected: the user's next message disapproved, whatever the calls looked like."""
    if not ep.calls:
        return "no_result"
    if any(s.startswith("neg:") for s in ep.next_feedback):
        return "corrected"
    if ep.calls[-1].is_error:
        return "failed"
    return "recovered" if ep.n_errors else "clean"


def _finish(ep: Episode) -> Episode:
    seen: dict[tuple[str, str], int] = {}
    for c in ep.calls:
        seen[(c.tool, c.args)] = seen.get((c.tool, c.args), 0) + 1
    ep.n_calls = len(ep.calls)
    ep.n_errors = sum(c.is_error for c in ep.calls)
    ep.n_timeouts = sum(c.error_code == "TIMEOUT" for c in ep.calls)
    ep.n_repeats = sum(n - 1 for n in seen.values() if n > 1)
    ep.outcome = classify(ep)
    return ep


def episodes_from_records(records: Iterable[dict], source: str = "") -> Iterator[Episode]:
    """Split one session's records into episodes (one per human request) that used at least one revit-mcp tool."""
    current: Episode | None = None
    pending: dict[str, dict] = {}
    session_id, project = "", ""

    def close(ep: Episode | None, next_text: str | None) -> Iterator[Episode]:
        if ep is not None and ep.calls:
            ep.next_feedback = _signals(next_text) if next_text else []
            ep.complete = next_text is not None
            yield _finish(ep)

    for rec in records:
        session_id = rec.get("sessionId") or session_id
        project = rec.get("cwd") or project
        human = _is_human_prompt(rec)
        if human is not None:
            yield from close(current, human)
            digest = hashlib.sha1(f"{session_id}|{rec.get('uuid', '')}|{human[:80]}".encode()).hexdigest()[:12]
            current = Episode(
                id=digest, session_id=session_id, project=project, started_at=str(rec.get("timestamp", "")),
                intent=_trim(human, 1500), source=source, client="claude-code", origin="transcript",
            )
            pending = {}
            continue
        if current is None:
            continue
        for block in _content_blocks(rec.get("message", {})) if rec.get("type") in ("assistant", "user") else []:
            kind = block.get("type")
            if kind == "tool_use" and str(block.get("name", "")).startswith(TOOL_PREFIX):
                pending[block.get("id", "")] = block
                call = _make_call(block, None)
                call.ts = str(rec.get("timestamp", ""))
                current.calls.append(call)
                block["_index"] = len(current.calls) - 1
            elif kind == "tool_result" and block.get("tool_use_id") in pending:
                use = pending.pop(block["tool_use_id"])
                done = _make_call(use, block)
                done.ts = current.calls[use["_index"]].ts
                current.calls[use["_index"]] = done
            elif kind == "text" and rec.get("type") == "assistant":
                current.final_text = _trim(str(block.get("text", "")), 600)
    yield from close(current, None)


def _parse_ts(text: str) -> float:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def episodes_from_call_log(lines: Iterable[dict], source: str = "") -> Iterator[Episode]:
    """Group revit-mcp call-log lines into episodes: one per server process (session) and burst of activity, split where
    the client was idle for IDLE_GAP_S seconds. There is no request text: the server never sees it."""
    by_session: dict[str, list[dict]] = {}
    for line in lines:
        if line.get("v") == 1 and line.get("tool"):
            by_session.setdefault(str(line.get("session", "")), []).append(line)
    for session, rows in by_session.items():
        rows.sort(key=lambda r: (_parse_ts(r.get("ts", "")), r.get("seq", 0)))
        burst: list[dict] = []
        for row in rows + [None]:  # None flushes the last burst
            if burst and (row is None or _parse_ts(row["ts"]) - _parse_ts(burst[-1]["ts"]) > IDLE_GAP_S):
                client = (burst[0].get("client") or {}).get("name", "") or "unknown"
                first = burst[0]
                ep = Episode(
                    id=hashlib.sha1(f"call-log|{session}|{first.get('seq')}|{first.get('ts')}".encode()).hexdigest()[:12],
                    session_id=session, project="", started_at=str(first.get("ts", "")),
                    intent="(not captured: the MCP server does not see the user's request)",
                    source=source, client=client, origin="call-log", complete=row is not None,
                )
                for r in burst:
                    ep.calls.append(Call(
                        tool=str(r["tool"]), args=str(r.get("args", "")), is_error=not r.get("ok", True),
                        error_code=str(r.get("code", "")), hint=str(r.get("hint", "")), result=_trim(str(r.get("error", "")), 160),
                        ts=str(r.get("ts", "")), ms=int(r.get("ms", 0) or 0),
                    ))
                yield _finish(ep)
                burst = []
            if row is not None:
                burst.append(row)


def is_duplicate(call_ep: dict, transcript_eps: Iterable[dict]) -> bool:
    """A call-log episode is a copy of a transcript episode when it came from Claude Code and most of its calls (same
    tool, within DEDUP_WINDOW_S seconds) appear in one transcript episode. Arguments cannot be compared: the server logs
    the plugin's parameters (for example wrapped in "data") while a transcript holds the arguments the model gave the tool."""
    if "claude" not in (call_ep.get("client") or "").lower():
        return False  # another client has no transcript here to duplicate
    mine = [(c["tool"], _parse_ts(c["ts"])) for c in call_ep["calls"] if c.get("ts")]
    if not mine:
        return False
    for ep in transcript_eps:
        theirs = [(c["tool"], _parse_ts(c["ts"])) for c in ep["calls"] if c.get("ts")]
        hits = sum(any(t == t2 and abs(s - s2) <= DEDUP_WINDOW_S for t2, s2 in theirs) for t, s in mine)
        if hits * 2 >= len(mine):
            return True
    return False


def read_jsonl(path: Path) -> Iterator[dict]:
    try:
        with path.open(encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return


# ------------------------------------------------------------------------------------------------------ ledger store
def load_ledger(path: Path = DEFAULT_LEDGER) -> dict[str, dict]:
    return {e["id"]: e for e in read_jsonl(path)}


def harvest(
    projects_dir: Path = DEFAULT_PROJECTS,
    ledger: Path = DEFAULT_LEDGER,
    skip_dir_markers: tuple[str, ...] = ("revitmcp-rollout",),
    calls_dir: Path | None = DEFAULT_CALLS,
) -> tuple[int, int]:
    """Add every new or grown episode to the ledger. Returns (new, updated). Sessions from the training harness itself
    (its scratch folders) are skipped: they are not real usage."""
    store = load_ledger(ledger)
    new = updated = 0
    for transcript in sorted(Path(projects_dir).glob("*/*.jsonl")):
        if any(m in transcript.parent.name for m in skip_dir_markers):
            continue
        for ep in episodes_from_records(read_jsonl(transcript), source=transcript.name):
            data = ep.to_json()
            old = store.get(ep.id)
            if old is None:
                new += 1
            elif old != data:
                updated += 1
            else:
                continue
            store[ep.id] = data
    if calls_dir is not None and Path(calls_dir).is_dir():
        lines = [row for f in sorted(Path(calls_dir).glob("calls-*.jsonl")) for row in read_jsonl(f)]
        transcripts_now = [e for e in store.values() if e.get("origin", "transcript") == "transcript"]
        for ep in episodes_from_call_log(lines, source="call-log"):
            data = ep.to_json()
            if is_duplicate(data, transcripts_now):
                continue
            old = store.get(ep.id)
            if old is None:
                new += 1
            elif old != data:
                updated += 1
            else:
                continue
            store[ep.id] = data
    # A Claude Code session is in both sources: keep the transcript episode (it has the request and the user's reaction).
    transcripts = [e for e in store.values() if e.get("origin", "transcript") == "transcript"]
    for key in [k for k, e in store.items() if e.get("origin") == "call-log" and is_duplicate(e, transcripts)]:
        del store[key]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in store.values()), encoding="utf-8")
    tmp.replace(ledger)
    return new, updated


# ---------------------------------------------------------------------------------------------------------- reports
def summarize(episodes: list[dict]) -> str:
    from collections import Counter

    if not episodes:
        return "ledger is empty"
    outcomes = Counter(e["outcome"] for e in episodes)
    errors = Counter((c["tool"], c["error_code"] or "ERROR") for e in episodes for c in e["calls"] if c["is_error"])
    clients = Counter(f"{e.get('client') or '?'} ({e.get('origin', 'transcript')})" for e in episodes)
    lines = [
        f"{len(episodes)} episodes, {sum(e['n_calls'] for e in episodes)} calls, {sum(e['n_errors'] for e in episodes)} errors, "
        f"{sum(e['n_timeouts'] for e in episodes)} timeouts, {sum(e['n_repeats'] for e in episodes)} repeated calls",
        "by client: " + ", ".join(f"{k}={v}" for k, v in sorted(clients.items())),
        "outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())),
        "most common failures (tool, code):",
    ]
    lines += [f"  {n:3d}  {tool}  {code}" for (tool, code), n in errors.most_common(10)]
    return "\n".join(lines)


def evidence_markdown(episodes: list[dict], limit: int = 30) -> str:
    """Failed, corrected and recovered episodes as a reflection digest: the request, the failing calls with the error and
    hint the tool returned, and how it ended. This is the input a skill-editing step reads."""
    chosen = [e for e in episodes if e["outcome"] in ("failed", "corrected", "recovered")][-limit:]
    out = ["# Real RevitMCP sessions that went wrong or needed recovery\n"]
    for e in chosen:
        out.append(f"## {e['id']} - {e['outcome']} ({e['n_calls']} calls, {e['n_errors']} errors, client {e.get('client') or '?'})\n")
        out.append(f"Request: {e['intent']}\n")
        for c in e["calls"]:
            if c["is_error"]:
                out.append(f"- `{c['tool']}` {c['args']}\n  -> {c['error_code'] or 'error'}: {c['result']}"
                           + (f"\n  hint: {c['hint']}" if c["hint"] else ""))
        if e["next_feedback"]:
            out.append(f"User's next message: {', '.join(e['next_feedback'])}")
        out.append(f"Ended with: {e['final_text']}\n")
    return "\n".join(out)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
