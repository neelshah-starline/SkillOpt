"""Revit-free tests of the Family Editor conformance rules, using a fake client that returns canned family data."""

from __future__ import annotations

import copy

from skillopt.envs.revitmcp import conformance as cf


def good_family():
    return {
        "get_family_rig": {
            "planes": [
                {"name": "Center (Left/Right)", "isReference": "Center (Left/Right)", "normal": {"x": 1, "y": 0, "z": 0}, "bubbleEnd": {"x": 0, "y": 0, "z": 0}},
                {"name": "Center (Front/Back)", "isReference": "Center (Front/Back)", "normal": {"x": 0, "y": -1, "z": 0}, "bubbleEnd": {"x": 0, "y": 0, "z": 0}},
                {"name": "Reference Plane", "isReference": "Not a Reference", "normal": {"x": 0, "y": 0, "z": 1}, "bubbleEnd": {"x": 0, "y": 0, "z": 0}},
                {"name": "Left", "isReference": "Left", "normal": {"x": 1, "y": 0, "z": 0}, "bubbleEnd": {"x": -300, "y": 0, "z": 0}},
                {"name": "Right", "isReference": "Right", "normal": {"x": 1, "y": 0, "z": 0}, "bubbleEnd": {"x": 300, "y": 0, "z": 0}},
                {"name": "Top", "isReference": "Top", "normal": {"x": 0, "y": 0, "z": 1}, "bubbleEnd": {"x": 0, "y": 0, "z": 750}},
            ],
            "dimensions": [
                {"id": 1, "references": ["Left", "Center (Left/Right)", "Right"], "label": None, "eq": True},
                {"id": 2, "references": ["Front", "Center (Front/Back)", "Back"], "label": None, "eq": True},
                {"id": 3, "references": ["Left", "Right"], "label": "Width", "eq": False},
                {"id": 4, "references": ["Reference Plane", "Top"], "label": "Height", "eq": False},
            ],
        },
        "get_family_parameters": {
            "types": ["600 x 750", "900 x 750"],
            "parameters": [
                {"name": "Keynote", "dataType": "Text", "isInstance": False, "group": "Identity Data"},
                {"name": "Width", "dataType": "Length", "isInstance": False, "group": "Dimensions", "values": {"600 x 750": 600, "900 x 750": 900}},
                {"name": "Height", "dataType": "Length", "isInstance": False, "group": "Dimensions", "values": {"600 x 750": 750, "900 x 750": 750}},
                {"name": "Has_Handle", "dataType": "YesNo", "isInstance": True, "group": "Visibility"},
            ],
        },
        "get_family_forms": {"forms": [{"isSolid": True}], "combinations": []},
    }


class FakeClient:
    def __init__(self, data):
        self.data = data

    def call(self, method, params=None, timeout=None):
        return self.data[method]


def run(data, rules, opts=None):
    return cf.check_family_conformance(FakeClient(data), {}, {"rules": rules, "opts": opts or {}})


ALL = list(cf.RULES)


def test_a_conforming_family_passes_every_rule():
    ok, detail = run(good_family(), ALL)
    assert ok, detail


def mutate(change):
    data = copy.deepcopy(good_family())
    change(data)
    return data


def test_eq_symmetry_needs_unlabelled_eq_across_three_planes():
    ok, detail = run(mutate(lambda d: d["get_family_rig"]["dimensions"][0].update(label="Width")), ["eq_symmetry"])
    assert not ok and "carries the label" in detail
    ok, detail = run(mutate(lambda d: d["get_family_rig"]["dimensions"][0].update(references=["Left", "Right"])), ["eq_symmetry"])
    assert not ok and "fewer than three planes" in detail
    ok, _ = run(mutate(lambda d: d["get_family_rig"].update(dimensions=[])), ["eq_symmetry"])
    assert not ok


def test_params_driven_flags_a_size_that_moves_nothing():
    data = mutate(lambda d: d["get_family_parameters"]["parameters"].append(
        {"name": "Spare", "dataType": "Length", "isInstance": False, "group": "Dimensions", "values": {}}))
    ok, detail = run(data, ["params_driven"])
    assert not ok and "Spare" in detail
    data["get_family_parameters"]["parameters"][-1]["formula"] = "Width / 2"
    ok, detail = run(data, ["params_driven"])
    assert not ok  # a formula on Spare itself does not make Spare drive anything... but Width does appear in a formula
    data["get_family_parameters"]["parameters"][-1]["isDeterminedByFormula"] = True
    assert run(data, ["params_driven"])[0]


