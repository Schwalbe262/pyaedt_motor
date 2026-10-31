"""Optional paired controls are input-only planning, never a quality certificate."""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import csv
import io
import json
from pathlib import Path
import unittest

from pyaedt_motor import doe
from tests import test_ipmsm_v3_doe as legacy


class PairedControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.V3DoeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def plan(self, geometries=10, **kwargs):
        return doe.generate_training_plan(self.fixture.spec, self.fixture.calibration, self.fixture.profile,
                                          geometries=geometries, seed=123, controls_per_geometry=4, **kwargs)

    def rewrite_signed(self, path, rows, proof):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
        payload = stream.getvalue().encode("utf-8")
        proof = copy.deepcopy(proof)
        proof.pop("manifest_id", None)
        proof["plan_sha256"] = doe._sha(payload)
        proof["manifest_id"] = "training_plan_v3:sha256:" + doe._sha(doe._json_bytes(proof))
        path.write_bytes(payload)
        path.with_suffix(".manifest.json").write_bytes(doe._json_bytes(proof))

    def test_four_rows_are_two_distinct_identical_controls_at_both_speeds(self):
        rows, manifest = self.plan()
        groups = defaultdict(list)
        for row in rows:
            groups[row["design_hash"]].append(row)
            self.assertEqual(row["beta_deg"], row["beta_dq_deg"])
            self.assertEqual(row["model_extent"], "full_360")
            self.assertEqual(row["symmetry_factor"], 1)
            self.assertNotIn("setup_fingerprint", row)
            self.assertNotIn("producer_fingerprint", row)
        for group in groups.values():
            self.assertEqual([row["base_rpm"] for row in group], [1200, 5000, 1200, 5000])
            first = {(row["i_peak_a"], row["beta_dq_deg"]) for row in group if row["base_rpm"] == 1200}
            second = {(row["i_peak_a"], row["beta_dq_deg"]) for row in group if row["base_rpm"] == 5000}
            self.assertEqual(len(first), 2)
            self.assertEqual(first, second)
            self.assertEqual(len({row["doe_split"] for row in group}), 1)
        self.assertEqual(manifest["controls_per_geometry"], 4)
        self.assertEqual(manifest["control_protocol"], doe.PAIRED_CONTROL_PROTOCOL)
        self.assertFalse(manifest["profile_convergence_certified_by_generator"])
        self.assertFalse(manifest["submission_performed"])

    def test_training_corners_are_four_unique_settings_in_two_geometry_groups(self):
        rows, manifest = self.plan()
        corners = {(current, beta) for current in manifest["current_domain_a"] for beta in manifest["beta_domain_deg"]}
        self.assertEqual(len(corners), 4)
        corner_groups = set()
        for speed in (1200, 5000):
            training = [row for row in rows if row["doe_split"] == "train" and row["base_rpm"] == speed]
            occurrences = Counter((row["i_peak_a"], row["beta_dq_deg"]) for row in training)
            self.assertTrue(corners <= occurrences.keys())
            self.assertTrue(all(occurrences[corner] == 1 for corner in corners))
            corner_groups.update(row["design_hash"] for row in training
                                 if (row["i_peak_a"], row["beta_dq_deg"]) in corners)
        self.assertEqual(len(corner_groups), 2)

    def test_determinism_partition_and_explicit_default_three_are_unchanged(self):
        rows, manifest = self.plan()
        repeated, repeated_manifest = self.plan()
        self.assertEqual(rows, repeated)
        self.assertEqual(manifest, repeated_manifest)
        default_rows, default_manifest = self.fixture.plan()
        explicit_rows, explicit_manifest = self.fixture.plan(controls_per_geometry=3)
        self.assertEqual(default_rows, explicit_rows)
        self.assertEqual(default_manifest, explicit_manifest)
        self.assertNotIn("controls_per_geometry", default_manifest)
        self.assertNotIn("control_protocol", default_manifest)
        self.assertNotIn("support_diagnostics", default_manifest)
        self.assertEqual(manifest["group_partitions"], default_manifest["group_partitions"])
        self.assertEqual([row["design_hash"] for row in rows[::4]],
                         [row["design_hash"] for row in default_rows[::3]])

    def test_full_cohort_540_times_four_preserves_budget_reserve_and_diagnostic_outside_rows(self):
        rows, manifest = self.plan(540)
        self.assertEqual(len(rows), 2160)
        self.assertEqual(manifest["group_counts"], {"train": 324, "calibration": 108, "test": 108})
        self.assertEqual(manifest["speed_rows"], {"1200": 1080, "5000": 1080})
        self.assertEqual(manifest["retry_attempt_reserve"], 240)
        self.assertEqual(manifest["unallocated_attempts"], 0)
        self.assertEqual(manifest["planned_wave_sizes"], [300] * 7 + [60])
        diagnostics = manifest["support_diagnostics"]
        self.assertEqual(len(diagnostics["raw_feature_names"]), 17)
        self.assertEqual(diagnostics["feasible_geometry_groups"], 540)
        self.assertLessEqual(diagnostics["maximum_slot_fill_ratio"], self.fixture.spec.winding.fill_factor)
        self.assertFalse(diagnostics["coverage_certified"])
        self.assertFalse(diagnostics["geometry_partition_changed"])
        self.assertFalse(diagnostics["test_targets_used"])
        self.assertEqual(diagnostics["rows_filtered"], 0)
        self.assertIn("derived model features not evaluated", diagnostics["scope"])
        names = diagnostics["raw_feature_names"]
        train = [row for row in rows if row["doe_split"] == "train"]
        bounds = {name: [min(row[name] for row in train), max(row[name] for row in train)] for name in names}
        self.assertEqual(diagnostics["training_bounds"], bounds)
        for label in ("train", "calibration", "test"):
            selected = [row for row in rows if row["doe_split"] == label]
            outside = [row for row in selected if any(not bounds[name][0] <= row[name] <= bounds[name][1] for name in names)]
            self.assertEqual(diagnostics["outside_training_box"][label],
                             {"rows": len(outside), "groups": len({row["design_hash"] for row in outside}),
                              "total_rows": len(selected)})
        self.assertGreater(diagnostics["outside_training_box"]["calibration"]["rows"], 0)
        self.assertGreater(diagnostics["outside_training_box"]["test"]["rows"], 0)
        path = self.fixture.root / "full_cohort_synthetic_unit.csv"
        proof = doe.write_training_plan(path, rows, manifest)
        self.assertEqual(doe.verify_training_plan(path), proof)
        with self.assertRaisesRegex(ValueError, "2400"):
            self.plan(541)

    def test_invalid_control_count_and_over_budget_never_sample_geometries(self):
        from unittest import mock
        for count in (True, False, 4.0, 0, 2, 6, "4", None):
            with self.subTest(count=count), mock.patch.object(doe, "sample_feasible_geometries") as sample:
                with self.assertRaisesRegex(ValueError, "controls_per_geometry"):
                    doe.generate_training_plan(self.fixture.spec, self.fixture.calibration, self.fixture.profile,
                                               geometries=10, controls_per_geometry=count)
                sample.assert_not_called()
        with mock.patch.object(doe, "sample_feasible_geometries") as sample:
            with self.assertRaisesRegex(ValueError, "2400"):
                self.plan(540, retry_reserve=241)
            sample.assert_not_called()

    def conflicting_three_metadata(self):
        # Presence is contradictory even for null/false-valued declarations.
        return [{"control_protocol": doe.PAIRED_CONTROL_PROTOCOL}, {"control_protocol": None},
                {"support_diagnostics": {"coverage_certified": True}},
                {"support_diagnostics": {"coverage_certified": False}}, {"support_diagnostics": None},
                {"controls_per_geometry": 3, "control_protocol": doe.PAIRED_CONTROL_PROTOCOL,
                 "support_diagnostics": {"coverage_certified": True}}]

    def test_legacy_three_writer_rejects_any_paired_metadata_presence_before_files(self):
        rows, manifest = self.fixture.plan()
        for index, update in enumerate(self.conflicting_three_metadata()):
            path = self.fixture.root / f"invalid_three_writer_{index}.csv"
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, "three-control.*paired-only"):
                doe.write_training_plan(path, rows, dict(manifest, **update))
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix(".manifest.json").exists())

    def test_rehashed_three_row_plan_cannot_claim_paired_protocol_or_support_certificate(self):
        rows, manifest = self.fixture.plan()
        path = self.fixture.root / "rehashed_three.csv"
        proof = doe.write_training_plan(path, rows, manifest)
        self.assertEqual(doe.verify_training_plan(path), proof)
        self.assertEqual(len(rows), 30)  # Three rows/group, unlike the old four-row count test.
        for update in self.conflicting_three_metadata():
            self.rewrite_signed(path, rows, dict(proof, **update))
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, "three-control.*paired-only"):
                doe.verify_training_plan(path)

    def test_round_trip_rejects_missing_or_tampered_protocol_and_false_certification(self):
        rows, manifest = self.plan()
        path = self.fixture.root / "paired.csv"
        proof = doe.write_training_plan(path, rows, manifest)
        self.assertEqual(doe.verify_training_plan(path), proof)
        variants = [dict(controls_per_geometry=3), dict(controls_per_geometry=5), dict(controls_per_geometry=True),
                    dict(control_protocol="unpaired"), dict(control_protocol=None),
                    dict(profile_convergence_certified_by_generator=True), dict(numpy_version="unrecorded"),
                    dict(scipy_version="unrecorded"), dict(unallocated_attempts=999), dict(retry_attempt_reserve=2400),
                    dict(speed_rows={"1200": 40}), dict(concurrency_cap=301), dict(submission_performed=True),
                    dict(bounded_refill_required=True)]
        for update in variants:
            with self.subTest(update=update):
                self.rewrite_signed(path, rows, dict(proof, **update))
                with self.assertRaises(ValueError):
                    doe.verify_training_plan(path)
        diagnostics = copy.deepcopy(proof["support_diagnostics"])
        diagnostics["coverage_certified"] = True
        self.rewrite_signed(path, rows, dict(proof, support_diagnostics=diagnostics))
        with self.assertRaisesRegex(ValueError, "support diagnostics"):
            doe.verify_training_plan(path)

    def test_resigned_unpaired_duplicated_missing_and_alias_control_rows_are_rejected(self):
        rows, manifest = self.plan()
        path = self.fixture.root / "controls.csv"
        proof = doe.write_training_plan(path, rows, manifest)
        # Modify a nontraining group so the per-group rule, rather than corner
        # loss, rejects independently rehashed CSVs and manifests.
        index = next(index for index, row in enumerate(rows) if row["doe_split"] != "train")
        variants = []
        unpaired = copy.deepcopy(rows); unpaired[index + 1]["i_peak_a"] *= 1.001; variants.append(unpaired)
        duplicate = copy.deepcopy(rows)
        for offset in (2, 3):
            for key in ("i_peak_a", "beta_deg", "beta_dq_deg"):
                duplicate[index + offset][key] = duplicate[index][key]
        variants.append(duplicate)
        wrong_speed = copy.deepcopy(rows); wrong_speed[index + 1]["base_rpm"] = 1200; variants.append(wrong_speed)
        alias = copy.deepcopy(rows); alias[index]["beta_deg"] += .1; variants.append(alias)
        invalid = copy.deepcopy(rows); invalid[index]["i_peak_a"] = "nan"; variants.append(invalid)
        variants.append(rows[:index] + rows[index + 1:])
        for changed in variants:
            with self.subTest(variant=variants.index(changed)):
                modified = dict(proof, cases=len(changed), nominal_attempts=len(changed))
                self.rewrite_signed(path, changed, modified)
                with self.assertRaises(ValueError):
                    doe.verify_training_plan(path)

    def test_content_and_source_hashes_remain_required_for_paired_plans(self):
        rows, manifest = self.plan()
        path = self.fixture.root / "hashes.csv"
        proof = doe.write_training_plan(path, rows, manifest)
        path.write_bytes(path.read_bytes().replace(b"v3train_", b"foreign_", 1))
        with self.assertRaisesRegex(ValueError, "SHA256"):
            doe.verify_training_plan(path)
        bad = copy.deepcopy(proof)
        bad["source_sha256"]["ipmsm_optimization.py"] = "a" * 64
        self.rewrite_signed(path, rows, bad)
        with self.assertRaisesRegex(ValueError, "source SHA256"):
            doe.verify_training_plan(path)

    def test_cli_explicit_option_produces_only_a_synthetic_unit_plan(self):
        spec = self.fixture.root / "spec.json"
        calibration = self.fixture.root / "zero.json"
        spec.write_text(json.dumps(self.fixture.raw), encoding="utf-8")
        calibration.write_text(json.dumps(self.fixture.calibration), encoding="utf-8")
        output = self.fixture.root / "unit_cli.csv"
        self.assertEqual(doe.main(["--spec", str(spec), "--calibration-manifest", str(calibration),
                                  "--profile-manifest", str(self.fixture.profile_path), "--output", str(output),
                                  "--geometries", "10", "--seed", "123", "--controls-per-geometry", "4"]), 0)
        proof = doe.verify_training_plan(output)
        self.assertEqual(proof["controls_per_geometry"], 4)
        self.assertEqual(proof["cases"], 40)
        self.assertFalse(proof["submission_performed"])


if __name__ == "__main__":
    unittest.main()
