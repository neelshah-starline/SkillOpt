"""Unit tests for the RevitMCP session ledger (no Revit, no network)."""

from __future__ import annotations

import json
from pathlib import Path

from skillopt.envs.revitmcp import session_ledger as sl


def _user(text, uuid="u1", session="s1"):
    return {"type": "user", "sessionId": session, "uuid": uuid, "cwd": "C:/work", "timestamp": "2026-10-16T10:00:00Z",
            "message": {"role": "user", "content": text}}


def _call(call_id, name, args, ts=None):
    rec = {"type": "assistant", "sessionId": "s1"}
    if ts:
        rec["timestamp"] = ts
    return {**rec,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": f"mcp__revit-mcp__{name}", "input": args}]}}


def _result(call_id, text, is_error=False):
    block = {"type": "tool_result", "tool_use_id": call_id, "content": [{"type": "text", "text": text}]}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "sessionId": "s1", "message": {"role": "user", "content": [block]}}


def _say(text):
    return {"type": "assistant", "sessionId": "s1", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


TIMEOUT = 'Failed\nDetails: {"errorCode": "TIMEOUT", "hint": "Revit may be busy."}'


def test_clean_recovered_failed_and_corrected():
    records = [
        _user("create a view", "u1"),
        _call("a", "create_view", {"viewType": "FloorPlan"}),
        _result("a", '{"created": true}'),
        _say("done"),
        _user("now a sheet", "u2"),
        _call("b", "create_sheet", {"sheetNumber": "A1"}),
        _result("b", '{"success": false, "errorCode": "ELEMENT_NOT_FOUND", "hint": "pick a title block"}'),
        _call("c", "create_sheet", {"sheetNumber": "A1", "noTitleBlock": True}),
        _result("c", '{"created": true}'),
        _say("sheet made"),
        _user("hang on, that's wrong, undo it", "u3"),
        _call("d", "get_all_views", {}),
        _result("d", TIMEOUT, is_error=True),
        _call("e", "get_all_views", {}),
        _result("e", TIMEOUT, is_error=True),
        _say("Revit is not responding"),
        _user("thanks", "u4"),
    ]
    eps = list(sl.episodes_from_records(records))
    assert [e.outcome for e in eps] == ["clean", "corrected", "failed"]
    assert eps[1].n_errors == 1 and eps[1].calls[0].error_code == "ELEMENT_NOT_FOUND"
    assert eps[1].calls[0].hint == "pick a title block"
    assert eps[2].n_timeouts == 2 and eps[2].n_repeats == 1
    assert eps[0].complete and eps[0].next_feedback == []


def test_corrected_overrides_clean_calls():
    records = [_user("make a plan"), _call("a", "create_view", {}), _result("a", '{"created": true}'), _say("ok"),
               _user("no, that's wrong", "u2")]
    (ep,) = list(sl.episodes_from_records(records))
    assert ep.n_errors == 0 and ep.outcome == "corrected" and "neg:that's wrong" in ep.next_feedback


def test_a_request_without_revit_calls_is_not_an_episode():
    records = [_user("explain git rebase"), _say("sure")]
    assert list(sl.episodes_from_records(records)) == []


def test_tool_results_and_injected_text_do_not_start_episodes():
    records = [_user("<command-message>x</command-message>"), _user("real request", "u2"), _call("a", "get_all_views", {}),
               _result("a", "[]"), _say("none")]
    (ep,) = list(sl.episodes_from_records(records))
    assert ep.intent == "real request" and ep.outcome == "clean"


def test_harvest_is_durable_and_idempotent(tmp_path: Path):
    projects = tmp_path / "projects"
    (projects / "proj").mkdir(parents=True)
    (projects / "revitmcp-rollout-abc").mkdir()  # the training harness's own scratch sessions are skipped
    records = [_user("make a view"), _call("a", "create_view", {}), _result("a", '{"created": true}'), _say("done")]
    for folder in ("proj", "revitmcp-rollout-abc"):
        (projects / folder / "s.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    ledger = tmp_path / "ledger" / "episodes.jsonl"

    assert sl.harvest(projects, ledger, calls_dir=tmp_path / 'calls') == (1, 0)
    assert sl.harvest(projects, ledger, calls_dir=tmp_path / 'calls') == (0, 0)

    # The transcript is pruned later: the ledger keeps the episode.
    (projects / "proj" / "s.jsonl").unlink()
    assert sl.harvest(projects, ledger, calls_dir=tmp_path / 'calls') == (0, 0)
    stored = sl.load_ledger(ledger)
    assert len(stored) == 1 and next(iter(stored.values()))["intent"] == "make a view"

    # The user then continues the conversation: the open episode grows and is updated, not duplicated.
    more = records + [_user("thanks", "u2")]
    (projects / "proj" / "s.jsonl").write_text("\n".join(json.dumps(r) for r in more), encoding="utf-8")
    assert sl.harvest(projects, ledger, calls_dir=tmp_path / 'calls') == (0, 1)
    assert len(sl.load_ledger(ledger)) == 1


def test_evidence_digest_lists_failures_with_hints():
    records = [_user("make a sheet"), _call("a", "create_sheet", {}), _result("a", '{"success": false, "errorCode": "X_Y", "hint": "do z"}'),
               _say("failed")]
    (ep,) = list(sl.episodes_from_records(records))
    text = sl.evidence_markdown([ep.to_json()])
    assert "create_sheet" in text and "X_Y" in text and "do z" in text and "failed" in text


def _line(seq, ts, tool, ok=True, code="", client="Cline", session="abc", args="{}"):
    row = {"v": 1, "ts": ts, "session": session, "seq": seq, "pid": 1, "client": {"name": client, "version": "3"} if client else None,
           "port": 8080, "tool": tool, "args": args, "ms": 12, "ok": ok}
    if not ok:
        row.update(code=code, error="it failed", hint="try z")
    return row


def test_call_log_lines_become_episodes_split_by_idle_gap_with_the_client_name():
    lines = [
        _line(1, "2026-10-16T10:00:00Z", "get_all_views"),
        _line(2, "2026-10-16T10:00:20Z", "create_view", ok=False, code="ELEMENT_NOT_FOUND"),
        _line(3, "2026-10-16T10:00:40Z", "create_view"),
        _line(4, "2026-10-16T10:30:00Z", "get_all_sheets", ok=False, code="TIMEOUT"),  # half an hour later: a new episode
    ]
    first, second = list(sl.episodes_from_call_log(lines))
    assert first.client == "Cline" and first.origin == "call-log" and first.n_calls == 3
    assert first.outcome == "recovered" and first.calls[1].error_code == "ELEMENT_NOT_FOUND" and first.calls[1].hint == "try z"
    assert second.outcome == "failed" and second.n_timeouts == 1 and second.intent.startswith("(not captured")


def test_a_client_with_no_name_and_other_sessions_are_kept_apart():
    lines = [_line(1, "2026-10-16T10:00:00Z", "a", client=None, session="s1"), _line(1, "2026-10-16T10:00:05Z", "b", session="s2")]
    eps = list(sl.episodes_from_call_log(lines))
    assert sorted(e.client for e in eps) == ["Cline", "unknown"]


def test_a_claude_code_session_is_not_counted_twice(tmp_path: Path):
    projects = tmp_path / "projects"
    (projects / "proj").mkdir(parents=True)
    records = [_user("make a view"), _call("a", "create_view", {"v": 1}, ts="2026-10-16T10:00:03Z"),
               _result("a", '{"created": true}'), _say("done")]
    (projects / "proj" / "s.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    calls = tmp_path / "calls"
    calls.mkdir()
    rows = [_line(1, "2026-10-16T10:00:04Z", "create_view", client="claude-code", args=json.dumps({"v": 1}, separators=(",", ":")))]
    rows += [_line(1, "2026-10-16T11:00:00Z", "get_all_views", client="Cline", session="other")]
    (calls / "calls-20261016.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"

    # json.dumps with default separators in the transcript vs compact in the log: normalise by using the same text
    sl.harvest(projects, ledger, calls_dir=calls)
    stored = list(sl.load_ledger(ledger).values())
    assert sorted((e["origin"], e["client"]) for e in stored) == [("call-log", "Cline"), ("transcript", "claude-code")]
    sl.harvest(projects, ledger, calls_dir=calls)  # idempotent
    assert len(sl.load_ledger(ledger)) == 2
    assert "Cline (call-log)=1" in sl.summarize(stored) and "claude-code (transcript)=1" in sl.summarize(stored)
