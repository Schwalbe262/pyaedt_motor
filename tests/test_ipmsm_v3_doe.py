"""Physics inputs, held-out geometry boundaries and immutable planning evidence."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from calibrate_ipmsm_beta import apply_zero_manifest_to_spec
from ipmsm_optimization import geometry_metrics, optimization_spec_from_mapping, phase_resistance_100c_ohm
from pyaedt_motor import doe
from tests.test_calibrate_ipmsm_beta import zero_manifest


def physical_zero():
    value = zero_manifest(-90.0)
    value.update(metric_contract="motor_metrics_v3", back_emf_source="periodic_flux_derivative",
                 producer_fingerprint="producer_v3:sha256:" + "a" * 64)
    value.pop("calibration_id")
    value["calibration_id"] = "beta-calibration:sha256:" + hashlib.sha256(doe._json_bytes(value)).hexdigest()
    return value


class V3DoeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.calibration = physical_zero()
        self.raw = json.loads((Path(__file__).parents[1] / "configs/ipmsm_7p5kw.json").read_text(encoding="utf-8"))
        self.spec = optimization_spec_from_mapping(apply_zero_manifest_to_spec(self.raw, self.calibration))
        evidence = self.root / "comparison.json"
        evidence.write_text('{"synthetic_test_evidence":true}\n', encoding="utf-8")
        self.profile_document = {
            "schema_version": doe.PROFILE_SCHEMA, "metric_contract_version": "motor_metrics_v3",
            "producer_fingerprint": "producer_v3:sha256:" + "b" * 64,
            "settings": {"quality_profile": "fixture_selected_4x150", "transient_periods": 4,
                         "steps_per_period": 150, "mesh_elements": {"magnet": 100, "rotor": 1000,
                         "stator": 1000, "winding": 100, "band": 2000},
                         "mesh_maximum_lengths_mm": {"band": .2}},
            "evidence": [{"path": evidence.name, "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}],
        }
        self.profile_path = self.root / "selected.json"
        self.profile_path.write_text(json.dumps(self.profile_document), encoding="utf-8")
        self.profile = doe.load_selected_profile(self.profile_path)

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self, **kwargs):
        return doe.generate_training_plan(self.spec, self.calibration, self.profile, geometries=10, seed=123, **kwargs)

    def isolated_sources(self):
        root = self.root / "runtime"
        package = root / "src" / "pyaedt_motor"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        shutil.copyfile(doe.__file__, package / "doe.py")
        shutil.copyfile(doe._geometry_helper.__file__, root / "ipmsm_optimization.py")
        return root

    def run_isolated(self, root, script):
        entry = root / "check.py"
        entry.write_text(script, encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(entry)], cwd=root, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-3000:])
        return result.stdout

    def test_sampling_prefix_is_repeatable_and_can_be_used_before_zero_calibration(self):
        first = doe.sample_feasible_geometries(self.spec, 1, 71)
        longer = doe.sample_feasible_geometries(self.spec, 10, 71)
        self.assertEqual(first, longer[:1])
        self.assertEqual(first, doe.sample_feasible_geometries(self.spec, 1, 71))
        generic = replace(self.spec, geometry_design_space=tuple(
            replace(bound, lower=170, upper=180) if bound.name == "stator_outer_radius" else bound
            for bound in self.spec.geometry_design_space))
        self.assertGreater(doe.sample_feasible_geometries(generic, 1, 71)[0][0]["stator_outer_radius"], 150)
        with self.assertRaisesRegex(ValueError, "diameter"):
            doe.generate_training_plan(generic, self.calibration, self.profile, geometries=10)

    def test_geometry_identity_ignores_inactive_parameter_controls_and_old_ids(self):
        geometry, depth, key = doe.sample_feasible_geometries(self.spec, 1, 71)[0]
        row = {"input_" + name: value for name, value in geometry.items()}
        row.update(input_stack_length_mm=depth, input_i_peak_a=137, input_beta_dq_deg=45,
                   design_hash="legacy-id", geometry_group_id="foreign-id")
        row["input_stator_shoe_thick"] = 999
        self.assertEqual(doe.physical_geometry_hash(row), key)
        row["input_rotator_gap"] += .001
        self.assertNotEqual(doe.physical_geometry_hash(row), key)

    def test_prior_csv_exclusions_compare_physical_shape_instead_of_legacy_ids(self):
        previous = doe.sample_feasible_geometries(self.spec, 5, 123)
        path = self.root / "old_results.csv"
        rows = [{**{"input_" + name: value for name, value in design.items()}, "input_stack_length_mm": depth,
                 "design_hash": "not-a-v3-hash"} for design, depth, _ in previous]
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        hashes, sources = doe.exclusion_hashes([path])
        self.assertEqual(hashes, {key for _, _, key in previous})
        new, manifest = self.plan(excluded_hashes=hashes)
        self.assertFalse(hashes & {row["design_hash"] for row in new})
        self.assertEqual(manifest["excluded_geometry_count"], 5)
        self.assertEqual(sources[0]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_group_split_control_coverage_constraints_and_resistance(self):
        rows, manifest = self.plan()
        repeated, _ = self.plan()
        self.assertEqual(rows, repeated)
        self.assertEqual(len(rows), 30)
        self.assertEqual(manifest["group_counts"], {"train": 6, "calibration": 2, "test": 2})
        self.assertEqual(Counter(row["base_rpm"] for row in rows), {1200: 15, 5000: 15})
        groups = defaultdict(list)
        for row in rows:
            groups[row["design_hash"]].append(row)
            self.assertEqual(row["stator_shoe_thick"], 1.5)
            self.assertEqual(row["dataset_schema_version"], "ipmsm_v2")
            self.assertNotIn("setup_fingerprint", row)
            self.assertNotIn("producer_fingerprint", row)
            self.assertLessEqual(row["stator_outer_radius"], 150)
            self.assertLessEqual(row["i_peak_a"], 137.8)
            self.assertAlmostEqual(row["phase_resistance_ohm"], phase_resistance_100c_ohm(
                row, row["stack_length_mm"], self.spec.winding))
            self.assertLessEqual(geometry_metrics(row, row["stack_length_mm"], self.spec.winding).slot_fill_ratio, .45)
        for values in groups.values():
            self.assertEqual(len(values), 3)
            self.assertEqual(len({row["doe_split"] for row in values}), 1)
            self.assertEqual({row["base_rpm"] for row in values}, {1200, 5000})
        for rpm in (1200, 5000):
            training = [row for row in rows if row["doe_split"] == "train" and row["base_rpm"] == rpm]
            self.assertEqual(min(row["beta_dq_deg"] for row in training), 0)
            self.assertEqual(max(row["beta_dq_deg"] for row in training), 80)
            self.assertAlmostEqual(min(row["i_peak_a"] for row in training), self.spec.effective_peak_current_limit_a * .25)
            self.assertAlmostEqual(max(row["i_peak_a"] for row in training), self.spec.effective_peak_current_limit_a)

    def test_default_budget_has720_groups2160_cases_and240_retry_reserve(self):
        rows, proof = doe.generate_training_plan(self.spec, self.calibration, self.profile)
        self.assertEqual(len(rows), 2160)
        self.assertEqual(proof["group_counts"], {"train": 432, "calibration": 144, "test": 144})
        self.assertEqual(proof["retry_attempt_reserve"], 240)
        self.assertEqual(proof["unallocated_attempts"], 0)
        self.assertEqual(proof["planned_wave_sizes"], [300] * 7 + [60])
        self.assertTrue(proof["bounded_refill_required"])
        self.assertFalse(proof["submission_performed"])
        self.assertFalse(proof["profile_convergence_certified_by_generator"])
        with self.assertRaisesRegex(ValueError, "2400"):
            doe.generate_training_plan(self.spec, self.calibration, self.profile, geometries=721)

    def test_invalid_profile_flags_or_modified_evidence_cannot_be_sealed(self):
        for key, invalid in [("circle_segments", 72), ("strict_setup", False), ("compute_inductance", False),
                             ("require_steady_state", False), ("analysis_purpose", "screening"),
                             ("loss_policy", "legacy_implicit"), ("transient_periods", True)]:
            settings = dict(self.profile_document["settings"], **{key: invalid})
            with self.subTest(key=key), self.assertRaises(ValueError): doe.profile_settings(settings)
        (self.root / "comparison.json").write_text("changed evidence", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"): doe.load_selected_profile(self.profile_path)
        with self.assertRaisesRegex(ValueError, "changed after selection"): self.plan()

    def altered_profile(self, **settings):
        document = dict(self.profile_document, settings=dict(self.profile_document["settings"], **settings))
        self.profile_path.write_text(json.dumps(document), encoding="utf-8")
        return doe.load_selected_profile(self.profile_path)

    def test_profile_option_ignored_by_current_runner_is_rejected(self):
        profile = self.altered_profile(winding_material="aluminum")
        with self.assertRaisesRegex(ValueError, "runner does not apply"):
            doe.generate_training_plan(self.spec, self.calibration, profile, geometries=10)

    def test_supported_band_option_survives_runner_round_trip(self):
        profile = self.altered_profile(band_circle_segments=0)
        rows, manifest = doe.generate_training_plan(self.spec, self.calibration, profile, geometries=10)
        self.assertTrue(all(row["band_circle_segments"] == 0 for row in rows))
        self.assertEqual(manifest["profile_settings"]["band_circle_segments"], 0)

    def test_loaded_profile_cannot_mutate_while_retaining_old_provenance(self):
        self.profile.settings["steps_per_period"] = 300
        with self.assertRaisesRegex(ValueError, "changed in memory"):
            self.plan()

    def test_small_partition_keeps_speed_balance_and_training_current_anchors(self):
        for seed in range(20):
            rows, _ = doe.generate_training_plan(self.spec, self.calibration, self.profile, geometries=8, seed=seed)
            with self.subTest(seed=seed):
                self.assertEqual(Counter(row["base_rpm"] for row in rows), {1200: 12, 5000: 12})
                rated = [row for row in rows if row["base_rpm"] == 1200 and row["doe_split"] == "train"]
                self.assertAlmostEqual(min(row["i_peak_a"] for row in rated), self.spec.effective_peak_current_limit_a * .25)

    def test_legacy_or_tampered_zero_and_invalid_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "flux-derived"):
            doe.generate_training_plan(self.spec, zero_manifest(), self.profile, geometries=10)
        altered = dict(self.calibration, electrical_zero_deg=0)
        with self.assertRaisesRegex(ValueError, "calibration_id"):
            doe.generate_training_plan(self.spec, altered, self.profile, geometries=10)
        for value in [0, 4, True, 5.1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                doe.generate_training_plan(self.spec, self.calibration, self.profile, geometries=value)
        for fraction in [0, 1, float("nan")]:
            with self.subTest(fraction=fraction), self.assertRaises(ValueError): self.plan(current_min_fraction=fraction)

    def test_no_unconfirmed_geometry_exclusions_and_impossible_fill_fails_closed(self):
        impossible = replace(self.spec, winding=replace(self.spec.winding, strand_area_mm2=1000))
        with self.assertRaisesRegex(ValueError, "slot_fill"):
            doe.sample_feasible_geometries(impossible, 2, 123, max_candidates=128)
        prior = self.root / "incomplete.csv"
        prior.write_text("case_id,input_stator_outer_radius\nold,140\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cannot establish prior geometry identity"):
            doe.exclusion_hashes([prior])

    def test_written_plan_is_exclusive_and_verifies_partition_identity(self):
        rows, manifest = self.plan()
        target = self.root / "cases.csv"
        proof = doe.write_training_plan(target, rows, manifest)
        self.assertEqual(doe.verify_training_plan(target), proof)
        self.assertEqual(proof["schema_version"], "ipmsm_v3_training_plan_v2")
        self.assertEqual(proof["generator_sha256"], hashlib.sha256(Path(doe.__file__).read_bytes()).hexdigest())
        helper = Path(doe._geometry_helper.__file__)
        self.assertEqual(proof["geometry_helper_sha256"], hashlib.sha256(helper.read_bytes()).hexdigest())
        self.assertEqual(proof["source_import_origins"]["ipmsm_optimization.py"], str(helper.resolve()))
        original = target.read_bytes()
        with self.assertRaises(FileExistsError): doe.write_training_plan(target, rows, manifest)
        self.assertEqual(target.read_bytes(), original)
        rows[0]["doe_split"] = "test" if rows[0]["doe_split"] != "test" else "train"
        payload = io_csv(rows)
        target.write_bytes(payload)
        proof["plan_sha256"] = hashlib.sha256(payload).hexdigest()
        proof.pop("manifest_id")
        proof["manifest_id"] = "training_plan_v3:sha256:" + hashlib.sha256(doe._json_bytes(proof)).hexdigest()
        target.with_suffix(".manifest.json").write_text(json.dumps(proof), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "leaks across partitions"):
            doe.verify_training_plan(target)

    def test_identical_sources_verify_from_another_native_checkout(self):
        rows, manifest = self.plan()
        target = self.root / "cases.csv"
        doe.write_training_plan(target, rows, manifest)
        root = self.isolated_sources()
        self.run_isolated(root, f"""
