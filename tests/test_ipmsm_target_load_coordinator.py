from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import ipmsm_dashboard_state as dashboard
import ipmsm_optimization as optimization
import ipmsm_target_load_coordinator as coordinator
import ipmsm_target_load_workflow as workflow
import optimize_ipmsm_nsga2 as optimizer
import validate_ipmsm_pareto_fea as pareto_validator
from tests.test_validate_ipmsm_pareto_fea import (
    ValidationFixture,
    predictor as validation_predictor,
    read_csv as read_fixture_csv,
    write_csv as write_fixture_csv,
)
from tests.test_ipmsm_target_load_workflow import (
    build_kwargs,
    fixed_mtpa_evidence,
    issue_observation,
    result_csv,
    result_row_for_attempt,
    root_manifest,
)


NOW = datetime(2026, 7, 12, 0, 0, tzinfo=timezone.utc)


def _write_upstream_audit_fixture(
    root: Path,
    *,
    two_candidates: bool = False,
    fea_filter_to_low: bool = False,
    relative_validator_argv: bool = False,
) -> tuple[SimpleNamespace, ValidationFixture]:
    fixture = ValidationFixture(root)
    if two_candidates:
        low_design = {
            bound.name: (bound.lower + bound.upper) / 2.0
            for bound in fixture.spec.design_space
        }
        high_design = dict(low_design)
        low_design["stack_length_mm"] = fixture.spec.stack_length_bounds_mm[0]
        high_design["stack_length_mm"] = fixture.spec.stack_length_bounds_mm[1]

        def length_tradeoff_predictor(features: dict[str, Any]) -> dict[str, float]:
            prediction = dict(validation_predictor(features))
            lower, upper = fixture.spec.stack_length_bounds_mm
            fraction = (float(features["stack_length_mm"]) - lower) / (upper - lower)
            prediction.update(
                {
                    "core_loss_w": 100.0 - 99.9 * fraction,
                    "core_loss_ucb_w": 101.0 - 99.9 * fraction,
                    "solid_loss_w": 50.0 - 49.9 * fraction,
                    "solid_loss_ucb_w": 51.0 - 49.9 * fraction,
                }
            )
            return prediction

        low = optimization.evaluate_design_candidate(
            low_design,
            fixture.spec,
            length_tradeoff_predictor,
            candidate_id="pareto_low",
            seed=42,
        )
        high = optimization.evaluate_design_candidate(
            high_design,
            fixture.spec,
            length_tradeoff_predictor,
            candidate_id="pareto_high",
            seed=42,
        )
        if not low.feasible or not high.feasible:
            raise AssertionError("two-candidate audit fixture must be surrogate feasible")
        decoded_metadata = json.loads(fixture.metadata_path.read_text(encoding="utf-8"))
        artifact_hashes: dict[str, str] = {}
        for target in sorted(decoded_metadata["model_paths"]):
            recorded = decoded_metadata["model_paths"][target]
            values = [recorded] if isinstance(recorded, str) else list(recorded)
            for index, value in enumerate(values):
                path = root / Path(value).name
                artifact_hashes[f"{target}[{index}]::{path.name}"] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        fixture.pareto_path.unlink()
        fixture.plan_path.unlink()
        optimizer.write_optimization_csv_pair(
            fixture.pareto_path,
            fixture.plan_path,
            [low, high],
            [high, low],
            fixture.spec,
            provenance_context={
                optimizer.OPTIMIZATION_SPEC_SHA256_FIELD: hashlib.sha256(
                    fixture.spec_path.read_bytes()
                ).hexdigest(),
                optimizer.SURROGATE_METADATA_SHA256_FIELD: hashlib.sha256(
                    fixture.metadata_path.read_bytes()
                ).hexdigest(),
                optimizer.SURROGATE_MODEL_ARTIFACTS_SHA256_FIELD: (
                    workflow._optimizer_canonical_json_sha256(artifact_hashes)
                ),
                optimizer.SURROGATE_VERIFICATION_FIELD: optimizer.STRICT_BUNDLE_VERIFICATION,
            },
        )
        fixture.pareto_fields, fixture.pareto_rows = read_fixture_csv(fixture.pareto_path)
        fixture.plan_fields, fixture.plan_rows = read_fixture_csv(fixture.plan_path)
        fixture.result_rows = [fixture.result_row(row) for row in fixture.plan_rows]
        for row in fixture.result_rows:
            if fea_filter_to_low:
                core_loss = 1.0 if row["candidate_id"] == "pareto_high" else 0.01
            else:
                core_loss = 0.05 if row["candidate_id"] == "pareto_high" else 99.0
            solid_loss = float(row["output_solidloss_last_avg_w"])
            copper_loss = float(row["output_copperloss_last_avg_w"])
            total_loss = core_loss + solid_loss + copper_loss
            point = next(
                item
                for item in fixture.spec.operating_points
                if item.name == row["operating_point_id"]
            )
            power = float(row["output_torque_last_avg_nm"]) * point.mechanical_angular_speed_rad_s
            row["output_coreloss_last_avg_w"] = core_loss
            row["output_total_loss_last_avg_w"] = total_loss
            row["output_efficiency_last_pct"] = power / (power + total_loss) * 100.0
        fixture.result_fields = list(fixture.result_rows[0])
        write_fixture_csv(fixture.results_path, fixture.result_fields, fixture.result_rows)
    summary, rows = fixture.validate()
    summary_path = root / "pareto_fea_validation.json"
    rows_path = root / "pareto_fea_validation_rows.csv"
    front_path = root / pareto_validator.DEFAULT_FINAL_FRONT_NAME
    summary_path.write_bytes(pareto_validator._json_text(summary).encode("utf-8"))
    rows_path.write_bytes(pareto_validator._row_csv_text(rows).encode("utf-8"))
    front_path.write_bytes(
        pareto_validator._final_front_csv_text(
            fixture.spec,
            summary["fea_filtered_final_front"],
        ).encode("utf-8")
    )
    beta_path = root / "beta_calibration_manifest.json"
    beta_path.write_text("{}", encoding="utf-8")

    _, fingerprints, _ = pareto_validator.read_model_metadata(
        fixture.metadata_path,
        fixture.spec,
    )
    metadata = json.loads(fixture.metadata_path.read_text(encoding="utf-8"))
    model_artifacts: dict[str, dict[str, str]] = {}
    for target in sorted(metadata["model_paths"]):
        recorded = metadata["model_paths"][target]
        values = [recorded] if isinstance(recorded, str) else list(recorded)
        for index, value in enumerate(values):
            path = root / Path(value).name
            model_artifacts[f"{target}[{index}]::{path.name}"] = {
                "path": str(path.resolve(strict=False)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    provenance = {
        field: fixture.plan_rows[0][field]
        for field in pareto_validator.PROVENANCE_FIELDS
    }
    candidate_order: list[str] = []
    for row in fixture.plan_rows:
        if row["candidate_id"] not in candidate_order:
            candidate_order.append(row["candidate_id"])

    def artifact(path: Path) -> dict[str, str]:
        return {
            "path": str(path.resolve(strict=False)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def validator_path(path: Path) -> str:
        return path.name if relative_validator_argv else str(path)

    contract = {
        "inputs": {
            "optimization_spec": artifact(fixture.spec_path),
            "beta": {"calibration_manifest": artifact(beta_path)},
            "model_bundle": {
                "model_dir": str(root.resolve(strict=False)),
                "metadata": artifact(fixture.metadata_path),
                "artifacts": model_artifacts,
                "fingerprints": fingerprints,
            },
        },
        "optimization": {
            "pareto_output": str(fixture.pareto_path.resolve(strict=False)),
            "fea_cases_output": str(fixture.plan_path.resolve(strict=False)),
            "max_fea_candidates": 12,
        },
        "pareto_fea": {
            "results": str(fixture.results_path.resolve(strict=False)),
        },
        "validation": {
            "argv": [
                "--spec",
                validator_path(fixture.spec_path),
                "--model-dir",
                "." if relative_validator_argv else str(root.resolve(strict=False)),
                "--pareto",
                validator_path(fixture.pareto_path),
                "--case-plan",
                validator_path(fixture.plan_path),
                "--results",
                validator_path(fixture.results_path),
                "--summary-output",
                validator_path(summary_path),
                "--rows-output",
                validator_path(rows_path),
                "--final-front-output",
                validator_path(front_path),
                "--minimum-coverage",
                str(pareto_validator.DEFAULT_MINIMUM_COVERAGE),
                "--identity-relative-tolerance",
                str(pareto_validator.DEFAULT_IDENTITY_RELATIVE_TOLERANCE),
            ],
            "minimum_coverage": pareto_validator.DEFAULT_MINIMUM_COVERAGE,
            "identity_relative_tolerance": (
                pareto_validator.DEFAULT_IDENTITY_RELATIVE_TOLERANCE
            ),
            "summary_output": str(summary_path.resolve(strict=False)),
            "rows_output": str(rows_path.resolve(strict=False)),
            "final_front_output": str(front_path.resolve(strict=False)),
        },
        "source_sha256": {
            name: hashlib.sha256(
                (Path(coordinator.__file__).resolve().parent / name).read_bytes()
            ).hexdigest()
            for name in coordinator.OPTIMIZATION_SOURCE_FILES
        },
    }
    decision_path = root / "ipmsm_v2_optimization_decision.json"
    final_ids = summary["fea_filtered_final_front_candidate_ids"]
    decision = {
        "schema_version": coordinator.OPTIMIZATION_DECISION_SCHEMA_VERSION,
        "decision_output": str(decision_path.resolve(strict=False)),
        "contract_sha256": coordinator.canonical_json_sha256(contract),
        "execution_contract": contract,
        "mode": "execute",
        "status": "complete",
        "selected_model": {
            "model_dir": str(root.resolve(strict=False)),
            "metadata_sha256": hashlib.sha256(fixture.metadata_path.read_bytes()).hexdigest(),
            "fingerprints": fingerprints,
        },
        "optimization_artifacts": {
            "pareto": artifact(fixture.pareto_path),
            "fea_cases": artifact(fixture.plan_path),
            "fea_candidate_ids": candidate_order,
            "fea_case_rows": len(fixture.plan_rows),
            "provenance": provenance,
        },
        "pareto_fea": {
            "results": str(fixture.results_path.resolve(strict=False)),
            "results_sha256": hashlib.sha256(fixture.results_path.read_bytes()).hexdigest(),
            "case_rows": len(fixture.plan_rows),
        },
        "validation": {
            "summary": artifact(summary_path),
            "rows": artifact(rows_path),
            "final_front": {
                **artifact(front_path),
                "candidate_count": len(final_ids),
                "candidate_ids": final_ids,
            },
            "validation_id": summary["validation_id"],
            "feasible_candidate_count": summary["feasible_candidate_count"],
            "gate_failures": [],
            "pass": True,
        },
    }
    decision_path.write_bytes(coordinator._indented_json_bytes(decision))
    args = SimpleNamespace(
        optimization_decision=decision_path,
        optimization_spec=fixture.spec_path,
        pareto_csv=fixture.pareto_path,
        seed_fea_plan=fixture.plan_path,
        pareto_validation_summary=summary_path,
        pareto_final_front=front_path,
        model_metadata=fixture.metadata_path,
        model_artifact_dir=root,
        beta_calibration_manifest=beta_path,
    )
    return args, fixture


class FakeSchedulerClient:
    """Small deterministic scheduler double for coordinator-cycle tests."""

    def __init__(
        self,
        *,
        cap: int = 100,
        unrelated_active: int = 0,
        response_overrides: Mapping[str, Any] | None = None,
    ) -> None:
        self.cap = cap
        self.unrelated_active = unrelated_active
        self.response_overrides = dict(response_overrides or {})
        self.history: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.results: dict[tuple[int, str], bytes] = {}
        self._next_id = 1

    def snapshot(self, contract: Mapping[str, Any]) -> coordinator.SchedulerSnapshot:
        self.assert_contract = dict(contract)
        active = self.unrelated_active + sum(
            str(task.get("status") or "").lower() in coordinator.ACTIVE_STATUSES
            for task in self.history
        )
        return coordinator.SchedulerSnapshot(
            history=tuple(copy.deepcopy(self.history)),
            project_total_count=len(self.history) + self.unrelated_active,
            project_active_count=active,
            server_cap=self.cap,
        )

    def post(self, payload: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
        record: dict[str, Any] = {
            "id": self._next_id,
            "status": "queued",
            "remote_cwd": "$HOME/slurm_scheduler/projects/PYAEDT_MOTOR_IPMSM_V2/pyaedt_motor",
        }
        for field in (
            "name",
            "project",
            "entrypoint",
            "required_capability",
            "env_profile",
            "partition",
            "cpus",
            "memory_mb",
            "scheduling_profile",
            "max_workers_per_node",
            "timeout_seconds",
            "dedupe_key",
        ):
            record[field] = payload[field]
        record.update(copy.deepcopy(self.response_overrides))
        self._next_id += 1
        self.history.append(record)
        self.posts.append({"endpoint": endpoint, "payload": copy.deepcopy(dict(payload))})
        return dict(record)

    def inject_task(
        self,
        payload: Mapping[str, Any],
        *,
        status: str = "queued",
    ) -> dict[str, Any]:
        record = self.post(payload, "/api/tasks")
        record["status"] = status
        self.history[-1]["status"] = status
        self.posts.clear()
        return record

    def clone_task(self, task: Mapping[str, Any], *, status: str = "queued") -> dict[str, Any]:
        record = copy.deepcopy(dict(task))
        record.update({"id": self._next_id, "status": status})
        record.pop("exit_code", None)
        record.pop("finished_at", None)
        self._next_id += 1
        self.history.append(record)
        return record

    def set_status(self, task_id: int, status: str, *, exit_code: int | None = None) -> None:
        task = next(item for item in self.history if item["id"] == task_id)
        task["status"] = status
        if exit_code is not None:
            task["exit_code"] = exit_code

    def fetch_result(self, task_id: int, result_path: str) -> bytes:
        return self.results[(task_id, result_path)]


class UpstreamFinalFrontAuditTests(unittest.TestCase):
    def audit(self, args: SimpleNamespace) -> tuple[bytes, dict[str, Any]]:
        metadata_json = args.model_metadata.read_bytes()
        return coordinator._audit_upstream_final_front(
            args,
            spec_json=args.optimization_spec.read_bytes(),
            pareto_csv=args.pareto_csv.read_bytes(),
            seed_plan_csv=args.seed_fea_plan.read_bytes(),
            metadata_json=metadata_json,
            beta_json=args.beta_calibration_manifest.read_bytes(),
            model_artifacts=coordinator._model_artifacts_from_directory(
                metadata_json,
                args.model_artifact_dir,
            ),
        )

    def test_completed_decision_strict_summary_and_final_front_are_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, fixture = _write_upstream_audit_fixture(Path(tmp))
            filtered, binding = self.audit(args)
            fields, rows = workflow._strict_csv(filtered, "filtered fixture plan")
            self.assertEqual(fields, fixture.plan_fields)
            self.assertEqual(
                [row["case_id"] for row in rows],
                [row["case_id"] for row in fixture.plan_rows],
            )
            self.assertEqual(binding["selected_candidate_ids"], ["pareto_001"])
            self.assertEqual(
                binding["source_artifacts"]["seed_fea_plan"]["sha256"],
                hashlib.sha256(fixture.plan_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                binding["optimization_decision"]["sha256"],
                hashlib.sha256(args.optimization_decision.read_bytes()).hexdigest(),
            )

    def test_relative_validator_argv_binds_inferred_execution_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, _ = _write_upstream_audit_fixture(
                root,
                relative_validator_argv=True,
            )
            _, binding = self.audit(args)
        self.assertEqual(binding["execution_cwd"], str(root.resolve(strict=False)))

    def test_final_front_is_filtered_in_original_seed_order_not_front_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, _ = _write_upstream_audit_fixture(Path(tmp), two_candidates=True)
            filtered, binding = self.audit(args)
            _, rows = workflow._strict_csv(filtered, "two-candidate filtered fixture plan")
            filtered_order: list[str] = []
            for row in rows:
                if row["candidate_id"] not in filtered_order:
                    filtered_order.append(row["candidate_id"])

        self.assertEqual(binding["original_seed_candidate_ids"], ["pareto_high", "pareto_low"])
        self.assertEqual(
            binding["fea_filtered_final_front_candidate_ids"],
            ["pareto_low", "pareto_high"],
        )
        self.assertEqual(binding["selected_candidate_ids"], ["pareto_high", "pareto_low"])
        self.assertEqual(filtered_order, ["pareto_high", "pareto_low"])

    def test_fea_dominated_seed_candidate_is_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, _ = _write_upstream_audit_fixture(
                Path(tmp),
                two_candidates=True,
                fea_filter_to_low=True,
            )
            filtered, binding = self.audit(args)
            _, rows = workflow._strict_csv(filtered, "subset filtered fixture plan")
            filtered_ids = {row["candidate_id"] for row in rows}

        self.assertEqual(binding["original_seed_candidate_ids"], ["pareto_high", "pareto_low"])
        self.assertEqual(binding["fea_filtered_final_front_candidate_ids"], ["pareto_low"])
        self.assertEqual(binding["selected_candidate_ids"], ["pareto_low"])
        self.assertEqual(filtered_ids, {"pareto_low"})

    def test_decision_summary_and_final_front_tamper_fail_closed(self) -> None:
        mutations = ("decision_status", "summary_bytes", "front_bytes", "front_ids")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                args, _ = _write_upstream_audit_fixture(Path(tmp))
                if mutation == "decision_status":
                    decision = json.loads(args.optimization_decision.read_text(encoding="utf-8"))
                    decision["status"] = "pareto_fea_started"
                    args.optimization_decision.write_bytes(coordinator._indented_json_bytes(decision))
                elif mutation == "summary_bytes":
                    args.pareto_validation_summary.write_bytes(
                        args.pareto_validation_summary.read_bytes() + b" "
                    )
                elif mutation == "front_bytes":
                    args.pareto_final_front.write_bytes(args.pareto_final_front.read_bytes() + b"\n")
                else:
                    decision = json.loads(args.optimization_decision.read_text(encoding="utf-8"))
                    decision["validation"]["final_front"]["candidate_ids"] = ["unknown"]
                    args.optimization_decision.write_bytes(coordinator._indented_json_bytes(decision))
                with self.assertRaises(coordinator.TargetLoadCoordinatorError):
                    self.audit(args)

    def test_coordinated_rehash_cannot_select_fea_dominated_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, fixture = _write_upstream_audit_fixture(
                root,
                two_candidates=True,
                fea_filter_to_low=True,
            )
            forged = json.loads(args.pareto_validation_summary.read_text(encoding="utf-8"))
            high = next(
                candidate
                for candidate in forged["candidates"]
                if candidate["candidate_id"] == "pareto_high"
            )
            forged_front = pareto_validator._final_front_row(fixture.spec, high)
            forged["fea_filtered_final_front"] = [forged_front]
            forged["fea_filtered_final_front_candidate_ids"] = ["pareto_high"]
            forged["fea_filtered_final_front_count"] = 1
            unsigned = dict(forged)
            unsigned.pop("validation_id")
            forged_id = pareto_validator.canonical_hash(
                "ipmsm-pareto-fea-validation",
                unsigned,
            )
            forged["validation_id"] = forged_id
            args.pareto_validation_summary.write_bytes(
                pareto_validator._json_text(forged).encode("utf-8")
            )
            args.pareto_final_front.write_bytes(
                pareto_validator._final_front_csv_text(
                    fixture.spec,
                    [forged_front],
                ).encode("utf-8")
            )
            rows_path = root / "pareto_fea_validation_rows.csv"
            _, forged_rows = read_fixture_csv(rows_path)
            for row in forged_rows:
                row["validation_id"] = forged_id
            rows_path.write_bytes(pareto_validator._row_csv_text(forged_rows).encode("utf-8"))

            decision = json.loads(args.optimization_decision.read_text(encoding="utf-8"))
            decision["validation"]["summary"]["sha256"] = hashlib.sha256(
                args.pareto_validation_summary.read_bytes()
            ).hexdigest()
            decision["validation"]["rows"]["sha256"] = hashlib.sha256(
                rows_path.read_bytes()
            ).hexdigest()
            decision["validation"]["final_front"].update(
                {
                    "sha256": hashlib.sha256(args.pareto_final_front.read_bytes()).hexdigest(),
                    "candidate_ids": ["pareto_high"],
                    "candidate_count": 1,
                }
            )
            decision["validation"]["validation_id"] = forged_id
            args.optimization_decision.write_bytes(coordinator._indented_json_bytes(decision))

            with self.assertRaisesRegex(
                coordinator.TargetLoadCoordinatorError,
                "independent strict validation",
            ):
                self.audit(args)

    def test_validator_argv_thresholds_and_candidate_bound_are_immutable(self) -> None:
        mutations = (
            "argv_order",
            "threshold_mismatch",
            "coordinated_threshold",
            "candidate_bound",
            "source_coverage",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                args, _ = _write_upstream_audit_fixture(
                    Path(tmp),
                    two_candidates=mutation == "candidate_bound",
                )
                decision = json.loads(args.optimization_decision.read_text(encoding="utf-8"))
                contract = decision["execution_contract"]
                validation = contract["validation"]
                if mutation == "argv_order":
                    validation["argv"][0] = "--pareto"
                elif mutation == "threshold_mismatch":
                    validation["minimum_coverage"] = 0.9
                elif mutation == "coordinated_threshold":
                    validation["minimum_coverage"] = 0.9
                    index = validation["argv"].index("--minimum-coverage") + 1
                    validation["argv"][index] = "0.9"
                else:
                    if mutation == "candidate_bound":
                        contract["optimization"]["max_fea_candidates"] = 1
                    else:
                        contract["source_sha256"].pop(next(iter(contract["source_sha256"])))
                decision["contract_sha256"] = coordinator.canonical_json_sha256(contract)
                args.optimization_decision.write_bytes(
                    coordinator._indented_json_bytes(decision)
                )
                with self.assertRaises(coordinator.TargetLoadCoordinatorError):
                    self.audit(args)

    def test_final_recheck_closes_toctou_for_every_bound_artifact_class(self) -> None:
        targets = (
            "optimization_decision",
            "pareto_validation_summary",
            "validation_rows",
            "pareto_final_front",
            "pareto_results",
            "optimization_spec",
            "pareto_csv",
            "seed_fea_plan",
            "model_metadata",
            "beta_calibration_manifest",
            "model_artifact",
        )
        original_recheck = coordinator._final_recheck_upstream_artifacts
        for target in targets:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                args, fixture = _write_upstream_audit_fixture(root)
                paths = {
                    "optimization_decision": args.optimization_decision,
                    "pareto_validation_summary": args.pareto_validation_summary,
                    "validation_rows": root / "pareto_fea_validation_rows.csv",
                    "pareto_final_front": args.pareto_final_front,
                    "pareto_results": fixture.results_path,
                    "optimization_spec": args.optimization_spec,
                    "pareto_csv": args.pareto_csv,
                    "seed_fea_plan": args.seed_fea_plan,
                    "model_metadata": args.model_metadata,
                    "beta_calibration_manifest": args.beta_calibration_manifest,
                    "model_artifact": fixture.artifact_paths[0],
                }
                target_path = paths[target]

                def mutate_then_recheck(artifacts):
                    target_path.write_bytes(target_path.read_bytes() + b"TOCTOU")
                    return original_recheck(artifacts)

                with mock.patch.object(
                    coordinator,
                    "_final_recheck_upstream_artifacts",
                    side_effect=mutate_then_recheck,
                ), self.assertRaisesRegex(
                    coordinator.TargetLoadCoordinatorError,
                    "changed during final audit",
                ):
                    self.audit(args)

    def test_root_and_progress_bind_decision_and_reject_rehashed_claim(self) -> None:
        first_kwargs = build_kwargs()
        second_kwargs = copy.deepcopy(first_kwargs)
        second_kwargs["upstream_pareto_binding"]["optimization_decision"]["sha256"] = "c" * 64
        first = workflow.build_root_manifest(**first_kwargs)  # type: ignore[arg-type]
        second = workflow.build_root_manifest(**second_kwargs)  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first_progress = coordinator.initialize_workspace(Path(first_tmp), first)
            with self.assertRaisesRegex(workflow.TargetLoadWorkflowError, "decision hash"):
                coordinator.initialize_workspace(Path(second_tmp), second)
        self.assertNotEqual(first["identity_sha256"], second["identity_sha256"])
        self.assertEqual(first_progress["identity_sha256"], first["identity_sha256"])

    def test_init_root_builder_consumes_only_final_front_without_workspace_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths, _ = _write_upstream_audit_fixture(
                root,
                two_candidates=True,
                fea_filter_to_low=True,
            )
            workspace = root / "must-not-be-created-by-root-builder"
            args = SimpleNamespace(
                **vars(paths),
                workspace=workspace,
                relative_tolerance=0.01,
                max_attempts=6,
                monotonic_relative_tolerance=0.005,
                minimum_step_relative=0.01,
                maximum_scale_per_attempt=1.5,
                project="PYAEDT_MOTOR_IPMSM_V2",
                project_id=2,
                project_active_cap=300,
                remote_root="$HOME/slurm_scheduler/projects/PYAEDT_MOTOR_IPMSM_V2/pyaedt_motor",
                env_setup="module load ansys-electronics/v252",
                max_workers_per_node=4,
                cpus=4,
                cores_per_process=4,
                memory_mb=32_768,
                task_timeout_seconds=43_200,
                scheduler_url="http://127.0.0.1:8000",
                scheduler_timeout=1.0,
                history_limit=10_000,
                task_retry_limit=2,
                result_settle_seconds=60,
                result_identity_relative_tolerance=1.0e-6,
            )
            sentinel = {"sentinel": True}
            injected_pyaedt_core = b"# exact injected pyaedt core source\n"
            runtime_paths = dict(workflow.RUNTIME_SOURCE_PATHS)
            runtime_paths["pyaedt_core_source"] = mock.Mock(
                **{"read_bytes.side_effect": AssertionError("fallback pyaedt path was read")}
            )
            snapshot = coordinator.SchedulerSnapshot((), 0, 0, 50)
            with mock.patch.object(
                coordinator.SchedulerClient,
                "snapshot",
                return_value=snapshot,
            ), mock.patch.object(
                workflow,
                "build_root_manifest",
                return_value=sentinel,
            ) as build, mock.patch.object(
                workflow,
                "RUNTIME_SOURCE_PATHS",
                runtime_paths,
            ):
                result = coordinator.build_root_from_files(
                    args,
                    pyaedt_core_source_bytes=injected_pyaedt_core,
                )
            call = build.call_args.kwargs
            _, filtered_rows = workflow._strict_csv(
                call["seed_fea_plan_csv"],
                "init filtered plan",
            )

        self.assertIs(result, sentinel)
        self.assertEqual({row["candidate_id"] for row in filtered_rows}, {"pareto_low"})
        self.assertEqual(
            call["upstream_pareto_binding"]["selected_candidate_ids"],
            ["pareto_low"],
        )
        self.assertEqual(call["scheduler_contract"]["server_cap"], 300)
        self.assertEqual(call["pyaedt_core_source"], injected_pyaedt_core)
        self.assertFalse(workspace.exists())

    def test_init_parser_requires_explicit_pyaedt_core_source(self) -> None:
        argv = [
            "init",
            "--workspace",
            "C:/target-load",
            "--optimization-decision",
            "C:/optimization-decision.json",
            "--optimization-spec",
            "C:/optimization-spec.json",
            "--pareto-csv",
            "C:/pareto.csv",
            "--seed-fea-plan",
            "C:/seed-plan.csv",
            "--pareto-validation-summary",
            "C:/validation.json",
            "--pareto-final-front",
            "C:/final-front.csv",
            "--model-metadata",
            "C:/metadata.json",
            "--model-artifact-dir",
            "C:/models",
            "--beta-calibration-manifest",
            "C:/beta.json",
        ]
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            coordinator.build_parser().parse_args(argv)
        parsed = coordinator.build_parser().parse_args(
            [*argv, "--pyaedt-core-source", "C:/pydesktop.py"]
        )
        self.assertEqual(parsed.pyaedt_core_source, Path("C:/pydesktop.py"))


class TargetLoadCoordinatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = root_manifest()

    def fresh_root(self) -> dict[str, Any]:
        return copy.deepcopy(self.root)

    def initialize(self, workspace: Path) -> tuple[dict[str, Any], str]:
        root = self.fresh_root()
        progress = coordinator.initialize_workspace(workspace, root)
        return root, str(root["identity"]["candidate_order"][0])

    def publish_fixed(self, workspace: Path, root: dict[str, Any], candidate_id: str) -> dict[str, Any]:
        evidence = fixed_mtpa_evidence(root, candidate_id)
        return coordinator.publish_fixed_mtpa_evidence(workspace, candidate_id, evidence)

    def complete_tail_attempts(
        self,
        workspace: Path,
        root: dict[str, Any],
        client: FakeSchedulerClient,
        output_ratios: Mapping[str, float],
    ) -> int:
        state = coordinator.replay_workspace(workspace)
        completed = 0
        for journal in state.probes:
            attempt = journal.tail_attempt
            if attempt is None:
                continue
            dedupe_key = str(attempt["dedupe_key"])
            task = max(
                (
                    item
                    for item in client.history
                    if item.get("dedupe_key") == dedupe_key
                ),
                key=lambda item: int(item["id"]),
            )
            role = str(journal.probe["beta_validation_role"])
            core_loss = 4.0 if role == "selected_center" else 20.0
            payload = result_csv(
                result_row_for_attempt(
                    root,
                    attempt,
                    output_ratio=output_ratios[role],
                    core_loss_w=core_loss,
                )
            )
            task_spec = coordinator.build_scheduler_task(root, attempt)
            task.update(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "finished_at": (NOW - timedelta(seconds=61)).isoformat(),
                }
            )
            client.results[(int(task["id"]), task_spec.result_csv)] = payload
            completed += 1
        return completed

    def test_initialize_replay_and_root_frozen_progress_match_dashboard_v1(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, _ = self.initialize(workspace)

            state = coordinator.replay_workspace(workspace)
            parsed = dashboard._read_target_load_progress(workspace / "progress.json")

        self.assertEqual(state.root, root)
        self.assertEqual(state.root_manifest_sha256, workflow.canonical_json_sha256(root))
        self.assertEqual(len(state.probes), len(root["probes"]))
        self.assertTrue(all(not journal.attempts for journal in state.probes))
        self.assertEqual(parsed["integrity_status"], "verified")
        self.assertEqual(parsed["status"], "root_frozen")
        self.assertEqual(parsed["counts"]["candidates_total"], 1)
        self.assertEqual(parsed["counts"]["probes_pending"], len(root["probes"]))
        self.assertEqual(parsed["counts"]["probes_total"], len(root["probes"]))
        self.assertEqual(parsed["root_manifest_sha256"], workflow.canonical_json_sha256(root))
        self.assertEqual(parsed["identity_sha256"], root["identity_sha256"])

    def test_one_cycle_strict_loads_unchanged_root_only_once(self) -> None:
        root = self.fresh_root()
        workflow._ROOT_VALIDATION_CACHE.clear()
        original_loader = workflow._validated_surrogate_bundle_documents
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            workflow,
            "_validated_surrogate_bundle_documents",
            wraps=original_loader,
        ) as loader:
            workspace = Path(temporary) / "target-load-v4"
            coordinator.initialize_workspace(workspace, root)
            coordinator.advance_workspace_once(
                workspace,
                FakeSchedulerClient(),
                submit=False,
                now=NOW,
            )
        self.assertEqual(loader.call_count, 1)

    def test_immutable_publication_is_idempotent_and_replay_rejects_root_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, _ = self.initialize(workspace)
            marker = workspace / "immutable.json"
            self.assertTrue(coordinator.publish_immutable_json(marker, {"value": 1}))
            self.assertFalse(coordinator.publish_immutable_json(marker, {"value": 1}))
            with self.assertRaisesRegex(coordinator.TargetLoadCoordinatorError, "differs"):
                coordinator.publish_immutable_json(marker, {"value": 2})

            tampered = copy.deepcopy(root)
            tampered["status"] = "tampered-after-publication"
            (workspace / "root.manifest.json").write_bytes(
                coordinator.canonical_json_bytes(tampered)
            )
            with self.assertRaises(workflow.TargetLoadWorkflowError):
                coordinator.replay_workspace(workspace)

    def test_managed_paths_reject_escape_and_reparse_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            self.initialize(workspace)
            outside = Path(temporary) / "outside.json"
            outside.write_text("untouched", encoding="utf-8")
            with self.assertRaisesRegex(coordinator.TargetLoadCoordinatorError, "escapes"):
                coordinator._guard_workspace_path(workspace, outside)

            simulated = workspace / "simulated-reparse"
            simulated.mkdir()
            original_detector = coordinator._path_is_link_or_reparse

            def detector(path: Path) -> bool:
                return path == simulated or original_detector(path)

            with mock.patch.object(
                coordinator,
                "_path_is_link_or_reparse",
                side_effect=detector,
            ), self.assertRaisesRegex(
                coordinator.TargetLoadCoordinatorError,
                "symlink/reparse",
            ):
                coordinator.publish_immutable_json(simulated / "escape.json", {"bad": True})
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_posix_symlink_escape_is_rejected_before_read_or_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "target-load-v4"
            self.initialize(workspace)
            outside = root / "outside.json"
            outside.write_text("untouched", encoding="utf-8")
            progress = workspace / "progress.json"
            progress.unlink()
            progress.symlink_to(outside)
            with self.assertRaisesRegex(
                coordinator.TargetLoadCoordinatorError,
                "symlink/reparse",
            ):
                coordinator.replace_progress(progress, {"bad": True})
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(
                coordinator.TargetLoadCoordinatorError,
                "symlink/reparse",
            ):
                coordinator.initialize_workspace(linked_parent / "escaped-workspace", self.fresh_root())

    def test_hardlinked_lock_and_authority_files_cannot_alias_outside_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "lock-workspace"
            workspace.mkdir()
            outside_lock = root / "outside-lock.bin"
            outside_lock.write_bytes(b"")
            try:
                os.link(outside_lock, workspace / ".coordinator.lock")
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")
            with self.assertRaisesRegex(
                coordinator.TargetLoadCoordinatorError,
                "single-link",
            ):
                with coordinator.workspace_lock(workspace):
                    pass
            self.assertEqual(outside_lock.read_bytes(), b"")

            managed = root / "authority-workspace"
            manifest = self.fresh_root()
            coordinator.initialize_workspace(managed, manifest)
            root_path = managed / "root.manifest.json"
            payload = root_path.read_bytes()
            root_path.unlink()
            outside_root = root / "outside-root.json"
            outside_root.write_bytes(payload)
            os.link(outside_root, root_path)
            with self.assertRaisesRegex(
                coordinator.TargetLoadCoordinatorError,
                "single-link",
            ):
                coordinator.publish_immutable_bytes(root_path, payload)
            self.assertEqual(outside_root.read_bytes(), payload)

    def test_fixed_mtpa_envelope_is_revalidated_on_replay_and_updates_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            evidence = fixed_mtpa_evidence(root, candidate_id)
            envelope = coordinator.publish_fixed_mtpa_evidence(
                workspace,
                candidate_id,
                evidence,
            )

            expected_receipt = workflow.validate_fixed_current_mtpa_evidence(
                root,
                candidate_id,
                evidence,
            )
            state = coordinator.replay_workspace(workspace)
            parsed = dashboard._read_target_load_progress(workspace / "progress.json")

        self.assertEqual(envelope["schema_version"], coordinator.FIXED_ENVELOPE_SCHEMA_VERSION)
        self.assertEqual(envelope["receipt"], expected_receipt)
        self.assertEqual(state.fixed_evidence[candidate_id], envelope)
        self.assertEqual(parsed["status"], "running")
        self.assertEqual(parsed["counts"]["fixed_mtpa_validated"], 1)
        self.assertEqual(parsed["counts"]["probes_pending"], len(root["probes"]))

    def test_attempt_and_observation_crash_windows_recover_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            state = coordinator.replay_workspace(workspace)
            journal = state.probes[0]
            attempt = dict(journal.decision["attempt"])

            # Crash window 1: attempt was journaled, but scheduler dispatch was not.
            coordinator._publish_attempt(workspace, state, attempt)
            recovered_tail = coordinator.replay_workspace(workspace).probes[0]
            self.assertEqual(recovered_tail.tail_attempt, attempt)
            self.assertEqual(recovered_tail.observations, ())

            client = FakeSchedulerClient()
            cycle = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            self.assertEqual(cycle["submitted"], 1)
            self.assertEqual(len(client.posts), 1)
            self.assertEqual(client.posts[0]["payload"]["dedupe_key"], attempt["dedupe_key"])

            expected_attempt, expected_observation, payload = issue_observation(
                root,
                str(attempt["probe_id"]),
                [],
                output_ratio=1.0,
            )
            self.assertEqual(expected_attempt, attempt)
            task = client.history[0]
            task.update(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "finished_at": (NOW - timedelta(seconds=61)).isoformat(),
                }
            )
            state = coordinator.replay_workspace(workspace)

            # Crash window 2: the atomic collection envelope was durable, while
            # its derived CSV and observation cache were not.
            coordinator.publish_collection_envelope(
                workspace,
                state,
                attempt,
                payload,
                task,
                NOW,
            )
            with self.assertRaisesRegex(
                coordinator.TargetLoadCoordinatorError,
                "awaits CSV recovery",
            ):
                coordinator.replay_workspace(workspace, repair=False)
            repaired = coordinator.replay_workspace(workspace, repair=True)
            repaired_journal = repaired.probes[0]

            self.assertEqual(repaired_journal.observations, (expected_observation,))
            self.assertEqual(repaired_journal.decision["terminal_status"], "matched")
            self.assertEqual(
                coordinator.replay_workspace(workspace).probes[0].observations,
                (expected_observation,),
            )

    def test_result_without_complete_dispatch_provenance_is_not_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            state = coordinator.replay_workspace(workspace)
            journal = state.probes[0]
            attempt, _, payload = issue_observation(
                root,
                str(journal.probe["probe_id"]),
                [],
                output_ratio=1.0,
            )
            coordinator._publish_attempt(workspace, state, attempt)
            state = coordinator.replay_workspace(workspace)
            coordinator.publish_dispatch_intent(workspace, state, attempt, 0, NOW)
            probe_dir = coordinator._probe_dir(workspace, str(attempt["probe_id"]))
            coordinator.publish_immutable_bytes(
                probe_dir / "results" / "0001.csv",
                payload,
            )

            with self.assertRaisesRegex(
                coordinator.TargetLoadCoordinatorError,
                "without a collection envelope",
            ):
                coordinator.replay_workspace(workspace, repair=True)

    def test_dispatch_receipt_is_recovered_from_same_dedupe_history_without_repost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            state = coordinator.replay_workspace(workspace)
            attempt = dict(state.probes[0].decision["attempt"])
            coordinator._publish_attempt(workspace, state, attempt)
            state = coordinator.replay_workspace(workspace)
            coordinator.publish_dispatch_intent(workspace, state, attempt, 0, NOW)

            client = FakeSchedulerClient(cap=1)
            task_spec = coordinator.build_scheduler_task(root, attempt)
            accepted = client.inject_task(task_spec.payload)
            cycle = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            intents, receipts = coordinator.load_dispatch_records(
                workspace,
                state.root,
                attempt,
            )

        self.assertEqual(cycle["submitted"], 0)
        self.assertEqual(client.posts, [])
        self.assertEqual(len(intents), 1)
        self.assertEqual(receipts[0]["scheduler_task_id"], accepted["id"])
        self.assertTrue(receipts[0]["recovered_from_history"])

    def test_scheduler_cap_defers_creation_and_retry_reuses_frozen_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            client = FakeSchedulerClient(cap=100, unrelated_active=100)

            capped = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            self.assertEqual(capped["submitted"], 0)
            self.assertEqual(client.posts, [])
            self.assertTrue(
                all(not journal.attempts for journal in coordinator.replay_workspace(workspace).probes)
            )

            client.unrelated_active = 0
            first = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            self.assertEqual(first["submitted"], 1)
            state = coordinator.replay_workspace(workspace)
            attempt = next(journal.tail_attempt for journal in state.probes if journal.tail_attempt)
            dedupe_key = str(attempt["dedupe_key"])
            first_task_id = int(client.history[0]["id"])
            client.set_status(first_task_id, "failed", exit_code=1)

            retried = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            self.assertEqual(retried["submitted"], 1)
            self.assertEqual(len(client.posts), 2)
            self.assertEqual(
                [post["payload"]["dedupe_key"] for post in client.posts],
                [dedupe_key, dedupe_key],
            )
            self.assertEqual(retried["progress"]["scheduler_counts"]["queued"], 1)
            self.assertEqual(retried["progress"]["scheduler_counts"]["failed"], 0)
            state = coordinator.replay_workspace(workspace)
            intents, receipts = coordinator.load_dispatch_records(
                workspace,
                state.root,
                attempt,
            )
            fresh_snapshot = client.snapshot(root["identity"]["scheduler_contract"])
            progress = coordinator.build_progress(
                state,
                fresh_snapshot.history,
                NOW,
                workspace=workspace,
            )

        self.assertEqual([intent["retry_index"] for intent in intents], [0, 1])
        self.assertTrue(all(receipt is not None for receipt in receipts))
        self.assertEqual(progress["scheduler_counts"]["queued"], 1)
        self.assertEqual(progress["scheduler_counts"]["failed"], 0)

    def test_retry_exhaustion_and_persisted_failure_never_post_a_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            self.assertEqual(root["identity"]["task_retry_limit"], 2)
            client = FakeSchedulerClient()

            initial = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            self.assertEqual(initial["submitted"], 1)
            attempt = next(
                journal.tail_attempt
                for journal in coordinator.replay_workspace(workspace).probes
                if journal.tail_attempt is not None
            )
            dedupe_key = str(attempt["dedupe_key"])

            for expected_post_count in (2, 3):
                client.set_status(int(client.history[-1]["id"]), "failed", exit_code=1)
                retried = coordinator.advance_workspace_once(
                    workspace,
                    client,
                    submit=True,
                    max_submissions=1,
                    now=NOW,
                )
                self.assertEqual(retried["submitted"], 1)
                self.assertEqual(len(client.posts), expected_post_count)

            client.set_status(int(client.history[-1]["id"]), "failed", exit_code=1)
            exhausted = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            posts_at_exhaustion = len(client.posts)
            state = coordinator.replay_workspace(workspace)

            self.assertEqual(exhausted["submitted"], 0)
            self.assertEqual(exhausted["status"], "failed")
            self.assertTrue(
                any(
                    action.get("action") == "failed:retry_exhausted"
                    for action in exhausted["actions"]
                )
            )
            self.assertEqual(posts_at_exhaustion, 3)
            self.assertEqual(
                [post["payload"]["dedupe_key"] for post in client.posts],
                [dedupe_key, dedupe_key, dedupe_key],
            )
            self.assertEqual(sum(len(journal.attempts) for journal in state.probes), 1)
            self.assertEqual(state.failures[0]["code"], "scheduler_retry_exhausted")

            persisted = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            self.assertEqual(persisted["submitted"], 0)
            self.assertEqual(persisted["status"], "failed")
            self.assertEqual(len(client.posts), posts_at_exhaustion)
            self.assertEqual(
                sum(
                    len(journal.attempts)
                    for journal in coordinator.replay_workspace(workspace).probes
                ),
                1,
            )

    def test_observed_old_dedupe_with_new_active_task_fails_closed_without_post(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            client = FakeSchedulerClient()

            first = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            self.assertEqual(first["submitted"], 1)
            attempt = next(
                journal.tail_attempt
                for journal in coordinator.replay_workspace(workspace).probes
                if journal.tail_attempt is not None
            )
            expected_attempt, expected_observation, payload = issue_observation(
                root,
                str(attempt["probe_id"]),
                [],
                output_ratio=0.5,
            )
            self.assertEqual(expected_attempt, attempt)
            task = client.history[0]
            task.update(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "finished_at": (NOW - timedelta(seconds=61)).isoformat(),
                }
            )
            task_spec = coordinator.build_scheduler_task(root, attempt)
            client.results[(int(task["id"]), task_spec.result_csv)] = payload

            collected = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=False,
                max_submissions=0,
                now=NOW,
            )
            self.assertEqual(collected["submitted"], 0)
            observed = next(
                journal
                for journal in coordinator.replay_workspace(workspace).probes
                if journal.probe["probe_id"] == attempt["probe_id"]
            )
            self.assertEqual(observed.observations, (expected_observation,))

            posts_before_injection = len(client.posts)
            client.clone_task(task, status="queued")
            with self.assertRaisesRegex(
                coordinator.TargetLoadCoordinatorError,
                "scheduler task exists without a prior dispatch intent|"
                "scheduler task exists after the observed successful attempt|"
                "observed attempt unexpectedly has an active scheduler task",
            ):
                coordinator.advance_workspace_once(
                    workspace,
                    client,
                    submit=True,
                    max_submissions=1,
                    now=NOW,
                )
            self.assertEqual(len(client.posts), posts_before_injection)

    def test_wrong_scheduler_post_identity_fields_are_rejected(self) -> None:
        corruptions = {
            "project": "NOT_THE_FROZEN_PROJECT",
            "dedupe_key": "forged-dedupe-key",
            "cpus": 999_999,
        }
        for field, bad_value in corruptions.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary) / "target-load-v4"
                root, candidate_id = self.initialize(workspace)
                self.publish_fixed(workspace, root, candidate_id)
                client = FakeSchedulerClient(response_overrides={field: bad_value})

                with self.assertRaisesRegex(
                    coordinator.TargetLoadCoordinatorError,
                    rf"scheduler task {field} differs",
                ):
                    coordinator.advance_workspace_once(
                        workspace,
                        client,
                        submit=True,
                        max_submissions=1,
                        now=NOW,
                    )
                self.assertEqual(len(client.posts), 1)

    def test_wrong_scheduler_history_identity_fields_are_rejected_without_repost(self) -> None:
        corruptions = {
            "project": "NOT_THE_FROZEN_PROJECT",
            "dedupe_key": "forged-dedupe-key",
            "cpus": 999_999,
        }
        for field, bad_value in corruptions.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary) / "target-load-v4"
                root, candidate_id = self.initialize(workspace)
                self.publish_fixed(workspace, root, candidate_id)
                client = FakeSchedulerClient()
                first = coordinator.advance_workspace_once(
                    workspace,
                    client,
                    submit=True,
                    max_submissions=1,
                    now=NOW,
                )
                self.assertEqual(first["submitted"], 1)
                client.history[0][field] = bad_value

                with self.assertRaises(coordinator.TargetLoadCoordinatorError):
                    coordinator.advance_workspace_once(
                        workspace,
                        client,
                        submit=True,
                        max_submissions=1,
                        now=NOW,
                    )
                self.assertEqual(len(client.posts), 1)

    def test_foreign_active_dry_run_and_single_slot_virtual_plan_match_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            client = FakeSchedulerClient()
            client.history.append(
                {
                    "id": 10_000,
                    "status": "queued",
                    "project": root["identity"]["scheduler_contract"]["project"],
                    "dedupe_key": "foreign-campaign-active-task",
                }
            )
            client._next_id = 10_001

            blocked_plan = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=False,
                max_submissions=1,
                now=NOW,
            )
            self.assertEqual(blocked_plan["submitted"], 0)
            self.assertEqual(client.posts, [])
            self.assertTrue(
                any(
                    action.get("action") == "deferred:foreign_project_tasks_active"
                    for action in blocked_plan["actions"]
                )
            )
            self.assertFalse(
                any(action.get("action") == "would_submit" for action in blocked_plan["actions"])
            )

            client.history[0].update({"status": "completed", "exit_code": 0})
            virtual_plan = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=False,
                max_submissions=1,
                now=NOW,
            )
            planned = [
                action
                for action in virtual_plan["actions"]
                if action.get("action") == "would_submit"
            ]
            self.assertEqual(len(planned), 1)
            self.assertEqual(client.posts, [])
            self.assertTrue(
                all(
                    not journal.attempts
                    for journal in coordinator.replay_workspace(workspace).probes
                )
            )

            actual = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            submitted = [
                action for action in actual["actions"] if action.get("action") == "submitted"
            ]
            self.assertEqual(actual["submitted"], 1)
            self.assertEqual(len(submitted), 1)
            self.assertEqual(len(client.posts), 1)
            self.assertEqual(submitted[0]["probe_id"], planned[0]["probe_id"])
            self.assertEqual(submitted[0]["attempt_id"], planned[0]["attempt_id"])

    def test_empty_result_requires_three_persisted_checks_over_ten_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            client = FakeSchedulerClient(cap=1)
            first = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            self.assertEqual(first["submitted"], 1)
            state = coordinator.replay_workspace(workspace)
            attempt = next(journal.tail_attempt for journal in state.probes if journal.tail_attempt)
            task = client.history[0]
            task.update(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "finished_at": (NOW - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            result_path = coordinator.build_scheduler_task(root, attempt).result_csv
            client.results[(int(task["id"]), result_path)] = b""

            pending_submissions: list[int] = []
            for offset in (0, 300):
                pending = coordinator.advance_workspace_once(
                    workspace,
                    client,
                    submit=True,
                    max_submissions=1,
                    now=NOW + timedelta(seconds=offset),
                )
                self.assertEqual(pending["status"], "running")
                pending_submissions.append(int(pending["submitted"]))

            failed = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW + timedelta(seconds=600),
            )
            replayed = coordinator.replay_workspace(workspace)

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["submitted"], 0)
        self.assertEqual(pending_submissions, [1, 0])
        self.assertEqual(
            sum(post["payload"]["dedupe_key"] == attempt["dedupe_key"] for post in client.posts),
            1,
        )
        self.assertEqual(replayed.failures[0]["code"], "result_visibility_timeout")

    def test_transport_fetch_error_remains_pending_and_never_fails_science(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            client = FakeSchedulerClient(cap=1)
            coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW,
            )
            task = client.history[0]
            task.update(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "finished_at": (NOW - timedelta(days=1)).isoformat(),
                }
            )
            pending = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=1,
                now=NOW + timedelta(days=1),
            )
            replayed = coordinator.replay_workspace(workspace)

        self.assertEqual(pending["status"], "running")
        self.assertEqual(pending["submitted"], 1)
        original_dedupe = client.posts[0]["payload"]["dedupe_key"]
        self.assertEqual(
            sum(post["payload"]["dedupe_key"] == original_dedupe for post in client.posts),
            1,
        )
        self.assertEqual(replayed.failures, ())

    def test_all_probes_finalize_candidate_and_publish_dashboard_valid_complete_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "target-load-v4"
            root, candidate_id = self.initialize(workspace)
            self.publish_fixed(workspace, root, candidate_id)
            client = FakeSchedulerClient()

            first_wave = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=6,
                now=NOW,
            )
            self.assertEqual(first_wave["submitted"], 6)
            self.assertEqual(
                self.complete_tail_attempts(
                    workspace,
                    root,
                    client,
                    {
                        "selected_center": 0.8,
                        "local_lower": 0.7,
                        "local_upper": 0.6,
                    },
                ),
                6,
            )

            second_wave = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=6,
                now=NOW,
            )
            self.assertEqual(second_wave["submitted"], 6)
            self.assertEqual(
                self.complete_tail_attempts(
                    workspace,
                    root,
                    client,
                    {
                        "selected_center": 1.0,
                        "local_lower": 1.0,
                        "local_upper": 1.0,
                    },
                ),
                6,
            )

            final = coordinator.advance_workspace_once(
                workspace,
                client,
                submit=True,
                max_submissions=6,
                now=NOW,
            )
            parsed = dashboard._read_target_load_progress(workspace / "progress.json")
            state = coordinator.replay_workspace(workspace)
            summary = state.summaries[candidate_id]

        self.assertEqual(final["status"], "complete")
        self.assertEqual(final["submitted"], 0)
        self.assertEqual(parsed["status"], "complete")
        self.assertEqual(parsed["counts"]["probes_matched"], 6)
        self.assertEqual(parsed["counts"]["attempts_issued"], 12)
        self.assertEqual(parsed["counts"]["attempts_active"], 0)
        self.assertEqual(parsed["counts"]["observations_validated"], 12)
        self.assertEqual(parsed["counts"]["candidates_finalized"], 1)
        self.assertEqual(parsed["counts"]["fixed_mtpa_validated"], 1)
        self.assertEqual(len(parsed["candidate_summaries"]), 1)
        self.assertEqual(parsed["scheduler_counts"]["completed"], 12)
        self.assertEqual(
            parsed["candidate_summaries"][0]["summary_sha256"],
            summary["summary_sha256"],
        )
        self.assertGreater(summary["objective_cycle_efficiency"], 0.0)
        self.assertLessEqual(summary["objective_cycle_efficiency"], 1.0)


if __name__ == "__main__":
    unittest.main()
