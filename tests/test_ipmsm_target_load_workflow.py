from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import pickle
import unittest

import calibrate_ipmsm_beta as beta_calibration
import ipmsm_optimization as optimization
import ipmsm_surrogate_bundle as surrogate_bundle
import ipmsm_target_load_matching as target_load_matching
import ipmsm_target_load_workflow as workflow
import optimize_ipmsm_nsga2 as nsga2
import validate_ipmsm_pareto_fea as pareto_validator


SETUP_FINGERPRINT = "setup_v2:sha256:" + "1" * 64
MATERIAL_FINGERPRINT = "materials_v2:sha256:" + "2" * 64
AEDT_VERSION = "2025.2"


class ConstantEstimator:
    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, rows: object) -> list[float]:
        return [self.value for _ in rows]  # type: ignore[arg-type]


MODEL_ARTIFACTS = {
    "torque_model.pkl": pickle.dumps([ConstantEstimator(100.0) for _ in range(5)]),
    "core_model.pkl": pickle.dumps([ConstantEstimator(20.0) for _ in range(5)]),
    "solid_model.pkl": pickle.dumps([ConstantEstimator(10.0) for _ in range(5)]),
    "voltage_model.pkl": pickle.dumps([ConstantEstimator(150.0) for _ in range(5)]),
}


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def beta_manifest() -> dict[str, object]:
    payload: dict[str, object] = {
        "workflow_version": "beta_calibration_v2",
        "method": beta_calibration.ZERO_CALIBRATION_METHOD,
        "convention": beta_calibration.BETA_CONVENTION,
        "electrical_zero_deg": 12.5,
        "source_case_id": "beta-zero-fixture",
        "design_hash": "f" * 64,
        "quality_profile": "reference_ultra",
        "initial_position_deg": 0.0,
        "successful_rows": 2,
        "successful_speeds_rpm": [1200.0, 5000.0],
        "circular_resultant": 1.0,
        "max_circular_deviation_deg": 0.0,
        "observations": [],
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["calibration_id"] = "beta-calibration:sha256:" + hashlib.sha256(encoded).hexdigest()
    return payload


def spec_mapping(calibration_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "operating_points": [
            {
                "name": "torque_point",
                "speed_rpm": 1200.0,
                "target_torque_nm": 50.0,
                "duty_weight": 0.4,
            },
            {
                "name": "power_point",
                "speed_rpm": 5000.0,
                "target_power_w": 10000.0,
                "duty_weight": 0.6,
            },
        ],
        "stack_length_bounds_mm": [40.0, 70.0],
        "inverter": {
            "vdc_v": 400.0,
            "phase_peak_current_limit_a": 200.0,
            "voltage_utilization": 0.95,
        },
        "winding": {
            "series_turns_per_phase": 48,
            "turns_per_coil_side": 12,
            "coils_per_phase": 4,
            "parallel_branches": 1,
            "strand_area_mm2": 5.0,
            "strands_per_turn": 1,
            "fill_factor": 0.6,
            "end_turn_factor": 1.0,
            "overhang_mm": 5.0,
        },
        "constraints": {"current_density_limit_a_per_mm2": 30.0},
        "beta_calibration": {
            "electrical_zero_deg": 12.5,
            "calibration_id": calibration_id,
            "convention": "dq_current_advance_v2",
        },
        "control": {
            "beta_bounds_deg": [0.0, 80.0],
            "current_grid_points": 9,
            "coarse_beta_step_deg": 20.0,
            "beta_refinement_steps_deg": [2.0],
            "current_refinement_denominators": [32],
        },
        "nsga2": {"population_size": 8, "max_generations": 2, "seeds": [42]},
    }


def predictor(features: dict[str, object]) -> dict[str, float]:
    current = float(features["current_peak_a"])
    beta_delta = math.radians(float(features["beta_deg"]) - 20.0)
    torque = 0.70 * current * math.cos(beta_delta)
    beta_penalty = (float(features["beta_deg"]) - 20.0) ** 2 * 0.002
    return {
        "torque_nm": torque,
        "torque_lcb_nm": torque - 0.25,
        "core_loss_w": 4.0 + beta_penalty,
        "core_loss_ucb_w": 5.0 + beta_penalty,
        "solid_loss_w": 2.0 + beta_penalty,
        "solid_loss_ucb_w": 3.0 + beta_penalty,
        "voltage_peak_v": current * 0.30,
        "voltage_peak_ucb_v": current * 0.35,
    }


def metadata(calibration_id: str) -> dict[str, object]:
    return {
        "training_schema": pareto_validator.V2_TRAINING_SCHEMA,
        "fingerprints": {
            "input_dataset_schema_version": nsga2.FEA_DATASET_SCHEMA_VERSION,
            "input_setup_fingerprint": SETUP_FINGERPRINT,
            "input_quality_profile": nsga2.REFERENCE_FEA_QUALITY_PROFILE,
            "input_material_fingerprint": MATERIAL_FINGERPRINT,
            "input_aedt_version": AEDT_VERSION,
            "input_beta_calibration_id": calibration_id,
            "input_beta_convention": nsga2.BETA_CONVENTION,
            "input_model_extent": nsga2.FEA_MODEL_EXTENT,
        },
        "r2_threshold": 0.95,
        "primary_test_r2_gate_complete": True,
        "primary_test_r2_gate_passed": True,
        "primary_test_r2": {
            target: 0.96 for target in pareto_validator.PRIMARY_R2_TARGETS
        },
        "voltage_r2_threshold": 0.95,
        "voltage_test_r2": 0.96,
        "voltage_test_r2_gate_complete": True,
        "voltage_test_r2_gate_passed": True,
        "ensemble_size": 5,
        "conformal_coverage": 0.95,
        "conformal_calibration_isolated": True,
        "input_columns": ["input_i_peak_a"],
        "modeled_output_columns": [
            surrogate_bundle.TORQUE_TARGET,
            surrogate_bundle.CORE_LOSS_TARGET,
            surrogate_bundle.SOLID_LOSS_TARGET,
        ],
        "auxiliary_output_columns": [surrogate_bundle.VOLTAGE_TARGET],
        "output_name_map": {
            target: target for target in surrogate_bundle.REQUIRED_OPTIMIZER_TARGETS
        },
        "feature_bounds_source": pareto_validator.FEATURE_BOUNDS_SOURCE,
        "feature_bounds": {"input_i_peak_a": {"min": 0.0, "max": 200.0}},
        "model_paths": {
            surrogate_bundle.TORQUE_TARGET: "nested/torque_model.pkl",
            surrogate_bundle.CORE_LOSS_TARGET: "nested/core_model.pkl",
            surrogate_bundle.SOLID_LOSS_TARGET: "nested/solid_model.pkl",
            surrogate_bundle.VOLTAGE_TARGET: "nested/voltage_model.pkl",
        },
        "conformal_absolute_residuals": {
            target: {
                "coverage": 0.95,
                "calibration_rows": 20,
                "rank": 20,
                "quantile_abs": 1.0,
            }
            for target in surrogate_bundle.REQUIRED_OPTIMIZER_TARGETS
        },
    }


def artifact_hashes() -> dict[str, str]:
    paths = metadata(beta_manifest()["calibration_id"])["model_paths"]
    result: dict[str, str] = {}
    for target in sorted(paths):
        recorded = paths[target]
        values = [recorded] if isinstance(recorded, str) else list(recorded)
        for index, value in enumerate(values):
            basename = Path(value).name
            result[f"{target}[{index}]::{basename}"] = hashlib.sha256(
                MODEL_ARTIFACTS[basename]
            ).hexdigest()
    return result


def optimizer_artifact_manifest_sha256() -> str:
    payload = json.dumps(
        artifact_hashes(),
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def fixture_documents() -> tuple[dict[str, object], optimization.OptimizationSpec, object]:
    calibration = beta_manifest()
    mapping = spec_mapping(str(calibration["calibration_id"]))
    spec = optimization.optimization_spec_from_mapping(mapping)
    design = {bound.name: (bound.lower + bound.upper) / 2.0 for bound in spec.design_space}
    candidate = optimization.evaluate_design_candidate(
        design,
        spec,
        predictor,
        candidate_id="pareto_001",
        seed=42,
    )
    if not candidate.feasible:
        raise AssertionError("target-load fixture candidate must be feasible")
    spec_json = canonical_bytes(mapping)
    metadata_json = canonical_bytes(metadata(str(calibration["calibration_id"])))
    pareto_csv = nsga2.render_pareto_csv_bytes([candidate], spec)
    provenance = nsga2.build_optimization_run_provenance(
        pareto_csv,
        {
            nsga2.OPTIMIZATION_SPEC_SHA256_FIELD: hashlib.sha256(spec_json).hexdigest(),
            nsga2.SURROGATE_METADATA_SHA256_FIELD: hashlib.sha256(metadata_json).hexdigest(),
            nsga2.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: optimizer_artifact_manifest_sha256(),
            nsga2.SURROGATE_VERIFICATION_FIELD: nsga2.STRICT_BUNDLE_VERIFICATION,
        },
    )
    plan_csv = nsga2.render_fea_cases_csv_bytes(
        [candidate],
        spec,
        quality_profile=nsga2.REFERENCE_FEA_QUALITY_PROFILE,
        provenance=provenance,
    )
    documents: dict[str, object] = {
        "optimization_spec_json": spec_json,
        "pareto_csv": pareto_csv,
        "seed_fea_plan_csv": plan_csv,
        "model_metadata_json": metadata_json,
        "model_artifacts_by_basename": dict(MODEL_ARTIFACTS),
        "beta_calibration_manifest_json": canonical_bytes(calibration),
        **{
            field: path.read_bytes()
            for field, path in workflow.RUNTIME_SOURCE_PATHS.items()
        },
    }
    return documents, spec, candidate


def scheduler_contract() -> dict[str, object]:
    return {
        "project": "PYAEDT_MOTOR_IPMSM_V2",
        "project_id": 2,
        "server_cap": 100,
        "endpoint": "/api/tasks",
        "scheduling_profile": "fea_bursty",
        "required_capability": "conda:pyaedt2026v1",
        "env_profile": "pyaedt2026v1",
        "env_setup": "module load ansys-electronics/v252",
        "partition": "auto",
        "max_workers_per_node": 4,
        "remote_root": "$HOME/slurm_scheduler/projects/PYAEDT_MOTOR_IPMSM_V2/pyaedt_motor",
        "entrypoint": "subprocess_run.py",
        "cpus": 4,
        "cores_per_process": 4,
        "memory_mb": 32_768,
        "task_timeout_seconds": 43_200,
    }


def policy_template(**overrides: object) -> workflow.MatchPolicyTemplate:
    values: dict[str, object] = {
        "relative_tolerance": 0.01,
        "minimum_current_peak_a": 0.0,
        "maximum_current_peak_a": 200.0,
        "max_attempts": 6,
        "monotonic_relative_tolerance": 0.005,
        "minimum_step_relative": 0.01,
        "maximum_scale_per_attempt": 1.5,
    }
    values.update(overrides)
    return workflow.MatchPolicyTemplate(**values)  # type: ignore[arg-type]


def build_kwargs() -> dict[str, object]:
    documents, _, _ = fixture_documents()
    return {
        **documents,
        "scheduler_contract": scheduler_contract(),
        "policy_template": policy_template(),
        "task_retry_limit": 2,
        "result_settle_seconds": 60,
        "result_identity_relative_tolerance": 1.0e-6,
    }


def root_manifest() -> dict[str, object]:
    return workflow.build_root_manifest(**build_kwargs())  # type: ignore[arg-type]


def rehash_root(manifest: dict[str, object]) -> dict[str, object]:
    identity = manifest["identity"]
    manifest["identity_sha256"] = workflow.canonical_json_sha256(identity)
    match_run_id = workflow._namespaced_id("ipmsm-target-load-match", identity)
    manifest["match_run_id"] = match_run_id
    probes: list[dict[str, object]] = []
    for seed in identity["probe_seeds"]:
        payload = {
            "match_run_id": match_run_id,
            "candidate_id": seed["candidate_id"],
            "operating_point_id": seed["operating_point_id"],
            "beta_validation_role": seed["beta_validation_role"],
            "beta_dq_deg": workflow.canonical_float(seed["beta_dq_deg"]),
            "base_row_sha256": seed["base_row_sha256"],
            "policy_sha256": seed["policy_sha256"],
        }
        probes.append(
            {
                **seed,
                "probe_id": workflow._namespaced_id("ipmsm-target-load-probe", payload),
            }
        )
    manifest["probes"] = probes
    return manifest


def rewrite_csv(payload: bytes, mutate) -> bytes:
    stream = io.StringIO(payload.decode("utf-8"), newline="")
    reader = csv.DictReader(stream)
    fields = list(reader.fieldnames or ())
    rows = [dict(row) for row in reader]
    mutate(rows)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


class RootManifestTests(unittest.TestCase):
    def test_exact_documents_freeze_strict_independent_probes(self) -> None:
        manifest = root_manifest()
        workflow.validate_root_manifest(manifest)
        probes = manifest["probes"]
        self.assertEqual(len(probes), 6)
        self.assertEqual(len({probe["probe_id"] for probe in probes}), 6)
        self.assertEqual(
            manifest["identity"]["strict_input_validation"]["pareto_binding"],
            "validate_pareto_front",
        )
        self.assertEqual(
            manifest["identity"]["source_hashes"]["seed_fea_plan_sha256"],
            hashlib.sha256(build_kwargs()["seed_fea_plan_csv"]).hexdigest(),
        )
        expected_source_paths = {
            "matcher_source": "ipmsm_target_load_matching.py",
            "workflow_source": "ipmsm_target_load_workflow.py",
            "coordinator_source": "ipmsm_target_load_coordinator.py",
            "validator_source": "validate_ipmsm_pareto_fea.py",
            "submit_ipmsm_v2_campaign_source": "submit_ipmsm_v2_campaign.py",
            "submit_ipmsm_scheduler_task_source": "submit_ipmsm_scheduler_task.py",
            "submit_ipmsm_scheduler_job_source": "submit_ipmsm_scheduler_job.py",
            "subprocess_run_source": "subprocess_run.py",
            "run_ipmsm_batch_source": "run_ipmsm_batch.py",
            "ipmsm_ppt_setup_source": "module/ipmsm_ppt_setup.py",
            "ipmsm_geometry_source": "module/ipmsm_geometry.py",
            "variable_source": "module/variable.py",
            "pyaedt_core_source": "pyaedt_module/core/pydesktop.py",
        }
        source_paths = workflow.RUNTIME_SOURCE_PATHS
        self.assertEqual(set(source_paths), set(expected_source_paths))
        for field, suffix in expected_source_paths.items():
            with self.subTest(runtime_path=field):
                self.assertTrue(source_paths[field].as_posix().endswith(suffix))
        embedded = manifest["identity"]["source_documents_base64"]
        source_hashes = manifest["identity"]["source_hashes"]
        for field, path in source_paths.items():
            with self.subTest(field=field):
                expected = path.read_bytes()
                self.assertEqual(base64.b64decode(embedded[field]), expected)
                self.assertEqual(
                    source_hashes[f"{field}_sha256"],
                    hashlib.sha256(expected).hexdigest(),
                )
        self.assertNotEqual(
            source_hashes["workflow_source_sha256"],
            source_hashes["coordinator_source_sha256"],
        )

    def test_spec_plan_and_beta_manifest_tamper_fail_closed(self) -> None:
        kwargs = build_kwargs()
        changed_spec = json.loads(kwargs["optimization_spec_json"])
        changed_spec["inverter"]["vdc_v"] = 399.0
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "strict seed"):
            workflow.build_root_manifest(
                **{**kwargs, "optimization_spec_json": canonical_bytes(changed_spec)}  # type: ignore[arg-type]
            )

        changed_plan = rewrite_csv(
            kwargs["seed_fea_plan_csv"],
            lambda rows: rows[0].__setitem__("base_rpm", "999"),
        )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "strict seed"):
            workflow.build_root_manifest(
                **{**kwargs, "seed_fea_plan_csv": changed_plan}  # type: ignore[arg-type]
            )

        calibration = json.loads(kwargs["beta_calibration_manifest_json"])
        calibration["electrical_zero_deg"] = 13.5
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "calibration"):
            workflow.build_root_manifest(
                **{**kwargs, "beta_calibration_manifest_json": canonical_bytes(calibration)}  # type: ignore[arg-type]
            )

    def test_pareto_semantics_are_validated_after_recomputed_provenance(self) -> None:
        kwargs = build_kwargs()
        changed_pareto = rewrite_csv(
            kwargs["pareto_csv"],
            lambda rows: rows[0].__setitem__("feasible", "False"),
        )
        provenance = nsga2.build_optimization_run_provenance(
            changed_pareto,
            {
                nsga2.OPTIMIZATION_SPEC_SHA256_FIELD: hashlib.sha256(
                    kwargs["optimization_spec_json"]
                ).hexdigest(),
                nsga2.SURROGATE_METADATA_SHA256_FIELD: hashlib.sha256(
                    kwargs["model_metadata_json"]
                ).hexdigest(),
                nsga2.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: optimizer_artifact_manifest_sha256(),
                nsga2.SURROGATE_VERIFICATION_FIELD: nsga2.STRICT_BUNDLE_VERIFICATION,
            },
        )
        changed_plan = rewrite_csv(
            kwargs["seed_fea_plan_csv"],
            lambda rows: [row.update(provenance) for row in rows],
        )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "feasible Pareto"):
            workflow.build_root_manifest(
                **{
                    **kwargs,
                    "pareto_csv": changed_pareto,
                    "seed_fea_plan_csv": changed_plan,
                }  # type: ignore[arg-type]
            )

    def test_duplicate_json_nonfinite_and_artifact_drift_fail_closed(self) -> None:
        kwargs = build_kwargs()
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "duplicate JSON"):
            workflow.build_root_manifest(
                **{**kwargs, "optimization_spec_json": b'{"schema_version":1,"schema_version":1}'}  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "non-finite"):
            workflow.build_root_manifest(
                **{**kwargs, "optimization_spec_json": b'{"schema_version":NaN}'}  # type: ignore[arg-type]
            )
        artifacts = dict(kwargs["model_artifacts_by_basename"])
        artifacts.pop("torque_model.pkl")
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "model artifacts"):
            workflow.build_root_manifest(
                **{**kwargs, "model_artifacts_by_basename": artifacts}  # type: ignore[arg-type]
            )

        invalid_artifacts = dict(kwargs["model_artifacts_by_basename"])
        invalid_artifacts["torque_model.pkl"] = b"not-a-pickle-model"
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "strict surrogate bundle"):
            workflow.build_root_manifest(
                **{**kwargs, "model_artifacts_by_basename": invalid_artifacts}  # type: ignore[arg-type]
            )

        duplicate_metadata = json.loads(kwargs["model_metadata_json"])
        duplicate_metadata["model_paths"][surrogate_bundle.CORE_LOSS_TARGET] = (
            duplicate_metadata["model_paths"][surrogate_bundle.TORQUE_TARGET]
        )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "duplicate basename"):
            workflow.build_root_manifest(
                **{**kwargs, "model_metadata_json": canonical_bytes(duplicate_metadata)}  # type: ignore[arg-type]
            )

        incomplete_metadata = json.loads(kwargs["model_metadata_json"])
        incomplete_metadata.pop("input_columns")
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "strict surrogate bundle"):
            workflow.build_root_manifest(
                **{**kwargs, "model_metadata_json": canonical_bytes(incomplete_metadata)}  # type: ignore[arg-type]
            )

        for field in workflow.RUNTIME_SOURCE_PATHS:
            with self.subTest(field=field):
                with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "runtime source"):
                    workflow.build_root_manifest(
                        **{**kwargs, field: f"stale {field}".encode("ascii")}  # type: ignore[arg-type]
                    )

    def test_scheduler_execution_resources_are_strict_and_frozen(self) -> None:
        manifest = root_manifest()
        self.assertEqual(
            manifest["identity"]["scheduler_contract"],
            {key: scheduler_contract()[key] for key in sorted(scheduler_contract())},
        )
        kwargs = build_kwargs()
        invalid = (
            ("entrypoint", "other.py", "subprocess_run.py"),
            ("partition", "cpu2", "partition='auto'"),
            ("cpus", 0, "cpus must be a positive integer"),
            ("cores_per_process", 5, "cores_per_process must not exceed cpus"),
            ("memory_mb", True, "memory_mb must be a positive integer"),
            ("task_timeout_seconds", 43_199, "task_timeout_seconds must be >= 43200"),
        )
        for field, value, message in invalid:
            with self.subTest(field=field):
                changed = {**scheduler_contract(), field: value}
                with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, message):
                    workflow.build_root_manifest(
                        **{**kwargs, "scheduler_contract": changed}  # type: ignore[arg-type]
                    )
        incomplete = scheduler_contract()
        incomplete.pop("cpus")
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "scheduler contract differs"):
            workflow.build_root_manifest(
                **{**kwargs, "scheduler_contract": incomplete}  # type: ignore[arg-type]
            )
        legacy = scheduler_contract()
        legacy["resource_class"] = "cpu2"
        legacy.pop("partition")
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "scheduler contract differs"):
            workflow.build_root_manifest(
                **{**kwargs, "scheduler_contract": legacy}  # type: ignore[arg-type]
            )

    def test_policy_requires_full_range_one_percent_and_tight_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 0"):
            policy_template(minimum_current_peak_a=1.0)
        with self.assertRaisesRegex(ValueError, "0.01"):
            policy_template(relative_tolerance=0.010001)
        kwargs = build_kwargs()
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "equal"):
            workflow.build_root_manifest(
                **{**kwargs, "policy_template": policy_template(maximum_current_peak_a=199.0)}  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "1e-6"):
            workflow.build_root_manifest(
                **{**kwargs, "result_identity_relative_tolerance": 1.1e-6}  # type: ignore[arg-type]
            )

    def test_rehashed_root_cannot_drop_points_or_rewrite_frozen_evidence(self) -> None:
        dropped = copy.deepcopy(root_manifest())
        dropped["identity"]["operating_point_order"] = ["torque_point"]
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "operating-point order"):
            workflow.validate_root_manifest(rehash_root(dropped))

        changed_target = copy.deepcopy(root_manifest())
        changed_target["identity"]["probe_seeds"][0]["target"]["required_power_w"] += 1.0
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "probe seeds"):
            workflow.validate_root_manifest(rehash_root(changed_target))

        changed_geometry = copy.deepcopy(root_manifest())
        seed = changed_geometry["identity"]["probe_seeds"][0]
        field = changed_geometry["identity"]["design_variable_names"][0]
        seed["base_row"][field] = float(seed["base_row"][field]) + 0.1
        seed["base_row_sha256"] = workflow.canonical_json_sha256(seed["base_row"])
        with self.assertRaises(workflow.TargetLoadWorkflowError):
            workflow.validate_root_manifest(rehash_root(changed_geometry))

        changed_hash = copy.deepcopy(root_manifest())
        changed_hash["identity"]["source_hashes"]["pareto_sha256"] = "0" * 64
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "embedded exact documents"):
            workflow.validate_root_manifest(rehash_root(changed_hash))

        dropped_runtime_source = copy.deepcopy(root_manifest())
        dropped_runtime_source["identity"]["source_documents_base64"].pop(
            "ipmsm_ppt_setup_source"
        )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "source-document coverage"):
            workflow.validate_root_manifest(rehash_root(dropped_runtime_source))

        changed_runtime_hash = copy.deepcopy(root_manifest())
        changed_runtime_hash["identity"]["source_hashes"][
            "run_ipmsm_batch_source_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "embedded exact documents"):
            workflow.validate_root_manifest(rehash_root(changed_runtime_hash))


def result_row_for_attempt(
    manifest: dict[str, object],
    attempt: dict[str, object],
    *,
    output_ratio: float,
    core_loss_w: float = 4.0,
    solid_loss_w: float = 2.0,
    voltage_peak_v: float = 100.0,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    plan = attempt["plan_row"]
    probe = next(
        probe
        for probe in manifest["probes"]
        if probe["probe_id"] == attempt["probe_id"]
    )
    target = probe["target"]
    rpm = float(plan["base_rpm"])
    desired_output = float(target["target_value"]) * output_ratio
    torque = (
        desired_output
        if target["target_kind"] == "torque"
        else desired_output / (2.0 * math.pi * rpm / 60.0)
    )
    current = float(plan["i_peak_a"])
    phase_rms = current / math.sqrt(2.0)
    resistance = float(plan["phase_resistance_ohm"])
    copper_loss = 3.0 * resistance * phase_rms * phase_rms
    total_loss = core_loss_w + solid_loss_w + copper_loss
    actual_power = torque * 2.0 * math.pi * rpm / 60.0
    efficiency = actual_power / (actual_power + total_loss) * 100.0
    row: dict[str, object] = {
        "case_id": plan["case_id"],
        "status": "ok",
        "geometry_group_id": plan["geometry_group_id"],
        "design_hash": plan["design_hash"],
        "doe_split": plan["doe_split"],
        "repeat_of_case_id": plan["repeat_of_case_id"],
        "optimization_run_id": plan["optimization_run_id"],
        "beta_calibration_id": plan["beta_calibration_id"],
        "candidate_id": plan["candidate_id"],
        "operating_point_id": plan["operating_point_id"],
        "control_source": plan["control_source"],
        "execution_host": "n107",
        "missing_required_outputs": "",
        "output_torque_last_min_nm": torque * 0.95,
        "output_torque_last_avg_nm": torque,
        "output_torque_last_max_nm": torque * 1.05,
        "output_coreloss_last_avg_w": core_loss_w,
        "output_solidloss_last_avg_w": solid_loss_w,
        "output_copperloss_last_avg_w": copper_loss,
        "output_phase_current_source": "measured_three_phase",
        "output_phase_voltage_source": "measured_three_phase",
        "output_phase_current_last_rms_a": phase_rms,
        "output_phasea_voltage_last_peak_abs_v": voltage_peak_v - 2.0,
        "output_phaseb_voltage_last_peak_abs_v": voltage_peak_v,
        "output_phasec_voltage_last_peak_abs_v": voltage_peak_v - 1.0,
        "output_phase_voltage_last_peak_abs_v": voltage_peak_v,
        "output_total_loss_last_avg_w": total_loss,
        "output_efficiency_last_pct": efficiency,
    }
    for name in manifest["identity"]["design_variable_names"]:
        row[f"input_{name}"] = plan[name]
    for name in (
        "slot_num",
        "pole_num",
        "base_rpm",
        "i_peak_a",
        "beta_dq_deg",
        "beta_convention",
        "electrical_zero_deg",
        "beta_calibration_id",
        "model_extent",
        "symmetry_factor",
        "use_periodic_boundary",
        "phase_resistance_ohm",
        "vdc_v",
        "series_turns_per_phase",
        "turns_per_coil_side",
        "quality_profile",
        "geometry_mode",
        "operation",
        "dataset_schema_version",
    ):
        row[f"input_{name}"] = plan[name]
    row.update(manifest["identity"]["model_fingerprints"])
    if overrides:
        row.update(overrides)
    return row


def result_csv(row: dict[str, object]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def issue_observation(
    manifest: dict[str, object],
    probe_id: str,
    history: list[dict[str, object]],
    *,
    output_ratio: float,
    core_loss_w: float = 4.0,
    solid_loss_w: float = 2.0,
    voltage_peak_v: float = 100.0,
    overrides: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object], bytes]:
    planned = workflow.plan_probe_attempt(manifest, probe_id, history)
    attempt = planned["attempt"]
    row = result_row_for_attempt(
        manifest,
        attempt,
        output_ratio=output_ratio,
        core_loss_w=core_loss_w,
        solid_loss_w=solid_loss_w,
        voltage_peak_v=voltage_peak_v,
        overrides=overrides,
    )
    payload = result_csv(row)
    observation = workflow.observation_from_result(
        manifest,
        attempt,
        history,
        payload,
    )
    return attempt, observation, payload


class AttemptAndResultTests(unittest.TestCase):
    def test_attempt_is_deterministic_and_seed_surrogate_values_are_not_reused(self) -> None:
        manifest = root_manifest()
        probe = manifest["probes"][0]
        first = workflow.plan_probe_attempt(manifest, probe["probe_id"], [])
        repeated = workflow.plan_probe_attempt(manifest, probe["probe_id"], [])
        self.assertEqual(first, repeated)
        attempt = first["attempt"]
        self.assertTrue(attempt["case_id"].startswith("tlm4__"))
        self.assertEqual(
            attempt["plan_row"]["surrogate_prediction_status"],
            workflow.ATTEMPT_SURROGATE_STATUS,
        )
        for field in workflow.SURROGATE_SELECTION_FIELDS:
            self.assertEqual(attempt["plan_row"][field], "")
            self.assertEqual(
                attempt["plan_row"][f"seed_selection_{field}"],
                probe["base_row"][field],
            )

    def test_rejects_forged_first_middle_and_post_terminal_observations(self) -> None:
        manifest = root_manifest()
        probe = manifest["probes"][0]
        _, first, _ = issue_observation(
            manifest,
            probe["probe_id"],
            [],
            output_ratio=0.5,
        )
        forged_first = copy.deepcopy(first)
        forged_first["current_peak_a"] = float(first["current_peak_a"]) * 1.1
        forged_first["attempt_id"] = workflow.attempt_id_for(
            probe["probe_id"],
            1,
            forged_first["current_peak_a"],
            probe["policy_sha256"],
        )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "exact FEA result|not proposed"):
            workflow.plan_probe_attempt(manifest, probe["probe_id"], [forged_first])

        _, second, _ = issue_observation(
            manifest,
            probe["probe_id"],
            [first],
            output_ratio=0.75,
        )
        forged_second = copy.deepcopy(second)
        forged_second["current_peak_a"] = float(second["current_peak_a"]) * 1.02
        forged_second["attempt_id"] = workflow.attempt_id_for(
            probe["probe_id"],
            2,
            forged_second["current_peak_a"],
            probe["policy_sha256"],
        )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "exact FEA result|not proposed"):
            workflow.plan_probe_attempt(
                manifest,
                probe["probe_id"],
                [first, forged_second],
            )

        _, matched, _ = issue_observation(
            manifest,
            probe["probe_id"],
            [],
            output_ratio=1.0,
        )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "terminal"):
            workflow.plan_probe_attempt(
                manifest,
                probe["probe_id"],
                [matched, copy.deepcopy(matched)],
            )

    def test_attempt_reconstruction_rejects_rehashed_plan_and_identity_tamper(self) -> None:
        manifest = root_manifest()
        probe = manifest["probes"][0]
        attempt = workflow.plan_probe_attempt(manifest, probe["probe_id"], [])["attempt"]
        tampered = copy.deepcopy(attempt)
        tampered["plan_row"]["base_rpm"] = 999.0
        tampered["plan_row_sha256"] = workflow.canonical_json_sha256(tampered["plan_row"])
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "planner reconstruction"):
            workflow.validate_attempt_manifest(manifest, tampered, [])
        for field, value in (
            ("case_id", "forged"),
            ("dedupe_key", "forged"),
            ("history_sha256", "0" * 64),
            ("policy_sha256", "0" * 64),
        ):
            changed = copy.deepcopy(attempt)
            changed[field] = value
            with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "planner reconstruction"):
                workflow.validate_attempt_manifest(manifest, changed, [])

    def test_result_accepts_real_schema_and_binds_geometry_fingerprints_and_rpm(self) -> None:
        manifest = root_manifest()
        probe = manifest["probes"][0]
        attempt, observation, payload = issue_observation(
            manifest,
            probe["probe_id"],
            [],
            output_ratio=0.75,
        )
        self.assertEqual(observation["result_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(observation["case_id"], attempt["case_id"])
        self.assertGreater(observation["target_load_efficiency_pct"], 0.0)

        base_row = result_row_for_attempt(manifest, attempt, output_ratio=0.75)
        geometry_name = manifest["identity"]["design_variable_names"][0]
        for field, value in (
            (f"input_{geometry_name}", float(base_row[f"input_{geometry_name}"]) * 1.05),
            ("input_base_rpm", float(base_row["input_base_rpm"]) * 1.05),
            ("input_setup_fingerprint", "setup_v2:sha256:" + "9" * 64),
            ("input_material_fingerprint", "materials_v2:sha256:" + "8" * 64),
            ("input_aedt_version", "2026.1"),
        ):
            changed = {**base_row, field: value}
            with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "result contract"):
                workflow.observation_from_result(
                    manifest,
                    attempt,
                    [],
                    result_csv(changed),
                )

    def test_power_output_is_derived_from_average_torque_and_spec_rpm(self) -> None:
        manifest = root_manifest()
        probe = next(
            probe
            for probe in manifest["probes"]
            if probe["operating_point_id"] == "power_point"
        )
        _, observation, _ = issue_observation(
            manifest,
            probe["probe_id"],
            [],
            output_ratio=0.8,
        )
        expected = observation["actual_torque_nm"] * 2.0 * math.pi * 5000.0 / 60.0
        self.assertAlmostEqual(observation["output_value"], expected, places=9)
        self.assertAlmostEqual(observation["actual_power_w"], expected, places=9)

    def test_history_replays_exact_result_bytes_and_rejects_derived_field_tamper(self) -> None:
        manifest = root_manifest()
        probe = manifest["probes"][0]
        _, observation, payload = issue_observation(
            manifest,
            probe["probe_id"],
            [],
            output_ratio=0.75,
        )
        self.assertEqual(
            base64.b64decode(observation["result_csv_base64"], validate=True),
            payload,
        )
        for field, value in (
            ("result_sha256", "0" * 64),
            ("output_value", float(observation["output_value"]) + 1.0),
            ("actual_total_loss_w", 0.001),
            ("hard_constraints_passed", False),
        ):
            changed = copy.deepcopy(observation)
            changed[field] = value
            with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "exact FEA result"):
                workflow.plan_probe_attempt(manifest, probe["probe_id"], [changed])

    def test_torque_order_physics_identity_and_hard_constraint_gate(self) -> None:
        manifest = root_manifest()
        probe = manifest["probes"][0]
        attempt = workflow.plan_probe_attempt(manifest, probe["probe_id"], [])["attempt"]
        bad_order = result_row_for_attempt(manifest, attempt, output_ratio=1.0)
        bad_order["output_torque_last_min_nm"] = (
            float(bad_order["output_torque_last_avg_nm"]) + 1.0
        )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "min <= average"):
            workflow.observation_from_result(manifest, attempt, [], result_csv(bad_order))

        first_attempt, constrained, _ = issue_observation(
            manifest,
            probe["probe_id"],
            [],
            output_ratio=1.0,
            voltage_peak_v=manifest["identity"]["spec_limits"]["phase_peak_voltage_limit_v"] + 1.0,
        )
        self.assertFalse(constrained["hard_constraints_passed"])
        refinement = workflow.plan_probe_attempt(manifest, probe["probe_id"], [constrained])
        self.assertIn("attempt", refinement)
        self.assertLess(
            refinement["attempt"]["current_peak_a"],
            first_attempt["current_peak_a"],
        )
        _, feasible_edge, _ = issue_observation(
            manifest,
            probe["probe_id"],
            [constrained],
            output_ratio=0.99,
        )
        terminal = workflow.plan_probe_attempt(
            manifest,
            probe["probe_id"],
            [constrained, feasible_edge],
        )
        self.assertEqual(terminal["terminal_status"], "matched")
        self.assertTrue(terminal["matched_observation"]["hard_constraints_passed"])

    def test_beta_roles_have_independent_histories_and_currents(self) -> None:
        manifest = root_manifest()
        probes = [
            probe
            for probe in manifest["probes"]
            if probe["operating_point_id"] == "torque_point"
        ]
        ratios = [0.8, 0.7, 0.6]
        next_currents: list[float] = []
        for probe, ratio in zip(probes, ratios):
            _, observation, _ = issue_observation(
                manifest,
                probe["probe_id"],
                [],
                output_ratio=ratio,
            )
            next_attempt = workflow.plan_probe_attempt(
                manifest,
                probe["probe_id"],
                [observation],
            )["attempt"]
            next_currents.append(float(next_attempt["current_peak_a"]))
        self.assertEqual(len(set(next_currents)), len(next_currents))


