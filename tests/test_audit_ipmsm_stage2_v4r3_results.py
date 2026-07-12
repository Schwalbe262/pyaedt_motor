from __future__ import annotations

import csv
from email.message import Message
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib import error, parse

import audit_ipmsm_stage2_v4r3_results as audit


def csv_bytes(fieldnames: list[str], rows: list[dict[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def plan_rows(count: int) -> list[dict[str, str]]:
    return [
        {
            "case_id": f"v2s2_test_{index:04d}",
            "design_hash": f"design-{index:04d}",
            "operation": "rated_torque",
            "quality_profile": "reference_ultra",
            "base_rpm": "1200",
            "i_peak_a": "10",
        }
        for index in range(1, count + 1)
    ]


def write_fixture(root: Path, *, count: int) -> tuple[Path, Path]:
    plan = root / "stage2.csv"
    rows = plan_rows(count)
    plan.write_bytes(csv_bytes(list(rows[0]), rows))
    output_dir = root / "old-stage2-output"
    argv = [
        "--cases",
        str(plan),
        "--project",
        audit.EXPECTED_POLICY["project"],
        "--project-active-cap",
        "100",
        "--scheduler-url",
        "http://scheduler.test",
        "--task-prefix",
        audit.EXPECTED_POLICY["task_prefix"],
        "--remote-cases-dir",
        audit.EXPECTED_POLICY["remote_cases_dir"],
        "--result-dir",
        audit.EXPECTED_POLICY["result_dir"],
        "--simulation-dir",
        audit.EXPECTED_POLICY["simulation_dir"],
        "--log-dir",
        audit.EXPECTED_POLICY["log_dir"],
        "--output-dir",
        str(output_dir),
        "--submit",
    ]
    decision = root / "decision.json"
    decision.write_text(
        json.dumps(
            {
                "schema_version": "ipmsm_v2_stage2_continuation_v1",
                "contract_sha256": "contract-sha",
                "decision": "run_stage2",
                "status": "stage2_started",
                "stage2": {
                    "case_plan": str(plan),
                    "case_plan_sha256": audit.sha256_bytes(plan.read_bytes()),
                    "output_dir": str(output_dir),
                    "runner_argv": argv,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return plan, decision


def scheduler_task(
    expected: object,
    task_id: int,
    status: str,
    *,
    exit_code: int | None,
    failure_message: str = "",
) -> dict[str, object]:
    payload = expected.payload
    return {
        "id": task_id,
        "name": expected.task_name,
        "status": status,
        "state": status,
        "project": payload["project"],
        "dedupe_key": expected.dedupe_key,
        "remote_cwd": "$HOME/project/pyaedt_motor",
        "entrypoint": payload["entrypoint"],
        "command": payload["command"],
        "env_setup": payload["env_setup"],
        "required_capability": payload["required_capability"],
        "env_profile": payload["env_profile"],
        "scheduling_profile": payload["scheduling_profile"],
        "max_workers_per_node": payload["max_workers_per_node"],
        "cpus": payload["cpus"],
        "memory_mb": payload["memory_mb"],
        "gpus": payload["gpus"],
        "gpu_model": payload["gpu_model"],
        "partition": payload["partition"],
        "node_name": payload["node_name"],
        "exclusive_node": payload["exclusive_node"],
        "priority": payload["priority"],
        "timeout_seconds": payload["timeout_seconds"],
        "requested_account_name": payload["account_name"],
        "account_name": "r1",
        "allocation_id": 12,
        "actual_node_name": "n001",
        "slurm_job_id": 555,
        "exit_code": exit_code,
        "failure_message": failure_message,
        "created_at": "2026-07-12T20:00:00+00:00",
        "started_at": "2026-07-12T20:01:00+00:00",
        "finished_at": "2026-07-12T20:10:00+00:00" if status not in audit.ACTIVE_STATUSES else None,
    }


def result_payload(
    plan_row: dict[str, str],
    *,
    row_status: str = "ok",
    contaminated_torque: bool = False,
    error_text: str = "",
) -> bytes:
    row: dict[str, object] = {
        "case_id": plan_row["case_id"],
        "status": row_status,
        "missing_required_outputs": "",
        "design_hash": plan_row["design_hash"],
        "input_design_hash": plan_row["design_hash"],
        "input_dataset_schema_version": "ipmsm_v2",
        "input_model_extent": "full_360",
        "input_symmetry_factor": "1",
        "input_use_periodic_boundary": "false",
        "input_beta_convention": "dq_current_advance_v2",
        "input_quality_profile": plan_row["quality_profile"],
        "input_setup_fingerprint": "setup-fingerprint",
        "input_material_fingerprint": "material-fingerprint",
        "input_aedt_version": "2025.2",
        "input_operation": plan_row["operation"],
        "input_base_rpm": plan_row["base_rpm"],
        "input_i_peak_a": plan_row["i_peak_a"],
        "output_phasea_voltage_last_rms_v": "10",
        "output_phaseb_voltage_last_rms_v": "10",
        "output_phasec_voltage_last_rms_v": "10",
        "output_phasea_current_last_rms_a": "10",
        "output_phaseb_current_last_rms_a": "10",
        "output_phasec_current_last_rms_a": "10",
        "output_mech_power_last_w": "2000" if contaminated_torque else "200",
        "output_total_loss_last_avg_w": "10",
        "output_torque_last_avg_nm": "1000" if contaminated_torque else "1",
        "error": error_text,
        "analysis_returned_false": "false",
    }
    return csv_bytes(list(row), [row])


class FakeClient:
    def __init__(
        self,
        attempts_by_name: dict[str, list[dict[str, object]]],
        results_by_task: dict[int, bytes | BaseException],
    ) -> None:
        self.attempts_by_name = attempts_by_name
        self.results_by_task = results_by_task
        self.query_urls: list[str] = []
        self.result_urls: list[str] = []
        self.rate_limit_retries = 0
        self.pace_seconds = 1.0
        self.backoff_seconds = 2.0
        self.max_backoff_seconds = 30.0
        self.max_429_retries = 5

    def get_json_list(self, url: str) -> list[dict[str, object]]:
        self.query_urls.append(url)
        query = parse.parse_qs(parse.urlparse(url).query)
        return list(self.attempts_by_name.get(query["name_prefix"][0], []))

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.result_urls.append(url)
        task_id = int(parse.urlparse(url).path.split("/")[3])
        value = self.results_by_task[task_id]
        if isinstance(value, BaseException):
            raise value
        if len(value) > max_bytes:
            raise RuntimeError("fixture result too large")
        return value


class Stage2V4R3AuditTests(unittest.TestCase):
    def evidence(self, root: Path, count: int) -> audit.CampaignEvidence:
        plan, decision = write_fixture(root, count=count)
        return audit.load_campaign_evidence(plan, decision, expected_rows=count)

    def test_dry_run_queries_every_exact_identity_and_fetches_only_scheduler_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 3)
            attempts = {
                evidence.tasks[0].task_name: [
                    scheduler_task(evidence.tasks[0], 101, "completed", exit_code=0)
                ],
                evidence.tasks[1].task_name: [
                    scheduler_task(evidence.tasks[1], 102, "running", exit_code=None)
                ],
                evidence.tasks[2].task_name: [
                    scheduler_task(
                        evidence.tasks[2],
                        103,
                        "failed",
                        exit_code=143,
                        failure_message="srun: task 0: Terminated",
                    )
                ],
            }
            client = FakeClient(
                attempts,
                {101: result_payload(evidence.rows[0])},
            )
            output = root / "audit-output"

            result = audit.audit_stage2(
                plan_path=evidence.plan_path,
                decision_path=evidence.decision_path,
                output_dir=output,
                scheduler_url="http://scheduler.test",
                publish=False,
                client=client,
                attempt_limit=20,
                max_result_bytes=1_000_000,
                expected_rows=3,
            )

            self.assertFalse(output.exists())
            self.assertEqual(result["publication"], "would_publish")
            self.assertEqual(result["summary"]["task_identity_queries"], 3)
            self.assertTrue(result["summary"]["coverage_complete"])
            self.assertEqual(result["summary"]["remote_result_fetches_this_run"], 1)
            self.assertEqual(
                result["summary"]["classifications"],
                {"physics_ok": 1, "retryable_infrastructure": 1, "running": 1},
            )
            self.assertEqual(len(client.query_urls), 3)
            self.assertEqual(len(client.result_urls), 1)
            for expected, url in zip(evidence.tasks, client.query_urls, strict=True):
                query = parse.parse_qs(parse.urlparse(url).query)
                self.assertEqual(
                    query,
                    {
                        "project": [audit.EXPECTED_POLICY["project"]],
                        "name_prefix": [expected.task_name],
                        "limit": ["20"],
                    },
                )
            result_query = parse.parse_qs(parse.urlparse(client.result_urls[0]).query)
            self.assertEqual(result_query["base"], [audit.REMOTE_FILE_BASE])
            self.assertEqual(result_query["path"], [evidence.tasks[0].result_csv])

    def test_current_physics_gate_flags_old_torque_unit_contamination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 1)
            task = scheduler_task(evidence.tasks[0], 201, "completed", exit_code=0)
            client = FakeClient(
                {evidence.tasks[0].task_name: [task]},
                {201: result_payload(evidence.rows[0], contaminated_torque=True)},
            )

            result = audit.audit_stage2(
                plan_path=evidence.plan_path,
                decision_path=evidence.decision_path,
                output_dir=root / "out",
                scheduler_url=None,
                publish=False,
                client=client,
                attempt_limit=20,
                max_result_bytes=1_000_000,
                expected_rows=1,
            )

            self.assertEqual(
                result["summary"]["classifications"],
                {"torque_unit_suspect": 1},
            )

    def test_completed_wrapper_with_infra_failure_row_is_classified_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 1)
            task = scheduler_task(evidence.tasks[0], 301, "completed", exit_code=0)
            client = FakeClient(
                {evidence.tasks[0].task_name: [task]},
                {
                    301: result_payload(
                        evidence.rows[0],
                        row_status="failed",
                        error_text="No module named 'ansys'",
                    )
                },
            )

            result = audit.audit_stage2(
                plan_path=evidence.plan_path,
                decision_path=evidence.decision_path,
                output_dir=root / "out",
                scheduler_url=None,
                publish=False,
                client=client,
                attempt_limit=20,
                max_result_bytes=1_000_000,
                expected_rows=1,
            )

            self.assertEqual(
                result["summary"]["classifications"],
                {"retryable_infrastructure_result": 1},
            )

    def test_publish_checkpoints_verified_result_and_resume_skips_its_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 2)
            first_task = scheduler_task(evidence.tasks[0], 401, "completed", exit_code=0)
            second_task = scheduler_task(evidence.tasks[1], 402, "completed", exit_code=0)
            attempts = {
                evidence.tasks[0].task_name: [first_task],
                evidence.tasks[1].task_name: [second_task],
            }
            output = root / "published"
            interrupted = FakeClient(
                attempts,
                {
                    401: result_payload(evidence.rows[0]),
                    402: KeyboardInterrupt(),
                },
            )

            with self.assertRaises(KeyboardInterrupt):
                audit.audit_stage2(
                    plan_path=evidence.plan_path,
                    decision_path=evidence.decision_path,
                    output_dir=output,
                    scheduler_url=None,
                    publish=True,
                    client=interrupted,
                    attempt_limit=20,
                    max_result_bytes=1_000_000,
                    expected_rows=2,
                )

            checkpoint = json.loads((output / audit.RECEIPT_NAME).read_text(encoding="utf-8"))
            self.assertEqual(list(checkpoint["audited_results"]), [evidence.tasks[0].case_id])
            resumed = FakeClient(
                attempts,
                {402: result_payload(evidence.rows[1])},
            )
            result = audit.audit_stage2(
                plan_path=evidence.plan_path,
                decision_path=evidence.decision_path,
                output_dir=output,
                scheduler_url=None,
                publish=True,
                client=resumed,
                attempt_limit=20,
                max_result_bytes=1_000_000,
                expected_rows=2,
            )

            self.assertEqual(result["publication"], "published")
            self.assertEqual(result["summary"]["remote_result_fetches_this_run"], 1)
            self.assertEqual(result["summary"]["reused_results_this_run"], 1)
            self.assertEqual(len(resumed.result_urls), 1)
            self.assertIn("/api/tasks/402/remote-file", resumed.result_urls[0])
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                sorted((audit.RECEIPT_NAME, audit.REPORT_NAME, audit.CHECKPOINT_DIR_NAME)),
            )
            self.assertEqual(
                len(list((output / audit.CHECKPOINT_DIR_NAME).glob("*.canonical.json"))),
                2,
            )
            final_receipt = json.loads((output / audit.RECEIPT_NAME).read_text(encoding="utf-8"))
            self.assertEqual(final_receipt["summary"]["selected_results_audited"], 2)
            self.assertEqual(final_receipt["scheduler_access"]["max_in_flight"], 1)

    def test_task_identity_collision_fails_before_any_result_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 1)
            wrong = scheduler_task(evidence.tasks[0], 501, "completed", exit_code=0)
            wrong["dedupe_key"] = "wrong-dedupe"
            client = FakeClient({evidence.tasks[0].task_name: [wrong]}, {})

            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                audit.audit_stage2(
                    plan_path=evidence.plan_path,
                    decision_path=evidence.decision_path,
                    output_dir=root / "out",
                    scheduler_url=None,
                    publish=False,
                    client=client,
                    attempt_limit=20,
                    max_result_bytes=1_000_000,
                    expected_rows=1,
                )
            self.assertEqual(client.result_urls, [])

    def test_execution_identity_checks_env_timeout_and_command_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 1)
            changes = {
                "env_setup": "module load wrong-module",
                "timeout_seconds": 1,
                "command": str(evidence.tasks[0].payload["command"]).replace(
                    "--cores-per-process 4", "--cores-per-process 8"
                ),
            }
            for field, value in changes.items():
                with self.subTest(field=field):
                    wrong = scheduler_task(
                        evidence.tasks[0], 510, "completed", exit_code=0
                    )
                    wrong[field] = value
                    client = FakeClient({evidence.tasks[0].task_name: [wrong]}, {})
                    with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                        audit.audit_stage2(
                            plan_path=evidence.plan_path,
                            decision_path=evidence.decision_path,
                            output_dir=root / f"out-{field}",
                            scheduler_url=None,
                            publish=False,
                            client=client,
                            attempt_limit=20,
                            max_result_bytes=1_000_000,
                            expected_rows=1,
                        )
                    self.assertEqual(client.result_urls, [])

    def test_live_task_shape_may_omit_exclusive_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 1)
            task = scheduler_task(evidence.tasks[0], 520, "completed", exit_code=0)
            task.pop("exclusive_node")
            client = FakeClient(
                {evidence.tasks[0].task_name: [task]},
                {520: result_payload(evidence.rows[0])},
            )

            result = audit.audit_stage2(
                plan_path=evidence.plan_path,
                decision_path=evidence.decision_path,
                output_dir=root / "out",
                scheduler_url=None,
                publish=False,
                client=client,
                attempt_limit=20,
                max_result_bytes=1_000_000,
                expected_rows=1,
            )

            self.assertEqual(result["summary"]["classifications"], {"physics_ok": 1})

    def test_aggregate_cannot_authorize_reuse_without_immutable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 1)
            task = scheduler_task(evidence.tasks[0], 601, "completed", exit_code=0)
            attempts = {evidence.tasks[0].task_name: [task]}
            output = root / "published"
            audit.audit_stage2(
                plan_path=evidence.plan_path,
                decision_path=evidence.decision_path,
                output_dir=output,
                scheduler_url=None,
                publish=True,
                client=FakeClient(attempts, {601: result_payload(evidence.rows[0])}),
                attempt_limit=20,
                max_result_bytes=1_000_000,
                expected_rows=1,
            )
            checkpoint = next(
                (output / audit.CHECKPOINT_DIR_NAME).glob("*.canonical.json")
            )
            checkpoint.unlink()
            resumed = FakeClient(attempts, {601: result_payload(evidence.rows[0])})

            with self.assertRaisesRegex(RuntimeError, "missing immutable checkpoint"):
                audit.audit_stage2(
                    plan_path=evidence.plan_path,
                    decision_path=evidence.decision_path,
                    output_dir=output,
                    scheduler_url=None,
                    publish=False,
                    client=resumed,
                    attempt_limit=20,
                    max_result_bytes=1_000_000,
                    expected_rows=1,
                )
            self.assertEqual(resumed.query_urls, [])
            self.assertEqual(resumed.result_urls, [])

    def test_resume_rejects_checkpoint_tamper_and_orphan(self) -> None:
        for mutation in ("tamper", "orphan", "collision"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                evidence = self.evidence(root, 1)
                task = scheduler_task(evidence.tasks[0], 701, "completed", exit_code=0)
                attempts = {evidence.tasks[0].task_name: [task]}
                output = root / "published"
                audit.audit_stage2(
                    plan_path=evidence.plan_path,
                    decision_path=evidence.decision_path,
                    output_dir=output,
                    scheduler_url=None,
                    publish=True,
                    client=FakeClient(
                        attempts, {701: result_payload(evidence.rows[0])}
                    ),
                    attempt_limit=20,
                    max_result_bytes=1_000_000,
                    expected_rows=1,
                )
                checkpoint_dir = output / audit.CHECKPOINT_DIR_NAME
                checkpoint = next(checkpoint_dir.glob("*.canonical.json"))
                if mutation == "tamper":
                    checkpoint.write_bytes(checkpoint.read_bytes() + b" ")
                    message = "hash/tamper mismatch"
                elif mutation == "orphan":
                    document = json.loads(checkpoint.read_text(encoding="utf-8"))
                    document["record"]["case_id"] = "orphan_case"
                    document["record"]["selected_task_id"] = 999
                    payload = audit.canonical_json_bytes(document)
                    sha256 = audit.sha256_bytes(payload)
                    orphan = checkpoint_dir / (
                        f"orphan_case.task-999.{sha256}.canonical.json"
                    )
                    orphan.write_bytes(payload)
                    message = "orphan result checkpoint case_id"
                else:
                    document = json.loads(checkpoint.read_text(encoding="utf-8"))
                    document["record"]["classification"] = "physics_failed"
                    document["record"]["physics_issues"] = ["synthetic_collision"]
                    payload = audit.canonical_json_bytes(document)
                    sha256 = audit.sha256_bytes(payload)
                    collision = checkpoint_dir / (
                        f"{evidence.tasks[0].safe_case_id}.task-701."
                        f"{sha256}.canonical.json"
                    )
                    collision.write_bytes(payload)
                    message = "multiple immutable checkpoints"
                resumed = FakeClient(attempts, {701: result_payload(evidence.rows[0])})
                with self.assertRaisesRegex(RuntimeError, message):
                    audit.audit_stage2(
                        plan_path=evidence.plan_path,
                        decision_path=evidence.decision_path,
                        output_dir=output,
                        scheduler_url=None,
                        publish=False,
                        client=resumed,
                        attempt_limit=20,
                        max_result_bytes=1_000_000,
                        expected_rows=1,
                    )
                self.assertEqual(resumed.query_urls, [])

    def test_replacement_set_readiness_blocks_only_active_or_success_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 4)
            attempts = {
                evidence.tasks[0].task_name: [
                    scheduler_task(evidence.tasks[0], 801, "running", exit_code=None)
                ],
                evidence.tasks[1].task_name: [
                    scheduler_task(evidence.tasks[1], 802, "completed", exit_code=0)
                ],
                evidence.tasks[3].task_name: [
                    scheduler_task(evidence.tasks[3], 804, "failed", exit_code=143)
                ],
            }
            blocked = audit.audit_stage2(
                plan_path=evidence.plan_path,
                decision_path=evidence.decision_path,
                output_dir=root / "blocked",
                scheduler_url=None,
                publish=False,
                client=FakeClient(attempts, {802: RuntimeError("result not visible")}),
                attempt_limit=20,
                max_result_bytes=1_000_000,
                expected_rows=4,
            )
            self.assertEqual(blocked["summary"]["active_task_count"], 1)
            self.assertEqual(
                blocked["summary"]["successful_result_pending_count"], 1
            )
            self.assertFalse(
                blocked["summary"]["replacement_set_ready_to_seal"]
            )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence = self.evidence(root, 2)
            retryable_only = {
                evidence.tasks[1].task_name: [
                    scheduler_task(evidence.tasks[1], 901, "cancelled", exit_code=143)
                ]
            }
            ready = audit.audit_stage2(
                plan_path=evidence.plan_path,
                decision_path=evidence.decision_path,
                output_dir=root / "ready",
                scheduler_url=None,
                publish=False,
                client=FakeClient(retryable_only, {}),
                attempt_limit=20,
                max_result_bytes=1_000_000,
                expected_rows=2,
            )
            self.assertEqual(ready["summary"]["active_task_count"], 0)
            self.assertEqual(
                ready["summary"]["successful_result_pending_count"], 0
            )
            self.assertTrue(ready["summary"]["replacement_set_ready_to_seal"])

    def test_paced_reader_enforces_minimums_and_429_backoff(self) -> None:
        now = [0.0]
        sleeps: list[float] = []
        calls = [0]

        def clock() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        def getter(url: str, _timeout: float, _max_bytes: int) -> bytes:
            calls[0] += 1
            if calls[0] == 1:
                headers = Message()
                headers["Retry-After"] = "3"
                raise error.HTTPError(url, 429, "rate limited", headers, None)
            return b"[]"

        client = audit.PacedHttpReader(
            timeout=10.0,
            pace_seconds=1.0,
            backoff_seconds=2.0,
            max_backoff_seconds=10.0,
            max_429_retries=2,
            raw_getter=getter,
            sleep=sleep,
            clock=clock,
        )
        self.assertEqual(client.get_json_list("http://scheduler/tasks"), [])
        self.assertEqual(client.get_json_list("http://scheduler/tasks2"), [])
        self.assertEqual(client.request_count, 3)
        self.assertEqual(client.rate_limit_retries, 1)
        self.assertIn(3.0, sleeps)
        self.assertIn(1.0, sleeps)

        with self.assertRaisesRegex(RuntimeError, "pace-seconds"):
            audit.PacedHttpReader(
                timeout=10.0,
                pace_seconds=0.99,
                backoff_seconds=1.0,
                max_backoff_seconds=2.0,
                max_429_retries=0,
            )
        with self.assertRaisesRegex(RuntimeError, "backoff-seconds"):
            audit.PacedHttpReader(
                timeout=10.0,
                pace_seconds=1.0,
                backoff_seconds=0.99,
                max_backoff_seconds=2.0,
                max_429_retries=0,
            )


if __name__ == "__main__":
    unittest.main()
