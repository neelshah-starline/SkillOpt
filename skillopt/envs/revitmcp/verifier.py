"""Scores a RevitMCP rollout from Revit's own state, using read-only commands over the plugin socket.

A task lists `checks`; each is a small dict with a `kind`. `snapshot()` is taken after a task's setup and before the
model runs, so a check can say "exactly one more view" without knowing the fixture's counts. The hard score is 1 only
when every check passes; the soft score is the fraction that pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from .conformance import CONFORMANCE_CHECKS
from .family_checks import FAMILY_CHECKS
from .revit_client import RevitClient, RevitRpcError


@dataclass
class CheckResult:
    kind: str
    passed: bool
    detail: str


@dataclass
class Verdict:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def hard(self) -> int:
        return int(bool(self.results) and all(r.passed for r in self.results))

    @property
    def soft(self) -> float:
        return sum(r.passed for r in self.results) / len(self.results) if self.results else 0.0

    def summary(self) -> str:
        return "; ".join(f"{'PASS' if r.passed else 'FAIL'} {r.kind}: {r.detail}" for r in self.results)


# ---------------------------------------------------------------------------------------------------- state reads
def _views(c: RevitClient) -> list[dict]:
    return [v for v in c.call("get_all_views") if not v.get("IsTemplate")]


def _sheets(c: RevitClient, layout: bool = False) -> list[dict]:
    return c.call("get_all_sheets", {"includeLayout": True} if layout else {})


def _schedules(c: RevitClient) -> list[dict]:
    r = c.call("get_model_elements", {"categoryList": ["OST_Schedules"]})
    return list(r.get("Elements", []))


def snapshot(c: RevitClient) -> dict:
    try:
        return {
            "views": len(_views(c)),
            "sheets": len(_sheets(c)),
            "schedules": len(_schedules(c)),
        }
    except (RevitRpcError, KeyError, TypeError, AttributeError):
        return {}  # e.g. a family document is active; project counts are only needed by project-category checks


def _find_view(c: RevitClient, name: str) -> dict | None:
    matches = [v for v in _views(c) if v.get("Name") == name]
    return matches[0] if matches else None


def _view_param(c: RevitClient, view_id: int, param: str) -> str | None:
    r = c.call("get_element_parameters", {"elementId": int(view_id)})
    # Revit pads some parameter names with spaces ("Scale Value    1:"), so compare with whitespace collapsed.
    wanted = " ".join(param.split())
    for p in r.get("parameters", []):
        if " ".join(str(p.get("name", "")).split()) == wanted:
            return str(p.get("value"))
    return None


# ---------------------------------------------------------------------------------------------------- check kinds
def _check_view(c, snap, chk) -> tuple[bool, str]:
    view = _find_view(c, chk["name"])
    if view is None:
        return False, f"no view named {chk['name']!r}"
    problems = []
    if "view_type" in chk and view.get("ViewType") != chk["view_type"]:
        problems.append(f"type {view.get('ViewType')} != {chk['view_type']}")
    for key, param in (("detail_level", "Detail Level"), ("scale", "Scale Value 1:"), ("template", "View Template")):
        if key in chk:
            actual = _view_param(c, view["Id"], param)
            if str(actual) != str(chk[key]):
                problems.append(f"{param} {actual!r} != {chk[key]!r}")
    return (not problems), ("ok" if not problems else "; ".join(problems))


def _check_count_delta(c, snap, chk) -> tuple[bool, str]:
    kind = chk["what"]  # views | sheets | schedules
    now = {"views": lambda: len(_views(c)), "sheets": lambda: len(_sheets(c)), "schedules": lambda: len(_schedules(c))}[kind]()
    delta = now - snap[kind]
    return delta == chk["delta"], f"{kind} changed by {delta}, expected {chk['delta']}"


def _check_sheet(c, snap, chk) -> tuple[bool, str]:
    sheets = [s for s in _sheets(c) if s.get("sheetNumber") == chk["number"]]
    if not sheets:
        return False, f"no sheet numbered {chk['number']!r}"
    sheet = sheets[0]
    problems = []
    if "name" in chk and sheet.get("sheetName") != chk["name"]:
        problems.append(f"name {sheet.get('sheetName')!r} != {chk['name']!r}")
    placed = [v.get("viewName") for v in sheet.get("placedViews", [])]
    if "min_views" in chk and len(placed) < chk["min_views"]:
        problems.append(f"{len(placed)} views placed < {chk['min_views']}")
    for name in chk.get("views", []):
        if name not in placed:
            problems.append(f"view {name!r} not placed (placed: {placed})")
    if chk.get("exact_views") and sorted(placed) != sorted(chk.get("views", [])):
        problems.append(f"placed views {placed} != {chk.get('views', [])}")
    if "titleblock_type" in chk:
        layout = _sheets_layout_for(c, chk["number"])
        actual = (layout or {}).get("titleBlock", {}).get("typeName", "")
        if chk["titleblock_type"].lower() not in actual.lower():
            problems.append(f"title block {actual!r} lacks {chk['titleblock_type']!r}")
    return (not problems), ("ok" if not problems else "; ".join(problems))


def _sheets_layout_for(c, number: str) -> dict | None:
    for s in c.call("get_all_sheets", {"sheetNumber": number, "includeLayout": True}):
        if s.get("sheetNumber") == number:
            return s.get("layout")
    return None


def _check_viewport(c, snap, chk) -> tuple[bool, str]:
    layout = _sheets_layout_for(c, chk["sheet"])
    if not layout:
        return False, f"no sheet {chk['sheet']!r}"
    for vp in layout.get("viewports", []):
        if vp.get("viewName") == chk["view"]:
            if "scale" in chk and int(vp.get("scale")) != int(chk["scale"]):
                return False, f"scale {vp.get('scale')} != {chk['scale']}"
            return True, "ok"
    return False, f"view {chk['view']!r} has no viewport on {chk['sheet']!r}"


def _schedule_data(c, name: str) -> dict | None:
    try:
        return c.call("get_schedule_data", {"scheduleName": name, "maxRows": 200})
    except RevitRpcError:
        return None


def _check_schedule(c, snap, chk) -> tuple[bool, str]:
    data = _schedule_data(c, chk["name"])
    if not data or not data.get("success"):
        return False, f"no readable schedule named {chk['name']!r}"
    headings = [h for h in data.get("headings", []) if h]
    problems = []
    if "headings" in chk and [h.lower() for h in headings] != [h.lower() for h in chk["headings"]]:
        problems.append(f"headings {headings} != {chk['headings']}")
    if "has_headings" in chk:
        missing = [h for h in chk["has_headings"] if h.lower() not in [x.lower() for x in headings]]
        if missing:
            problems.append(f"missing headings {missing} in {headings}")
    rows = data.get("rows", [])
    if "min_rows" in chk and len(rows) < chk["min_rows"]:
        problems.append(f"{len(rows)} rows < {chk['min_rows']}")
    if "row_count" in chk and len(rows) != chk["row_count"]:
        problems.append(f"{len(rows)} rows != {chk['row_count']}")
    if "first_column" in chk:
        col = [str(r[0]) for r in rows if r]
        if col != [str(x) for x in chk["first_column"]]:
            problems.append(f"first column {col} != {chk['first_column']}")
    return (not problems), ("ok" if not problems else "; ".join(problems))


def _check_sheet_named(c, snap, chk) -> tuple[bool, str]:
    """A sheet with this name exists under any number (for tasks where the asked-for number may be taken)."""
    hits = [s.get("sheetNumber") for s in _sheets(c) if s.get("sheetName") == chk["name"]]
    return bool(hits), (f"sheet(s) {hits}" if hits else f"no sheet named {chk['name']!r}")


def _check_any_of(c, snap, chk, final_text: str = "") -> tuple[bool, str]:
    details = []
    for sub in chk["checks"]:
        if sub["kind"] == "answer":
            ok, d = _check_answer(c, snap, sub, final_text)
        else:
            ok, d = CHECKS[sub["kind"]](c, snap, sub)
        if ok:
            return True, f"{sub['kind']}: {d}"
        details.append(f"{sub['kind']}: {d}")
    return False, " | ".join(details)


def _check_answer(c, snap, chk, final_text: str = "") -> tuple[bool, str]:
    """The model's final reply must contain every `contains` string (case-insensitive) for question-type tasks."""
    low = final_text.lower()
    missing = [s for s in chk["contains"] if str(s).lower() not in low]
    return (not missing), ("ok" if not missing else f"reply lacks {missing}")


