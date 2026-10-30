"""Regress the planar winding-window area against saved live CAD readbacks."""
from __future__ import annotations

import math
import unittest

import ipmsm_optimization as opt


# Scalar input geometry and one-sheet areas independently read from the saved
# Maxwell projects (snapshot_003006, 2026-10-31). All 24 sheets agree per project.
# These expected values are CAD measurements, not a second copy of the formula.
A57 = {
    "stator_outer_radius": 148.806032259017,
    "stator_back_yoke_thick_ratio": 0.129131790734828,
    "stator_inner_ratio": 0.523746726904064,
    "stator_shoe_thick": 1.5,
    "stator_teeth_length_ratio": 0.853803803026676,
    "stator_teeth_width_ratio": 0.621375668793917,
    "stator_gap": 1.77626098133624,
    "slot_opening_ratio": 0.175060123763979,
    "rotator_gap": 1.08954149372876,
    "shaft_ratio": 0.527731129992753,
    "magnet_shield_thick": 1.61485776491463,
    "magnet_setback_ratio": 0.144695685431361,
    "magnet_thick_ratio": 0.291019161138683,
    "magnet_space_height_ratio": 0.901623307354748,
    "magnet_height_ratio": 0.786338815931231,
}
G1 = {
    "stator_outer_radius": 120.066859927028,
    "stator_back_yoke_thick_ratio": 0.155508870892227,
    "stator_inner_ratio": 0.45562615335919,
    "stator_shoe_thick": 1.5,
    "stator_teeth_length_ratio": 0.89346409314312,
    "stator_teeth_width_ratio": 0.406753448396921,
    "stator_gap": 1.77520799357444,
    "slot_opening_ratio": 0.0386014494579285,
    "rotator_gap": 0.816918783262372,
    "shaft_ratio": 0.463832660438493,
    "magnet_shield_thick": 1.51550061628222,
    "magnet_setback_ratio": 0.117504410166293,
    "magnet_thick_ratio": 0.461942603997886,
    "magnet_space_height_ratio": 0.843970818817616,
    "magnet_height_ratio": 0.879953371360898,
}


def actual_winding() -> opt.WindingSpec:
    return opt.WindingSpec(48, 12, 4, 1, 15.3206469, 1, 0.45, 1.0, 5.0)


class CoilWindowAreaTests(unittest.TestCase):
    def test_exact_area_matches_both_saved_live_cad_projects(self) -> None:
        for name, design, side_area, expected_fill in [
            ("A57", A57, 585.0874100443018, 0.3142227291920009),
            ("G1", G1, 582.7370609249315, 0.3154900814240184),
        ]:
            with self.subTest(project=name):
                metrics = opt.geometry_metrics(design, 70.0, actual_winding())
                self.assertAlmostEqual(metrics.slot_area_mm2, 2.0 * side_area, delta=1e-9)
                self.assertAlmostEqual(metrics.slot_fill_ratio, expected_fill, delta=1e-12)
                self.assertLess(metrics.slot_fill_ratio, actual_winding().fill_factor)

    def test_old_annular_approximation_can_falsely_accept_fill_limit(self) -> None:
        design = dict(A57, stator_outer_radius=147.0, stator_teeth_width_ratio=0.8)
        metrics = opt.geometry_metrics(design, 70.0, actual_winding())
        # This area was independently checked with the shoelace formula on the
        # four CAD vertices, then divided in half at the angular midplane.
        self.assertAlmostEqual(metrics.slot_area_mm2, 2.0 * 401.9862160142477, delta=1e-9)
        self.assertLessEqual(0.4346264396901005, actual_winding().fill_factor)
        self.assertGreater(metrics.slot_fill_ratio, actual_winding().fill_factor)
        self.assertAlmostEqual(metrics.slot_fill_ratio, 0.45734842508501294, delta=1e-12)

    def test_resistance_turn_length_and_active_volume_are_unchanged(self) -> None:
        for name, design, depth, turn_length, resistance, volume in [
            ("A57", A57, 70.0, 261.33292750517546,
             0.018553381701385107, 0.004869551760236673),
            ("G1", G1, 49.45, 192.2786275704211,
             0.013650858329981565, 0.0022395588249376993),
        ]:
            with self.subTest(project=name):
                metrics = opt.geometry_metrics(design, depth, actual_winding())
                self.assertAlmostEqual(metrics.mean_turn_length_mm, turn_length, delta=1e-10)
                self.assertAlmostEqual(
                    opt.phase_resistance_ohm(metrics.mean_turn_length_mm, actual_winding()),
                    resistance, delta=1e-14,
                )
                self.assertAlmostEqual(
                    opt.active_volume_m3(design["stator_outer_radius"], depth),
                    volume, delta=1e-15,
                )

    def test_invalid_or_crossing_coil_window_is_rejected(self) -> None:
        for args, keywords in [
            ((60.0, 100.0, 40.0), {}),  # Negative inner half-height.
            ((100.0, 100.0, 1.0), {}),  # Outer chord lies inside inner chord.
            ((60.0, 100.0, 200.0), {}),  # No outer-circle intersection.
            ((60.0, math.nan, 10.0), {}),
            ((60.0, 100.0, 10.0), {"slot_number": 0}),
            ((60.0, 100.0, 10.0), {"slot_number": True}),
        ]:
            with self.subTest(dimensions=args, options=keywords):
                with self.assertRaises(ValueError):
                    opt.coil_window_area_mm2(*args, **keywords)


if __name__ == "__main__":
    unittest.main()
