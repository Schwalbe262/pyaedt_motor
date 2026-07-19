from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

import generate_ipmsm_v2_cases as foundation
from ipmsm_optimization import optimization_spec_from_mapping
import recover_ipmsm_v2_adaptive_failed_geometry as recovery


def spec_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "operating_points": [
            {
                "name": "rated_torque",
                "speed_rpm": 1200,
                "target_torque_nm": 65.1,
                "duty_weight": 0.5,
            },
            {
                "name": "rated_power_at_max_speed",
                "speed_rpm": 5000,
                "target_power_w": 7500,
                "duty_weight": 0.5,
            },
        ],
        "stack_length_bounds_mm": [40, 70],
        "inverter": {"vdc_v": 200, "phase_peak_current_limit_a": 137.8},
        "winding": {
            "series_turns_per_phase": 48,
            "turns_per_coil_side": 12,
            "coils_per_phase": 4,
            "parallel_branches": 1,
            "strand_area_mm2": 0.5,
            "strands_per_turn": 4,
            "fill_factor": 0.8,
            "end_turn_factor": 1.2,
            "overhang_mm": 5,
        },
        "constraints": {"current_density_limit_a_per_mm2": 20},
        "beta_calibration": {
            "electrical_zero_deg": 12.5,
            "calibration_id": "fixture-calibration",
            "convention": "dq_current_advance_v2",
        },
    }


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_json(path: Path, value: object) -> None:
    write_bytes(path, recovery._canonical_json_bytes(value))


def fixture_rows(spec):
    raw = foundation.generate_foundation_rows(
        spec,
        geometry_count=51,
        samples_per_operating_point=3,
        repeat_count=0,
        seed=1701,
        quality_profile="reference_ultra",
        electrical_zero_deg=12.5,
        case_prefix="recovery-fixture-pool",
    )
    groups = foundation._rows_by_design_hash(raw)
    selected_groups = list(groups.items())[:50]
    candidate_hash, candidate_rows = list(groups.items())[50]
    rows = []
    for group_index, (design_hash, group_rows) in enumerate(selected_groups, start=1):
        split = "train" if group_index <= 40 else "calibration"
        local_by_point: dict[str, int] = {}
        group_id = (
            f"fixture_batch_0001_{split}_geometry_{group_index:04d}_"
            f"{design_hash[:12]}"
        )
        for source in group_rows:
            point = str(source["operating_point_id"])
            local_by_point[point] = local_by_point.get(point, 0) + 1
            row = dict(source)
            row["case_id"] = (
                f"fixture_batch_0001_{split}_{group_index:04d}_{point}_"
                f"{local_by_point[point]:02d}"
            )
            row["geometry_group_id"] = group_id
            row["doe_split"] = split
            row["repeat_of_case_id"] = ""
            rows.append(row)
    payload = foundation._stage3_csv_bytes(rows, spec)
    fieldnames, string_rows = recovery._csv_from_payload(payload, "fixture adaptive plan")
    return fieldnames, string_rows, candidate_hash, candidate_rows, payload


def summary_contract() -> dict[str, object]:
    return {
        "cross_split_design_overlap": 0,
        "geometry_groups": 50,
        "prior_or_confirmed_design_overlap": 0,
        "repeats": 0,
        "rows": 300,
        "split_groups": {"train": 40, "calibration": 10, "test": 0},
        "split_rows": {"train": 240, "calibration": 60, "test": 0},
    }


class RecoveryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = optimization_spec_from_mapping(spec_mapping())
        (
            self.fieldnames,
            self.rows,
            self.candidate_hash,
            self.candidate_rows,
            self.plan_payload,
        ) = fixture_rows(self.spec)
        self.failed_hash = str(self.rows[24]["design_hash"])
        self.failed_rows = [row for row in self.rows if row["design_hash"] == self.failed_hash]
        self.failed_case_ids = tuple(row["case_id"] for row in self.failed_rows)
        self.failed_group = str(self.failed_rows[0]["geometry_group_id"])
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.spec_path = self.root / "spec.json"
        self.plan_path = self.root / "original.csv"
        self.manifest_path = self.root / "original_manifest.json"
        write_json(self.spec_path, spec_mapping())
        write_bytes(self.plan_path, self.plan_payload)
        write_json(self.manifest_path, {"fixture": True})
        self.manifest = {
            "execution_contract_sha256": "e" * 64,
            "schema_version": "ipmsm_v2_adaptive_enrichment_batch_v1",
            "selection": {
                "adaptation": {
                    "candidate_pool": {
                        "geometry_count": 1024,
                        "pool_sha256": "a" * 64,
                        "signals_sha256": "b" * 64,
                        "required_invalid_derived_prediction_geometry_count": 2,
                    },
                    "scoring": {
                        "diversity_weight": 0.2,
                        "residual_weight": 0.5,
                        "uncertainty_weight": 0.3,
                        "domain_distance_weight": 0.2,
                    },
                    "selected": [
                        {
                            "design_hash": design_hash,
                            "invalid_derived_prediction_signal": 0.0,
                        }
                        for design_hash in list(
                            dict.fromkeys(row["design_hash"] for row in self.rows[:240])
                        )
                    ],
                },
                "calibration": {
                    "design_hashes": list(
                        dict.fromkeys(row["design_hash"] for row in self.rows[240:])
                    )
                },
                "candidate_pool_geometries": 1024,
                "seed_policy": {
                    "adaptation_seed": 730131,
                    "adaptation_seed_base": 730031,
                    "calibration_seed": 730133,
                    "calibration_seed_base": 730033,
                    "formula": "role_seed_base + 100 * batch_index",
                    "stride": 100,
                },
            },
            "summary": summary_contract(),
        }
        self.original = recovery.OriginalAuthority(
            spec=self.spec,
            spec_snapshot=recovery._snapshot(self.spec_path, "fixture spec"),
            plan_snapshot=recovery._snapshot(self.plan_path, "fixture plan"),
            manifest_snapshot=recovery._snapshot(self.manifest_path, "fixture manifest"),
            fieldnames=tuple(self.fieldnames),
            rows=tuple(self.rows),
            manifest=self.manifest,
            excluded_design_hashes=frozenset(),
            adaptive_evidence={},
            evidence_snapshots=(),
        )
        dummy = recovery._snapshot(self.manifest_path, "dummy")
        self.terminal = recovery.TerminalAuthority(
            summary_snapshot=dummy,
            decision_snapshot=dummy,
            selected_plan_snapshot=self.original.plan_snapshot,
            successful_plan_snapshot=dummy,
            merged_snapshot=dummy,
            failed_design_hash=self.failed_hash,
            failed_geometry_group_id=self.failed_group,
            failed_case_ids=self.failed_case_ids,
            failure_results=(),
            evidence_snapshots=(),
        )
        self.candidate = recovery.CandidateSelection(
            design_hash=self.candidate_hash,
            pool_ordinal=578,
            acquisition_score=0.72,
            diversity_score=0.28,
            final_selection_score=0.63,
            selection_constraint="adaptive_score",
            signals={
                "domain_distance_signal": 0.1,
                "invalid_derived_prediction_signal": 0.0,
                "residual_signal": 0.2,
                "uncertainty_component_rank": 0.3,
                "uncertainty_signal": 0.01,
            },
            rows=tuple(self.candidate_rows),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()


class ReplacementRowTests(RecoveryFixture):
    def test_replaces_only_six_rows_with_pool_controls_and_fresh_case_ids(self) -> None:
        result = recovery.build_replacement_rows(
            self.original, self.terminal, self.candidate
        )
        failed = set(self.failed_case_ids)
        self.assertEqual(len(result.rows), 300)
        self.assertEqual(len(result.case_id_map), 6)
        mapping = dict(result.case_id_map)
        self.assertEqual(
            list(mapping.values()), [f"{case_id}_clean_retry_01" for case_id in self.failed_case_ids]
        )
        changed = 0
        for before, after in zip(self.rows, result.rows, strict=True):
            if before["case_id"] not in failed:
                self.assertEqual(after, before)
            else:
                changed += 1
                self.assertEqual(after["case_id"], mapping[before["case_id"]])
                self.assertEqual(after["design_hash"], self.candidate_hash)
                self.assertEqual(after["repeat_of_case_id"], "")
        self.assertEqual(changed, 6)
        replacement = [row for row in result.rows if row["design_hash"] == self.candidate_hash]
        self.assertEqual(
            [(float(row["i_peak_a"]), float(row["beta_dq_deg"])) for row in replacement],
            [
                (float(row["i_peak_a"]), float(row["beta_dq_deg"]))
                for row in self.candidate_rows
            ],
        )
        self.assertNotIn(self.failed_hash, {row["design_hash"] for row in result.rows})
        self.assertTrue(result.replacement_geometry_group_id.endswith(self.candidate_hash[:12]))

    def test_rejects_clean_retry_case_id_collision(self) -> None:
        rows = [dict(row) for row in self.original.rows]
        rows[-1]["case_id"] = f"{self.failed_case_ids[0]}_clean_retry_01"
        changed = recovery.OriginalAuthority(
            **{**self.original.__dict__, "rows": tuple(rows)}
        )
        with self.assertRaisesRegex(recovery.AdaptiveRecoveryError, "collide"):
            recovery.build_replacement_rows(changed, self.terminal, self.candidate)

    def test_rejects_candidate_without_all_operating_controls(self) -> None:
        candidate = recovery.CandidateSelection(
            **{**self.candidate.__dict__, "rows": self.candidate.rows[:-1]}
        )
        with self.assertRaisesRegex(recovery.AdaptiveRecoveryError, "consume|lacks"):
            recovery.build_replacement_rows(self.original, self.terminal, candidate)


class GreedyCandidateTests(unittest.TestCase):
    @staticmethod
    def candidate(acquisition: float, vector: tuple[float, ...], invalid: float = 0.0):
        return {
            "acquisition_score": acquisition,
            "geometry_vector": vector,
            "invalid_derived_prediction_signal": invalid,
        }

    def test_uses_retained_set_and_deterministic_final_score(self) -> None:
        candidates = {
            "retained": self.candidate(0.0, (0.0,)),
            "a": self.candidate(0.7, (0.2,)),
            "b": self.candidate(0.6, (1.0,)),
        }
        design_hash, diversity, score, constraint = recovery._choose_next_candidate(
            candidates,
            retained_design_hashes=["retained"],
            banned_design_hashes=[],
            required_invalid_derived=0,
        )
        self.assertEqual(design_hash, "b")
        self.assertEqual(diversity, 1.0)
        self.assertAlmostEqual(score, 0.68)
        self.assertEqual(constraint, "adaptive_score")

    def test_reserves_replacement_for_missing_invalid_coverage(self) -> None:
        candidates = {
            "retained": self.candidate(0.0, (0.0,)),
            "finite": self.candidate(1.0, (1.0,)),
            "invalid": self.candidate(0.1, (0.1,), invalid=1.0),
        }
        result = recovery._choose_next_candidate(
            candidates,
            retained_design_hashes=["retained"],
            banned_design_hashes=[],
            required_invalid_derived=1,
        )
        self.assertEqual(result[0], "invalid")
        self.assertEqual(result[3], "invalid_derived_minimum_coverage")

    def test_hash_breaks_exact_score_ties(self) -> None:
        candidates = {
            "retained": self.candidate(0.0, (0.0,)),
            "b": self.candidate(0.5, (1.0,)),
            "a": self.candidate(0.5, (1.0,)),
        }
        result = recovery._choose_next_candidate(
            candidates,
            retained_design_hashes=["retained"],
            banned_design_hashes=[],
            required_invalid_derived=0,
        )
        self.assertEqual(result[0], "a")


class SchedulerIdentityTests(unittest.TestCase):
    def argv(self):
        return [
            "--cases",
            "cases.csv",
            "--scheduler-url",
            "http://127.0.0.1:8002",
            "--project",
            "PYAEDT_MOTOR_IPMSM_V2",
            "--project-active-cap",
            "300",
            "--task-prefix",
            "adaptive-b1",
            "--remote-cases-dir",
            "remote/cases/b1",
            "--result-dir",
            "simul_log/results/b1",
            "--simulation-dir",
            "simulation/b1",
            "--log-dir",
            "scheduler_logs/b1",
            "--output-dir",
            "output",
            "--submit",
        ]

    def test_extracts_exact_scheduler_identity(self) -> None:
        value = recovery._scheduler_identity_from_argv(self.argv())
        self.assertEqual(set(value), recovery.SCHEDULER_IDENTITY_FIELDS)
        self.assertEqual(value["project_active_cap"], 300)
        self.assertEqual(value["task_prefix"], "adaptive-b1")

    def test_rejects_cap_change(self) -> None:
        argv = self.argv()
        argv[argv.index("300")] = "299"
        with self.assertRaisesRegex(recovery.AdaptiveRecoveryError, "cap=300"):
            recovery._scheduler_identity_from_argv(argv)

    def test_rejects_nested_remote_roots(self) -> None:
        argv = self.argv()
        argv[argv.index("simul_log/results/b1")] = "remote/cases/b1/results"
        with self.assertRaisesRegex(recovery.AdaptiveRecoveryError, "non-nested"):
            recovery._scheduler_identity_from_argv(argv)


class TerminalAuthorityTests(RecoveryFixture):
    def make_terminal_campaign(self):
        output_dir = self.root / "terminal"
        output_dir.mkdir()
        selected = output_dir / "selected_cases.csv"
        successful = output_dir / "successful_cases.csv"
        merged = output_dir / "merged_results.csv"
        write_bytes(selected, self.plan_payload)
        failed = set(self.failed_case_ids)
        successful_rows = [row for row in self.rows if row["case_id"] not in failed]
        write_bytes(successful, foundation._stage3_csv_bytes(successful_rows, self.spec))
        merged_payload = recovery.geometry_replacement._render_csv(
            ["case_id", "status"],
            [{"case_id": row["case_id"], "status": "ok"} for row in successful_rows],
        )
        write_bytes(merged, merged_payload)
        failed_dir = output_dir / "failed_results"
        failed_dir.mkdir()
        permanent = []
        for case_index, case_id in enumerate(self.failed_case_ids):
            evidence = []
            for retry_index in (0, 1):
                result_path = failed_dir / f"failed-{case_index}-{retry_index}.csv"
                payload = f"case_id,status\n{case_id},failed\n".encode()
                write_bytes(result_path, payload)
                evidence.append(
                    {
                        "kind": "result_level_terminal",
                        "retry_index": retry_index,
                        "task_id": 1000 + case_index * 2 + retry_index,
                        "dedupe_key": f"dedupe-{case_index}-{retry_index}",
                        "scheduler_status": "completed",
                        "result_status": "failed",
                        "remote_result": f"remote/{case_id}-{retry_index}.csv",
                        "local_result": str(result_path.resolve()),
                        "local_result_sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
            permanent.append(
                {"case_id": case_id, "attempts": 2, "failure_evidence": evidence}
            )
        cases = [
            {
                "case_id": row["case_id"],
                "outcome": "permanent_failure" if row["case_id"] in failed else "success",
            }
            for row in self.rows
        ]
        summary = {
            "schema_version": recovery.collector.CAMPAIGN_SUMMARY_SCHEMA_VERSION,
            "status": "completed_with_permanent_failures",
            "project": "PYAEDT_MOTOR_IPMSM_V2",
            "history_rows": 306,
            "history_campaign_tasks": 306,
            "selected_cases": 300,
            "successful_cases": 294,
            "permanently_failed_cases": 6,
            "selected_plan": str(selected.resolve()),
            "successful_plan": str(successful.resolve()),
            "merged_output": str(merged.resolve()),
            "output_dir": str(output_dir.resolve()),
            "cases": cases,
            "permanent_failures": permanent,
        }
        summary_path = output_dir / "campaign_summary.json"
        write_json(summary_path, summary)
        summary_sha = hashlib.sha256(summary_path.read_bytes()).hexdigest()
        decision = {
            "schema_version": recovery.collector.CAMPAIGN_DECISION_SCHEMA_VERSION,
            "status": "completed_with_permanent_failures",
            "selected_cases": 300,
            "successful_cases": 294,
            "permanently_failed_cases": 6,
            "summary": {"path": str(summary_path.resolve()), "sha256": summary_sha},
            "permanent_failures": permanent,
        }
        decision_path = output_dir / "campaign_decision.json"
        write_json(decision_path, decision)
        continuation_authority = recovery.ContinuationAuthority(
            snapshot=self.original.manifest_snapshot,
            scheduler_identity={
                "scheduler_url": "http://127.0.0.1:8002",
                "project": "PYAEDT_MOTOR_IPMSM_V2",
                "project_active_cap": 300,
                "task_prefix": "adaptive-b1",
                "remote_cases_dir": "remote/cases/b1",
                "result_dir": "results/b1",
                "simulation_dir": "simulation/b1",
                "log_dir": "logs/b1",
            },
            output_dir=output_dir.resolve(),
        )
        return summary_path, decision_path, continuation_authority, permanent

    def test_accepts_exact_294_plus_six_terminal_contract(self) -> None:
        summary, decision, authority, _ = self.make_terminal_campaign()
        result = recovery.validate_terminal_authority(
            summary,
            decision,
            self.original,
            authority,
            failed_design_hash=self.failed_hash,
        )
        self.assertEqual(result.failed_case_ids, self.failed_case_ids)
        self.assertEqual(len(result.failure_results), 12)
        self.assertEqual(result.failed_geometry_group_id, self.failed_group)

    def test_rejects_changed_failed_result_bytes(self) -> None:
        summary, decision, authority, permanent = self.make_terminal_campaign()
        Path(permanent[0]["failure_evidence"][0]["local_result"]).write_text("changed")
        with self.assertRaisesRegex(recovery.AdaptiveRecoveryError, "SHA-256"):
            recovery.validate_terminal_authority(
                summary,
                decision,
                self.original,
                authority,
                failed_design_hash=self.failed_hash,
            )

    def test_rejects_wrong_failed_design_hash(self) -> None:
        summary, decision, authority, _ = self.make_terminal_campaign()
        other = next(row["design_hash"] for row in self.rows if row["design_hash"] != self.failed_hash)
        with self.assertRaisesRegex(recovery.AdaptiveRecoveryError, "six-row geometry"):
            recovery.validate_terminal_authority(
                summary,
                decision,
                self.original,
                authority,
                failed_design_hash=other,
            )


class ManifestAndPublishTests(RecoveryFixture):
    def continuation_authority(self):
        return recovery.ContinuationAuthority(
            snapshot=self.original.manifest_snapshot,
            scheduler_identity={
                "scheduler_url": "http://127.0.0.1:8002",
                "project": "PYAEDT_MOTOR_IPMSM_V2",
                "project_active_cap": 300,
                "task_prefix": "adaptive-b1",
                "remote_cases_dir": "remote/cases/b1",
                "result_dir": "results/b1",
                "simulation_dir": "simulation/b1",
                "log_dir": "logs/b1",
            },
            output_dir=(self.root / "terminal").resolve(),
        )

    def test_manifest_has_exact_schema_contract_hash_and_mapping(self) -> None:
        rows = recovery.build_replacement_rows(self.original, self.terminal, self.candidate)
        output = self.root / "recovery.csv"
        manifest_output = self.root / "recovery_manifest.json"
        manifest = recovery.build_recovery_manifest(
            self.original,
            self.continuation_authority(),
            self.terminal,
            self.candidate,
            rows,
            output=output,
            manifest_output=manifest_output,
            mode="dry-run",
        )
        self.assertEqual(set(manifest), {"schema_version", "mode", "status", "contract", "contract_sha256", "checks"})
        self.assertEqual(manifest["schema_version"], recovery.SCHEMA_VERSION)
        self.assertEqual(set(manifest["contract"]), recovery.RECOVERY_CONTRACT_FIELDS)
        self.assertEqual(
            manifest["contract_sha256"], recovery._canonical_sha256(manifest["contract"])
        )
        mapping = manifest["contract"]["replacement"]["case_id_map"]
        self.assertEqual(len(mapping), 6)
        self.assertTrue(all(item["replacement"].endswith("_clean_retry_01") for item in mapping))

    def test_main_is_dry_run_then_no_replace_execute(self) -> None:
        inputs = []
        for name in (
            "spec",
            "plan",
            "manifest",
            "continuation",
            "summary",
            "decision",
        ):
            path = self.root / f"{name}.input"
            write_bytes(path, name.encode())
            inputs.append(path)
        output = self.root / "published" / "recovery.csv"
        manifest_output = self.root / "published" / "recovery.json"
        argv = [
            "--spec",
            str(inputs[0]),
            "--original-plan",
            str(inputs[1]),
            "--original-manifest",
            str(inputs[2]),
            "--continuation-decision",
            str(inputs[3]),
            "--campaign-summary",
            str(inputs[4]),
            "--campaign-decision",
            str(inputs[5]),
            "--failed-design-hash",
            "f" * 64,
            "--output",
            str(output),
            "--manifest-output",
            str(manifest_output),
        ]

        def fake_build(**kwargs):
            contract = {
                "output": {"plan": {"path": str(output), "sha256": hashlib.sha256(b"plan").hexdigest()}},
            }
            manifest = {
                "schema_version": recovery.SCHEMA_VERSION,
                "mode": kwargs["mode"],
                "status": "created" if kwargs["mode"] == "execute" else "validated",
                "contract": contract,
                "contract_sha256": recovery._canonical_sha256(contract),
                "checks": {},
            }
            return recovery.RecoveryBuild(b"plan", manifest, ())

        with mock.patch.object(recovery, "build_recovery", side_effect=fake_build):
            stdout = __import__("io").StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(recovery.main(argv), 0)
            self.assertFalse(output.exists())
            self.assertFalse(manifest_output.exists())
            with redirect_stdout(__import__("io").StringIO()):
                self.assertEqual(recovery.main([*argv, "--execute"]), 0)
            self.assertEqual(output.read_bytes(), b"plan")
            published = json.loads(manifest_output.read_text(encoding="utf-8"))
            self.assertEqual(published["mode"], "execute")
            with self.assertRaisesRegex(recovery.AdaptiveRecoveryError, "refusing to overwrite"):
                recovery.main([*argv, "--execute"])


class ContinuationAuthorityTests(RecoveryFixture):
    def make_decision(self, *, plan_sha: str | None = None):
        decision_path = self.root / "continuation.json"
        output_dir = self.root / "terminal-output"
        plan_record = {
            "path": str(self.plan_path.resolve()),
            "sha256": plan_sha or self.original.plan_snapshot.sha256,
        }
        manifest_record = self.original.manifest_snapshot.artifact()
        argv = [
            "--cases",
            str(self.plan_path.resolve()),
            "--scheduler-url",
            "http://127.0.0.1:8002",
            "--project",
            "PYAEDT_MOTOR_IPMSM_V2",
            "--project-active-cap",
            "300",
            "--task-prefix",
            "adaptive-b1",
            "--remote-cases-dir",
            "remote/cases/b1",
            "--result-dir",
            "results/b1",
            "--simulation-dir",
            "simulation/b1",
            "--log-dir",
            "logs/b1",
            "--output-dir",
            str(output_dir.resolve()),
            "--submit",
        ]
        execution = {
            "stage2": {
                "case_plan": plan_record,
                "case_manifest": manifest_record,
                "output_dir": str(output_dir.resolve()),
                "runner_argv": argv,
            }
        }
        value = {
            "schema_version": recovery.continuation.SCHEMA_VERSION,
            "contract_sha256": recovery.continuation._contract_sha256(execution),
            "decision": "run_stage2",
            "decision_output": str(decision_path.resolve()),
            "execution_contract": execution,
            "mode": "execute",
            "owner": {"hostname": socket.gethostname(), "pid": os.getpid() + 1000000},
            "stage2": {
                "case_plan": str(self.plan_path.resolve()),
                "case_plan_sha256": plan_record["sha256"],
                "case_manifest": str(self.original.manifest_snapshot.path),
                "case_manifest_sha256": self.original.manifest_snapshot.sha256,
                "output_dir": str(output_dir.resolve()),
                "runner_argv": argv,
            },
            "status": "stage2_started",
        }
        write_json(decision_path, value)
        return decision_path

    def test_binds_scheduler_identity_from_stage2_started_decision(self) -> None:
        result = recovery.validate_continuation_authority(
            self.make_decision(), self.original
        )
        self.assertEqual(result.scheduler_identity["task_prefix"], "adaptive-b1")
        self.assertEqual(result.scheduler_identity["project_active_cap"], 300)

    def test_rejects_original_plan_hash_change(self) -> None:
        with self.assertRaisesRegex(recovery.AdaptiveRecoveryError, "original adaptive pair"):
            recovery.validate_continuation_authority(
                self.make_decision(plan_sha="0" * 64), self.original
            )


if __name__ == "__main__":
    unittest.main()