def fixed_mtpa_evidence(
    manifest: dict[str, object],
    candidate_id: str,
    *,
    neighbor_output_ratio: float = 0.9,
) -> dict[str, object]:
    points: list[dict[str, object]] = []
    for point_id in manifest["identity"]["operating_point_order"]:
        probes = [
            probe
            for probe in manifest["probes"]
            if probe["candidate_id"] == candidate_id
            and probe["operating_point_id"] == point_id
        ]
        rows: list[dict[str, object]] = []
        for probe in probes:
            output_ratio = (
                1.0
                if probe["beta_validation_role"] == "selected_center"
                else neighbor_output_ratio
            )
            payload = result_csv(
                result_row_for_attempt(
                    manifest,
                    {"plan_row": probe["base_row"], "probe_id": probe["probe_id"]},
                    output_ratio=output_ratio,
                )
            )
            rows.append(
                {
                    "beta_validation_role": probe["beta_validation_role"],
                    "case_id": probe["base_case_id"],
                    "result_csv_base64": base64.b64encode(payload).decode("ascii"),
                }
            )
        points.append(
            {
                "operating_point_id": point_id,
                "rows": rows,
            }
        )
    return {
        "schema_version": workflow.FIXED_MTPA_EVIDENCE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "control_source": "fixed_current_mtpa",
        "operating_points": points,
    }


