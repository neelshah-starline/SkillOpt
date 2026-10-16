"""Verifier checks for the Family Editor category.

They read the family document open for editing through get_family_parameters / get_family_rig / get_family_forms /
flex_family_parameters. Lengths are millimetres on the wire. `family_drive` writes (it sets one type value and
restores it), so put it after the read-only checks in a task.
"""

from __future__ import annotations

from .revit_client import RevitClient, RevitRpcError


def _parameters(c: RevitClient) -> dict:
    return c.call("get_family_parameters")


def _rig(c: RevitClient) -> dict:
    return c.call("get_family_rig")


def solid_box(c: RevitClient) -> tuple[list[float], list[float]] | None:
    """Union bounding box (min, max in mm) of the family's solid forms, or None when there are none."""
    forms = [f for f in c.call("get_family_forms").get("forms", []) if f.get("isSolid") and f.get("boundingBox")]
    if not forms:
        return None
    mins = [min(f["boundingBox"]["min"][a] for f in forms) for a in "xyz"]
    maxs = [max(f["boundingBox"]["max"][a] for f in forms) for a in "xyz"]
    return mins, maxs


def _result(problems: list[str]) -> tuple[bool, str]:
    return (not problems), ("ok" if not problems else "; ".join(problems))


def check_family_params(c, snap, chk) -> tuple[bool, str]:
    have = {p["name"]: p for p in _parameters(c).get("parameters", [])}
    problems = []
    for want in chk.get("params", []):
        p = have.get(want["name"])
        if p is None:
            problems.append(f"no parameter {want['name']!r}")
            continue
        if "dataType" in want and p.get("dataType") != want["dataType"]:
            problems.append(f"{want['name']} is {p.get('dataType')}, not {want['dataType']}")
        if "isInstance" in want and bool(p.get("isInstance")) != bool(want["isInstance"]):
            problems.append(f"{want['name']} isInstance={p.get('isInstance')}")
        if "formula" in want:
            actual = "".join((p.get("formula") or "").lower().split())
            if actual != "".join(want["formula"].lower().split()):
                problems.append(f"{want['name']} formula {p.get('formula')!r} != {want['formula']!r}")
    for name in chk.get("absent", []):
        if name in have:
            problems.append(f"parameter {name!r} should not exist")
    return _result(problems)


def check_family_planes(c, snap, chk) -> tuple[bool, str]:
    planes = {p["name"]: p for p in _rig(c).get("planes", [])}
    problems = []
    for want in chk["planes"]:
        p = planes.get(want["name"])
        if p is None:
            problems.append(f"no plane {want['name']!r} (have {sorted(planes)})")
            continue
        if "at" in want:  # {axis: x|y|z, value: mm}: the plane's end points sit at this coordinate
            actual = p["bubbleEnd"][want["at"]["axis"]]
            if abs(actual - want["at"]["value"]) > float(want.get("tol", 1.0)):
                problems.append(f"{want['name']} at {actual} != {want['at']['value']}")
        if want.get("is_reference") and p.get("isReference") in (None, "Not a Reference"):
            problems.append(f"{want['name']} is not a reference")
    return _result(problems)


def check_family_types(c, snap, chk) -> tuple[bool, str]:
    data = _parameters(c)
    types = data.get("types", [])
    problems = [f"no type {n!r} (have {types})" for n in chk.get("types", []) if n not in types]
    by_name = {p["name"]: p for p in data.get("parameters", [])}
    for type_name, values in chk.get("values", {}).items():
        for param, want in values.items():
            actual = (by_name.get(param, {}).get("values") or {}).get(type_name)
            try:
                if abs(float(actual) - float(want)) > float(chk.get("tol", 0.5)):
                    problems.append(f"{type_name}.{param}={actual} != {want}")
            except (TypeError, ValueError):
                problems.append(f"{type_name}.{param}={actual!r} is not a number")
    return _result(problems)


