from __future__ import annotations

from types import SimpleNamespace
import unittest

from module import variable


FIXED_GEOMETRY = {
    "slot_num": 12,
    "pole_num": 8,
    "stator_outer_radius": 155.0,
    "stator_back_yoke_thick_ratio": 0.142,
    "stator_inner_ratio": 0.513,
    "stator_shoe_thick": 1.1,
    "stator_teeth_length_ratio": 0.847,
    "stator_teeth_width_ratio": 0.722,
    "stator_gap": 2.43,
    "slot_opening_ratio": 0.09,
    "rotator_gap": 1.54,
    "shaft_ratio": 0.516,
    "magnet_shield_thick": 1.435,
    "magnet_setback_ratio": 0.163,
    "magnet_thick_ratio": 0.313,
    "magnet_space_height_ratio": 1.0,
    "magnet_height_ratio": 1.0,
}


class FakeDesign(dict):
    def __init__(self, values: dict[str, float]) -> None:
        super().__init__()
        self.values = values

    def random_variable(self, variable_name: str, **_: object) -> float:
        return self.values[variable_name]


class VariableFixedGeometryTests(unittest.TestCase):
    def test_set_variable_accepts_fixed_geometry_without_pandas(self) -> None:
        design: dict[str, str] = {}
        simulation = SimpleNamespace(
            fixed_geometry=FIXED_GEOMETRY
        )

        frame = variable.set_variable(simulation, design)
        row = frame.iloc[0].to_dict()
        expected_rotor_radius = row["stator_inner_radius"] - FIXED_GEOMETRY["rotator_gap"]

        self.assertEqual(design["stator_outer_radius"], "155.0mm")
        self.assertEqual(design["slot_num"], "12")
        self.assertEqual(design["stator_teeth_width_ratio"], "0.722")
        self.assertEqual(design["rotor_radius"], "stator_inner_radius - rotator_gap")
        self.assertAlmostEqual(row["stator_outer_radius"], 155.0)
        self.assertAlmostEqual(row["slot_opening_ratio"], 0.09)
        self.assertAlmostEqual(row["rotor_radius"], expected_rotor_radius)
        self.assertAlmostEqual(row["shaft_radius"], expected_rotor_radius * FIXED_GEOMETRY["shaft_ratio"])
        self.assertIn("stator_teeth_width", row)

    def test_random_geometry_records_rotor_radius_matching_design_expression(self) -> None:
        design = FakeDesign(FIXED_GEOMETRY)
        simulation = SimpleNamespace()

        frame = variable.set_variable(simulation, design)
        row = frame.iloc[0].to_dict()

        self.assertEqual(design["rotor_radius"], "stator_inner_radius - rotator_gap")
        self.assertAlmostEqual(row["rotor_radius"], row["stator_inner_radius"] - row["rotator_gap"])
        self.assertAlmostEqual(row["shaft_radius"], row["rotor_radius"] * row["shaft_ratio"])


if __name__ == "__main__":
    unittest.main()
