from __future__ import annotations

import argparse
from email.message import Message
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

import snapshot_ipmsm_v2_partial_results as snapshot


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def rate_limiter(fake: FakeTime) -> snapshot.FetchRateLimiter:
    return snapshot.FetchRateLimiter(
        interval_seconds=0.5,
        max_requests_per_window=10,
        window_seconds=30.0,
        clock=fake.clock,
        sleeper=fake.sleep,
    )


def task(row_number: int, case_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        row_number=row_number,
        case_id=case_id,
        result_csv=f"results/{case_id}.csv",
    )


def settled(row_number: int, group: str, split: str) -> snapshot.SettledResult:
    case_id = f"case-{row_number}"
    return snapshot.SettledResult(
        task=task(row_number, case_id),
        history_task={"id": row_number, "status": "completed", "exit_code": 0},
        plan_row={
            "case_id": case_id,
            "geometry_group_id": group,
            "design_hash": f"hash-{group}",
            "doe_split": split,
        },
    )


class SelectionTests(unittest.TestCase):
    def test_only_fully_settled_designs_are_selected(self) -> None:
        rows = [
            {"case_id": "case-1", "geometry_group_id": "g1"},
            {"case_id": "case-2", "geometry_group_id": "g1"},
            {"case_id": "case-3", "geometry_group_id": "g2"},
            {"case_id": "case-4", "geometry_group_id": "g2"},
            {"case_id": "case-5", "geometry_group_id": "g3"},
            {"case_id": "case-6", "geometry_group_id": "g3"},
        ]
        tasks = [task(index, f"case-{index}") for index in range(1, 7)]
        available = [
            settled(1, "g1", "train"),
            settled(2, "g1", "train"),
            settled(3, "g2", "calibration"),
            settled(5, "g3", "test"),
            settled(6, "g3", "test"),
        ]
        chosen, complete, groups = snapshot.select_complete_designs(
            tasks=tasks,
            selected_rows=rows,
            settled=available,
            max_designs=0,
        )
        self.assertEqual(complete, ["g1", "g3"])
        self.assertEqual(groups, ["g1", "g3"])
        self.assertEqual([item.task.row_number for item in chosen], [1, 2, 5, 6])

    def test_max_designs_samples_evenly_in_plan_order(self) -> None:
        groups = [f"g{index}" for index in range(1, 6)]
        rows = [
            {"case_id": f"case-{index}", "geometry_group_id": group}
            for index, group in enumerate(groups, start=1)
        ]
        tasks = [task(index, f"case-{index}") for index in range(1, 6)]
        available = [settled(index, group, "train") for index, group in enumerate(groups, start=1)]
        _, _, chosen_groups = snapshot.select_complete_designs(
            tasks=tasks,
            selected_rows=rows,
            settled=available,
            max_designs=3,
        )
        self.assertEqual(chosen_groups, ["g1", "g3", "g5"])

    def test_unfinished_late_repeat_does_not_hide_a_complete_base_design(self) -> None:
        rows = [
            {"case_id": "case-1", "geometry_group_id": "g1", "repeat_of_case_id": ""},
            {"case_id": "case-2", "geometry_group_id": "g1", "repeat_of_case_id": ""},
            {"case_id": "case-3", "geometry_group_id": "g1", "repeat_of_case_id": "case-1"},
        ]
        tasks = [task(index, f"case-{index}") for index in range(1, 4)]
        available = [settled(1, "g1", "train"), settled(2, "g1", "train")]
        chosen, complete, groups = snapshot.select_complete_designs(
            tasks=tasks,
            selected_rows=rows,
            settled=available,
            max_designs=0,
        )
        self.assertEqual(complete, ["g1"])
        self.assertEqual(groups, ["g1"])
        self.assertEqual([item.task.row_number for item in chosen], [1, 2])

    def test_diagnostic_scope_never_claims_the_official_gate(self) -> None:
        self.assertEqual(
            snapshot.diagnostic_scope(59, {"train": 40, "calibration": 10, "test": 10}),
            "physics_only",
        )
        self.assertEqual(
            snapshot.diagnostic_scope(60, {"train": 30, "calibration": 10, "test": 20}),
            "provisional_minimum",
        )
        self.assertEqual(
            snapshot.diagnostic_scope(80, {"train": 40, "calibration": 15, "test": 25}),
            "provisional_stronger",
        )

    def test_checkpoint_selection_gate_fails_before_fetch_when_not_exact_or_too_weak(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "selected designs=59"):
            snapshot.enforce_selection_gate(
                selected_designs=59,
                max_designs=60,
                require_exact_designs=True,
                selected_scope="provisional_minimum",
                minimum_diagnostic_scope="provisional_minimum",
            )
        with self.assertRaisesRegex(RuntimeError, "physics_only"):
            snapshot.enforce_selection_gate(
                selected_designs=60,
                max_designs=60,
                require_exact_designs=True,
                selected_scope="physics_only",
                minimum_diagnostic_scope="provisional_minimum",
            )
        with self.assertRaisesRegex(RuntimeError, "selected rows=354"):
            snapshot.enforce_selection_gate(
                selected_designs=60,
                max_designs=60,
                require_exact_designs=True,
                selected_scope="provisional_minimum",
                minimum_diagnostic_scope="provisional_minimum",
                selected_rows=354,
                required_rows=360,
            )

    def test_checkpoint_selection_gate_accepts_stronger_scope(self) -> None:
        snapshot.enforce_selection_gate(
            selected_designs=60,
            max_designs=60,
            require_exact_designs=True,
            selected_scope="provisional_stronger",
            minimum_diagnostic_scope="provisional_minimum",
        )


