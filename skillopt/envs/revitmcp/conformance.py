"""Conformance checks for the Family Editor category: does the family follow the house standard, not only work?

Each rule cites the line of revit-mcp/.agents/skills/revit-family-authoring/SKILL.md it comes from. They read the
family through get_family_rig / get_family_parameters / get_family_forms (millimetres on the wire). A task selects the
rules it wants in `rules`. The standard is NOT given in a conformance task's prompt: the point is whether the skill
makes a client follow it, so a no-skill run is expected to score low here and the seed skill's score is the measure.

`user parameters` are those the model added: every parameter except the template's own (template_builtin_params.json,
written by scripts/revitmcp/dump_template_params.py from a live Revit; a short built-in list is the fallback).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .revit_client import RevitClient, RevitRpcError

BUILTIN_FILE = Path(__file__).resolve().parent / "tasks" / "template_builtin_params.json"
FALLBACK_BUILTIN = {
    "Default Elevation", "Type Image", "Keynote", "Model", "Manufacturer", "Type Comments", "URL", "Description",
    "Assembly Description", "Assembly Code", "Cost", "OmniClass Number", "OmniClass Title", "Type Mark", "Mark",
    "Code Name", "Always vertical", "Can Host Rebar", "Work Plane-Based", "Shared", "Room Calculation Point",
    "Cut with Voids When Loaded", "Light Source",
}
OUTER_PLANES = {"Left", "Right", "Front", "Back", "Top", "Bottom"}
WORD = r"[A-Z][A-Za-z0-9]*"
SNAKE = re.compile(rf"^{WORD}(_{WORD})*$")  # words joined by underscores, never a space (SKILL.md Step 3 "Names")
QUESTION = re.compile(r"^(Has|Show|Is|Can|Use|Include|Allow)_")  # Yes/No names read as questions (Step 3)
TYPE_NAME = re.compile(r"^\d+(\.\d+)?( x \d+(\.\d+)?)*$")  # dimensions only, e.g. "600 x 400" (Step 7)
DEFAULT_PLANE = re.compile(r"^Reference Plane \d+$")
EDITABLE_GROUPS = {"Dimensions", "Geometry"}  # Step 3 "Groups": editable sizes go in Geometry (Revit's Dimensions)


def builtin_params() -> set[str]:
    try:
        return set(json.loads(BUILTIN_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set(FALLBACK_BUILTIN)


def _user_params(c: RevitClient) -> list[dict]:
    skip = builtin_params()
    return [p for p in c.call("get_family_parameters").get("parameters", []) if p.get("name") not in skip]


def _rig(c: RevitClient) -> dict:
    return c.call("get_family_rig")


# ---------------------------------------------------------------------------------------------------------- rules
def rule_eq_symmetry(c, opts) -> list[str]:
    """Step 4: symmetry comes from EQ strings across three or more planes, and an EQ dimension carries no label."""
    dims = _rig(c).get("dimensions", [])
    eq = [d for d in dims if d.get("eq")]
    problems = []
    if len(eq) < int(opts.get("min_eq", 2)):
        problems.append(f"{len(eq)} EQ dimension(s), expected at least {opts.get('min_eq', 2)}")
    problems += [f"EQ dimension {d.get('id')} has fewer than three planes" for d in eq if len(d.get("references", [])) < 3]
    problems += [f"EQ dimension {d.get('id')} carries the label {d.get('label')!r}" for d in eq if d.get("label")]
    return problems


def rule_params_driven(c, opts) -> list[str]:
    """Step 3 and the checklist: every size parameter moves a plane: it labels a dimension or appears in a formula."""
    rig = _rig(c)
    labels = {d.get("label") for d in rig.get("dimensions", []) if d.get("label")}
    params = _user_params(c)
    formulas = " ".join((p.get("formula") or "") for p in params)
    problems = []
    for p in params:
        if p.get("dataType") not in ("Length", "Angle"):
            continue
        if p["name"] in labels or p.get("isDeterminedByFormula") or re.search(rf"\b{re.escape(p['name'])}\b", formulas):
            continue
        problems.append(f"{p['name']} is not on a labelled dimension or in a formula")
    return problems


def rule_param_names(c, opts) -> list[str]:
    """Step 3 'Names': words joined by underscores, never a space; Yes/No names phrased as questions."""
    problems = []
    for p in _user_params(c):
        if not SNAKE.match(p["name"]):
            problems.append(f"parameter {p['name']!r} is not Words_Joined_By_Underscores")
        elif p.get("dataType") == "YesNo" and not QUESTION.match(p["name"]):
            problems.append(f"Yes/No parameter {p['name']!r} is not phrased as a question (Has_/Show_/Is_...)")
    return problems


def rule_param_scope(c, opts) -> list[str]:
    """Step 3 'Type or instance': a size that is the same for every copy of a product is a type parameter."""
    instance_ok = set(opts.get("instance_ok", []))
    return [
        f"{p['name']} is an instance parameter" for p in _user_params(c)
        if p.get("dataType") == "Length" and p.get("isInstance") and p["name"] not in instance_ok
    ]


def rule_param_groups(c, opts) -> list[str]:
    """Step 3 'Groups': editable sizes go in Geometry (Revit's Dimensions group)."""
    return [
        f"{p['name']} is in group {p.get('group')!r}" for p in _user_params(c)
        if p.get("dataType") == "Length" and not p.get("isDeterminedByFormula") and p.get("group") not in EDITABLE_GROUPS
    ]


def rule_type_names(c, opts) -> list[str]:
    """Step 7: type names are the dimensions only, e.g. '600 x 400', no family or category name."""
    return [f"type {t!r} is not named by its dimensions" for t in c.call("get_family_parameters").get("types", []) if not TYPE_NAME.match(t)]


def rule_plane_names(c, opts) -> list[str]:
    """Step 2 'Names': named planes; no leftover default names."""
    planes = _rig(c).get("planes", [])
    problems = [f"plane {p['name']!r} has a default name" for p in planes if DEFAULT_PLANE.match(p["name"])]
    # The template ships one plane called "Reference Plane" (its reference-level plane); a second one is a user's unnamed plane.
    extra = sum(1 for p in planes if p["name"] == "Reference Plane") - 1
    if extra > 0:
        problems.append(f"{extra} more plane(s) still called 'Reference Plane'")
    return problems


def rule_reference_types(c, opts) -> list[str]:
    """Step 2 'Is Reference': outer planes (Left, Right, Front, Back, Top, Bottom) get the matching named value."""
    return [
        f"plane {p['name']} is {p.get('isReference')!r}, expected {p['name']!r}"
        for p in _rig(c).get("planes", []) if p["name"] in OUTER_PLANES and p.get("isReference") != p["name"]
    ]


def rule_no_duplicate_planes(c, opts) -> list[str]:
    """Step 2: reuse the template's origin planes; never draw a second plane on top of another."""
    seen: dict[tuple, str] = {}
    problems = []
    for p in _rig(c).get("planes", []):
        n = p.get("normal") or {}
        axis = max(("x", "y", "z"), key=lambda a: abs(n.get(a, 0.0)))
        offset = round(p.get("bubbleEnd", {}).get(axis, 0.0) / 1.0)
        key = (axis, offset)
        if key in seen:
            problems.append(f"planes {seen[key]!r} and {p['name']!r} coincide on {axis} = {offset} mm")
        seen[key] = p["name"]
    return problems


def rule_void_cut(c, opts) -> list[str]:
    """Step 6 'Voids': a void cuts nothing until it is combined (combine_family_geometry cut)."""
    forms = c.call("get_family_forms")
    voids = [f for f in forms.get("forms", []) if not f.get("isSolid")]
    if voids and not forms.get("combinations"):
        return [f"{len(voids)} void form(s) but no combination: the void cuts nothing"]
    return []


RULES = {
    "eq_symmetry": rule_eq_symmetry,
    "params_driven": rule_params_driven,
    "param_names": rule_param_names,
    "param_scope": rule_param_scope,
    "param_groups": rule_param_groups,
    "type_names": rule_type_names,
    "plane_names": rule_plane_names,
    "reference_types": rule_reference_types,
    "no_duplicate_planes": rule_no_duplicate_planes,
    "void_cut": rule_void_cut,
}


def check_family_conformance(c, snap, chk) -> tuple[bool, str]:
    """chk: {rules: [name, ...], opts: {rule: {...}}}. Passes only when every selected rule has no problems."""
    problems = []
    for name in chk["rules"]:
        found = RULES[name](c, (chk.get("opts") or {}).get(name, {}))
        problems += [f"{name}: {m}" for m in found]
    return (not problems), ("ok" if not problems else "; ".join(problems))


def check_family_params_move_geometry(c, snap, chk) -> tuple[bool, str]:
    """Name-free functional test: every user Length parameter, set to 1.5x on one type, must change the size of the
    solid on some axis; the box must also be centred on the origin in plan. Values are restored afterwards."""
    from .family_checks import solid_box

    data = c.call("get_family_parameters")
    types = data.get("types", [])
    if not types:
        return False, "the family has no types"
    skip = builtin_params()
    lengths = [p for p in data.get("parameters", []) if p.get("dataType") == "Length" and p["name"] not in skip and not p.get("isDeterminedByFormula")]
    if len(lengths) < int(chk.get("min_params", 1)):
        return False, f"{len(lengths)} user Length parameter(s), expected at least {chk.get('min_params', 1)}"
    type_name = chk.get("type") or types[-1]
    base = solid_box(c)
    if base is None:
        return False, "no solid form"
    problems = []
    if chk.get("centered_xy", True) and any(abs(base[0][i] + base[1][i]) > 1.0 for i in (0, 1)):
        problems.append("solid is not centred on the origin in plan")
    size0 = [base[1][i] - base[0][i] for i in range(3)]
    for p in lengths:
        original = (p.get("values") or {}).get(type_name)
        if not original:
            problems.append(f"{p['name']} has no value on type {type_name!r}")
            continue
        try:
            c.call("set_family_type_values", {"typeName": type_name, "values": {p["name"]: float(original) * 1.5}})
            box = solid_box(c)
            size1 = [box[1][i] - box[0][i] for i in range(3)] if box else size0
            if max(abs(size1[i] - size0[i]) for i in range(3)) < 1.0:
                problems.append(f"{p['name']} does not change the geometry")
        except RevitRpcError as exc:
            problems.append(f"{p['name']}: {exc}")
        finally:
            try:
                c.call("set_family_type_values", {"typeName": type_name, "values": {p["name"]: original}})
            except RevitRpcError:
                pass
    return (not problems), ("ok" if not problems else "; ".join(problems))


CONFORMANCE_CHECKS = {
    "family_conformance": check_family_conformance,
    "family_params_move_geometry": check_family_params_move_geometry,
}
