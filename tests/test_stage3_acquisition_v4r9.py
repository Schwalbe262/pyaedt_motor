from __future__ import annotations

import csv
import copy
import dataclasses
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

import build_ipmsm_v2_stage3_acquisition_v4r9 as builder
import continue_ipmsm_v2_stage3_acquisition_v4r9 as runner
import run_ipmsm_v2_campaign as campaign
import submit_ipmsm_v2_campaign as submit


def task(number: int, *, retry_index: int = 1) -> submit.CampaignTask:
    case_id = f"case-{number:03d}"
    dedupe_key = f"dedupe-{number:03d}-retry-{retry_index:02d}"
    payload = {
        "project": "PYAEDT_MOTOR_IPMSM_V2",
        "scheduling_profile": "fea_bursty",
        "required_capability": "conda:pyaedt2026v1",
        "env_profile": "pyaedt2026v1",
        "dedupe_key": dedupe_key,
        "env_setup": "module load ansys-electronics/v252",
    }
    return submit.CampaignTask(
        row_number=number,
        case_id=case_id,
        safe_case_id=case_id,
        remote_cases=f"remote/{case_id}.csv",
        result_csv=f"results/{case_id}.csv",
        simulation_dir=f"simulation/{case_id}",
        task_name=f"prefix-{case_id}",
        dedupe_key=dedupe_key,
        payload=payload,
        retry_index=retry_index,
    )


def context() -> SimpleNamespace:
    return SimpleNamespace(
        project="PYAEDT_MOTOR_IPMSM_V2",
        scheduler_url=builder.SCHEDULER_URL,
        scheduler_timeout_seconds=builder.SCHEDULER_TIMEOUT_SECONDS,
        project_active_cap=builder.PROJECT_ACTIVE_CAP,
    )


def reconciliation(
    *,
    successful: int = builder.EXPECTED_INITIAL_OK,
    retry_tasks: tuple[submit.CampaignTask, ...] | None = None,
    history: list[dict[str, object]] | None = None,
    permanent: tuple[dict[str, object], ...] = (),
    rows: tuple[dict[str, str], ...] = (),
) -> runner.Reconciliation:
    retry_tasks = retry_tasks if retry_tasks is not None else tuple(
        task(index) for index in range(1, 7)
    )
    successes = tuple(task(index, retry_index=0) for index in range(7, 7 + successful))
    all_tasks = tuple(task(index, retry_index=0) for index in range(1, 301))
    lineages = {
        base.dedupe_key: (base, task(base.row_number)) for base in all_tasks
    }
    state = campaign.CampaignState(
        successful=successes,
        active=(),
        missing=(),
        retryable=retry_tasks,
        pending=(),
        permanently_failed=permanent,
    )
    failures = {
        retry_task.dedupe_key.removesuffix("-retry-01"): campaign.ResultLevelFailure(
            case_id=retry_task.case_id,
            retry_index=0,
            task_id=retry_task.row_number,
            dedupe_key=retry_task.dedupe_key.removesuffix("-retry-01"),
            remote_result=retry_task.result_csv,
            raw_result_text="status,failed\n",
            result_error="AEDT analysis returned False",
        )
        for retry_task in retry_tasks
    }
    scheduler_snapshot = campaign.SchedulerSnapshot(
        history=history or [],
        campaign_history_tasks=(
            builder.EXPECTED_INITIAL_HISTORY if history is None else len(history)
        ),
        project_total_count=builder.EXPECTED_INITIAL_HISTORY,
        server_project_cap=builder.PROJECT_ACTIVE_CAP,
        project_active_count=0,
    )
    return runner.Reconciliation(
        kind="original",
        args=SimpleNamespace(project="PYAEDT_MOTOR_IPMSM_V2"),
        rows=rows,
        tasks=all_tasks,
        lineages=lineages,
        snapshot=scheduler_snapshot,
        state=state,
        validated_task_ids={},
        validated_result_rows={},
        result_failures=failures,
        result_audit_pending=(),
    )