from pathlib import Path
from pyaedt_motor import doe
proof = doe.verify_training_plan(Path({str(target)!r}))
assert proof['source_import_origins'] != doe._assert_source_pin()['source_import_origins']
assert proof['source_sha256'] == doe._assert_source_pin()['source_sha256']
""")

    def test_helper_only_change_rejects_an_existing_plan_in_a_fresh_process(self):
        rows, manifest = self.plan()
        target = self.root / "cases.csv"
        doe.write_training_plan(target, rows, manifest)
        root = self.isolated_sources()
        generator = root / "src" / "pyaedt_motor" / "doe.py"
        self.assertEqual(generator.read_bytes(), Path(doe.__file__).read_bytes())
        helper = root / "ipmsm_optimization.py"
        helper.write_bytes(helper.read_bytes() + b"\n# changed geometry-helper revision\n")
        self.run_isolated(root, f"""
from pathlib import Path
from pyaedt_motor import doe
try:
    doe.verify_training_plan(Path({str(target)!r}))
except ValueError as exc:
    assert 'generator/helper source SHA256' in str(exc), str(exc)
else:
    raise AssertionError('old plan accepted a different helper')
""")

    def test_source_drift_in_a_running_process_blocks_all_planning_entrypoints(self):
        root = self.isolated_sources()
        self.run_isolated(root, """
