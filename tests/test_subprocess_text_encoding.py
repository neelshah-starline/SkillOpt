"""Prompts and replies must survive the pipe to a CLI whatever the console locale is.

``subprocess.run(..., text=True)`` with no ``encoding`` uses the locale code page: cp1252
on Windows, ASCII under a POSIX ``C`` locale. One ``→`` or ``✅`` in a prompt then raises
``UnicodeEncodeError`` inside ``subprocess``. The Sleep backend catches that, logs
"Claude CLI could not be executed", returns "" and carries on, so a whole night scores 0.0
and exits 0 -- indistinguishable from a skill that never complies.

A mock cannot show this: the failure is in how real bytes cross a real pipe. So the test
runs the real backend against a stub CLI in a child interpreter forced onto a legacy locale.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROMPT = "arrow → check ✅ dash — end"

_STUB = textwrap.dedent(
    """
    import sys
    data = sys.stdin.buffer.read().decode("utf-8")
    sys.stdout.buffer.write(("ECHO:" + data).encode("utf-8"))
    """
)

_CHILD = textwrap.dedent(
    """
    import sys
    from skillopt_sleep.backend import ClaudeCliBackend
    backend = ClaudeCliBackend(model="sonnet", claude_path=sys.argv[1])
    reply = backend._call(sys.argv[2])
    sys.stdout.buffer.write(reply.encode("utf-8"))
    sys.stdout.buffer.write(b"\\n--ERR--\\n")
    sys.stdout.buffer.write(str(getattr(backend, "last_call_error", "") or "").encode("utf-8"))
    """
)


def _make_stub_cli(tmp_path) -> str:
    script = tmp_path / "stub_claude.py"
    script.write_text(_STUB, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "claude.cmd"
        shim.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="ascii")
    else:
        shim = tmp_path / "claude"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="ascii")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return str(shim)


def _legacy_locale_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    env.update({"PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "PYTHONPATH": _ROOT})
    if os.name != "nt":
        # Python would otherwise coerce the C locale to UTF-8 and hide the bug.
        env.update({"LC_ALL": "C", "LANG": "C"})
    return env


def test_cli_prompt_with_non_ascii_round_trips_under_a_legacy_locale(tmp_path) -> None:
    stub = _make_stub_cli(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, stub, _PROMPT],
        capture_output=True,
        env=_legacy_locale_env(),
        cwd=str(tmp_path),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    reply, _, error = proc.stdout.decode("utf-8").partition("\n--ERR--\n")
    assert error == "", f"backend reported a failed call: {error}"
    assert reply == "ECHO:" + _PROMPT


@pytest.mark.skipif(os.name != "nt", reason="needs the Windows ANSI code page to differ from UTF-8")
def test_the_stub_really_runs_under_cp1252() -> None:
    """Guards the test above: the child must not already be in UTF-8 mode."""
    out = subprocess.run(
        [sys.executable, "-c", "import locale,sys; sys.stdout.write(locale.getpreferredencoding(False))"],
        capture_output=True,
        env=_legacy_locale_env(),
        timeout=30,
    )
    assert out.stdout.decode("ascii").lower() in {"cp1252", "cp1250", "cp1251", "cp1253", "cp1254", "cp1257", "cp932", "cp936", "cp949", "cp950", "cp437", "cp850"}
