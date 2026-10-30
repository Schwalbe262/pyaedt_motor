import unittest
from types import SimpleNamespace
from unittest import mock

import run_ipmsm_batch as runner
from module import ipmsm_ppt_setup as setup


class BandControlTests(unittest.TestCase):
    def test_band_resolution_changes_its_provenance_without_changing_motor_circles(self):
        legacy = runner.build_spec({"circle_segments": 0})
        exact = runner.build_spec({"circle_segments": 0, "band_circle_segments": 0})
        self.assertEqual(legacy.band_circle_segments, 144)
        self.assertEqual(exact.circle_segments, legacy.circle_segments)
        self.assertNotEqual(runner.setup_fingerprint(legacy, "test", "sin_current", False),
                            runner.setup_fingerprint(exact, "test", "sin_current", False))

    def test_exact_band_leaves_outer_domain_unchanged_and_reads_cad_edges(self):
        motor = mock.Mock()
        motor.modeler.create_circle.side_effect = lambda **kwargs: SimpleNamespace(name=kwargs["name"])
        motor.modeler.oeditor.GetEdgeIDsFromObject.return_value = [100]
        with mock.patch.object(setup, "_m2d", return_value=motor), \
             mock.patch.object(setup, "_get_group", return_value=[]):
            result = setup.ensure_region_and_band(object(), {}, band_circle_segments=0)
        circles = motor.modeler.create_circle.call_args_list
        self.assertEqual(circles[0].kwargs["num_sides"], 144)
        self.assertEqual(circles[1].kwargs["num_sides"], 0)
        self.assertEqual(result["band_geometry"]["edge_count"], 1)

    def test_reusing_old_faceted_band_cannot_claim_an_exact_interface(self):
        motor = mock.Mock()
        motor.modeler.oeditor.GetEdgeIDsFromObject.return_value = list(range(144))
        with mock.patch.object(setup, "_m2d", return_value=motor), \
             mock.patch.object(setup, "_get_group", side_effect=lambda m, g, key, p: [key.title()]):
            with self.assertRaisesRegex(RuntimeError, "differs from requested"):
                setup.ensure_region_and_band(object(), {}, band_circle_segments=0)
        motor.modeler.create_circle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
