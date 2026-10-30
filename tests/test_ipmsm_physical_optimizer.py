"""Analytic salient motor exercises control search without an FEA solver."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from ipmsm_optimization import (GEOMETRY_VARIABLE_NAMES, geometry_metrics, mtpa_seed,
                                optimization_spec_from_mapping, phase_resistance_100c_ohm)
from pyaedt_motor import optimize_physical as optimization
from pyaedt_motor.model_bundle import ALL_TARGETS, canonical_json, derived_geometry_values


ROOT = Path(__file__).resolve().parents[1]


def source_spec():
    return json.loads((ROOT / "configs/ipmsm_7p5kw.json").read_text(encoding="utf-8"))


def zero_manifest():
    from calibrate_ipmsm_beta import ZERO_CALIBRATION_METHOD
    value = {"method": ZERO_CALIBRATION_METHOD, "convention": "dq_current_advance_v2",
             "metric_contract": "motor_metrics_v3", "back_emf_source": "periodic_flux_derivative",
             "electrical_zero_deg": -90.9, "initial_position_deg": -22.5,
             "producer_fingerprint": "producer_v3:sha256:" + "a" * 64}
    raw = source_spec()
    value["material_context"] = raw["material_context"]
    winding = raw["winding"]
    value["winding_context"] = {
        "series_turns_per_phase": winding["series_turns_per_phase"],
        "turns_per_coil_side": winding["turns_per_coil_side"],
        "conductor_area_per_branch_mm2": winding["strand_area_mm2"] * winding["strands_per_turn"],
        "slot_fill_limit": winding["fill_factor"],
    }
    value["material_fingerprint"] = "materials_v2:sha256:" + "b" * 64
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    value["calibration_id"] = "beta-calibration:sha256:" + hashlib.sha256(payload).hexdigest()
    return value


def fingerprints():
    return {"input_dataset_schema_version": "ipmsm_v2", "input_metric_contract_version": "motor_metrics_v3",
            "input_producer_fingerprint": "producer_v3:sha256:" + "a" * 64,
            "input_material_fingerprint": "materials_v2:sha256:" + "b" * 64, "input_setup_fingerprint": "setup_v2:sha256:fixture",
            "input_beta_calibration_id": zero_manifest()["calibration_id"], "input_beta_convention": "dq_current_advance_v2",
            "input_model_extent": "full_360", "input_quality_profile": "analytic", "input_aedt_version": "2025.2"}


def parsed_spec(raw=None):
    from calibrate_ipmsm_beta import apply_zero_manifest_to_spec
    return optimization_spec_from_mapping(apply_zero_manifest_to_spec(raw or source_spec(), zero_manifest()))


def baseline():
    return {**{name: (lower + upper) / 2 for name, (lower, upper) in source_spec()["design_space"].items()},
            "stator_shoe_thick": 1.5, "stack_length_mm": 55.}


def domain(raw):
    spec = parsed_spec(raw)
    geometry = baseline()
    derived = derived_geometry_values(geometry)
    bounds = {"input_" + bound.name: [bound.lower, bound.upper] for bound in spec.design_space}
    bounds.update(input_i_peak_a=[.25 * spec.effective_peak_current_limit_a, spec.effective_peak_current_limit_a],
                  input_beta_dq_deg=list(spec.beta_bounds_deg))
    constants = {"input_slot_num": 12., "input_pole_num": 8., "input_stator_shoe_thick": 1.5}
    columns = list(dict.fromkeys([*bounds, *constants, *("input_" + name for name in derived),
                                 "input_base_rpm", "input_phase_resistance_ohm", "input_steps_per_period"]))
    observed = {column: list(bounds.get(column, [0., 1e6])) for column in columns}
    observed.update({column: [value, value] for column, value in constants.items()})
    observed.update(input_base_rpm=[1200., 5000.], input_steps_per_period=[150., 150.],
                    input_phase_resistance_ohm=[.001, 1.])
    return {"input_columns": columns,
            "declaration": {"bounds": bounds, "constants": constants, "winding": asdict(spec.winding),
                            "speeds_rpm": [1200., 5000.]},
            "winding_case_constants": {"input_series_turns_per_phase": 48, "input_turns_per_coil_side": 12,
                                       "input_winding_material": "copper"},
            "observed_bounds": observed}


class AnalyticBundle:
    """Exact dq salient motor; synthetic test double is never a production artifact."""

    def __init__(self, *, torque_width=0., voltage_width=0., approved=True):
        self.approved = approved
        raw = source_spec()
        self.metadata = {"fingerprints": fingerprints(), "spec_sha256": hashlib.sha256(canonical_json(raw)).hexdigest(),
                         "domain": domain(raw), "dataset_sha256": "analytic-fixture", "prediction_sources": {}}
        self.torque_width, self.voltage_width = torque_width, voltage_width
        self.batches = []

    def predict(self, inputs, *, fingerprints, with_bounds):
        self.batches.append(inputs.copy())
        assert with_bounds and fingerprints == self.metadata["fingerprints"]
        current = inputs.input_i_peak_a.to_numpy(float)
        beta = np.deg2rad(inputs.input_beta_dq_deg.to_numpy(float))
        id_a, iq_a = -current * np.sin(beta), current * np.cos(beta)
        length_scale = inputs.input_stack_length_mm.to_numpy(float) / 55.
        psi, ld, lq = .06 * length_scale, .0002 * length_scale, .0008 * length_scale
        torque = 6 * (psi + (ld - lq) * id_a) * iq_a
        omega_m = inputs.input_base_rpm.to_numpy(float) * 2 * np.pi / 60
        omega_e = 4 * omega_m
        resistance = inputs.input_phase_resistance_ohm.to_numpy(float)
        vd = resistance * id_a - omega_e * lq * iq_a
        vq = resistance * iq_a + omega_e * (psi + ld * id_a)
        voltage = np.hypot(vd, vq)
        copper = 1.5 * resistance * current**2
        core = 40 * length_scale * (omega_m / (40 * np.pi))**1.5
        solid = 5 * length_scale
        loss = copper + core + solid
        power = torque * omega_m
        point = pd.DataFrame({"output_torque_last_avg_nm": torque, "output_torque_last_max_nm": 1.1 * torque,
                              "output_coreloss_last_avg_w": core, "output_solidloss_last_avg_w": solid,
                              "output_ld_last_avg_h": ld, "output_lq_last_avg_h": lq,
                              "output_phase_voltage_last_peak_abs_v": voltage,
                              "output_voltage_span_last_peak_v": np.sqrt(3) * voltage,
                              "output_total_loss_last_avg_w": loss, "output_efficiency_last_pct": 100 * power / (power + loss)},
                             index=inputs.index)[list(ALL_TARGETS)]
        lower, upper = point.copy(), point.copy()
        lower["output_coreloss_last_avg_w"] = np.maximum(0, core - 10.)
        upper["output_coreloss_last_avg_w"] = core + 10.
        lower["output_solidloss_last_avg_w"] = np.maximum(0, solid - 2.)
        upper["output_solidloss_last_avg_w"] = solid + 2.
        lower[optimization.LOSS] = copper + lower.output_coreloss_last_avg_w + lower.output_solidloss_last_avg_w
        upper[optimization.LOSS] = copper + upper.output_coreloss_last_avg_w + upper.output_solidloss_last_avg_w
        lower[optimization.TORQUE] -= self.torque_width
        upper[optimization.TORQUE] += self.torque_width
        lower[optimization.VOLTAGE_SPAN] = np.maximum(0, lower[optimization.VOLTAGE_SPAN] - self.voltage_width)
        upper[optimization.VOLTAGE_SPAN] += self.voltage_width
        power_lo = np.maximum(0, lower[optimization.TORQUE]) * omega_m
        power_hi = np.maximum(0, upper[optimization.TORQUE]) * omega_m
        lower[optimization.EFFICIENCY] = 100 * power_lo / (power_lo + upper[optimization.LOSS])
        upper[optimization.EFFICIENCY] = 100 * power_hi / (power_hi + lower[optimization.LOSS])
        return point, lower, upper


def validation_profile(root):
    """Sealed synthetic replay fixture, not a certified research profile."""
    from ipmsm_optimization import fixed_case_context_fields
    from pyaedt_motor import doe
    from run_ipmsm_batch import build_spec, setup_fingerprint
    fields = fixed_case_context_fields(parsed_spec().material_context, parsed_spec().winding)
    settings = {"quality_profile": "analytic", "transient_periods": 6, "steps_per_period": 150,
                "mesh_elements": {group: 1000 for group in doe.MESH_GROUPS},
                "band_circle_segments": 0, "initial_position_deg": -22.5, **doe.CANONICAL_FLAGS}
    flat = {key: value for key, value in settings.items() if key != "mesh_elements"}
    flat.update({f"mesh_{key}_elements": value for key, value in settings["mesh_elements"].items()})
    case = {**baseline(), **fields, **flat, "model_extent": "full_360", "symmetry_factor": 1,
            "beta_convention": "dq_current_advance_v2", "electrical_zero_deg": zero_manifest()["electrical_zero_deg"]}
    expected = fingerprints()
    expected["input_setup_fingerprint"] = setup_fingerprint(build_spec(case), "analytic", "sin_current", False)
    bundle = AnalyticBundle(); bundle.metadata["fingerprints"] = expected
    bound = optimization.PhysicalBundleAdapter(bundle, source_spec(), expected, zero_manifest())
    evidence = root / "synthetic_evidence.txt"; evidence.write_text("synthetic replay settings only", encoding="utf-8")
    document = {"schema_version": doe.PROFILE_SCHEMA, "metric_contract_version": "motor_metrics_v3",
                "producer_fingerprint": expected["input_producer_fingerprint"], "settings": settings,
                "material_context": {key: fields[key] for key in ("core_material_model", "core_material")},
                "material_fingerprint": expected["input_material_fingerprint"],
                "winding_context": {key: fields[key] for key in doe.WINDING_CONTEXT_FIELDS},
                "evidence": [{"path": evidence.name, "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]}
    path = root / "selected_profile.json"; path.write_text(json.dumps(document), encoding="utf-8")
    return bound, path, document


def adapter(bundle=None):
    return optimization.PhysicalBundleAdapter(bundle or AnalyticBundle(), source_spec(), fingerprints(), zero_manifest())


class PhysicalOptimizerTests(unittest.TestCase):
    def test_mtpa_and_field_weakening_are_selected_from_batched_controls(self):
        bound = adapter()
        low, high = [optimization.search_controls(bound, baseline(), point) for point in bound.spec.operating_points]
        self.assertEqual((low.status, high.status), ("feasible", "feasible"))
        expected = mtpa_seed(low.selected["current_peak_a"], .06, .0002, .0008).beta_deg
        self.assertAlmostEqual(low.selected["beta_dq_deg"], expected, delta=2.)
        self.assertGreater(high.selected["beta_dq_deg"], low.selected["beta_dq_deg"] + 15)
        self.assertGreater(high.selected["beta_dq_deg"], 50)
        for result in (low, high):
            self.assertGreaterEqual(result.selected["metrics"][optimization.TORQUE]["lower"], result.operating_point["required_torque_nm"] - 1e-9)
            self.assertLessEqual(result.selected["metrics"][optimization.VOLTAGE_SPAN]["upper"], 190 + 1e-9)
            self.assertLessEqual(result.support["model_batch_calls"], 3)
            density = result.selected["current_peak_a"] / math.sqrt(2) / bound.spec.winding.total_parallel_conductor_area_mm2
            self.assertAlmostEqual(result.selected["current_density_a_per_mm2"], density)
            self.assertLessEqual(density, 6.36 + 1e-10)
        self.assertTrue(all(len(batch) > 1 for batch in bound.bundle.batches))
        self.assertEqual(len(bound.bundle.batches[0]), 33 * 9)

    def test_phase_peak_is_diagnostic_while_span_bounds_drive_svm_gate(self):
        bundle = AnalyticBundle()
        original = bundle.predict
        def common_mode_peak(inputs, **kwargs):
            outputs = original(inputs, **kwargs)
            # A common-mode component can raise phase peaks without changing line voltages.
            for output in outputs:
                output[optimization.PHASE_VOLTAGE] *= 2
            return outputs
        bundle.predict = common_mode_peak
        bound = adapter(bundle)
        high = optimization.search_controls(bound, baseline(), bound.spec.operating_points[1])
        self.assertEqual(high.status, "feasible")
        self.assertGreater(high.selected["metrics"][optimization.PHASE_VOLTAGE]["upper"], bound.spec.phase_peak_voltage_limit_v)
        self.assertLessEqual(high.selected["metrics"][optimization.VOLTAGE_SPAN]["upper"], 190)

    def test_torque_and_voltage_uncertainty_can_reject_mean_feasible_controls(self):
        for kwargs in ({"torque_width": 100.}, {"voltage_width": 1000.}):
            bound = adapter(AnalyticBundle(**kwargs))
            result = optimization.search_controls(bound, baseline(), bound.spec.operating_points[0])
            self.assertEqual(result.status, "no_feasible_supported_control")
            self.assertFalse(result.selected["feasible"])
            self.assertGreater(result.selected["normalized_violation"], 0)

    def test_unsupported_geometry_is_not_reported_as_physical_infeasibility(self):
        bundle = AnalyticBundle()
        bundle.metadata["domain"]["observed_bounds"]["input_stator_outer_radius"] = [130., 140.]
        bound = adapter(bundle)
        result = optimization.evaluate_design(bound, {**baseline(), "stator_outer_radius": 145.})
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(bundle.batches, [])
        self.assertTrue(all(control.support["supported_controls"] == 0 for control in result.controls))
        self.assertIn("do not prove physical infeasibility", result.controls[0].support["interpretation"])

    def test_partial_control_support_is_filtered_before_batch_prediction(self):
        bundle = AnalyticBundle()
        bundle.metadata["domain"]["observed_bounds"]["input_beta_dq_deg"] = [25., 70.]
        bound = adapter(bundle)
        result = optimization.search_controls(bound, baseline(), bound.spec.operating_points[0])
        self.assertGreater(result.support["unsupported_controls"], 0)
        self.assertTrue(all(batch.input_beta_dq_deg.between(25, 70).all() for batch in bundle.batches))

    def test_features_recompute_derived_geometry_resistance_and_current_density(self):
        bound = adapter()
        values = {**baseline(), "stack_length_mm": 61.23456789}
        frame = bound.build_inputs(values, bound.spec.operating_points[0], [(120., 32.)])
        row = frame.iloc[0]
        self.assertEqual(row.input_stack_length_mm, 61.2346)
        for key, expected in derived_geometry_values(values).items():
            self.assertAlmostEqual(row["input_" + key], expected)
        self.assertAlmostEqual(row.input_phase_resistance_ohm, phase_resistance_100c_ohm(values, 61.2346, bound.spec.winding))
        self.assertEqual(row.input_series_turns_per_phase, 48)
        self.assertEqual(row.input_steps_per_period, 150)
        self.assertNotEqual(row.input_phase_resistance_ohm,
                            bound.build_inputs(baseline(), bound.spec.operating_points[0], [(120., 32.)]).iloc[0].input_phase_resistance_ohm)

    def test_rejected_unverified_and_wrong_context_are_refused(self):
        with self.assertRaisesRegex(ValueError, "rejected or unverified"):
            adapter(AnalyticBundle(approved=False))
        bundle = AnalyticBundle()
        wrong_spec = source_spec(); wrong_spec["winding"]["overhang_mm"] += 1
        with self.assertRaisesRegex(ValueError, "training spec"):
            optimization.PhysicalBundleAdapter(bundle, wrong_spec, fingerprints(), zero_manifest())
        expected = fingerprints(); expected.pop("input_setup_fingerprint")
        with self.assertRaisesRegex(ValueError, "full independent"):
            optimization.PhysicalBundleAdapter(bundle, source_spec(), expected, zero_manifest())
        expected = fingerprints(); expected["input_material_fingerprint"] += "-wrong"
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            optimization.PhysicalBundleAdapter(bundle, source_spec(), expected, zero_manifest())
        bundle.metadata["domain"]["declaration"]["winding"]["overhang_mm"] += 1
        with self.assertRaisesRegex(ValueError, "domain differs"):
            adapter(bundle)

    def test_disk_loader_always_requests_approved_full_context(self):
        bundle = AnalyticBundle()
        with mock.patch.object(optimization, "load_bundle", return_value=bundle) as loader, \
                mock.patch.object(optimization, "sha256", return_value="fixture"):
            optimization.PhysicalBundleAdapter.from_directory(Path("C:/fixture"), source_spec(), fingerprints(), zero_manifest())
        self.assertEqual(loader.call_args.kwargs, {"expected_fingerprints": fingerprints(), "require_approved": True})

    def test_real_checked_bundle_roundtrip_uses_the_same_flat_input_contract(self):
        from tests.test_ipmsm_physical_bundle import PhysicalBundleTests
        from pyaedt_motor.model_bundle import save_bundle
        PhysicalBundleTests.setUpClass()
        real = PhysicalBundleTests().bundle()
        real.metadata["fingerprints"] = fingerprints()
        real.metadata["spec_sha256"] = hashlib.sha256(canonical_json(source_spec())).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp) / "artifact"
            save_bundle(real, location)
            bound = optimization.PhysicalBundleAdapter.from_directory(location, source_spec(), fingerprints(), zero_manifest())
            result = bound.predict_controls(baseline(), bound.spec.operating_points[0], [(80., 30.), (10., 30.), (80., 70.)])
        self.assertEqual(result.requested, 3)
        self.assertEqual(len(result.inputs), 1)
        self.assertEqual(set(result.point), set(ALL_TARGETS))
        self.assertAlmostEqual(result.point.iloc[0][optimization.TORQUE], .01 * 55 * 80 * math.cos(math.radians(30)))
        self.assertEqual(bound.artifact_provenance["kind"], "checked_artifact")

    def test_optimizer_code_drift_does_not_publish_misidentified_predictions(self):
        bound = adapter()
        with mock.patch.object(optimization, "sha256", side_effect=[*optimization._IMPORTED_OPTIMIZER_SOURCES.values(), "changed"]):
            with self.assertRaisesRegex(ValueError, "source changed during search"):
                optimization.search_designs(bound, [baseline()])

    def test_loss_bounds_objective_and_pareto_ranking(self):
        bound = adapter()
        evaluated = optimization.evaluate_design(bound, baseline())
        self.assertTrue(evaluated.feasible)
        expected_loss = sum(control.operating_point["duty_weight"] * control.selected["metrics"][optimization.LOSS]["upper"]
                            for control in evaluated.controls)
        self.assertEqual(evaluated.objectives, (evaluated.active_volume_m3, expected_loss))
        self.assertEqual(evaluated.final_design_status, optimization.PROPOSAL_STATUS)
        self.assertLess(evaluated.duty_weighted_loss_w["lower"], evaluated.duty_weighted_loss_w["mean"])
        self.assertLess(evaluated.duty_weighted_loss_w["mean"], evaluated.duty_weighted_loss_w["upper"])
        self.assertLess(evaluated.cycle_efficiency_pct["lower"], evaluated.cycle_efficiency_pct["mean"])
        self.assertLess(evaluated.cycle_efficiency_pct["mean"], evaluated.cycle_efficiency_pct["upper"])
        first = replace(evaluated, design_hash="one", active_volume_m3=.003, duty_weighted_loss_w={"upper": 700})
        second = replace(evaluated, design_hash="two", active_volume_m3=.004, duty_weighted_loss_w={"upper": 500})
        dominated = replace(evaluated, design_hash="three", active_volume_m3=.005, duty_weighted_loss_w={"upper": 900})
        unsupported = replace(evaluated, design_hash="unsupported", status="unsupported", active_volume_m3=.001)
        self.assertEqual([item.design_hash for item in optimization.pareto_front([dominated, second, unsupported, first])], ["one", "two"])

    def test_reproducible_unique_input_plan_and_auditable_report(self):
        bound = adapter()
        first = optimization.generate_design_plan(bound, baseline(), count=3, seed=37)
        self.assertEqual(first, optimization.generate_design_plan(bound, baseline(), count=3, seed=37))
        self.assertNotEqual(first, optimization.generate_design_plan(bound, baseline(), count=3, seed=38))
        report = optimization.search_designs(bound, first, seed=37, search_variables=["stator_outer_radius", "stack_length_mm"])
        self.assertEqual(len(report["proposals"]), 3)
        self.assertFalse(report["search"]["held_out_labels_read"])
        self.assertFalse(report["search"]["fea_submitted"])
        self.assertEqual(report["fea_validation_plan"]["status"], "proposal_only_profile_required")
        self.assertEqual(report["fea_validation_rows"], [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proposals.json"
            optimization.write_search_report(report, path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
            with self.assertRaises(FileExistsError):
                optimization.write_search_report(report, path)
            changed = deepcopy(report); changed["search"]["seed"] = 0
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                optimization.write_search_report(changed, Path(tmp) / "altered.json")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            optimization.search_designs(bound, [baseline(), baseline()])

    def test_validation_rows_replay_explicit_profile_material_wire_and_setup(self):
        from run_ipmsm_batch import build_spec, setup_fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            bound, path, document = validation_profile(Path(tmp))
            report = optimization.search_designs(bound, [baseline()], selected_profile=path)
        rows = report["fea_validation_rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(report["fea_validation_plan"]["status"], "ready_for_independent_fea")
        self.assertFalse(report["fea_validation_plan"]["submitted"])
        self.assertEqual(len({row["case_id"] for row in rows}), 2)
        for row in rows:
            applied = build_spec(row)
            for key, value in {**document["material_context"], **document["winding_context"]}.items():
                self.assertEqual(row[key], value)
                self.assertEqual(getattr(applied, key), value)
            self.assertEqual(row["expected_material_fingerprint"], bound.fingerprints["input_material_fingerprint"])
            self.assertEqual(row["analysis_purpose"], "training")
            self.assertEqual(row["transient_periods"], 6)
            self.assertEqual(row["steps_per_period"], 150)
            self.assertEqual(row["band_circle_segments"], 0)
            self.assertEqual(setup_fingerprint(applied, "analytic", "sin_current", False), bound.fingerprints["input_setup_fingerprint"])

    def test_validation_rows_reject_wrong_or_incomplete_profile_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            bound, path, original = validation_profile(Path(tmp))
            proposal = optimization.evaluate_design(bound, baseline())
            variants = []
            for key in ("material_context", "winding_context", "material_fingerprint"):
                changed = deepcopy(original); changed.pop(key); variants.append(changed)
            changed = deepcopy(original); changed["material_context"] = {"core_material_model": "legacy_pnf_hybrid_v1", "core_material": "27PNF1500_CustomCoreLoss"}; variants.append(changed)
            changed = deepcopy(original); changed["winding_context"]["conductor_area_per_branch_mm2"] += .01; variants.append(changed)
            changed = deepcopy(original); changed["material_fingerprint"] = "materials_v2:sha256:" + "c" * 64; variants.append(changed)
            changed = deepcopy(original); changed["producer_fingerprint"] = "producer_v3:sha256:" + "d" * 64; variants.append(changed)
            changed = deepcopy(original); changed["settings"]["quality_profile"] = "other_profile"; variants.append(changed)
            changed = deepcopy(original); changed["settings"]["transient_periods"] = 12; variants.append(changed)
            changed = deepcopy(original); changed["settings"]["initial_position_deg"] = -20.; variants.append(changed)
            for index, changed in enumerate(variants):
                with self.subTest(variant=index):
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        optimization.build_fea_validation_rows(bound, [proposal], path)

    def test_validation_profile_cannot_silently_ignore_a_runner_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            bound, path, document = validation_profile(Path(tmp))
            proposal = optimization.evaluate_design(bound, baseline())
            # This field is absent from setup_fingerprint and build_spec does
            # not apply it. A matching setup hash alone cannot prove replay.
            document["settings"]["winding_material"] = "aluminum"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "setting 'winding_material'"):
                optimization.build_fea_validation_rows(bound, [proposal], path)

    def test_full_geometry_doe_and_explicit_local_mode_respect_budgets(self):
        bound = adapter()
        full = optimization.generate_design_plan(bound, baseline(), count=4, seed=9)
        self.assertEqual(len(full), 4)
        changed = [name for name in GEOMETRY_VARIABLE_NAMES if full[1][name] != baseline()[name]]
        self.assertEqual(len(changed), 14)
        for design in full:
            self.assertEqual(design["stator_shoe_thick"], 1.5)
            self.assertLessEqual(geometry_metrics(design, design["stack_length_mm"], bound.spec.winding).slot_fill_ratio, .45)
        local = optimization.generate_design_plan(bound, baseline(), count=4, seed=9, mode="local_subspace")
        self.assertTrue(all(all(row[name] == baseline()[name] for name in GEOMETRY_VARIABLE_NAMES if name != "stator_outer_radius") for row in local))
        with self.assertRaisesRegex(ValueError, "evaluation budget"):
            optimization.search_designs(bound, full, max_design_evaluations=3)
        self.assertEqual(bound.bundle.batches, [])
        with self.assertRaisesRegex(ValueError, "explicit local_subspace"):
            optimization.generate_design_plan(bound, baseline(), variables=["stack_length_mm"])
        with mock.patch.object(optimization, "ALL_TARGETS", tuple(target for target in ALL_TARGETS if target != optimization.VOLTAGE_SPAN)):
            with self.assertRaisesRegex(ValueError, "span-bearing"):
                adapter()

    def test_invalid_geometry_and_inactive_shoe_are_rejected(self):
        bound = adapter()
        with self.assertRaisesRegex(ValueError, "inactive"):
            optimization.evaluate_design(bound, {**baseline(), "stator_shoe_thick": 1.4})
        narrow = {**baseline(), "stator_teeth_width_ratio": source_spec()["design_space"]["stator_teeth_width_ratio"][1]}
        metrics = geometry_metrics(narrow, narrow["stack_length_mm"], bound.spec.winding)
        self.assertGreater(metrics.slot_fill_ratio, .45)
        self.assertEqual(optimization.evaluate_design(bound, narrow).status, "geometry_constraint_violation")
        with self.assertRaisesRegex(ValueError, "no supported search span"):
            bundle = AnalyticBundle(); bundle.metadata["domain"]["observed_bounds"]["input_stack_length_mm"] = [55., 55.]
            optimization.generate_design_plan(adapter(bundle), baseline(), mode="local_subspace")


if __name__ == "__main__":
    unittest.main()