class RateLimitTests(unittest.TestCase):
    def test_ten_request_window_forces_a_thirty_second_boundary(self) -> None:
        fake = FakeTime()
        limiter = rate_limiter(fake)
        for _ in range(11):
            limiter.before_request()
        self.assertGreaterEqual(fake.value, 30.0)

    def test_429_retries_with_exponential_backoff(self) -> None:
        fake = FakeTime()
        limiter = rate_limiter(fake)
        calls = 0

        def fetch() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPError("http://scheduler", 429, "busy", Message(), None)
            return "ok"

        result = snapshot.fetch_with_policy(
            fetch,
            task_id=101,
            request_index=0,
            limiter=limiter,
            retry_limit=5,
            backoff_seconds=1.0,
            max_backoff_seconds=10.0,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls, 2)
        self.assertTrue(any(delay >= 0.8 for delay in fake.sleeps))

    def test_retry_after_header_takes_precedence(self) -> None:
        fake = FakeTime()
        limiter = rate_limiter(fake)
        calls = 0
        headers = Message()
        headers["Retry-After"] = "3"

        def fetch() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPError("http://scheduler", 429, "busy", headers, None)
            return "ok"

        snapshot.fetch_with_policy(
            fetch,
            task_id=102,
            request_index=0,
            limiter=limiter,
            retry_limit=5,
            backoff_seconds=1.0,
            max_backoff_seconds=10.0,
        )
        self.assertTrue(any(delay == 3.0 for delay in fake.sleeps))

    def test_retry_after_header_is_capped_by_max_backoff(self) -> None:
        fake = FakeTime()
        limiter = rate_limiter(fake)
        calls = 0
        headers = Message()
        headers["Retry-After"] = "3600"

        def fetch() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPError("http://scheduler", 429, "busy", headers, None)
            return "ok"

        snapshot.fetch_with_policy(
            fetch,
            task_id=103,
            request_index=0,
            limiter=limiter,
            retry_limit=5,
            backoff_seconds=1.0,
            max_backoff_seconds=10.0,
        )
        self.assertTrue(any(delay == 10.0 for delay in fake.sleeps))

    def test_non_429_http_error_fails_without_retry(self) -> None:
        fake = FakeTime()
        calls = 0

        def fetch() -> str:
            nonlocal calls
            calls += 1
            raise HTTPError("http://scheduler", 504, "timeout", Message(), None)

        with self.assertRaises(HTTPError):
            snapshot.fetch_with_policy(
                fetch,
                task_id=103,
                request_index=0,
                limiter=rate_limiter(fake),
                retry_limit=5,
                backoff_seconds=1.0,
                max_backoff_seconds=10.0,
            )
        self.assertEqual(calls, 1)


