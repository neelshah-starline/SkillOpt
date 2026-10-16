"""Runs one RevitMCP task through `claude -p` against the real revit-mcp MCP server and records what happened.

SkillOpt's own claude_code_exec backend cannot do this: it passes --tools Read,Bash, returns plain text and has no
MCP option. This runner spawns the CLI directly with stream-json output, so every tool_use / tool_result is captured
for reflection and scoring. It is used for the TARGET model only; optimizer and reflection stay on SkillOpt's backend.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# The target never gets code execution, document-lifecycle or arbitrary-path tools: the harness owns documents.
DENIED_TOOLS = (
    "send_code_to_revit",
    "open_revit_model",
    "save_close_model",
    "save_family",
    "load_family",
    "load_family_into_project",
    "create_family_from_template",
)


@dataclass
class RolloutTrace:
    conversation: list[dict] = field(default_factory=list)
    final_text: str = ""
    tool_calls: int = 0
    tool_errors: int = 0
    timeouts: int = 0
    turns: int = 0
    duration_s: float = 0.0
    cost_usd: float = 0.0
    five_hour_utilization: float | None = None
    seven_day_utilization: float | None = None
    stop_reason: str = ""
    returncode: int = 0
    stderr: str = ""
    init_skills: list = field(default_factory=list)
    init_tools: int = 0
    init_memory_paths: dict = field(default_factory=dict)


def resolve_claude_exe(configured: str | None = None) -> str:
    """The claude binary: an explicit path, $CLAUDE_CODE_EXEC_PATH, PATH, then the VS Code extension's bundled one."""
    for candidate in (configured, os.environ.get("CLAUDE_CODE_EXEC_PATH")):
        if candidate and Path(candidate).exists():
            return str(candidate)
    from shutil import which

    found = which("claude")
    if found:
        return found
    ext_root = Path.home() / ".vscode" / "extensions"
    matches = sorted(ext_root.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"))
    if matches:
        return str(matches[-1])
    raise FileNotFoundError("claude executable not found; set CLAUDE_CODE_EXEC_PATH")


def write_mcp_config(path: Path, server_js: Path, port: int) -> Path:
    config = {
        "mcpServers": {
            "revit-mcp": {
                "command": "node",
                "args": [str(server_js)],
                "env": {"REVIT_MCP_PORT": str(port)},
            }
        }
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def _is_error_result(block: dict) -> bool:
    """revit-mcp flags every failed tool call with isError, which the CLI reports as is_error."""
    return bool(block.get("is_error"))


def _cleanup(work: Path) -> None:
    """Remove the scratch folder and the empty per-folder memory directory Claude Code creates for it under
    ~/.claude/projects (one per rollout, otherwise hundreds pile up). Only an empty directory is removed."""
    shutil.rmtree(work, ignore_errors=True)
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(work))
    memory_dir = Path.home() / ".claude" / "projects" / slug
    try:
        if memory_dir.is_dir() and not any(p.is_file() for p in memory_dir.rglob("*")):
            shutil.rmtree(memory_dir, ignore_errors=True)
    except OSError:
        pass


def run_task(
    *,
    prompt: str,
    skill_text: str | None,
    mcp_config: Path,
    claude_exe: str,
    model: str = "haiku",
    max_turns: int = 24,
    timeout_s: int = 600,
    extra_denied: tuple[str, ...] = (),
    allow_tools: tuple[str, ...] = (),
) -> RolloutTrace:
    """Run one task. `skill_text` is appended to the system prompt (None runs the no-skill floor)."""
    work = Path(tempfile.mkdtemp(prefix="revitmcp_rollout_"))
    cmd = [
        claude_exe,
        "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--strict-mcp-config",
        "--mcp-config", str(mcp_config),
        "--tools", "",
        "--disallowedTools", *[f"mcp__revit-mcp__{t}" for t in (*DENIED_TOOLS, *extra_denied) if t not in allow_tools],
        "--permission-mode", "bypassPermissions",
        "--disable-slash-commands",
        "--setting-sources", "",
        "--no-session-persistence",
        "--model", model,
        "--max-turns", str(max_turns),
    ]
    if skill_text:
        skill_file = work / "skill.md"
        skill_file.write_text(skill_text, encoding="utf-8")
        cmd += ["--append-system-prompt-file", str(skill_file)]
    cmd += ["--", prompt]

    trace = RolloutTrace()
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=work, capture_output=True, text=True, encoding="utf-8", timeout=timeout_s)
        stdout, trace.returncode, trace.stderr = proc.stdout, proc.returncode, proc.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        trace.stop_reason = "timeout"
        trace.returncode = -1
    trace.duration_s = time.time() - started
    _cleanup(work)

    for line in stdout.splitlines():
        line = line.strip().lstrip("﻿")
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            trace.init_skills = list(event.get("skills", []))
            trace.init_tools = len(event.get("tools", []))
            trace.init_memory_paths = dict(event.get("memory_paths", {}) or {})
        elif kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    trace.tool_calls += 1
                    trace.conversation.append(
                        {"type": "tool_call", "id": block.get("id"), "name": block.get("name"), "input": block.get("input")}
                    )
                elif block.get("type") == "text" and block.get("text"):
                    trace.conversation.append({"type": "message", "content": block["text"]})
        elif kind == "user":
            content = event.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        failed = _is_error_result(block)
                        trace.tool_errors += int(failed)
                        # A TIMEOUT means Revit stopped answering (busy or blocked), not that the client erred.
                        trace.timeouts += int(failed and "TIMEOUT" in _result_text(block))
                        trace.conversation.append(
                            {"type": "tool_result", "id": block.get("tool_use_id"), "is_error": failed, "content": block.get("content")}
                        )
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info", {})
            windows = info.get("unifiedWindows", {})
            trace.five_hour_utilization = windows.get("five_hour", {}).get("utilization", trace.five_hour_utilization)
            trace.seven_day_utilization = windows.get("seven_day", {}).get("utilization", trace.seven_day_utilization)
        elif kind == "result":
            trace.turns = int(event.get("num_turns", 0) or 0)
            trace.cost_usd = float(event.get("total_cost_usd", 0.0) or 0.0)
            trace.final_text = str(event.get("result", "") or "")
            trace.stop_reason = trace.stop_reason or str(event.get("subtype", ""))
    return trace
