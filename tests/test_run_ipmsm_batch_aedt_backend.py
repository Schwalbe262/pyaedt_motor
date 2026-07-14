from __future__ import annotations

import csv
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from module import aedt_attach_client
import run_ipmsm_batch as runner


class FakeProject:
    def __init__(self, path: str, events: list[tuple[str, object]]) -> None:
        self.path = path
        self.events = events

    def save(self) -> None:
        self.events.append(("save", self.path))

    def close(self) -> None:
        self.events.append(("close_project", self.path))


class FakeDesktop:
    def __init__(self, events: list[tuple[str, object]], kwargs: dict[str, object]) -> None:
        self.events = events
        self.events.append(("desktop", kwargs))

    def create_project(self, path: str, name: str) -> FakeProject:
        self.events.append(("create_project", name))
        return FakeProject(path, self.events)

    def release_desktop(self, **kwargs: object) -> None:
        self.events.append(("release_desktop", kwargs))


class FakeLease:
    def __init__(
        self,
        events: list[tuple[str, object]],
        *,
        wait_error: Exception | None = None,
        release_state: str = "released",
        release_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.wait_error = wait_error
        self.release_state = release_state
        self.release_error = release_error

    def wait_until_leased(self, *, timeout_seconds: int) -> dict[str, object]:
        self.events.append(("wait", timeout_seconds))
        if self.wait_error is not None:
            raise self.wait_error
        return {"state": "leased", "endpoint": "n114:50051"}

    def connect_desktop(self, *, non_graphical: bool, desktop_factory: object) -> object:
        self.events.append(("connect", non_graphical))
        return desktop_factory(
            new_desktop=False,
            non_graphical=non_graphical,
            close_on_exit=False,
            machine="n114",
            port=50051,
        )

    def bind_project_name(self, project_name: str) -> dict[str, object]:
        self.events.append(("bind", project_name))
        return {"state": "active"}

    def report_fault(self, fault_kind: str) -> dict[str, object]:
        self.events.append(("fault", fault_kind))
        return {"state": "failed"}

    def release(self, *, wait_seconds: int) -> dict[str, object]:
        self.events.append(("release", wait_seconds))
        if self.release_error is not None:
            raise self.release_error
        return {"state": self.release_state}


class RunIpmsmBatchAedtBackendTests(unittest.TestCase):
    def test_backend_selection_precedence(self) -> None:
        self.assertEqual(runner.resolve_aedt_backend(None, {}), "standalone")
        self.assertEqual(
            runner.resolve_aedt_backend(None, {"MFT_AEDT_BACKEND": ""}),
            "standalone",
        )
        self.assertEqual(
            runner.resolve_aedt_backend(None, {"MFT_AEDT_BACKEND": "pooled"}),
            "pooled",
        )
        self.assertEqual(
            runner.resolve_aedt_backend(
                "standalone",
                {"MFT_AEDT_BACKEND": "pooled"},
            ),
            "standalone",
        )
        self.assertEqual(
            runner.resolve_aedt_backend(
                "pooled",
                {"MFT_AEDT_BACKEND": "standalone"},
            ),
            "pooled",
        )
        with self.assertRaisesRegex(ValueError, "standalone or pooled"):
            runner.resolve_aedt_backend(None, {"MFT_AEDT_BACKEND": "shared"})

        with mock.patch.object(sys, "argv", ["run_ipmsm_batch.py", "--aedt-backend", "pooled"]):
            parsed = runner.parse_args()
        self.assertEqual(
            runner.resolve_aedt_backend(parsed.aedt_backend, {"MFT_AEDT_BACKEND": "standalone"}),
            "pooled",
        )

    def _run_case(
        self,
        *,
        backend: str,
        analyze: bool,
        events: list[tuple[str, object]],
        lease: FakeLease | None = None,
        configure_error: Exception | None = None,
        configure_error_is_solver: bool = True,
        analysis_returns_false: bool = False,
        acquire_error: Exception | None = None,
    ) -> dict[str, object]:
        core_module = types.ModuleType("pyaedt_module.core")
        ansys_core_module = types.ModuleType("ansys.aedt.core")
        settings = types.SimpleNamespace(
            enable_error_handler=True,
            skip_license_check=False,
            wait_for_license=True,
        )

        def desktop_factory(**kwargs: object) -> FakeDesktop:
            return FakeDesktop(events, kwargs)

        core_module.pyDesktop = desktop_factory
        package_module = types.ModuleType("pyaedt_module")
        package_module.core = core_module
        ansys_core_module.settings = settings

        def create_design(_project: object, _sim: object) -> tuple[object, None, dict[str, object]]:
            events.append(("create_design", None))
            return object(), None, {}

        def configure(*_args: object, **_kwargs: object) -> dict[str, object]:
            events.append(("configure", None))
            if configure_error is not None:
                callback = _kwargs.get("analysis_error_callback")
                if configure_error_is_solver and callable(callback):
                    callback()
                raise configure_error
            analysis = False if analysis_returns_false else (True if analyze else None)
            return {"analysis": analysis, "validation": True}

        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp) / "base"
            base_dir.mkdir()
            options = runner.RunnerOptions(
                simulation_dir=str(Path(tmp) / "simulation"),
                result_csv=str(Path(tmp) / "results.csv"),
                analyze=analyze,
                non_graphical=True,
                cleanup_linux=False,
                symmetry_factor=1,
                use_periodic_boundary=False,
                cores=1,
                aedt_backend=backend,
            )
            environment = {
                "MFT_AEDT_SCHEDULER_URL": "http://172.16.10.37:18790",
                "MFT_AEDT_LEASE_WAIT_SECONDS": "41",
                "MFT_AEDT_RELEASE_WAIT_SECONDS": "42",
                "SLURM_SCHED_TASK_ID": "321",
                "SLURMD_NODENAME": "n114",
            }

            def acquire(*args: object, **kwargs: object) -> FakeLease:
                events.append(("acquire", (args, kwargs)))
                if acquire_error is not None:
                    raise acquire_error
                if lease is None:
                    raise AssertionError("pooled acquire was not expected")
                return lease

            with mock.patch.object(runner, "BASE_DIR", base_dir), mock.patch.dict(
                os.environ,
                environment,
                clear=False,
            ), mock.patch.dict(
                sys.modules,
                {
                    "pyaedt_module": package_module,
                    "pyaedt_module.core": core_module,
                    "ansys.aedt.core": ansys_core_module,
                },
            ), mock.patch.object(
                aedt_attach_client,
                "acquire_project_lease",
                side_effect=acquire,
            ), mock.patch(
                "module.ipmsm_geometry.create_ipmsm_design",
                side_effect=create_design,
            ), mock.patch(
                "module.ipmsm_ppt_setup.configure_ipmsm_from_ppt",
                side_effect=configure,
            ), mock.patch.object(
                runner,
                "output_physics_issues",
                return_value=[],
            ), mock.patch(
                "logging.exception"
            ):
                row = runner.run_one_case(({"case_id": "case-001"}, options.__dict__))
            with Path(options.result_csv).open("r", encoding="utf-8-sig", newline="") as file:
                self.persisted_rows = list(csv.DictReader(file))
            return row

    def test_pooled_happy_path_binds_closes_then_releases(self) -> None:
        events: list[tuple[str, object]] = []
        lease = FakeLease(events)

        row = self._run_case(
            backend="pooled",
            analyze=False,
            events=events,
            lease=lease,
        )

        self.assertEqual(row["status"], "ok")
        acquire_args, acquire_kwargs = next(value for name, value in events if name == "acquire")
        self.assertEqual(acquire_args[0], "http://172.16.10.37:18790")
        self.assertEqual(acquire_kwargs["request_key"], "")
        self.assertEqual(acquire_kwargs["task_id"], 321)
        self.assertEqual(acquire_kwargs["allocation_id"], 0)
        self.assertEqual(acquire_kwargs["node_name"], "n114")
        project_name = acquire_args[1]
        self.assertEqual(next(value for name, value in events if name == "bind"), project_name)
        names = [name for name, _value in events]
        self.assertLess(names.index("wait"), names.index("connect"))
        self.assertLess(names.index("connect"), names.index("create_project"))
        self.assertLess(names.index("create_project"), names.index("bind"))
        self.assertLess(names.index("bind"), names.index("close_project"))
        self.assertLess(names.index("close_project"), names.index("release"))
        for name in ("bind", "close_project", "release"):
            self.assertEqual(names.count(name), 1)
        self.assertNotIn("release_desktop", names)
        desktop_kwargs = next(value for name, value in events if name == "desktop")
        self.assertEqual(
            desktop_kwargs,
            {
                "new_desktop": False,
                "non_graphical": True,
                "close_on_exit": False,
                "machine": "n114",
                "port": 50051,
            },
        )

    def test_pooled_solve_failure_reports_fault_before_release(self) -> None:
        events: list[tuple[str, object]] = []
        lease = FakeLease(events)

        row = self._run_case(
            backend="pooled",
            analyze=True,
            events=events,
            lease=lease,
            configure_error=TimeoutError("solver timed out"),
        )

        self.assertEqual(row["status"], "failed")
        self.assertIn("solver timed out", row["error"])
        names = [name for name, _value in events]
        self.assertEqual(next(value for name, value in events if name == "fault"), "solver_timeout")
        self.assertLess(names.index("fault"), names.index("close_project"))
        self.assertLess(names.index("close_project"), names.index("release"))
        self.assertNotIn("release_desktop", names)

    def test_pooled_pre_solve_failure_does_not_quarantine_session(self) -> None:
        events: list[tuple[str, object]] = []
        lease = FakeLease(events)

        row = self._run_case(
            backend="pooled",
            analyze=True,
            events=events,
            lease=lease,
            configure_error=RuntimeError("material setup failed"),
            configure_error_is_solver=False,
        )

        self.assertEqual(row["status"], "failed")
        self.assertIn("material setup failed", row["error"])
        names = [name for name, _value in events]
        self.assertNotIn("fault", names)
        self.assertLess(names.index("close_project"), names.index("release"))

    def test_pooled_analysis_false_reports_solver_fault(self) -> None:
        events: list[tuple[str, object]] = []
        lease = FakeLease(events)

        row = self._run_case(
            backend="pooled",
            analyze=True,
            events=events,
            lease=lease,
            analysis_returns_false=True,
        )

        self.assertEqual(row["status"], "failed")
        self.assertIn("AEDT analysis returned False", row["error"])
        self.assertEqual(next(value for name, value in events if name == "fault"), "solver_timeout")
        names = [name for name, _value in events]
        self.assertLess(names.index("fault"), names.index("close_project"))
        self.assertLess(names.index("close_project"), names.index("release"))

    def test_pooled_release_requires_close_ack(self) -> None:
        for lease in (
            FakeLease([], release_state="releasing"),
            FakeLease([], release_error=ConnectionError("release relay failed")),
        ):
            with self.subTest(release_state=lease.release_state, release_error=lease.release_error):
                events = lease.events
                row = self._run_case(
                    backend="pooled",
                    analyze=False,
                    events=events,
                    lease=lease,
                )

                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["error_class"], "pooled_release_failed")
                self.assertIn("pooled_release_error", row)
                names = [name for name, _value in events]
                self.assertLess(names.index("close_project"), names.index("release"))
                self.assertNotIn("release_desktop", names)

    def test_pooled_lease_unavailable_fails_closed(self) -> None:
        events: list[tuple[str, object]] = []
        lease = FakeLease(events, wait_error=TimeoutError("no pooled session"))

        row = self._run_case(
            backend="pooled",
            analyze=False,
            events=events,
            lease=lease,
        )

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_class"], "pooled_lease_unavailable")
        self.assertEqual(self.persisted_rows[0]["error_class"], "pooled_lease_unavailable")
        self.assertIn("PooledLeaseUnavailableError", row["error"])
        names = [name for name, _value in events]
        self.assertIn("release", names)
        self.assertNotIn("connect", names)
        self.assertNotIn("desktop", names)
        self.assertNotIn("create_project", names)
        self.assertNotIn("release_desktop", names)

    def test_pooled_control_plane_failure_never_starts_desktop(self) -> None:
        events: list[tuple[str, object]] = []

        row = self._run_case(
            backend="pooled",
            analyze=False,
            events=events,
            acquire_error=ConnectionError("relay unreachable"),
        )

        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_class"], "pooled_lease_unavailable")
        self.assertIn("relay unreachable", row["error"])
        names = [name for name, _value in events]
        self.assertEqual(names.count("acquire"), 1)
        self.assertNotIn("wait", names)
        self.assertNotIn("desktop", names)
        self.assertNotIn("release_desktop", names)

    def test_standalone_lifecycle_kwargs_remain_unchanged(self) -> None:
        events: list[tuple[str, object]] = []

        row = self._run_case(
            backend="standalone",
            analyze=False,
            events=events,
        )

        self.assertEqual(row["status"], "ok")
        self.assertEqual(
            next(value for name, value in events if name == "desktop"),
            {
                "version": None,
                "non_graphical": True,
                "close_on_exit": True,
                "new_desktop": True,
            },
        )
        self.assertEqual(
            next(value for name, value in events if name == "release_desktop"),
            {"close_projects": True, "close_on_exit": True},
        )
        self.assertNotIn("close_project", [name for name, _value in events])


if __name__ == "__main__":
    unittest.main()
