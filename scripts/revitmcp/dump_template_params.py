"""Write skillopt/envs/revitmcp/tasks/template_builtin_params.json: the parameters Revit's Generic Model template already
has, so the conformance rules can tell the parameters a model ADDED from the template's own.

  python scripts/revitmcp/dump_template_params.py

Starts a Revit 2027 (and refuses to if one is already running: it may be someone's working session), opens the template,
reads get_family_parameters, closes everything. Run it once, and again if the template or Revit version changes.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from skillopt.envs.revitmcp.harness import FIXTURE_DIR, WORKSPACE  # noqa: E402
from skillopt.envs.revitmcp.session import RevitProcess, RevitSession, UnsafeSession  # noqa: E402

TEMPLATE = r"C:\ProgramData\Autodesk\RVT 2027\Family Templates\English\Metric Generic Model.rft"
OUT = REPO / "skillopt" / "envs" / "revitmcp" / "tasks" / "template_builtin_params.json"


def main() -> int:
    revit = RevitProcess(WORKSPACE)
    scratch = Path(tempfile.mkdtemp(prefix="revitmcp_dump_"))
    session = None
    try:
        port = revit.start()
        session = RevitSession(port, FIXTURE_DIR / "BLANK.rvt", scratch, FIXTURE_DIR)
        session.start()
        session.reset()
        session.client.call("create_family_from_template", {"templatePath": TEMPLATE})
        names = sorted(p["name"] for p in session.client.call("get_family_parameters")["parameters"])
        OUT.write_text(json.dumps(names, indent=1), encoding="utf-8")
        print(f"wrote {len(names)} template parameters to {OUT}")
        return 0
    except UnsafeSession as exc:
        print(f"REFUSED: {exc}")
        return 3
    finally:
        if session is not None:
            try:
                session.teardown()
            except Exception as exc:  # noqa: BLE001
                print(f"teardown: {exc}")
            session.close()
        revit.stop()


if __name__ == "__main__":
    sys.exit(main())
