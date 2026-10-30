"""Live-editor contract and independent saved-CAD regressions; no AEDT needed."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from module.ipmsm_ppt_setup import audit_actual_winding_geometry


class LiveEditor:
    def __init__(self, area=585.0874100443018):
        self.units = "mm"
        self.faces = {f"winding{i+1}": [1001+i] for i in range(24)}
        self.areas = {1001+i: area for i in range(24)}
        self.calls = []

    def GetModelUnits(self):
        self.calls.append(("GetModelUnits",))
        if isinstance(self.units, Exception):
            raise self.units
        return self.units

    def GetFaceIDs(self, name):
        self.calls.append(("GetFaceIDs", name))
        value = self.faces[name]
        if isinstance(value, Exception):
            raise value
        return value

    def GetFaceArea(self, face):
        self.calls.append(("GetFaceArea", face))
        value = self.areas[face]
        if isinstance(value, Exception):
            raise value
        return value


class ActualWindingGeometryTests(unittest.TestCase):
    def setUp(self):
        self.editor = LiveEditor()
        # A believable cached unit/area must not rescue failed live readback.
        self.m2d = SimpleNamespace(
            modeler=SimpleNamespace(oeditor=self.editor, model_units="mm", cached_area=1e9),
            analyze=Mock(), analyze_setup=Mock(), current_a=100.0,
            get_evaluated_value=Mock(side_effect=AssertionError("no inferred conductor area")),
        )
        self.names = list(self.editor.faces)
        self.options = dict(turns_per_coil_side=12, conductor_area_per_branch_mm2=15.3206469,
                            slot_fill_limit=0.45)

    def call(self, names=None, **options):
        result = audit_actual_winding_geometry(
            self.m2d, self.names if names is None else names, **dict(self.options, **options))
        self.assert_no_analysis()
        json.dumps(result, allow_nan=False)
        return result

    def assert_no_analysis(self):
        self.m2d.analyze.assert_not_called()
        self.m2d.analyze_setup.assert_not_called()
        self.m2d.get_evaluated_value.assert_not_called()

    def fails(self, names=None, **options):
        with self.assertRaises(ValueError) as caught:
            self.call(names, **options)
        self.assert_no_analysis()
        audit = caught.exception.winding_geometry_audit
        self.assertFalse(audit["passed"])
        json.dumps(audit, allow_nan=False)
        return audit

    def test_saved_A57_and_G1_actual_CAD_windows(self):
        # Independent saved live-editor area measurements for all24 sheets,
        # not outputs of the analytic geometry helper being checked elsewhere.
        for side_area, fill in [(585.0874100443018, 0.3142227291920009),
                                (582.7370609249315, 0.3154900814240184)]:
            with self.subTest(area=side_area):
                self.editor.areas = {face: side_area for face in self.editor.areas}
                audit = self.call()
                self.assertTrue(audit["passed"])
                self.assertEqual(audit["count"], 24)
                self.assertEqual(audit["expected_count"], 24)
                self.assertEqual(audit["model_units_live"], "mm")
                self.assertIn("GetFaceArea", audit["source"])
                self.assertEqual(audit["units"]["window_area"], "mm^2")
                self.assertAlmostEqual(audit["min_window_area_mm2"], side_area, delta=1e-10)
                self.assertAlmostEqual(audit["max_slot_fill_ratio"], fill, delta=1e-12)
                self.assertEqual(len(audit["objects"]), 24)
                self.assertEqual(audit["objects"][0]["face_ids"], [1001])
                self.assertEqual(audit["objects"][0]["face_areas_mm2"], [side_area])
                self.assertAlmostEqual(audit["copper_area_per_coil_side_mm2"], 183.8477628)

    def test_actual_boundary_false_accept_is_rejected_before_analyze(self):
        # Independent polygon-shoelace area at Rout147,width_ratio0.8; the old
        # approximate filter reported0.43462644, while actual fill is0.45734843.
        self.editor.areas[1001] = 401.9862160142477
        audit = self.fails()
        self.assertEqual(audit["count"], 24)
        self.assertEqual(len(audit["objects"]), 24)
        self.assertAlmostEqual(audit["max_slot_fill_ratio"], 0.45734842508501294, delta=1e-12)
        self.assertEqual([x["name"] for x in audit["violations"]], ["winding1"])
        self.assertEqual(audit["min_window_area_mm2"], 401.9862160142477)

    def test_equal_limit_is_allowed_and_current_does_not_set_conductor_area(self):
        self.editor.areas = {face: 400.0 for face in self.editor.areas}
        for current in [0.0, 1.0, 500.0]:
            self.m2d.current_a = current
            audit = self.call(conductor_area_per_branch_mm2=15.0)
            self.assertEqual(audit["max_slot_fill_ratio"], 0.45)
            self.assertEqual(audit["conductor_area_per_branch_mm2"], 15.0)

    def test_multiple_distinct_live_faces_are_summed_once(self):
        self.editor.faces["winding1"] = ["2001", 2002]
        self.editor.areas.update({2001: "200.0", 2002: 385.0874100443018})
        audit = self.call()
        self.assertEqual(audit["objects"][0]["face_ids"], [2001, 2002])
        self.assertAlmostEqual(audit["objects"][0]["window_area_mm2"], 585.0874100443018)

    def test_missing_or_non_mm_live_units_cannot_use_cached_units(self):
        for units in [None, False, "", "m", "cm", "mm^2", RuntimeError("unit read failed")]:
            with self.subTest(units=units):
                self.editor.units = units
                self.editor.calls.clear()
                self.fails()
                self.assertFalse(any(call[0] == "GetFaceArea" for call in self.editor.calls))

    def test_missing_editor_and_missing_object_are_fail_closed(self):
        editor = self.m2d.modeler.oeditor
        self.m2d.modeler.oeditor = None
        self.fails()
        self.m2d.modeler.oeditor = editor
        del self.editor.faces["winding1"]
        self.fails()

    def test_invalid_names_duplicates_and_count_are_rejected(self):
        for names in [False, "winding1", self.names[:-1], self.names+["winding25"],
                      ["winding1"]*24, ["WINDING1"]+self.names[:-1],
                      [True]+self.names[1:], [""]+self.names[1:], [" winding1"]+self.names[1:]]:
            with self.subTest(names=repr(names)[:70]):
                self.fails(names)
        with self.assertRaises(ValueError):
            audit_actual_winding_geometry(self.m2d, None, **self.options)
        self.assert_no_analysis()

    def test_invalid_declared_physical_inputs_are_rejected(self):
        variations = {
            "turns_per_coil_side": [None, False, 0, -1, 12.0, math.inf],
            "conductor_area_per_branch_mm2": [None, False, True, "", 0, -1, math.nan, math.inf],
            "slot_fill_limit": [None, False, "", 0, -1, math.nan, math.inf, 1.01],
            "expected_count": [None, False, 0, -1, 24.0],
        }
        for field, values in variations.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.fails(**{field: value})
        self.fails(conductor_area_per_branch_mm2=1e308)

    def test_invalid_or_duplicate_live_face_ids_are_rejected(self):
        original = self.editor.faces["winding1"]
        for ids in [None, False, "1001", [], [True], [0], [-1], [1.2], ["1.2"],
                    ["nan"], [1001, 1001], RuntimeError("faces unavailable")]:
            with self.subTest(ids=ids):
                self.editor.faces["winding1"] = ids
                self.fails()
        self.editor.faces["winding1"] = original
        self.editor.faces["winding2"] = original
        audit = self.fails()
        self.assertIn("duplicate face ID", audit["error"])

    def test_missing_bool_nonfinite_or_nonpositive_live_area_is_rejected(self):
        for area in [None, False, True, "", 0.0, -1.0, math.nan, math.inf,
                     RuntimeError("area unavailable")]:
            with self.subTest(area=area):
                self.editor.areas[1001] = area
                audit = self.fails()
                self.assertEqual(audit["objects"][0]["face_areas_mm2"], [])
        del self.editor.areas[1001]
        self.fails()


if __name__ == "__main__":
    unittest.main()