def completed_fixture(root: Path) -> tuple[SimpleNamespace, runner.Reconciliation, dict[str, object]]:
    contract_path = root / "contract.json"
    contract_path.write_text("{}\n", encoding="utf-8")
    contract_snapshot = runner.authority.read_single_link_snapshot(
        contract_path, "fixture contract"
    )
    plan_path = root / "plan.csv"
    plan_rows: list[dict[str, str]] = []
    with plan_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=("case_id", "geometry_group_id", "design_hash", "doe_split"),
        )
        writer.writeheader()
        for group in range(50):
            design_hash = f"{group + 1:064x}"
            for offset in range(6):
                row = {
                    "case_id": f"case-{group * 6 + offset + 1:03d}",
                    "geometry_group_id": f"group-{group:02d}",
                    "design_hash": design_hash,
                    "doe_split": "test" if group >= 30 else "train",
                }
                writer.writerow(row)
                plan_rows.append(row)
    plan_snapshot = runner.authority.read_single_link_snapshot(plan_path, "fixture plan")

    successful_tasks = tuple(task(index, retry_index=0) for index in range(1, 301))
    remote_rows = {
        candidate.dedupe_key: {
            "case_id": candidate.case_id,
            "design_hash": plan_rows[index]["design_hash"],
            "status": "ok",
        }
        for index, candidate in enumerate(successful_tasks)
    }
    merged_path = root / "merged.csv"
    with merged_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=("case_id", "design_hash", "status")
        )
        writer.writeheader()
        writer.writerows(remote_rows[candidate.dedupe_key] for candidate in successful_tasks)
    merged_snapshot = runner.authority.read_single_link_snapshot(
        merged_path, "fixture merged result"
    )
    completion_path = root / "completion.json"
    fake_context = SimpleNamespace(
        path=contract_path,
        snapshot=contract_snapshot,
        contract_sha256="c" * 64,
        repository_revision="f" * 40,
        scheduler_url=builder.SCHEDULER_URL,
        project="PYAEDT_MOTOR_IPMSM_V2",
        task_prefix="ipmsm-v2-foundation-s3-v4r4",
        project_active_cap=builder.PROJECT_ACTIVE_CAP,
        history_limit=builder.HISTORY_LIMIT,
        result_retry_limit=builder.RESULT_RETRY_LIMIT,
        plan=plan_path,
        plan_sha256=plan_snapshot.sha256,
        replacement={
            "plan_output": str(root / "replacement.csv"),
            "failure_evidence_dir": str(root / "failed_results"),
            "failure_evidence_manifest": str(root / "failure_evidence.json"),
        },
        outputs={"completion": completion_path, "merged_result": merged_path},
    )
    history_count = runner._expected_completion_history("original")
    scheduler_snapshot = campaign.SchedulerSnapshot(
        history=[],
        campaign_history_tasks=history_count,
        project_total_count=history_count,
        server_project_cap=builder.PROJECT_ACTIVE_CAP,
        project_active_count=0,
    )
    state = campaign.CampaignState(
        successful=successful_tasks,
        active=(),
        missing=(),
        retryable=(),
        pending=(),
    )
    reconciled = runner.Reconciliation(
        kind="original",
        args=SimpleNamespace(project=fake_context.project),
        rows=tuple(plan_rows),
        tasks=successful_tasks,
        lineages={},
        snapshot=scheduler_snapshot,
        state=state,
        validated_task_ids={
            candidate.dedupe_key: 10_000 + index
            for index, candidate in enumerate(successful_tasks)
        },
        validated_result_rows=remote_rows,
        result_failures={},
        result_audit_pending=(),
    )
    completion = {
        "schema_version": runner.COMPLETION_SCHEMA_VERSION,
        "status": "acquisition_complete",
        "contract": runner._contract_record(fake_context),
        "repository_revision": fake_context.repository_revision,
        "scheduler": {
            "url": fake_context.scheduler_url,
            "project": fake_context.project,
            "task_prefix": fake_context.task_prefix,
            "history_tasks": history_count,
            "project_active_cap": fake_context.project_active_cap,
        },
        "effective_plan": {
            "path": str(plan_path),
            "sha256": plan_snapshot.sha256,
            "kind": "original",
            "rows": builder.EXPECTED_ROWS,
            "geometry_groups": builder.EXPECTED_GROUPS,
        },
        "replacement_manifest": None,
        "result": {
            "path": str(merged_path),
            "sha256": merged_snapshot.sha256,
            "rows": builder.EXPECTED_ROWS,
        },
    }
    completion_path.write_bytes(runner.authority.canonical_json_bytes(completion))
    return fake_context, reconciled, completion