from pathlib import Path
from pyaedt_motor import doe
helper = Path('ipmsm_optimization.py')
helper.write_bytes(helper.read_bytes() + b'\\n# edited after import\\n')
for operation in [lambda: doe.sample_feasible_geometries(None,1,1),
                  lambda: doe.generate_training_plan(None,None,None),
                  lambda: doe.write_training_plan(Path('blocked.csv'),[],{}),
                  lambda: doe.verify_training_plan(Path('absent.csv'))]:
    try:
        operation()
    except RuntimeError as exc:
        assert 'changed after import' in str(exc), str(exc)
    else:
        raise AssertionError('stale process accepted planning work')
assert not Path('blocked.csv').exists()
""")

    def test_helper_cached_before_doe_import_must_match_imported_source_pin(self):
        root = self.isolated_sources()
        self.run_isolated(root, """
from pathlib import Path
import ipmsm_optimization
helper = Path('ipmsm_optimization.py')
helper.write_bytes(helper.read_bytes() + b'\\ndef geometry_metrics(*args, **kwargs):\\n    raise ValueError("new helper")\\n')
try:
    from pyaedt_motor import doe
except RuntimeError as exc:
    assert 'geometry helper source changed after import' in str(exc), str(exc)
else:
    raise AssertionError('cached old helper was bound to new source bytes')