def check_family_dimensions(c, snap, chk) -> tuple[bool, str]:
    labels = [d.get("label") for d in _rig(c).get("dimensions", [])]
    missing = [name for name in chk["labels"] if name not in labels]
    return (not missing), ("ok" if not missing else f"no dimension labelled {missing} (have {labels})")


def check_family_flex(c, snap, chk) -> tuple[bool, str]:
    r = c.call("flex_family_parameters", {}, timeout=300)
    ok = bool(r.get("flexes")) and r.get("failures", 1) == 0 and r.get("trialsRun", 0) >= chk.get("min_trials", 1)
    return ok, f"flexes={r.get('flexes')} failures={r.get('failures')} trials={r.get('trialsRun')}"


def check_family_category(c, snap, chk) -> tuple[bool, str]:
    actual = _rig(c).get("category")
    return actual == chk["category"], f"category {actual!r}"


def check_family_forms(c, snap, chk) -> tuple[bool, str]:
    forms = c.call("get_family_forms").get("forms", [])
    solids = [f for f in forms if f.get("isSolid")]
    voids = [f for f in forms if not f.get("isSolid")]
    problems = []
    if len(solids) < chk.get("min_solids", 0):
        problems.append(f"{len(solids)} solids < {chk.get('min_solids')}")
    if len(voids) < chk.get("min_voids", 0):
        problems.append(f"{len(voids)} voids < {chk.get('min_voids')}")
    if "size_mm" in chk or chk.get("centered_xy"):
        box = solid_box(c)
        if box is None:
            problems.append("no solid form")
        else:
            if "size_mm" in chk:
                size = [box[1][i] - box[0][i] for i in range(3)]
                tol = float(chk.get("tol", 1.0))
                if any(abs(size[i] - chk["size_mm"][i]) > tol for i in range(3)):
                    problems.append(f"size {[round(x, 1) for x in size]} != {chk['size_mm']}")
            if chk.get("centered_xy") and any(abs(box[0][i] + box[1][i]) > 1.0 for i in (0, 1)):
                problems.append(f"solid not centred on the origin: {box}")
    return _result(problems)


def check_family_drive(c, snap, chk) -> tuple[bool, str]:
    """Parametric test: set one type value, and the solid's size along an axis must follow it. Restores the value."""
    by_name = {p["name"]: p for p in _parameters(c).get("parameters", [])}
    original = (by_name.get(chk["param"], {}).get("values") or {}).get(chk["type"])
    if original is None:
        return False, f"{chk['type']}.{chk['param']} has no value to restore"
    axis = "xyz".index(chk["axis"])
    expected = chk.get("expected_mm", chk["value_mm"])
    try:
        c.call("set_family_type_values", {"typeName": chk["type"], "values": {chk["param"]: chk["value_mm"]}})
        box = solid_box(c)
        if box is None:
            return False, "no solid form to measure"
        size = box[1][axis] - box[0][axis]
        ok = abs(size - expected) <= float(chk.get("tol", 1.0))
        detail = f"{chk['param']}={chk['value_mm']} gave {chk['axis']} size {size:.1f}, expected {expected}"
        if chk.get("centered"):  # the box must grow about the origin, not off to one side
            offset = box[0][axis] + box[1][axis]
            if abs(offset) > 1.0:
                ok, detail = False, f"{detail}; not centred on {chk['axis']} (min+max = {offset:.1f})"
        return ok, detail
    finally:
        try:
            c.call("set_family_type_values", {"typeName": chk["type"], "values": {chk["param"]: original}})
        except RevitRpcError:
            pass


FAMILY_CHECKS = {
    "family_params": check_family_params,
    "family_planes": check_family_planes,
    "family_types": check_family_types,
    "family_dimensions": check_family_dimensions,
    "family_flex": check_family_flex,
    "family_category": check_family_category,
    "family_forms": check_family_forms,
    "family_drive": check_family_drive,
}
