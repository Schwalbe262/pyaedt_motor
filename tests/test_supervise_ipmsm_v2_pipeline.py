from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import continue_ipmsm_v2_stage2 as stage2_continuation
import supervise_ipmsm_v2_pipeline as supervisor


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fieldnames or list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["python"], returncode, stdout, stderr)


def gate(decision: str = "run_stage2") -> stage2_continuation.GateResult:
    failures = () if decision == "skip_stage2" else ("output_torque_last_avg_nm",)
    return stage2_continuation.GateResult(
        decision=decision,
        validation={},
        primary_test_r2={"output_torque_last_avg_nm": 0.96 if not failures else 0.91},
        primary_failures=failures,
        voltage_test_r2=0.96,
        voltage_failed=False,
        fingerprints={},
    )


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.input = root / "immutable.txt"
        self.input.write_text("fixed\n", encoding="utf-8")
        self.contract_path = root / "pipeline.json"
        py = sys.executable
        self.pipeline: dict[str, object] = {
            "workdir": ".",
            "lock_path": "pipeline.lock",
            "immutable_inputs": [{"path": "immutable.txt", "sha256": sha256(self.input)}],
            "stage1": {
                "case_plan": "stage1.csv",
                "output_dir": "stage1-out",
                "result": "stage1-out/merged_results.csv",
                "validation": "stage1-validation.csv",
                "model_dir": "stage1-models",
                "metadata": "stage1-models/metadata.json",
                "r2": "stage1-r2.csv",
                "expected_rows": 2,
                "expected_groups": 1,
                "expected_repeats": 1,
                "r2_threshold": 0.95,
                "ensemble_size": 5,
                "conformal_coverage": 0.95,
                "campaign_argv": [
                    py, "run_ipmsm_v2_campaign.py", "--cases", "stage1.csv",
                    "--output-dir", "stage1-out", "--submit",
                ],
                "validation_argv": [
                    py, "validate_ipmsm_v2_dataset.py", "--data", "stage1-out/merged_results.csv",
                    "--summary", "stage1-validation.csv",
                ],
                "training_argv": [
                    py, "train_ipmsm_lightgbm.py", "--v2", "--data", "stage1-out/merged_results.csv",
                    "--model-dir", "stage1-models", "--verification-output", "stage1-r2.csv",
                ],
            },
            "stage2": {
                "decision": "stage2-decision.json",
                "argv": [py, "continue_ipmsm_v2_stage2.py", "--decision-output", "stage2-decision.json"],
            },
            "stage3": {
                "prior_plan": "stage12.csv",
                "prior_manifest": "stage12.manifest.json",
                "plan": "stage3.csv",
                "manifest": "stage3.manifest.json",
                "decision": "stage3-decision.json",
                "expected_rows": 2,
                "merge_argv": [
                    py, "merge_ipmsm_v2_case_plans.py", "--output", "stage12.csv",
                    "--manifest-output", "stage12.manifest.json",
                ],
                "generate_argv": [
                    py, "generate_ipmsm_v2_cases.py", "--stage3-fallback", "--output", "stage3.csv",
                    "--stage3-manifest-output", "stage3.manifest.json",
                    "--stage2-failed-decision", "stage2-decision.json",
                ],
                "continuation_argv": [py, "continue_ipmsm_v2_stage2.py", "--decision-output", "stage3-decision.json"],
            },
            "optimization": {
                "decision": "optimization-decision.json",
                "argv_template": [
                    py,
                    "continue_ipmsm_v2_optimization.py",
                    "--stage2-decision",
                    supervisor.UPSTREAM_PLACEHOLDER,
                    "--decision-output",
                    "optimization-decision.json",
                ],
            },
            "speed": {
                "plan": "speed.csv",
                "output_dir": "speed-out",
                "result": "speed-out/merged_results.csv",
                "rank": "speed-rank.csv",
                "top": "speed-top.csv",
                "marker": "speed-complete.json",
                "expected_rows": 2,
                "minimum_top_profiles": 2,
                "plan_argv": [py, "generate_ipmsm_second_pass_cases.py", "--output", "speed.csv"],
                "campaign_argv": [
                    py, "run_ipmsm_v2_campaign.py", "--cases", "speed.csv",
                    "--output-dir", "speed-out", "--submit",
                ],
                "rank_argv": [
                    py, "rank_ipmsm_second_pass_profiles.py", "--strict-speed-plan", "speed.csv",
                    "--strict-candidate-results", "speed-out/merged_results.csv",
                    "--output", "speed-rank.csv", "--top-profiles-output", "speed-top.csv",
                ],
            },
        }
        write_csv(
            root / "stage1.csv",
            [
                {"case_id": "a", "design_hash": "d1"},
                {"case_id": "b", "design_hash": "d1"},
            ],
        )
        self.write_contract()

    def write_contract(self) -> None:
        payload = {
            "schema_version": supervisor.CONTRACT_SCHEMA_VERSION,
            "pipeline": self.pipeline,
        }
        payload["contract_sha256"] = supervisor._canonical_sha256(payload)
        self.contract_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def load(self) -> supervisor.PipelineContract:
        return supervisor.load_contract(self.contract_path)

    def stage1_campaign(self) -> None:
        write_csv(
            self.root / "stage1-out" / "merged_results.csv",
            [
                {"case_id": "a", "status": "ok", "design_hash": "d1"},
                {"case_id": "b", "status": "ok", "design_hash": "d1"},
            ],
        )

    def validation(self) -> None:
        write_csv(
            self.root / "stage1-validation.csv",
            [{
                "rows": "2", "ok_rows": "2", "unique_case_ids": "2",
                "unique_geometry_groups": "1", "repeat_pairs": "1", "failures": "0",
                "status": "pass", "issues": "",
            }],
        )

    def training(self) -> None:
        self.validation()
        model_dir = self.root / "stage1-models"
        model_dir.mkdir(exist_ok=True)
        (model_dir / "metadata.json").write_text("{}\n", encoding="utf-8")
        write_csv(self.root / "stage1-r2.csv", [{"target": "x", "test_r2": "0.96"}])

    def decision(self, name: str, schema: str, status: str) -> Path:
        path = self.root / name
        execution_contract: dict[str, object] = {}
        payload = {
            "schema_version": schema,
            "mode": "execute",
            "status": status,
            "decision_output": str(path.resolve()),
            "execution_contract": execution_contract,
            "contract_sha256": supervisor._canonical_sha256(execution_contract),
        }
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    def merge_pair(self) -> None:
        plan = self.root / "stage12.csv"
        write_csv(plan, [{"case_id": "a", "design_hash": "d1"}])
        manifest = {
            "schema_version": supervisor.MERGE_MANIFEST_SCHEMA_VERSION,
            "mode": "execute",
            "output": {
                "path": str(plan.resolve()),
                "sha256": sha256(plan),
                "rows": 1,
                "design_hashes": 1,
            },
        }
        (self.root / "stage12.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def stage3_pair(self) -> None:
        plan = self.root / "stage3.csv"
        write_csv(
            plan,
            [
                {"case_id": "s3a", "design_hash": "d3"},
                {"case_id": "s3b", "design_hash": "d3"},
            ],
        )
        manifest = {
            "schema_version": supervisor.STAGE3_MANIFEST_SCHEMA_VERSION,
            "mode": "write",
            "case_plan": str(plan.resolve()),
            "case_plan_sha256": sha256(plan),
            "summary": {"rows": 2},
        }
        (self.root / "stage3.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def speed_plan(self) -> None:
        write_csv(
            self.root / "speed.csv",
            [
                {"case_id": "q1", "design_hash": "sd1"},
                {"case_id": "q2", "design_hash": "sd1"},
            ],
        )

    def speed_result(self) -> None:
        self.speed_plan()
        write_csv(
            self.root / "speed-out" / "merged_results.csv",
            [
                {"case_id": "q1", "status": "ok", "design_hash": "sd1"},
                {"case_id": "q2", "status": "ok", "design_hash": "sd1"},
            ],
        )

    def speed_rank(self) -> None:
        self.speed_result()
        write_csv(self.root / "speed-rank.csv", [{"quality_profile": "fast", "recommended_rank": "1"}])
        (self.root / "speed-top.csv").write_text("reference_ultra,fast\n", encoding="utf-8")


class SupervisorContractTests(unittest.TestCase):
    def test_contract_is_hash_bound_typed_and_rejects_wrong_child_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            tampered = json.loads(fixture.contract_path.read_text(encoding="utf-8"))
            tampered["pipeline"]["lock_path"] = "changed.lock"
            fixture.contract_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(supervisor.PipelineContractError, "contract_sha256"):
                fixture.load()

            fixture.pipeline["lock_path"] = "pipeline.lock"
            fixture.write_contract()
            stage1 = fixture.pipeline["stage1"]
            assert isinstance(stage1, dict)
            stage1["campaign_argv"] = [sys.executable, "not_the_campaign.py"]
            fixture.write_contract()
            with self.assertRaisesRegex(supervisor.PipelineContractError, "run_ipmsm_v2_campaign"):
                fixture.load()

            stage1["campaign_argv"] = [
                sys.executable,
                "run_ipmsm_v2_campaign.py",
                "--cases",
                "stage1.csv",
                "--output-dir",
                "wrong-output",
                "--submit",
            ]
            fixture.write_contract()
            with self.assertRaisesRegex(supervisor.PipelineContractError, "artifact path"):
                fixture.load()

    def test_default_dry_run_is_read_only_and_selects_stage1_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            stdout = io.StringIO()
            with mock.patch.object(supervisor, "run_child") as child, contextlib.redirect_stdout(stdout):
                code = supervisor.main(["--contract", str(fixture.contract_path)])
            report = json.loads(stdout.getvalue())

            self.assertEqual(code, 0)
            self.assertEqual(report["next_action"], "run_stage1_campaign")
            self.assertEqual(report["writes_performed"], 0)
            child.assert_not_called()
            self.assertFalse((fixture.root / "pipeline.lock").exists())

            fixture.input.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(supervisor.PipelineStateError, "immutable input hash changed"):
                supervisor.inspect_pipeline(fixture.load())

    def test_stage1_validation_training_and_partial_states_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.stage1_campaign()
            contract = fixture.load()
            self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_stage1_validation")

            fixture.validation()
            self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_stage1_training")

            (fixture.root / "stage1-models").mkdir()
            with self.assertRaisesRegex(supervisor.PipelineStateError, "partial Stage1 training"):
                supervisor.inspect_pipeline(contract)

    def test_live_external_pid_blocks_execute_without_lock_or_child_and_prior_boot_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            pid_file = fixture.root / "foundation_stage1_runner.pid"
            pid_file.write_text("12345\n", encoding="utf-8")
            fixture.pipeline["external_pid_files"] = [
                {"role": "stage1_runner", "path": pid_file.name}
            ]
            fixture.write_contract()
            modified = pid_file.stat().st_mtime
            stdout = io.StringIO()
            with (
                mock.patch.object(supervisor, "_boot_time_epoch", return_value=modified - 100.0),
                mock.patch.object(supervisor.stage2_continuation, "pid_is_running", return_value=True),
                mock.patch.object(supervisor, "run_child") as child,
                contextlib.redirect_stdout(stdout),
            ):
                code = supervisor.main(
                    ["--contract", str(fixture.contract_path), "--execute"]
                )
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(report["next_action"], "wait_external_process")
            self.assertEqual(report["detail"]["active_external_processes"][0]["pid"], 12345)
            child.assert_not_called()
            self.assertFalse((fixture.root / "pipeline.lock").exists())

            with (
                mock.patch.object(supervisor, "_boot_time_epoch", return_value=modified + 100.0),
                mock.patch.object(supervisor.stage2_continuation, "pid_is_running") as running,
            ):
                snapshot = supervisor.inspect_pipeline(fixture.load())
            self.assertEqual(snapshot.next_action, "run_stage1_campaign")
            running.assert_not_called()

    def test_stage2_fresh_and_resume_are_selected_from_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.stage1_campaign()
            fixture.training()
            contract = fixture.load()
            with mock.patch.object(supervisor, "_audit_stage1_training", return_value=gate()):
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_stage2_fresh")
                fixture.decision(
                    "stage2-decision.json", supervisor.STAGE2_DECISION_SCHEMA_VERSION, "stage2_started"
                )
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_stage2_resume")

    def test_stage2_failure_routes_through_stage3_and_only_complete_reaches_optimization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.stage1_campaign()
            fixture.training()
            contract = fixture.load()
            fixture.decision(
                "stage2-decision.json",
                supervisor.STAGE2_DECISION_SCHEMA_VERSION,
                "combined_r2_failed",
            )
            with mock.patch.object(supervisor, "_audit_stage1_training", return_value=gate()):
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "merge_stage12_plan")
                fixture.merge_pair()
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "generate_stage3_plan")
                fixture.stage3_pair()
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_stage3_fresh")
                fixture.decision(
                    "stage3-decision.json",
                    supervisor.STAGE2_DECISION_SCHEMA_VERSION,
                    "stage2_started",
                )
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_stage3_resume")
                fixture.decision(
                    "stage3-decision.json",
                    supervisor.STAGE2_DECISION_SCHEMA_VERSION,
                    "combined_r2_failed",
                )
                failed = supervisor.inspect_pipeline(contract)
                self.assertTrue(failed.terminal)
                self.assertEqual(failed.next_action, "blocked_stage3_r2_failed")

                fixture.decision(
                    "stage3-decision.json", supervisor.STAGE2_DECISION_SCHEMA_VERSION, "complete"
                )
                ready = supervisor.inspect_pipeline(contract)
                self.assertEqual(ready.next_action, "run_optimization_fresh")
                self.assertEqual(ready.upstream_decision, fixture.root / "stage3-decision.json")

    def test_optimization_resume_and_speed_require_complete_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.stage1_campaign()
            fixture.training()
            fixture.decision("stage2-decision.json", supervisor.STAGE2_DECISION_SCHEMA_VERSION, "complete")
            contract = fixture.load()
            with mock.patch.object(supervisor, "_audit_stage1_training", return_value=gate("skip_stage2")):
                fixture.decision(
                    "optimization-decision.json",
                    supervisor.OPTIMIZATION_DECISION_SCHEMA_VERSION,
                    "optimization_started",
                )
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_optimization_resume")
                fixture.decision(
                    "optimization-decision.json",
                    supervisor.OPTIMIZATION_DECISION_SCHEMA_VERSION,
                    "failed",
                )
                with self.assertRaisesRegex(supervisor.PipelineStateError, "not resumable"):
                    supervisor.inspect_pipeline(contract)
                fixture.decision(
                    "optimization-decision.json",
                    supervisor.OPTIMIZATION_DECISION_SCHEMA_VERSION,
                    "complete",
                )
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_speed_plan")

    def test_speed_steps_fail_closed_on_partial_and_commit_exact_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.stage1_campaign()
            fixture.training()
            fixture.decision("stage2-decision.json", supervisor.STAGE2_DECISION_SCHEMA_VERSION, "complete")
            fixture.decision(
                "optimization-decision.json", supervisor.OPTIMIZATION_DECISION_SCHEMA_VERSION, "complete"
            )
            contract = fixture.load()
            with mock.patch.object(supervisor, "_audit_stage1_training", return_value=gate("skip_stage2")):
                fixture.speed_plan()
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_speed_campaign")
                fixture.speed_result()
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "run_speed_rank")
                (fixture.root / "speed-rank.csv").write_text("partial\n", encoding="utf-8")
                with self.assertRaisesRegex(supervisor.PipelineStateError, "partial speed rank"):
                    supervisor.inspect_pipeline(contract)
                (fixture.root / "speed-rank.csv").unlink()
                fixture.speed_rank()
                self.assertEqual(supervisor.inspect_pipeline(contract).next_action, "commit_speed_completion")
                supervisor.execute_action(
                    contract,
                    supervisor.PipelineSnapshot(
                        "commit_speed_completion",
                        "stage2_complete",
                        upstream_decision=fixture.root / "stage2-decision.json",
                    ),
                )
                done = supervisor.inspect_pipeline(contract)
                self.assertTrue(done.terminal)
                self.assertEqual(done.next_action, "complete")

    def test_resume_child_argv_is_exact_and_shell_is_never_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            contract = fixture.load()
            responses = [
                completed(stdout='{"status":"stage2_started"}\n'),
                completed(returncode=1),
            ]
            with mock.patch.object(supervisor.subprocess, "run", side_effect=responses) as run:
                supervisor.execute_action(
                    contract,
                    supervisor.PipelineSnapshot("run_stage2_resume", "stage2"),
                )
            dry_argv = run.call_args_list[0].args[0]
            execute_argv = run.call_args_list[1].args[0]
            self.assertEqual(dry_argv[-1], "--resume")
            self.assertEqual(execute_argv[-2:], ["--resume", "--execute"])
            self.assertIs(run.call_args_list[0].kwargs["shell"], False)
            self.assertIs(run.call_args_list[1].kwargs["shell"], False)

            upstream = fixture.root / "stage3-decision.json"
            responses = [
                completed(stdout='{"status":"pareto_fea_started"}\n'),
                completed(),
            ]
            with mock.patch.object(supervisor.subprocess, "run", side_effect=responses) as run:
                supervisor.execute_action(
                    contract,
                    supervisor.PipelineSnapshot(
                        "run_optimization_resume", "stage3_complete", upstream_decision=upstream
                    ),
                )
            dry_argv = run.call_args_list[0].args[0]
            self.assertIn(str(upstream), dry_argv)
            self.assertNotIn(supervisor.UPSTREAM_PLACEHOLDER, dry_argv)
            self.assertEqual(dry_argv[-1], "--resume")

    def test_stage1_r2_gate_failure_is_a_valid_training_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            contract = fixture.load()
            with mock.patch.object(
                supervisor.subprocess,
                "run",
                return_value=completed(returncode=1),
            ) as run:
                supervisor.execute_action(
                    contract,
                    supervisor.PipelineSnapshot("run_stage1_training", "stage1"),
                )

            self.assertIs(run.call_args.kwargs["shell"], False)

    def test_duplicate_execution_lock_is_rejected_and_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.lock"
            with supervisor.ExecutionLock(path):
                with self.assertRaisesRegex(supervisor.PipelineStateError, "another pipeline supervisor"):
                    with supervisor.ExecutionLock(path):
                        pass
            with supervisor.ExecutionLock(path):
                pass


if __name__ == "__main__":
    unittest.main()