""")

    def test_cached_helper_constant_or_dataclass_default_changes_are_rejected(self):
        root = self.isolated_sources()
        self.run_isolated(root, """
from pathlib import Path
import sys
helper = Path('ipmsm_optimization.py')
original = helper.read_bytes()
changes = [(b'BETA_CONVENTION = "dq_current_advance_v2"', b'BETA_CONVENTION = "changed_convention"'),
           (b'copper_temp_coefficient_per_c: float = 0.00393', b'copper_temp_coefficient_per_c: float = 0.004')]
for old,new in changes:
    assert old in original
    helper.write_bytes(original)
    sys.modules.pop('ipmsm_optimization',None)
    import ipmsm_optimization
    helper.write_bytes(original.replace(old,new))
    try:
        from pyaedt_motor import doe
    except RuntimeError as exc:
        assert 'geometry helper source changed after import' in str(exc), str(exc)
    else:
        raise AssertionError('cached constant/default accepted a new source hash')
""")

    def test_same_helper_bytes_imported_from_wrong_project_are_rejected(self):
        root = self.isolated_sources()
        foreign = root / "foreign"
        foreign.mkdir()
        shutil.copyfile(root / "ipmsm_optimization.py", foreign / "ipmsm_optimization.py")
        self.run_isolated(root, """
