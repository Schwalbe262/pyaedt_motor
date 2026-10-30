"""Exercise cached setters that report success while AEDT keeps another material."""
import unittest
from unittest import mock

from module import ipmsm_ppt_setup as setup


def material_fixture(blocked=()):
    groups = {"stator": ["stator"], "rotor": ["rotator"], "shaft": ["shaft"],
              "magnets": [f"magnet_{index}" for index in range(8)],
              "windings": [f"winding{index}" for index in range(24)],
              "region": ["Region"], "band": ["Band"], "air_pockets": ["magnet_space"]}
    live = {name: "copper" for values in groups.values() for name in values}
    blocked = set(blocked)

    class Body:
        def __init__(self, name):
            self.name, self.cached_material = name, "copper"

        @property
        def material_name(self):
            return self.cached_material

        @material_name.setter
        def material_name(self, value):
            self.cached_material = value
            if (self.name, value) not in blocked:
                live[self.name] = value

    bodies = {name: Body(name) for name in live}
    motor = mock.Mock()
    motor.modeler.get_object_from_name.side_effect = bodies.__getitem__
    motor.modeler.oeditor.GetPropertyValue.side_effect = lambda tab, name, prop: '"' + live[name] + '"'
    return motor, groups, live, bodies


class MaterialReadbackTests(unittest.TestCase):
    def assign(self, blocked=(), *, strict=True):
        motor, groups, live, bodies = material_fixture(blocked)
        with mock.patch.object(setup, "_m2d", return_value=motor), \
             mock.patch.object(setup, "_get_group", side_effect=lambda m, g, key, p: groups.get(key, [])):
            result = setup.assign_ppt_materials(object(), groups, setup.IPMSMPPTSpec(strict_setup=strict))
        return result, live, bodies

    def test_air_pockets_get_air_and_live_assignments_cover_every_body(self):
        result, live, _ = self.assign()
        self.assertEqual(result["errors"], [])
        self.assertEqual(live["magnet_space"], "air")
        self.assertIn("magnet_space", result["air"])
        self.assertEqual(result["material_readback"], live)

    def test_cached_success_cannot_hide_one_failed_magnet_assignment(self):
        material = setup.IPMSMPPTSpec().magnet_material
        result, live, bodies = self.assign([("magnet_0", material)])
        self.assertEqual(bodies["magnet_0"].material_name, material)
        self.assertEqual(live["magnet_0"], "copper")
        self.assertTrue(any("magnets: assigned 7/8" in error for error in result["errors"]))
        self.assertTrue(setup.critical_setup_issues({"materials": result, "validation": 1}))

    def test_strict_contract_rejects_successful_fallback_to_another_steel(self):
        requested = setup.IPMSMPPTSpec().core_material
        blocked = [(name, requested) for name in ["stator", "rotator"]]
        result, _, _ = self.assign(blocked)
        self.assertNotEqual(result["core_material"], [requested])
        self.assertTrue(any("fallback core material" in error for error in result["errors"]))
        compatible, _, _ = self.assign(blocked, strict=False)
        self.assertEqual(compatible["errors"], [])


if __name__ == "__main__":
    unittest.main()
