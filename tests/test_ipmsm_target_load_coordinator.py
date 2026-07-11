from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import ipmsm_dashboard_state as dashboard
import ipmsm_target_load_coordinator as coordinator
import ipmsm_target_load_workflow as workflow
from tests.test_ipmsm_target_load_workflow import (
    fixed_mtpa_evidence,
    issue_observation,
    result_csv,
    result_row_for_attempt,
    root_manifest,
)


NOW = datetime(2026, 7, 12, 0, 0, tzinfo=timezone.utc)


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