import sys
from pathlib import Path
sys.path.insert(0,str(Path('foreign').resolve()))
import ipmsm_optimization
sys.path.pop(0)
try:
    from pyaedt_motor import doe
except RuntimeError as exc:
    assert 'import origin differs from project source' in str(exc), str(exc)
else:
    raise AssertionError('foreign helper import was accepted')
""")

    def test_source_change_during_sampling_cannot_return_rows(self):
        root = self.isolated_sources()
        (root / "spec.json").write_text(json.dumps(apply_zero_manifest_to_spec(self.raw, self.calibration)), encoding="utf-8")
        self.run_isolated(root, """
import json
from pathlib import Path
from pyaedt_motor import doe
spec = doe.optimization_spec_from_mapping(json.loads(Path('spec.json').read_text()))
original = doe.qmc.LatinHypercube.random
def change_source(self,*args,**kwargs):
    result = original(self,*args,**kwargs)
    helper = Path('ipmsm_optimization.py')
    helper.write_bytes(helper.read_bytes() + b'\\n# source changed while sampling\\n')
    return result
doe.qmc.LatinHypercube.random = change_source
try:
    doe.sample_feasible_geometries(spec,2,71)
except RuntimeError as exc:
    assert 'changed after import' in str(exc), str(exc)
else:
    raise AssertionError('rows escaped after source changed')
""")

    def test_incomplete_or_old_source_manifest_cannot_be_published(self):
        rows, manifest = self.plan()
        for index, missing in enumerate(["geometry_helper_sha256", "source_sha256", "source_import_origins"]):
            altered = dict(manifest)
            altered.pop(missing)
            target = self.root / f"missing{index}.csv"
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                doe.write_training_plan(target, rows, altered)
            self.assertFalse(target.exists())
        old = dict(manifest, schema_version="ipmsm_v3_training_plan_v1")
        with self.assertRaisesRegex(ValueError, "source SHA256/schema"):
            doe.write_training_plan(self.root / "old.csv", rows, old)

    def test_cli_records_input_provenance_without_claiming_convergence_or_submission(self):
        config = self.root / "spec.json"
        config.write_text(json.dumps(self.raw), encoding="utf-8")
        calibration = self.root / "zero.json"
        calibration.write_text(json.dumps(self.calibration), encoding="utf-8")
        target = self.root / "cli.csv"
        result = doe.main(["--spec", str(config), "--calibration-manifest", str(calibration),
                           "--profile-manifest", str(self.profile_path), "--output", str(target),
                           "--geometries", "10", "--seed", "123"])
        self.assertEqual(result, 0)
        proof = doe.verify_training_plan(target)
        self.assertEqual(proof["spec_file_sha256"], hashlib.sha256(config.read_bytes()).hexdigest())
        self.assertEqual(proof["expected_producer_fingerprint"], self.profile.producer_fingerprint)
        self.assertFalse(proof["submission_performed"])


def io_csv(rows):
    import io
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


if __name__ == "__main__":
    unittest.main()