def test_parameter_names_follow_the_underscore_convention():
    data = mutate(lambda d: d["get_family_parameters"]["parameters"].append(
        {"name": "Shelf Height", "dataType": "Length", "isInstance": False, "group": "Dimensions"}))
    ok, detail = run(data, ["param_names"])
    assert not ok and "Shelf Height" in detail
    data = mutate(lambda d: d["get_family_parameters"]["parameters"][3].update(name="Handle"))
    ok, detail = run(data, ["param_names"])
    assert not ok and "question" in detail


def test_type_scope_and_group_rules():
    data = mutate(lambda d: d["get_family_parameters"]["parameters"][1].update(isInstance=True))
    assert not run(data, ["param_scope"])[0]
    assert run(data, ["param_scope"], {"param_scope": {"instance_ok": ["Width"]}})[0]
    data = mutate(lambda d: d["get_family_parameters"]["parameters"][1].update(group="Other"))
    assert not run(data, ["param_groups"])[0]


def test_type_names_are_dimensions_only():
    data = mutate(lambda d: d["get_family_parameters"].update(types=["Cabinet 600 wide"]))
    ok, detail = run(data, ["type_names"])
    assert not ok and "Cabinet 600 wide" in detail


def test_plane_rules():
    data = mutate(lambda d: d["get_family_rig"]["planes"].append(
        {"name": "Reference Plane 1", "isReference": "Not a Reference", "normal": {"x": 1, "y": 0, "z": 0}, "bubbleEnd": {"x": 123, "y": 0, "z": 0}}))
    ok, detail = run(data, ["plane_names"])
    assert not ok and "default name" in detail
    data = mutate(lambda d: d["get_family_rig"]["planes"][3].update(isReference="Strong Reference"))
    ok, detail = run(data, ["reference_types"])
    assert not ok and "Left" in detail
    data = mutate(lambda d: d["get_family_rig"]["planes"].append(
        {"name": "Mid", "isReference": "Not a Reference", "normal": {"x": 1, "y": 0, "z": 0}, "bubbleEnd": {"x": 0, "y": 0, "z": 0}}))
    ok, detail = run(data, ["no_duplicate_planes"])
    assert not ok and "coincide" in detail


def test_a_void_must_be_combined():
    data = mutate(lambda d: d["get_family_forms"]["forms"].append({"isSolid": False}))
    ok, detail = run(data, ["void_cut"])
    assert not ok and "cuts nothing" in detail
    data["get_family_forms"]["combinations"] = [{"id": 1}]
    assert run(data, ["void_cut"])[0]


class GeometryClient:
    """A box whose size follows only the parameters in `live`, to test the name-free functional check."""

    def __init__(self, live):
        self.live = live
        self.values = {"Width": 900, "Depth": 500, "Height": 750}
        self.base = {k: v for k, v in self.values.items()}

    def call(self, method, params=None, timeout=None):
        if method == "get_family_parameters":
            return {"types": ["T"], "parameters": [
                {"name": n, "dataType": "Length", "isInstance": False, "values": {"T": v}} for n, v in self.values.items()]}
        if method == "set_family_type_values":
            self.values.update(params["values"])
            return {"success": True}
        if method == "get_family_forms":
            w, d, h = (self.values[n] if n in self.live else self.base[n] for n in ("Width", "Depth", "Height"))
            return {"forms": [{"isSolid": True, "boundingBox": {"min": {"x": -w / 2, "y": -d / 2, "z": 0}, "max": {"x": w / 2, "y": d / 2, "z": h}}}]}
        raise KeyError(method)


def test_every_parameter_must_move_the_geometry_by_name_free_check():
    ok, detail = cf.check_family_params_move_geometry(GeometryClient({"Width", "Depth", "Height"}), {}, {})
    assert ok, detail
    client = GeometryClient({"Width", "Depth"})
    ok, detail = cf.check_family_params_move_geometry(client, {}, {})
    assert not ok and "Height does not change the geometry" in detail
    assert client.values == {"Width": 900, "Depth": 500, "Height": 750}  # values are restored
