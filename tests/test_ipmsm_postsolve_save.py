"""Save outcomes must gate exports/ok rows; all AEDT objects are inert fixtures."""
from unittest import mock
import unittest

import run_ipmsm_batch as runner
from tests import test_run_ipmsm_batch_aedt_backend as fixtures


class PostSolveSaveTests(unittest.TestCase):
    def run_case(self, *, backend="standalone", saved=True, save_error=None,
                 legacy_saved=None, reuse_pid=False, configure_error=None):
        fixture = fixtures.RunIpmsmBatchAedtBackendTests()
        events, sdk_saves, legacy_saves = [], [], []
        original_create = fixtures.FakeProject.create_design

        def create(project, **kwargs):
            app = original_create(project, **kwargs)

            def save_project():
                sdk_saves.append(project.path)
                if reuse_pid:
                    project.desktop.identity_epoch += 1
                if save_error is not None:
                    raise save_error
                return saved

            app.save_project = save_project
            return app

        def legacy_save(project):
            legacy_saves.append(project.path)
            if backend == "pooled" and save_error is not None:
                raise save_error
            return legacy_saved

        lease = fixtures.FakeLease(events) if backend == "pooled" else None
        with mock.patch.object(fixtures.FakeProject, "create_design", new=create), \
             mock.patch.object(fixtures.FakeProject, "save", new=legacy_save), \
             mock.patch.object(runner, "export_ppt_reports", return_value={}) as export, \
             mock.patch.object(runner, "summarize_transient_outputs", return_value={}), \
             mock.patch.object(runner, "missing_required_output_metrics", return_value=[]):
            row = fixture._run_case(backend=backend, analyze=True, events=events, lease=lease,
                                    configure_error=configure_error,
                                    configure_error_is_solver=False)
        return fixture, events, row, export, sdk_saves, legacy_saves

    def test_standalone_requires_exact_True_and_still_exports_after_success(self):
        fixture, events, row, export, sdk, legacy = self.run_case()
        self.assertEqual(row["status"], "ok")
        self.assertEqual(fixture.persisted_rows[0]["status"], "ok")
        self.assertEqual(len(sdk), 1)
        self.assertEqual(legacy, [])
        export.assert_called_once()
        self.assertEqual(len([e for e in events if e[0] == "native_analyze"]), 1)
        self.assertEqual(len([e for e in events if e[0] == "native_quit"]), 1)

    def test_standalone_False_None_integer_and_exception_stop_export_without_resave(self):
        for value, error in [(False, None), (None, None), (1, None),
                             (True, RuntimeError("SDK save failed"))]:
            with self.subTest(value=value, error=error):
                fixture, events, row, export, sdk, legacy = self.run_case(saved=value, save_error=error)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["error_class"], "project_save_failed")
                self.assertEqual(fixture.persisted_rows[0]["status"], "failed")
                self.assertEqual(len(sdk), 1)
                self.assertEqual(legacy, [])
                export.assert_not_called()
                self.assertIs(row["standalone_solver_uncertain"], False)
                self.assertEqual(fixture.preserved_solver_state["stage"], "analyze_returned_true")
                self.assertEqual(len([e for e in events if e[0] == "native_quit"]), 1)

    def test_pooled_void_None_save_is_success_and_does_not_use_standalone_API(self):
        _, _, row, export, sdk, legacy = self.run_case(backend="pooled", legacy_saved=None)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(len(legacy), 1)
        self.assertEqual(sdk, [])
        export.assert_called_once()

    def test_pooled_False_or_exception_stop_export_without_resave(self):
        for value, error in [(False, None), (None, RuntimeError("Native save exception"))]:
            with self.subTest(value=value, error=error):
                fixture, _, row, export, sdk, legacy = self.run_case(
                    backend="pooled", legacy_saved=value, save_error=error)
                self.assertEqual(row["status"], "failed")
                self.assertEqual(row["error_class"], "project_save_failed")
                self.assertEqual(fixture.persisted_rows[0]["status"], "failed")
                self.assertEqual(len(legacy), 1)
                self.assertEqual(sdk, [])
                export.assert_not_called()

    def test_save_failure_with_reused_PID_preserves_existing_no_quit_guard(self):
        _, events, row, export, sdk, legacy = self.run_case(saved=False, reuse_pid=True)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(len(sdk), 1)
        self.assertEqual(legacy, [])
        export.assert_not_called()
        self.assertEqual([e for e in events if e[0] in {"native_quit", "native_close_project"}], [])

    def test_unrelated_failure_preserves_original_diagnostic_save_path(self):
        _, _, row, export, sdk, legacy = self.run_case(configure_error=RuntimeError("setup failed"))
        self.assertEqual(row["status"], "failed")
        self.assertNotEqual(row.get("error_class"), "project_save_failed")
        self.assertEqual(sdk, [])
        self.assertEqual(len(legacy), 1)
        export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