class SafetyTests(unittest.TestCase):
    def test_atomic_snapshot_directory_contains_contract_bound_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contract = root / "contract.json"
            source_plan = root / "source.csv"
            producer = root / "snapshot.py"
            contract.write_text("contract", encoding="utf-8")
            source_plan.write_text("source", encoding="utf-8")
            producer.write_text("producer", encoding="utf-8")
            context = snapshot.SnapshotManifestContext(
                contract_source=contract,
                contract_sha256="a" * 64,
                contract_document_sha256=snapshot.supervisor._file_sha256(contract),
                source_case_plan=source_plan,
                source_case_plan_sha256=snapshot.supervisor._file_sha256(source_plan),
                producer_path=producer,
                producer_sha256=snapshot.supervisor._file_sha256(producer),
                complete_designs_available=1,
                selected_designs=1,
                split_design_counts={"train": 1, "calibration": 0, "test": 0},
                diagnostic_scope="physics_only",
            )
            output = root / "snapshot"
            task_value = SimpleNamespace(safe_case_id="case-1")
            plan, merged, results, manifest_path = snapshot.stage_and_commit_snapshot(
                output,
                [
                    {
                        "case_id": "case-1",
                        "geometry_group_id": "group-1",
                        "doe_split": "train",
                        "repeat_of_case_id": "",
                    }
                ],
                [(task_value, "case_id,status\ncase-1,ok\n")],
                manifest_context=context,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertTrue(plan.is_file())
            self.assertTrue(merged.is_file())
            self.assertEqual(len(results), 1)
            self.assertEqual(manifest["contract"]["canonical_sha256"], "a" * 64)
            self.assertEqual(
                manifest["artifacts"]["selected_plan"]["sha256"],
                snapshot.supervisor._file_sha256(plan),
            )
            self.assertEqual(manifest["producer"]["sha256"], context.producer_sha256)
            self.assertEqual(manifest["counts"]["selected_rows"], 1)

    def test_minimum_scope_is_checked_before_fetch_or_output(self) -> None:
        selected = []
        row_number = 1
        for split, count in (("train", 31), ("calibration", 9), ("test", 20)):
            for group_number in range(count):
                group = f"{split}-{group_number}"
                for _ in range(6):
                    selected.append(settled(row_number, group, split))
                    row_number += 1
        groups = list(
            dict.fromkeys(item.plan_row["geometry_group_id"] for item in selected)
        )
        campaign_args = SimpleNamespace(
            cases=Path("cases.csv"),
            max_plan_cases=700,
            case_start_index=1,
            case_limit=700,
            project="project",
            terminal_retry_limit=1,
            completed_result_settle_seconds=300.0,
        )
        state = SimpleNamespace(active=[], successful=[], missing=[], retryable=[])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = Path(tmp) / "snapshot"
            contract_path = root / "contract.json"
            case_plan = root / "cases.csv"
            contract_path.write_text("contract", encoding="utf-8")
            case_plan.write_text("cases", encoding="utf-8")
            contract_value = SimpleNamespace(
                source=contract_path,
                stage1=SimpleNamespace(case_plan=case_plan),
            )
            with (
                mock.patch.object(
                    snapshot.supervisor,
                    "load_contract",
                    return_value=contract_value,
                ),
                mock.patch.object(snapshot.supervisor, "audit_immutable_inputs"),
                mock.patch.object(snapshot, "validate_output_dir"),
                mock.patch.object(snapshot, "_campaign_args", return_value=campaign_args),
                mock.patch.object(snapshot.submitter, "load_and_validate_cases", return_value=[]),
                mock.patch.object(snapshot.submitter, "select_case_rows", return_value=[]),
                mock.patch.object(snapshot.submitter, "build_campaign_tasks", return_value=[]),
                mock.patch.object(
                    snapshot.runner,
                    "read_scheduler_snapshot",
                    return_value=SimpleNamespace(history=[]),
                ),
                mock.patch.object(snapshot.runner, "classify_campaign_state", return_value=state),
                mock.patch.object(snapshot, "settled_successful_results", return_value=selected),
                mock.patch.object(
                    snapshot,
                    "select_complete_designs",
                    return_value=(selected, groups, groups),
                ),
                mock.patch.object(snapshot, "fetch_selected_results") as fetch,
            ):
                with self.assertRaisesRegex(RuntimeError, "physics_only"):
                    snapshot.main(
                        [
                            "--contract",
                            "contract.json",
                            "--output-dir",
                            str(output),
                            "--max-designs",
                            "60",
                            "--require-exact-designs",
                            "--base-only",
                            "--require-exact-rows",
                            "360",
                            "--minimum-diagnostic-scope",
                            "provisional_minimum",
                        ]
                    )
            fetch.assert_not_called()
            self.assertFalse(output.exists())

    def test_directory_publish_is_no_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            (source / "proof.txt").write_text("owned", encoding="utf-8")
            snapshot._rename_directory_no_replace(source, destination)
            self.assertFalse(source.exists())
            self.assertEqual((destination / "proof.txt").read_text(encoding="utf-8"), "owned")

            other = root / "other"
            other.mkdir()
            (other / "proof.txt").write_text("foreign", encoding="utf-8")
            with self.assertRaises(OSError):
                snapshot._rename_directory_no_replace(other, destination)
            self.assertEqual((destination / "proof.txt").read_text(encoding="utf-8"), "owned")

    def test_fetch_failure_writes_no_output(self) -> None:
        fake = FakeTime()
        item = settled(1, "g1", "train")
        campaign_args = SimpleNamespace(
            scheduler_url="http://scheduler",
            timeout=1.0,
        )

        def fail(*_: object) -> str:
            raise HTTPError("http://scheduler", 504, "timeout", Message(), None)

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "snapshot"
            with self.assertRaises(HTTPError):
                snapshot.fetch_selected_results(
                    [item],
                    campaign_args=campaign_args,
                    limiter=rate_limiter(fake),
                    retry_limit=5,
                    backoff_seconds=1.0,
                    max_backoff_seconds=10.0,
                    remote_fetch=fail,
                )
            self.assertFalse(output.exists())

    def test_existing_output_and_unsafe_rate_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                output_dir=Path(tmp),
                max_designs=0,
                request_interval_seconds=0.1,
                max_fetches_per_window=10,
                window_seconds=30.0,
                retry_limit=5,
                backoff_seconds=1.0,
                max_backoff_seconds=10.0,
            )
            with self.assertRaisesRegex(RuntimeError, "must not already exist"):
                snapshot.validate_args(args)
            args.output_dir = Path(tmp) / "new"
            with self.assertRaisesRegex(RuntimeError, ">= 0.5"):
                snapshot.validate_args(args)

    def test_backoff_cap_cannot_be_reduced_below_policy(self) -> None:
        args = argparse.Namespace(
            output_dir=Path("new-output"),
            max_designs=0,
            request_interval_seconds=0.5,
            max_fetches_per_window=10,
            window_seconds=30.0,
            retry_limit=5,
            backoff_seconds=1.0,
            max_backoff_seconds=2.0,
        )
        with self.assertRaisesRegex(RuntimeError, r"max\(10"):
            snapshot.validate_args(args)


if __name__ == "__main__":
    unittest.main()
