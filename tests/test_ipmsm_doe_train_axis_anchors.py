"""Synthetic input-only opt-in planning; no calibration/profile/production approval."""
from copy import deepcopy
from dataclasses import asdict
import csv
import io
import json
from pathlib import Path
import unittest
from unittest import mock

from pyaedt_motor import cli, doe
from tests import test_ipmsm_v3_doe as legacy
import train_ipmsm_lightgbm as trainer


class TrainAxisAnchorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.V3DoeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def plan(self, controls=3, count=120, **kwargs):
        return doe.generate_training_plan(self.fixture.spec, self.fixture.calibration, self.fixture.profile,
                                         geometries=count, seed=123, controls_per_geometry=controls, **kwargs)

    def test_axes_match_actual_trainer_and_builder_feature_values(self):
        required = trainer.select_v2_training_input_columns((*trainer.INPUT_COLUMNS, *trainer.V2_REQUIRED_CONDITIONAL_INPUT_COLUMNS))
        constants_controls = {"slot_num", "pole_num", "stator_shoe_thick", "base_rpm", "i_peak_a", "beta_dq_deg"}
        self.assertEqual(set(doe.STATIC_MODEL_AXES), {name.removeprefix("input_") for name in required} - constants_controls)
        self.assertEqual(len(doe.STATIC_MODEL_AXES), 22)
        rows, metadata = self.plan(partition_policy=doe.TRAIN_AXIS_ANCHOR_POLICY)
        from module import variable
        from types import SimpleNamespace
        for row in rows[::3]:
            sim = SimpleNamespace(**{name: row[name] for name in doe.GEOMETRY_VARIABLE_NAMES}, slot_num=12, pole_num=8)
            variable._populate_derived_geometry(sim, {})
            for name in doe.STATIC_MODEL_AXES:
                value = row[name] if name in {"stack_length_mm", "phase_resistance_ohm"} else getattr(sim, name)
                lower, upper = metadata["partition_policy"]["training_bounds"][name]
                self.assertLessEqual(lower, value)
                self.assertLessEqual(value, upper)

    def test_optin_preserves_every_case_control_order_and_original_corner_anchors(self):
        for controls in (3, 4):
            baseline, default_proof = self.plan(controls)
            explicit, explicit_proof = self.plan(controls, partition_policy=None)
            self.assertEqual((baseline, default_proof), (explicit, explicit_proof))
            self.assertNotIn("partition_policy", default_proof)
            rows, proof = self.plan(controls, partition_policy=doe.TRAIN_AXIS_ANCHOR_POLICY)
            repeated = self.plan(controls, partition_policy=doe.TRAIN_AXIS_ANCHOR_POLICY)
            self.assertEqual((rows, proof), repeated)
            self.assertEqual([{k: v for k, v in row.items() if k != "doe_split"} for row in rows],
                             [{k: v for k, v in row.items() if k != "doe_split"} for row in baseline])
            self.assertEqual(proof["group_counts"], {"train": 72, "calibration": 24, "test": 24})
            mandatory = proof["partition_policy"]["mandatory_original_control_anchor_indices"]
            self.assertTrue(all(baseline[i * controls]["doe_split"] == rows[i * controls]["doe_split"] == "train" for i in mandatory))
            for rpm in (1200, 5000):
                train = [row for row in rows if row["doe_split"] == "train" and row["base_rpm"] == rpm]
                self.assertEqual([min(r["i_peak_a"] for r in train), max(r["i_peak_a"] for r in train)], proof["current_domain_a"])
                self.assertEqual([min(r["beta_dq_deg"] for r in train), max(r["beta_dq_deg"] for r in train)], proof["beta_domain_deg"])
            self.assertFalse(proof["partition_policy"]["targets_or_results_read"])
            self.assertFalse(proof["partition_policy"]["joint_coverage_or_accuracy_certified"])

    def test_exact_capacity_ties_and_unknown_input_reject(self):
        pool = doe.sample_feasible_geometries(self.fixture.spec, 120, 123)
        import numpy as np
        order = np.random.default_rng(np.random.SeedSequence(123).spawn(1)[0]).permutation(120).tolist()
        fixed = [(dict(pool[0][0]), float(40 + i / 10), "") for i in range(120)]
        fixed = [(design, depth, doe.physical_geometry_hash(design, depth)) for design, depth, _ in fixed]
        _, metadata = doe._train_axis_anchor_partition(fixed, order, self.fixture.spec.winding, 4)
        self.assertEqual(metadata["anchor_reasons"]["stator_outer_radius:min"]["index"], order[0])
        self.assertEqual(metadata["anchor_reasons"]["stator_outer_radius:max"]["index"], order[0])
        with self.assertRaisesRegex(ValueError, "capacity"):
            self.plan(count=5, partition_policy=doe.TRAIN_AXIS_ANCHOR_POLICY)
        for bad in ("unknown", True, 1):
            with self.assertRaisesRegex(ValueError, "partition policy"):
                self.plan(partition_policy=bad)
        bad = deepcopy(pool); bad[0][0]["output_torque_avg"] = 1
        with self.assertRaisesRegex(ValueError, "input-only"):
            doe._train_axis_anchor_partition(bad, order, self.fixture.spec.winding, 4)
        with self.assertRaises(ValueError):
            doe._train_axis_anchor_partition(pool, [True, *order[1:]], self.fixture.spec.winding, 4)

    def test_write_verify_recompute_policy_and_exclusive_no_repartition(self):
        rows, proof = self.plan(partition_policy=doe.TRAIN_AXIS_ANCHOR_POLICY)
        path = self.fixture.root / "new plan.csv"
        doe.write_training_plan(path, rows, proof)
        self.assertEqual(doe.verify_training_plan(path)["partition_policy"], proof["partition_policy"])
        with self.assertRaises(FileExistsError):
            doe.write_training_plan(path, rows, proof)
        for change in ("policy", "bound", "label"):
            changed_rows, changed_proof = deepcopy(rows), deepcopy(proof)
            if change == "policy": changed_proof["partition_policy"]["policy"] = "unknown"
            elif change == "bound": changed_proof["partition_policy"]["training_bounds"][doe.STATIC_MODEL_AXES[0]][0] -= 1
            else: changed_rows[0]["doe_split"] = "test" if rows[0]["doe_split"] == "train" else "train"
            with self.assertRaises(ValueError):
                doe.write_training_plan(self.fixture.root / (change + ".csv"), changed_rows, changed_proof)

    def corrupt_static_rows(self, rows, proof, controls, change):
        changed = deepcopy(rows)
        # A non-first row proves checks cover every control row of a geometry.
        index = next(i for i, row in enumerate(changed) if row["doe_split"] != "train" and i % controls == 1)
        if change == "resistance":
            changed[index]["phase_resistance_ohm"] = proof["partition_policy"]["training_bounds"]["phase_resistance_ohm"][1] + 1
        elif change == "missing_resistance":
            for row in changed: del row["phase_resistance_ohm"]
        else:
            designs = [({name: row[name] for name in doe.GEOMETRY_VARIABLE_NAMES}, row["stack_length_mm"], row["design_hash"])
                       for row in changed[::controls]]
            values, _ = doe._anchor_axis_values(designs, self.fixture.spec.winding)
            optional = set(doe.STATIC_MODEL_AXES) - {*doe.ACTIVE_GEOMETRY, "stack_length_mm", "phase_resistance_ohm"}
            for i, row in enumerate(changed):
                row.update({name: values[i // controls][name] for name in optional})
            if change == "derived": changed[index]["stator_teeth_width"] += 1
        return changed

    def test_explicit_static_inputs_write_guard_for_all_control_rows(self):
        for controls in (3, 4):
            rows, proof = self.plan(controls, partition_policy=doe.TRAIN_AXIS_ANCHOR_POLICY)
            valid = self.corrupt_static_rows(rows, proof, controls, "valid_optional_derived")
            path = self.fixture.root / ("valid_derived_" + str(controls) + ".csv")
            doe.write_training_plan(path, valid, proof)
            doe.verify_training_plan(path)  # Exact explicit derived inputs remain supported.
            for change in ("resistance", "missing_resistance", "derived"):
                with self.subTest(controls=controls, change=change):
                    bad = self.corrupt_static_rows(rows, proof, controls, change)
                    with self.assertRaisesRegex(ValueError, "static input"):
                        doe.write_training_plan(self.fixture.root / (change + str(controls) + ".csv"), bad, proof)

    def test_restored_reader_rejects_bad_file_after_only_anchor_write_guard_bypassed(self):
        for controls in (3, 4):
            rows, proof = self.plan(controls, partition_policy=doe.TRAIN_AXIS_ANCHOR_POLICY)
            for change in ("resistance", "missing_resistance", "derived"):
                with self.subTest(controls=controls, change=change):
                    bad = self.corrupt_static_rows(rows, proof, controls, change)
                    path = self.fixture.root / ("bypassed_" + change + str(controls) + ".csv")
                    real_guard = doe._require_anchor_partition
                    # The real writer, hashes, manifest and all other validation remain active.
                    with mock.patch.object(doe, "_require_anchor_partition", return_value=None):
                        doe.write_training_plan(path, bad, proof)
                    self.assertIs(doe._require_anchor_partition, real_guard)
                    self.assertTrue(path.is_file())
                    with self.assertRaisesRegex(ValueError, "static input"):
                        doe.verify_training_plan(path)

    def test_public_cli_dispatch_and_equals_option_with_space_paths(self):
        folder = self.fixture.root / "input only synthetic fixture"; folder.mkdir()
        spec = folder / "spec.json"; spec.write_text(json.dumps(self.fixture.raw))
        calibration = folder / "zero.json"; calibration.write_text(json.dumps(self.fixture.calibration))
        output = folder / "new synthetic plan.csv"
        self.assertEqual(cli.main(["doe", "--spec", str(spec), "--calibration-manifest", str(calibration),
                                   "--profile-manifest", str(self.fixture.profile_path), "--output", str(output),
                                   "--geometries", "120", "--controls-per-geometry", "4", "--seed", "123",
                                   "--partition-policy=" + doe.TRAIN_AXIS_ANCHOR_POLICY]), 0)
        proof = doe.verify_training_plan(output)
        self.assertEqual(proof["partition_policy"]["policy"], doe.TRAIN_AXIS_ANCHOR_POLICY)
        self.assertEqual(proof["group_counts"], {"train": 72, "calibration": 24, "test": 24})
        self.assertFalse(proof["submission_performed"])


if __name__ == "__main__":
    unittest.main()
