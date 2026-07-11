from __future__ import annotations

import argparse
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
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
        with self.assertRaisesRegex(RuntimeError, "max\(10"):
            snapshot.validate_args(args)


if __name__ == "__main__":
    unittest.main()