class Stage3AcquisitionV4r9Tests(unittest.TestCase):
    def test_contract_constants_seal_live_port_cap_and_recovery_bounds(self) -> None:
        self.assertEqual(builder.SCHEDULER_URL, "http://127.0.0.1:8002")
        self.assertEqual(builder.PROJECT_ACTIVE_CAP, 50)
        self.assertEqual(builder.EXPECTED_INITIAL_HISTORY, 303)
        self.assertEqual(builder.EXPECTED_INITIAL_OK, 294)
        self.assertEqual(builder.EXPECTED_INITIAL_RESULT_FAILURES, 6)
        self.assertEqual(builder.REPLACEMENT_SEED, 730037)
        self.assertEqual(builder.REPLACEMENT_GROUP_LIMIT, 1)
        parsed = builder.build_parser().parse_args(
            ["--source-root", r"C:\exact-v4r9", "--source-revision", "a" * 40]
        )
        self.assertEqual(parsed.runtime_root, builder.EXPECTED_RUNTIME_ROOT)
        self.assertEqual(parsed.source_root, Path(r"C:\exact-v4r9"))

    def test_set_flag_replaces_once_or_appends_and_rejects_duplicates(self) -> None:
        self.assertEqual(
            builder._set_flag(("--project", "old"), "--project", "new"),
            ("--project", "new"),
        )
        self.assertEqual(
            builder._set_flag(("--submit",), "--scheduler-url", builder.SCHEDULER_URL),
            ("--submit", "--scheduler-url", builder.SCHEDULER_URL),
        )
        with self.assertRaisesRegex(builder.Stage3RecoveryBuildError, "duplicate"):
            builder._set_flag(("--x", "1", "--x", "2"), "--x", "3")

    def test_source_provenance_requires_single_links_except_for_exact_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_root = Path(temporary)
            relative = Path("bound_source.py")
            source_path = (source_root / relative).resolve()
            source_payload = b"# bound source\n"
            source_path.write_bytes(source_payload)
            revision = "f" * 40
            executable_path = Path(sys.executable).resolve(strict=True)
            source_snapshot = SimpleNamespace(
                path=source_path,
                payload=source_payload,
                sha256="a" * 64,
                require_single_link=True,
            )
            executable_snapshot = SimpleNamespace(
                path=executable_path,
                payload=b"bound executable",
                sha256="b" * 64,
                require_single_link=False,
            )

            def git_result(_root: Path, *args: str) -> bytes:
                if args == ("rev-parse", "HEAD"):
                    return f"{revision}\n".encode("ascii")
                if args == ("status", "--porcelain", "--untracked-files=no"):
                    return b""
                if args == ("show", f"{revision}:{relative.as_posix()}"):
                    return source_payload
                self.fail(f"unexpected Git authority query: {args}")

            with (
                mock.patch.object(builder, "SOURCE_RELATIVE_PATHS", {"runner": relative}),
                mock.patch.object(builder, "_git", side_effect=git_result),
                mock.patch.object(
                    builder.authority,
                    "read_single_link_snapshot",
                    side_effect=(source_snapshot, executable_snapshot),
                ) as read_snapshot,
            ):
                records, snapshots = builder._source_provenance(source_root, revision)

            self.assertEqual(records["runner"]["path"], str(source_path))
            self.assertEqual(
                records["runner_executable"],
                {"path": str(executable_path), "sha256": "b" * 64},
            )
            self.assertEqual(snapshots, (source_snapshot, executable_snapshot))
            self.assertEqual(read_snapshot.call_count, 2)
            source_call, executable_call = read_snapshot.call_args_list
            self.assertEqual(source_call.args, (source_path, "v4r9 source runner"))
            self.assertIs(source_call.kwargs["require_single_link"], True)
            self.assertEqual(
                executable_call.args,
                (executable_path, "v4r9 runner executable"),
            )
            self.assertIs(executable_call.kwargs["require_single_link"], False)

    def test_builder_separates_non_git_runtime_from_exact_source_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime_root = base / "runtime_lf325"
            source_root = base / "source_detached"
            runtime_root.mkdir()
            source_root.mkdir()
            # Deliberately create no .git under runtime_root.  Git authority is
            # exclusively a source_root concern.
            prior_contract = runtime_root / builder.EXPECTED_PRIOR_CONTRACT
            prior_contract.parent.mkdir(parents=True)
            prior_contract.write_text("prior\n", encoding="utf-8")
            plan = runtime_root / "sealed" / "stage3.csv"
            plan.parent.mkdir()
            plan.write_text("case_id\nfixture\n", encoding="utf-8")
            output_dir = runtime_root / "collected" / "stage3"
            executable = str(Path(sys.executable).resolve())
            source_records = {
                "runner_executable": {"path": executable, "sha256": "a" * 64},
                "campaign": {
                    "path": str(source_root / builder.SOURCE_RELATIVE_PATHS["campaign"]),
                    "sha256": "b" * 64,
                    "repository_path": builder.SOURCE_RELATIVE_PATHS["campaign"].as_posix(),
                    "git_blob_sha256": "b" * 64,
                },
                "runner": {
                    "path": str(source_root / builder.SOURCE_RELATIVE_PATHS["runner"]),
                    "sha256": "c" * 64,
                    "repository_path": builder.SOURCE_RELATIVE_PATHS["runner"].as_posix(),
                    "git_blob_sha256": "c" * 64,
                },
            }
            base_args = (
                "--cases",
                str(plan),
                "--project",
                "PYAEDT_MOTOR_IPMSM_V2",
                "--scheduler-url",
                "http://127.0.0.1:8000",
                "--project-active-cap",
                "50",
                "--task-prefix",
                "ipmsm-v2-foundation-s3-v4r4",
                "--output-dir",
                str(output_dir),
                "--merged-output",
                "merged_results.csv",
                "--terminal-retry-limit",
                "1",
                "--submit",
                "--history-limit",
                "601",
                "--timeout",
                "300.0",
            )
            prior_context = SimpleNamespace(
                root=runtime_root,
                expected_rows=300,
                project_active_cap=50,
                plan=plan,
                campaign_argv=(executable, "-B", "old_campaign.py", *base_args),
                project="PYAEDT_MOTOR_IPMSM_V2",
                task_prefix="ipmsm-v2-foundation-s3-v4r4",
                outputs={
                    "campaign_output_dir": output_dir,
                    "merged_result": output_dir / "merged_results.csv",
                },
            )
            prior_audit = {"binding": {"sealed_prior": True}}
            with (
                mock.patch.object(builder, "EXPECTED_RUNTIME_ROOT", runtime_root),
                mock.patch.object(builder, "_assert_loaded_source_root"),
                mock.patch.object(
                    builder.v4r8_builder,
                    "_audit_prior_acquisition",
                    return_value=(prior_context, prior_audit, ()),
                ),
                mock.patch.object(
                    builder, "_read_plan_groups", return_value=(300, {"group": 6})
                ),
                mock.patch.object(
                    builder,
                    "_activation_inputs",
                    return_value=(
                        {"path": str(runtime_root / "spec.json"), "sha256": "d" * 64},
                        {"path": str(runtime_root / "stage12.csv"), "sha256": "e" * 64},
                    ),
                ),
                mock.patch.object(
                    builder,
                    "_source_provenance",
                    return_value=(source_records, ()),
                ),
                mock.patch.object(
                    builder,
                    "_runtime_dependencies",
                    return_value={"numpy": "1", "scipy": "1"},
                ),
            ):
                document, _ = builder.build_contract_document(
                    runtime_root,
                    source_root,
                    prior_contract,
                    "f" * 40,
                )
            recovery = document["recovery"]
            execution = recovery["execution"]
            self.assertEqual(recovery["runtime_root"], str(runtime_root.absolute()))
            self.assertEqual(recovery["source_root"], str(source_root.absolute()))
            self.assertEqual(execution["cwd"], str(runtime_root.absolute()))
            self.assertEqual(execution["pythonpath"], [str(source_root.absolute())])
            self.assertEqual(Path(execution["campaign_argv"][2]).parent, source_root)
            self.assertEqual(Path(execution["runner_dry_argv"][2]).parent, source_root)
            self.assertEqual(Path(recovery["plan"]["path"]), plan)
            self.assertEqual(
                Path(recovery["outputs"]["campaign_output_dir"]), output_dir
            )
            self.assertFalse((runtime_root / ".git").exists())

    def test_plan_shape_requires_fifty_coherent_six_row_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=("case_id", "geometry_group_id", "design_hash", "doe_split"),
                )
                writer.writeheader()
                for group in range(50):
                    for row in range(6):
                        writer.writerow(
                            {
                                "case_id": f"case-{group:02d}-{row}",
                                "geometry_group_id": f"group-{group:02d}",
                                "design_hash": f"{group:064x}",
                                "doe_split": "test" if group >= 30 else "train",
                            }
                        )
            rows, groups = builder._read_plan_groups(path)
            self.assertEqual(rows, 300)
            self.assertEqual(len(groups), 50)
            self.assertEqual(set(groups.values()), {6})

    def test_get_only_initial_reconciliation_is_exactly_294_ok_and_six_failed(self) -> None:
        value = reconciliation()
        runner._audit_initial_reconciliation(value)
        changed = reconciliation(successful=293)
        with self.assertRaisesRegex(runner.Stage3RecoveryError, "GET-only"):
            runner._audit_initial_reconciliation(changed)

    def test_retry_posts_are_six_fresh_api_tasks_with_required_policy(self) -> None:
        value = reconciliation()
        responses = [{"id": 9000 + index} for index in range(6)]
        with mock.patch.object(
            runner.submit,
            "post_scheduler_task",
            side_effect=responses,
        ) as post:
            submitted = runner._post_candidates(context(), value)
        self.assertEqual(len(submitted), 6)
        self.assertEqual(post.call_count, 6)
        for call in post.call_args_list:
            scheduler_url, payload, timeout, endpoint = call.args
            self.assertEqual(scheduler_url, builder.SCHEDULER_URL)
            self.assertEqual(endpoint, "/api/tasks")
            self.assertEqual(timeout, builder.SCHEDULER_TIMEOUT_SECONDS)
            self.assertEqual(payload["scheduling_profile"], "fea_bursty")
            self.assertEqual(payload["required_capability"], "conda:pyaedt2026v1")
            self.assertEqual(payload["env_profile"], "pyaedt2026v1")
            self.assertIn("module load ansys-electronics/v252", payload["env_setup"])

    def test_retry_post_can_finish_a_partially_visible_six_case_wave_without_duplicates(self) -> None:
        remaining = tuple(task(index) for index in range(4, 7))
        value = reconciliation(retry_tasks=remaining, history=[])
        with mock.patch.object(
            runner.submit,
            "post_scheduler_task",
            side_effect=({"id": 9101}, {"id": 9102}, {"id": 9103}),
        ) as post:
            submitted = runner._post_candidates(context(), value)
        self.assertEqual(len(submitted), 3)
        self.assertEqual(post.call_count, 3)

    def test_payload_policy_rejects_pooled_or_missing_module_requests(self) -> None:
        candidate = task(1)
        candidate.payload["aedt_backend"] = "pooled"
        with self.assertRaisesRegex(runner.Stage3RecoveryError, "pooled"):
            runner._task_payload_policy(candidate, context())
        candidate.payload.pop("aedt_backend")
        candidate.payload["env_setup"] = "true"
        with self.assertRaisesRegex(runner.Stage3RecoveryError, "module"):
            runner._task_payload_policy(candidate, context())

    def test_result_get_retries_rate_limit_without_losing_validation(self) -> None:
        candidate = task(1, retry_index=0)
        history = [
            {
                "id": 101,
                "project": "PYAEDT_MOTOR_IPMSM_V2",
                "dedupe_key": candidate.dedupe_key,
                "status": "completed",
                "exit_code": 0,
                "finished_at": "2026-01-01T00:00:00+00:00",
            }
        ]
        args = SimpleNamespace(
            project="PYAEDT_MOTOR_IPMSM_V2",
            scheduler_url=builder.SCHEDULER_URL,
            timeout=300.0,
            completed_result_settle_seconds=0.0,
        )
        rate_limit = RuntimeError("rate limited")
        rate_limit.code = 429
        validated: dict[str, int] = {}
        failures: dict[str, campaign.ResultLevelFailure] = {}
        with (
            mock.patch.object(
                runner.collector,
                "fetch_task_remote_file",
                side_effect=(rate_limit, "result"),
            ) as fetch,
            mock.patch.object(
                runner.collector,
                "_one_remote_result",
                return_value=(["case_id", "status"], {"case_id": candidate.case_id, "status": "ok"}),
            ),
            mock.patch.object(runner.collector, "validate_result_matches_plan"),
            mock.patch.object(runner.time, "sleep") as sleep,
            mock.patch.object(runner, "RESULT_FETCH_MIN_INTERVAL_SECONDS", 0.0),
        ):
            pending = runner._audit_completed_results_bounded(
                args,
                (candidate,),
                ({"case_id": candidate.case_id, "design_hash": "d" * 64},),
                history,
                validated,
                {},
                failures,
            )
        self.assertEqual(pending, ())
        self.assertEqual(validated, {candidate.dedupe_key: 101})
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once()

    def test_replacement_expands_one_failed_row_to_its_entire_six_row_group(self) -> None:
        rows = tuple(
            {
                "case_id": f"case-{index}",
                "geometry_group_id": "v2s3_final_audit_0014_deadbeef0000",
                "design_hash": "d" * 64,
            }
            for index in range(1, 7)
        )
        value = reconciliation(
            retry_tasks=(),
            successful=299,
            permanent=({"case_id": "case-3"},),
            rows=rows,
        )
        group, design_hash, case_ids = runner._failed_group_identity(value)
        self.assertIn("final_audit_0014", group)
        self.assertEqual(design_hash, "d" * 64)
        self.assertEqual(case_ids, tuple(f"case-{index}" for index in range(1, 7)))

    def test_replacement_preserves_all_six_failed_result_rows_before_remote_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            contract_path.write_text("{}\n", encoding="utf-8")
            snapshot = runner.authority.read_single_link_snapshot(
                contract_path, "test recovery contract"
            )
            fake_context = SimpleNamespace(
                path=contract_path,
                snapshot=snapshot,
                contract_sha256="c" * 64,
                replacement={
                    "failure_evidence_dir": str(root / "failed_results"),
                    "failure_evidence_manifest": str(root / "failure_evidence.json"),
                },
            )
            failures = {
                f"dedupe-{index}": campaign.ResultLevelFailure(
                    case_id=f"case-{index}",
                    retry_index=0,
                    task_id=100 + index,
                    dedupe_key=f"dedupe-{index}",
                    remote_result=f"remote/case-{index}.csv",
                    raw_result_text=f"case_id,status\ncase-{index},failed\n",
                    result_error="AEDT analysis returned False",
                )
                for index in range(1, 7)
            }
            fake_reconciliation = SimpleNamespace(result_failures=failures)
            manifest = runner._publish_failure_evidence(
                fake_context,
                fake_reconciliation,
                failed_group="v2s3_final_audit_0014_deadbeef0000",
                failed_group_case_ids=tuple(f"case-{index}" for index in range(1, 7)),
            )
            self.assertEqual(len(manifest["entries"]), 6)
            self.assertEqual(
                {path.name for path in (root / "failed_results").iterdir()},
                {
                    f"case-{index}_attempt_00_task_{100 + index}.csv"
                    for index in range(1, 7)
                },
            )
            first = Path(manifest["entries"][0]["local_result"])
            first.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(runner.Stage3RecoveryError, "bytes changed"):
                runner._audit_failure_evidence(fake_context)

    def test_dry_run_reconciles_without_posting_or_writing(self) -> None:
        value = reconciliation()
        fake_context = context()
        fake_context.plan = Path("sealed-plan.csv")
        fake_context.result_retry_limit = builder.RESULT_RETRY_LIMIT
        with (
            mock.patch.object(runner, "_assert_authority"),
            mock.patch.object(runner, "_load_replacement_manifest", return_value=None),
            mock.patch.object(runner, "_reconcile", return_value=value),
            mock.patch.object(runner.submit, "post_scheduler_task") as post,
        ):
            report = runner.dry_run(fake_context)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["history_tasks"], 303)
        self.assertEqual(report["successful_results"], 294)
        self.assertEqual(report["result_level_failures"], 6)
        self.assertEqual(report["planned_submissions"], 6)
        self.assertEqual(report["writes_performed"], 0)
        post.assert_not_called()

    def test_existing_completion_recurring_verification_reaudits_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_context, reconciled, _completion = completed_fixture(Path(temporary))
            with mock.patch.object(
                runner, "_reconcile", return_value=reconciled
            ) as reconcile_live:
                report = runner._verify_existing_completion(fake_context)
            self.assertEqual(report["action"], "verified_existing_completion")
            self.assertEqual(report["plan_kind"], "original")
            self.assertEqual(report["history_tasks"], 309)
            self.assertEqual(report["successful_results"], 300)
            self.assertEqual(report["writes_performed"], 0)
            reconcile_live.assert_called_once_with(
                fake_context,
                fake_context.plan,
                kind="original",
                retry_limit=builder.RESULT_RETRY_LIMIT,
            )

    def test_existing_completion_rejects_bound_field_and_artifact_tampering(self) -> None:
        mutations = {
            "repository": lambda value: value.__setitem__("repository_revision", "0" * 40),
            "scheduler": lambda value: value["scheduler"].__setitem__("project_active_cap", 49),
            "effective_plan": lambda value: value["effective_plan"].__setitem__("rows", 299),
            "replacement": lambda value: value.__setitem__("replacement_manifest", {}),
            "result": lambda value: value["result"].__setitem__("rows", 299),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                fake_context, reconciled, completion = completed_fixture(Path(temporary))
                changed = copy.deepcopy(completion)
                mutate(changed)
                fake_context.outputs["completion"].write_bytes(
                    runner.authority.canonical_json_bytes(changed)
                )
                with mock.patch.object(runner, "_reconcile", return_value=reconciled):
                    with self.assertRaises(runner.Stage3RecoveryError):
                        runner._verify_existing_completion(fake_context)

        with tempfile.TemporaryDirectory() as temporary:
            fake_context, reconciled, _completion = completed_fixture(Path(temporary))
            Path(fake_context.replacement["plan_output"]).write_text(
                "unexpected\n", encoding="utf-8"
            )
            with mock.patch.object(runner, "_reconcile", return_value=reconciled):
                with self.assertRaisesRegex(
                    runner.Stage3RecoveryError, "unexpected replacement artifact"
                ):
                    runner._verify_existing_completion(fake_context)

        with tempfile.TemporaryDirectory() as temporary:
            fake_context, reconciled, _completion = completed_fixture(Path(temporary))
            fake_context.outputs["merged_result"].write_text("changed\n", encoding="utf-8")
            with mock.patch.object(runner, "_reconcile", return_value=reconciled):
                with self.assertRaisesRegex(runner.Stage3RecoveryError, "bytes changed"):
                    runner._verify_existing_completion(fake_context)

    def test_existing_completion_rejects_live_scheduler_or_remote_result_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_context, reconciled, _completion = completed_fixture(Path(temporary))
            changed_snapshot = campaign.SchedulerSnapshot(
                history=[],
                campaign_history_tasks=310,
                project_total_count=310,
                server_project_cap=builder.PROJECT_ACTIVE_CAP,
                project_active_count=0,
            )
            changed = dataclasses.replace(reconciled, snapshot=changed_snapshot)
            with mock.patch.object(runner, "_reconcile", return_value=changed):
                with self.assertRaisesRegex(runner.Stage3RecoveryError, "scheduler provenance"):
                    runner._verify_existing_completion(fake_context)

        with tempfile.TemporaryDirectory() as temporary:
            fake_context, reconciled, _completion = completed_fixture(Path(temporary))
            rows = {
                key: dict(value) for key, value in reconciled.validated_result_rows.items()
            }
            first_key = next(iter(rows))
            rows[first_key]["status"] = "tampered"
            changed = dataclasses.replace(reconciled, validated_result_rows=rows)
            with mock.patch.object(runner, "_reconcile", return_value=changed):
                with self.assertRaisesRegex(runner.Stage3RecoveryError, "ordered remote"):
                    runner._verify_existing_completion(fake_context)

    def test_replacement_completion_reaudits_manifest_and_failure_evidence_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_context, reconciled, completion = completed_fixture(root)
            replacement_plan = Path(fake_context.replacement["plan_output"])
            replacement_plan.write_bytes(fake_context.plan.read_bytes())
            replacement_snapshot = runner.authority.read_single_link_snapshot(
                replacement_plan, "fixture replacement plan"
            )
            replacement_manifest = {
                "failed_geometry_group_id": "old-group",
                "replacement_geometry_group_id": "new-group",
            }
            sealed_replacement_record = {
                "path": str(root / "replacement.csv.manifest.json"),
                "sha256": "a" * 64,
                "failed_geometry_group_id": "old-group",
                "replacement_geometry_group_id": "new-group",
                "failure_evidence_manifest": {
                    "path": str(root / "failure_evidence.json"),
                    "sha256": "b" * 64,
                },
            }
            completion["effective_plan"] = {
                "path": str(replacement_plan),
                "sha256": replacement_snapshot.sha256,
                "kind": "replacement",
                "rows": builder.EXPECTED_ROWS,
                "geometry_groups": builder.EXPECTED_GROUPS,
            }
            completion["replacement_manifest"] = sealed_replacement_record
            completion["scheduler"]["history_tasks"] = runner._expected_completion_history(
                "replacement"
            )
            fake_context.outputs["completion"].write_bytes(
                runner.authority.canonical_json_bytes(completion)
            )
            replacement_scheduler = dataclasses.replace(
                reconciled.snapshot,
                campaign_history_tasks=runner._expected_completion_history("replacement"),
            )
            replacement_reconciliation = dataclasses.replace(
                reconciled,
                kind="replacement",
                snapshot=replacement_scheduler,
            )
            with (
                mock.patch.object(
                    runner,
                    "_load_replacement_manifest",
                    return_value=replacement_manifest,
                ) as load_manifest,
                mock.patch.object(
                    runner,
                    "_completion_replacement_record",
                    return_value=sealed_replacement_record,
                ),
                mock.patch.object(
                    runner, "_reconcile", return_value=replacement_reconciliation
                ),
            ):
                report = runner._verify_existing_completion(fake_context)
            self.assertEqual(report["plan_kind"], "replacement")
            self.assertEqual(report["history_tasks"], 315)
            self.assertEqual(report["writes_performed"], 0)
            load_manifest.assert_called_once_with(fake_context)


if __name__ == "__main__":
    unittest.main()
