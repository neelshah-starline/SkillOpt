"""The Revit 2027 session a RevitMCP rollout runs against: process, dialog watcher, and per-task document reset.

Safety contract (see docs/plans/SKILLOPT_INTEGRATION_PLAN.md in the RevitMCP workspace):
  * only Revit 2027 is ever driven (the user's live Revit is 2023); the year is checked on every connect;
  * a task never opens a fixture directly: it opens a scratch COPY under the training folder, read-only for saves,
    and closes it without saving, so the fixture and every other model stay untouched;
  * a document that does not close cleanly marks the session dirty and the caller restarts Revit.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .revit_client import RevitClient, RevitRpcError

REQUIRED_YEAR = "2027"
DEFAULT_REVIT_EXE = r"C:\Program Files\Autodesk\Revit 2027\Revit.exe"


class UnsafeSession(RuntimeError):
    """A safety check failed; nothing was run."""


def _ok(result: object) -> bool:
    return isinstance(result, dict) and (result.get("Success") is True or result.get("success") is True)


def _find_listening_port(pid: int, timeout: float) -> int:
    """The loopback port the plugin of this Revit process listens on (8080 first, then 8081 to 8085)."""
    deadline = time.time() + timeout
    script = (
        f"Get-NetTCPConnection -State Listen -OwningProcess {pid} -ErrorAction SilentlyContinue "
        "| Where-Object { $_.LocalPort -ge 8080 -and $_.LocalPort -le 8085 } "
        "| Select-Object -First 1 -ExpandProperty LocalPort"
    )
    while time.time() < deadline:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True
        ).stdout.strip()
        if out.isdigit():
            return int(out)
        time.sleep(3)
    raise TimeoutError(f"Revit (pid {pid}) did not start listening on 8080-8085 within {timeout:.0f}s")


class RevitProcess:
    """Starts and stops the Revit 2027 used for training, with the dialog watcher scoped to its PID."""

    def __init__(self, workspace: Path, revit_exe: str = DEFAULT_REVIT_EXE):
        self.workspace = Path(workspace)
        self.revit_exe = revit_exe
        self.proc: subprocess.Popen | None = None
        self.watcher: subprocess.Popen | None = None
        self.port: int | None = None

    def start(self, startup_timeout: float = 240.0) -> int:
        if not Path(self.revit_exe).exists():
            raise UnsafeSession(f"Revit 2027 not found at {self.revit_exe}")
        if "2027" not in self.revit_exe:
            raise UnsafeSession("only Revit 2027 may be started for training")
        self.proc = subprocess.Popen([self.revit_exe])
        watcher_script = self.workspace / "scripts" / "test" / "accept_revit_dialogs.ps1"
        self.watcher = subprocess.Popen(
            ["powershell", "-NoProfile", "-File", str(watcher_script), "-RevitPid", str(self.proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.port = _find_listening_port(self.proc.pid, startup_timeout)
        return self.port

    def stop(self) -> None:
        for p in (self.watcher, self.proc):
            if p is not None and p.poll() is None:
                p.kill()
        self.watcher = None
        self.proc = None
        self.port = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class RevitSession:
    """A connected Revit 2027 plus the scratch-copy document lifecycle for one task at a time."""

    def __init__(self, port: int, fixture: Path, scratch_dir: Path, allowed_fixture_dir: Path):
        self.client = RevitClient(port)
        self.fixture = Path(fixture).resolve()
        self.scratch_dir = Path(scratch_dir).resolve()
        self.allowed_fixture_dir = Path(allowed_fixture_dir).resolve()
        self.current_copy: Path | None = None
        self.anchor: Path | None = None
        self.dirty = False

    # -- safety ----------------------------------------------------------------------------------------------
    def check_safe(self) -> dict:
        try:
            self.fixture.relative_to(self.allowed_fixture_dir)
        except ValueError as exc:
            raise UnsafeSession(f"{self.fixture} is outside the allowlisted fixture folder {self.allowed_fixture_dir}") from exc
        if not self.fixture.exists():
            raise UnsafeSession(f"fixture {self.fixture} does not exist")
        info = self.client.call("get_revit_application_info")
        year = str(info.get("revit_version_number", "")) if isinstance(info, dict) else ""
        if year != REQUIRED_YEAR:
            raise UnsafeSession(f"connected Revit is {year or 'unknown'}, training only runs on Revit {REQUIRED_YEAR}")
        return info

    # -- document lifecycle ----------------------------------------------------------------------------------
    # The Revit API cannot close the only open document, so the session keeps one "anchor" copy of the fixture open
    # for its whole life; each task opens its own copy on top of it (the active one) and closes that copy afterwards.
    def documents(self) -> list[dict]:
        info = self.client.call("get_current_document_info")
        return list(info.get("open_documents", [])) if isinstance(info, dict) else []

    def _open_copy(self, prefix: str) -> Path:
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        copy = self.scratch_dir / f"{prefix}_{uuid.uuid4().hex[:10]}.rvt"
        shutil.copyfile(self.fixture, copy)
        result = self.client.call(
            "open_revit_model",
            {"source_type": "filesystem", "path_or_urn": str(copy), "open_in_background": False, "read_only": True},
            timeout=180,
        )
        if not _ok(result):
            copy.unlink(missing_ok=True)
            raise RuntimeError(f"open_revit_model failed: {result}")
        return copy

    def start(self) -> None:
        """Open the anchor document. The session must begin with no document open (a fresh Revit)."""
        self.check_safe()
        if self.documents():
            raise UnsafeSession(f"documents are open at session start: {[d.get('title') for d in self.documents()]}")
        self.anchor = self._open_copy("anchor")

    def _active_path(self) -> Path | None:
        active = [d for d in self.documents() if d.get("is_active")]
        return Path(active[0].get("file_path", "")).resolve() if len(active) == 1 else None

    def reset(self) -> Path:
        """Open a fresh scratch copy of the fixture as the active document. Returns the copy's path."""
        self.check_safe()
        docs = self.documents()
        if self.anchor is None or len(docs) != 1 or Path(docs[0].get("file_path", "")).resolve() != self.anchor.resolve():
            self.dirty = True
            raise RuntimeError(f"expected only the anchor document before a task, found {[d.get('title') for d in docs]}")
        copy = self._open_copy("task")
        if self._active_path() != copy.resolve():
            self.dirty = True
            raise RuntimeError("the task copy is not the active document after opening")
        self.current_copy = copy
        return copy

    def teardown(self) -> None:
        """Close the task copy without saving and delete it. Marks the session dirty on any problem."""
        copy, self.current_copy = self.current_copy, None
        if copy is None:
            return
        try:
            self.client.call(
                "save_close_model",
                {"path": str(copy), "saveMode": "none", "close": True, "discardChanges": True},
                timeout=120,
            )
            remaining = [Path(d.get("file_path", "")).resolve() for d in self.documents()]
            if self.anchor is None or remaining != [self.anchor.resolve()]:
                self.dirty = True
        except (RevitRpcError, ConnectionError, OSError):
            self.dirty = True
        finally:
            for _ in range(5):
                try:
                    copy.unlink(missing_ok=True)
                    break
                except OSError:
                    time.sleep(1)

    def close(self) -> None:
        self.client.close()