def matched_cohort(
    manifest: dict[str, object],
    candidate_id: str,
    *,
    center_overshoot: float = 1.0,
    center_core_loss_w: float = 4.0,
    neighbor_core_loss_w: float = 20.0,
) -> dict[str, list[dict[str, object]]]:
    histories: dict[str, list[dict[str, object]]] = {}
    first_ratios = {
        "selected_center": 0.8,
        "local_lower": 0.7,
        "local_upper": 0.6,
    }
    for probe in manifest["probes"]:
        if probe["candidate_id"] != candidate_id:
            continue
        role = probe["beta_validation_role"]
        _, first, _ = issue_observation(
            manifest,
            probe["probe_id"],
            [],
            output_ratio=first_ratios[role],
            core_loss_w=(
                center_core_loss_w if role == "selected_center" else neighbor_core_loss_w
            ),
        )
        ratio = center_overshoot if role == "selected_center" else 1.0
        _, second, _ = issue_observation(
            manifest,
            probe["probe_id"],
            [first],
            output_ratio=ratio,
            core_loss_w=(
                center_core_loss_w if role == "selected_center" else neighbor_core_loss_w
            ),
        )
        histories[probe["probe_id"]] = [first, second]
    return histories


class CandidateFinalizationTests(unittest.TestCase):
    def test_all_roles_match_independently_and_required_power_efficiency_is_objective(self) -> None:
        manifest = root_manifest()
        candidate_id = manifest["identity"]["candidate_order"][0]
        evidence = fixed_mtpa_evidence(manifest, candidate_id)
        exact = workflow.finalize_candidate_target_load(
            manifest,
            candidate_id,
            matched_cohort(manifest, candidate_id),
            evidence,
        )
        overshoot = workflow.finalize_candidate_target_load(
            manifest,
            candidate_id,
            matched_cohort(manifest, candidate_id, center_overshoot=1.005),
            evidence,
        )
        self.assertEqual(exact["status"], "matched_and_beta_validated")
        self.assertAlmostEqual(
            exact["objective_cycle_efficiency"],
            overshoot["objective_cycle_efficiency"],
            places=12,
        )
        self.assertNotEqual(
            exact["diagnostic_weighted_actual_power_w"],
            overshoot["diagnostic_weighted_actual_power_w"],
        )
        for point in exact["operating_points"]:
            currents = point["matched_current_by_beta_role_a"]
            self.assertEqual(len(set(currents.values())), len(currents))

    def test_missing_hard_failed_or_nonoptimal_evidence_fails_closed(self) -> None:
        manifest = root_manifest()
        candidate_id = manifest["identity"]["candidate_order"][0]
        histories = matched_cohort(manifest, candidate_id)
        missing = dict(histories)
        missing.pop(next(iter(missing)))
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "coverage"):
            workflow.finalize_candidate_target_load(
                manifest,
                candidate_id,
                missing,
                fixed_mtpa_evidence(manifest, candidate_id),
            )

        bad_evidence = fixed_mtpa_evidence(
            manifest,
            candidate_id,
            neighbor_output_ratio=1.1,
        )
        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "local torque/A maximum"):
            workflow.finalize_candidate_target_load(
                manifest,
                candidate_id,
                histories,
                bad_evidence,
            )

        invalid_evidence = fixed_mtpa_evidence(manifest, candidate_id)
        invalid_evidence["operating_points"][0]["rows"][0]["result_csv_base64"] = (
            base64.b64encode(b"not a result CSV").decode("ascii")
        )
        with self.assertRaises(workflow.TargetLoadWorkflowError):
            workflow.finalize_candidate_target_load(
                manifest,
                candidate_id,
                histories,
                invalid_evidence,
            )

        with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "loss minimum"):
            workflow.finalize_candidate_target_load(
                manifest,
                candidate_id,
                matched_cohort(
                    manifest,
                    candidate_id,
                    center_core_loss_w=5000.0,
                    neighbor_core_loss_w=4.0,
                ),
                fixed_mtpa_evidence(manifest, candidate_id),
            )


if __name__ == "__main__":
    unittest.main()