CHECKS: dict[str, Callable[..., tuple[bool, str]]] = {
    "view": _check_view,
    "count_delta": _check_count_delta,
    "sheet": _check_sheet,
    "viewport": _check_viewport,
    "schedule": _check_schedule,
    "sheet_named": _check_sheet_named,
    **FAMILY_CHECKS,
    **CONFORMANCE_CHECKS,
}


def verify(c: RevitClient, snap: dict, checks: list[dict], final_text: str = "") -> Verdict:
    verdict = Verdict()
    for chk in checks:
        kind = chk["kind"]
        try:
            if kind == "answer":
                ok, detail = _check_answer(c, snap, chk, final_text)
            elif kind == "any_of":
                ok, detail = _check_any_of(c, snap, chk, final_text)
            else:
                ok, detail = CHECKS[kind](c, snap, chk)
        except Exception as exc:  # noqa: BLE001 - a check that cannot run is a failed check, with the reason
            ok, detail = False, f"check error: {exc!r}"
        verdict.results.append(CheckResult(kind, bool(ok), detail))
    return verdict


PLACEHOLDER = __import__("re").compile(r"^\$(LAST_FORM_ID|LAST_PLANE_ID|FORM_ID_(\d+)|PLANE_ID_(\d+))$")


def _substitute(value: Any, ids: dict) -> Any:
    """Replace id placeholders in a solution's parameters with ids the task's own earlier calls produced:
    $LAST_FORM_ID / $FORM_ID_<n> (family forms: extrusions, revolutions...) and $LAST_PLANE_ID / $PLANE_ID_<n>
    (reference planes), n counting from 1 in creation order."""
    if isinstance(value, str):
        m = PLACEHOLDER.match(value)
        if m:
            if m.group(1) == "LAST_FORM_ID":
                return ids["forms"][-1]
            if m.group(1) == "LAST_PLANE_ID":
                return ids["planes"][-1]
            if m.group(2):
                return ids["forms"][int(m.group(2)) - 1]
            return ids["planes"][int(m.group(3)) - 1]
        return value
    if isinstance(value, dict):
        return {k: _substitute(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, ids) for v in value]
    return value


def reset_ids(c: RevitClient) -> None:
    c.created_ids = {"forms": [], "planes": []}


def run_calls(c: RevitClient, calls: list[dict[str, Any]]) -> list[Any]:
    """Run harness-side RPC calls (a task's setup or its oracle solution)."""
    if not hasattr(c, "created_ids"):
        reset_ids(c)
    out = []
    for call in calls:
        params = _substitute(call.get("params", {}), c.created_ids)
        result = c.call(call["method"], params, timeout=180)
        # Commands report most failures as {"success": false, ...} in the result, not as a JSON-RPC error.
        if isinstance(result, dict) and result.get("success") is False:
            raise RevitRpcError(call["method"], {"code": result.get("errorCode"), "message": result.get("error")})
        if isinstance(result, dict) and result.get("formType") == "ReferencePlane":
            c.created_ids["planes"].append(result.get("elementId"))
        elif isinstance(result, dict) and result.get("formType") and result.get("elementId") is not None:
            c.created_ids["forms"].append(result.get("elementId"))
        out.append(result)
    return out


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
