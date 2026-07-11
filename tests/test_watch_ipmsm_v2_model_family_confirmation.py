from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import confirm_ipmsm_v2_model_families as confirmation
import watch_ipmsm_v2_model_family_confirmation as watcher
from tests.test_supervise_ipmsm_v2_pipeline import Fixture, gate


class WatcherHarness:
    def __init__(self, root: Path) -> None:
        self.fixture = Fixture(root)
        contract = self.fixture.load()
        frozen_root = root / "frozen"
        frozen_root.mkdir()
        input_paths: dict[str, Path] = {}
        for name in (
            "baseline_metadata",
            "frozen_selection_manifest",
            "audit_case_plan",
            "untouched_plan_manifest",
            "full_case_plan",
            "explored_case_plan",
        ):
            path = frozen_root / f"{name}.json"
            path.write_text(f'{{"name":"{name}"}}\n', encoding="utf-8")
            input_paths[name] = path.resolve()
        inputs = watcher.FrozenInputs(**input_paths)
        sources = {
            "watcher": Path(watcher.__file__).resolve(),
            "confirmation": Path(confirmation.__file__).resolve(),
        }
        self.bound = watcher.BoundContext(
            contract=contract,
            contract_file_sha256=watcher.sha256_file(contract.source),
            sources=sources,
            source_sha256={
                name: watcher.sha256_file(path) for name, path in sources.items()
            },
            inputs=inputs,
            input_sha256={
                name: watcher.sha256_file(path)
                for name, path in inputs.as_mapping().items()
            },
            frozen_selection={},
            untouched_contract={},
        )
        self.paths = watcher.make_paths(root / "confirmation-sidecar")

    def ready(self) -> watcher.Readiness:
        return watcher.Readiness(
            True,
            "ready",
            "audited",
            data_sha256="d" * 64,
            gate_decision="skip_stage2",
            gate_passed=True,
        )

    @staticmethod
    def args(*, resume: bool) -> argparse.Namespace:
        return argparse.Namespace(
            resume=resume,
            poll_interval_seconds=1.0,
            overall_timeout_seconds=30.0,
            child_timeout_seconds=17.0,
            n_jobs=3,
        )

    @staticmethod
    def write_json(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(watcher.canonical_json_bytes(value))

    def write_prefix(self, *names: str) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        for name in names:
            self.write_json(self.paths.root / name, {"name": name})


class LauncherSourceTests(unittest.TestCase):
    @staticmethod
    def launcher_path() -> Path:
        return Path(watcher.__file__).resolve().with_name(
            "run_ipmsm_model_family_confirmation.ps1"
        )

    def test_launcher_ast_parses_and_uses_native_argument_splatting(self) -> None:
        path = self.launcher_path()
        source = path.read_text(encoding="utf-8-sig")
        self.assertNotIn("Start-Process", source)
        self.assertNotIn("-ArgumentList", source)
        self.assertNotIn("-WindowStyle", source)
        self.assertRegex(
            source,
            r"(?m)^\s*& \$python @arguments 1>> \$stdout 2>> \$stderr\s*$",
        )
        self.assertIn("Push-Location -LiteralPath $repoRoot", source)
        self.assertIn("Pop-Location", source)
        self.assertIn("$exitCode = $LASTEXITCODE", source)
        self.assertIn("exit $exitCode", source)

        powershell = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        parser_script = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:IPMSM_CONFIRMATION_LAUNCHER,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -ne 0) {
    $errors | ForEach-Object { Write-Output $_.Message }
    exit 2
}
$splats = @($ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $node.Splatted -and
        $node.VariablePath.UserPath -eq 'arguments'
}, $true))
Write-Output 'PARSE=OK'
Write-Output ("ARGUMENT_SPLATS={0}" -f $splats.Count)
"""
        completed = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                parser_script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=30.0,
            check=False,
            env={
                **os.environ,
                "IPMSM_CONFIRMATION_LAUNCHER": str(path),
            },
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PARSE=OK", completed.stdout)
        self.assertIn("ARGUMENT_SPLATS=1", completed.stdout)

    def test_launcher_pins_venv_logs_and_resume_contract(self) -> None:
        source = self.launcher_path().read_text(encoding="utf-8-sig")
        self.assertIn(
            "$python = Join-Path $repoRoot '.venv\\Scripts\\python.exe'",
            source,
        )
        self.assertIn(
            "$watcher = Join-Path $repoRoot "
            "'watch_ipmsm_v2_model_family_confirmation.py'",
            source,
        )
        self.assertIn(
            "$stdout = Join-Path $artifactDir "
            "'foundation_stage1_model_family_confirmation_v1.stdout.log'",
            source,
        )
        self.assertIn(
            "$stderr = Join-Path $artifactDir "
            "'foundation_stage1_model_family_confirmation_v1.stderr.log'",
            source,
        )
        self.assertRegex(
            source,
            r"(?s)if \(\(Test-Path -LiteralPath \$outputDir\) -or "
            r"\(Test-Path -LiteralPath \$pidFile\)\) \{\s*"
            r"\$arguments \+= '--resume'\s*\}",
        )
        self.assertIn("$watcher,", source)
        self.assertIn("'--execute'", source)
        self.assertIn("'--poll-interval-seconds', $PollIntervalSeconds", source)
        self.assertIn("'--overall-timeout-seconds', $OverallTimeoutSeconds", source)
        self.assertIn("'--child-timeout-seconds', $ChildTimeoutSeconds", source)
        self.assertIn("'--n-jobs', $NJobs", source)


class ReadinessAndDryRunTests(unittest.TestCase):
    def test_dry_run_waits_without_writing_for_missing_and_partial_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            with mock.patch.object(watcher, "assert_bound_context"):
                missing = watcher.inspect_sidecar(
                    harness.bound,
                    harness.paths,
                    n_jobs=3,
                )
            self.assertEqual(missing["status"], "waiting")
            self.assertEqual(missing["readiness"]["phase"], "stage1_results")
            self.assertFalse(harness.paths.root.exists())
            self.assertFalse(harness.paths.pid.exists())
            self.assertFalse(harness.paths.execution_lock.exists())

            harness.fixture.stage1_campaign()
            with mock.patch.object(watcher, "assert_bound_context"):
                validation_wait = watcher.inspect_sidecar(
                    harness.bound,
                    harness.paths,
                    n_jobs=3,
                )
            self.assertEqual(validation_wait["status"], "waiting")
            self.assertEqual(
                validation_wait["readiness"]["phase"],
                "official_validation",
            )

            harness.fixture.validation()
            with mock.patch.object(watcher, "assert_bound_context"):
                training_wait = watcher.inspect_sidecar(
                    harness.bound,
                    harness.paths,
                    n_jobs=3,
                )
            self.assertEqual(training_wait["status"], "waiting")
            self.assertEqual(
                training_wait["readiness"]["phase"],
                "official_training",
            )
            self.assertFalse(harness.paths.root.exists())
            self.assertFalse(harness.paths.pid.exists())

    def test_exact_audited_stage1_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            harness.fixture.stage1_campaign()
            harness.fixture.training()
            with (
                mock.patch.object(watcher, "assert_bound_context"),
                mock.patch.object(
                    watcher.supervisor,
                    "_audit_stage1_training",
                    return_value=gate("skip_stage2"),
                ),
            ):
                readiness = watcher.inspect_readiness(harness.bound)
            self.assertTrue(readiness.ready)
            self.assertEqual(readiness.phase, "ready")
            self.assertEqual(
                readiness.data_sha256,
                watcher.sha256_file(harness.bound.contract.stage1.result),
            )
            self.assertEqual(readiness.gate_decision, "skip_stage2")
            self.assertTrue(readiness.gate_passed)

    def test_corrupt_visible_final_is_terminal_instead_of_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            harness.bound.contract.stage1.output_dir.mkdir()
            with (
                mock.patch.object(watcher, "assert_bound_context"),
                self.assertRaisesRegex(
                    watcher.ConfirmationWatcherError,
                    "structurally partial",
                ),
            ):
                watcher.inspect_readiness(harness.bound)

    def test_existing_non_regular_official_files_are_terminal(self) -> None:
        for artifact in ("validation", "metadata", "r2"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as tmp:
                harness = WatcherHarness(Path(tmp))
                harness.fixture.stage1_campaign()
                if artifact != "validation":
                    harness.fixture.validation()
                path = getattr(harness.bound.contract.stage1, artifact)
                path.mkdir(parents=True)
                with (
                    mock.patch.object(watcher, "assert_bound_context"),
                    self.assertRaisesRegex(
                        watcher.ConfirmationWatcherError,
                        "invalid path type",
                    ),
                ):
                    watcher.inspect_readiness(harness.bound)

    def test_dry_run_rejects_corrupt_completed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            harness.write_prefix(
                watcher.LOCK_NAME,
                watcher.REPORT_NAME,
                watcher.COMPLETION_NAME,
            )
            with (
                mock.patch.object(
                    watcher,
                    "inspect_readiness",
                    return_value=harness.ready(),
                ),
                mock.patch.object(
                    watcher,
                    "audit_completion",
                    side_effect=watcher.ConfirmationWatcherError(
                        "completion manifest differs from exact replay"
                    ),
                ),
                self.assertRaisesRegex(
                    watcher.ConfirmationWatcherError,
                    "differs from exact replay",
                ),
            ):
                watcher.inspect_sidecar(harness.bound, harness.paths, n_jobs=3)


class CommandAndProcessTests(unittest.TestCase):
    def test_exact_argv_uses_current_interpreter_and_resume_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            expected = [
                sys.executable,
                str(harness.bound.sources["confirmation"]),
                "--data",
                str(harness.bound.contract.stage1.result),
                "--baseline-metadata",
                str(harness.bound.inputs.baseline_metadata),
                "--frozen-selection-manifest",
                str(harness.bound.inputs.frozen_selection_manifest),
                "--audit-case-plan",
                str(harness.bound.inputs.audit_case_plan),
                "--untouched-plan-manifest",
                str(harness.bound.inputs.untouched_plan_manifest),
                "--full-case-plan",
                str(harness.bound.inputs.full_case_plan),
                "--explored-case-plan",
                str(harness.bound.inputs.explored_case_plan),
                "--lock-output",
                str(harness.paths.lock_output),
                "--output",
                str(harness.paths.report),
                "--n-jobs",
                "3",
            ]
            self.assertEqual(
                watcher.build_confirmation_argv(
                    harness.bound,
                    harness.paths,
                    n_jobs=3,
                    resume=False,
                ),
                expected,
            )
            self.assertEqual(
                watcher.build_confirmation_argv(
                    harness.bound,
                    harness.paths,
                    n_jobs=3,
                    resume=True,
                ),
                [*expected, "--resume"],
            )

    def test_child_is_shell_free_and_honors_exact_timeout(self) -> None:
        completed = subprocess.CompletedProcess(["child"], 0, "ok", "")
        with mock.patch.object(
            watcher.subprocess,
            "run",
            return_value=completed,
        ) as run:
            actual = watcher.run_child(
                ["child", "--flag"],
                workdir=Path("workdir"),
                timeout_seconds=17.25,
            )
        self.assertIs(actual, completed)
        run.assert_called_once_with(
            ["child", "--flag"],
            cwd=Path("workdir"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            shell=False,
            timeout=17.25,
            check=False,
        )

        with (
            mock.patch.object(
                watcher.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["child"], 9.0),
            ),
            self.assertRaisesRegex(watcher.ConfirmationWatcherError, "timed out after 9s"),
        ):
            watcher.run_child(
                ["child"],
                workdir=Path("workdir"),
                timeout_seconds=9.0,
            )


class PidAndIsolationTests(unittest.TestCase):
    @staticmethod
    def marker_payload(
        marker: watcher.PidMarker,
        *,
        pid: int = 4242,
    ) -> dict[str, object]:
        return {
            **marker.expected_identity(),
            "pid": pid,
            "nonce": "stale-nonce",
            "boot_time_epoch": 1.0,
        }

    def test_active_stale_and_wrong_identity_pid_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            active = watcher.PidMarker(
                harness.paths.pid,
                harness.bound,
                harness.paths.root,
                resume=True,
            )
            harness.write_json(harness.paths.pid, self.marker_payload(active))
            with (
                mock.patch.object(watcher, "marker_is_current_boot", return_value=True),
                mock.patch.object(
                    watcher.continuation,
                    "pid_is_running",
                    return_value=True,
                ),
                self.assertRaisesRegex(watcher.ConfirmationWatcherError, "is active"),
            ):
                active.__enter__()
            self.assertTrue(harness.paths.pid.is_file())

            harness.paths.pid.unlink()
            stale_without_resume = watcher.PidMarker(
                harness.paths.pid,
                harness.bound,
                harness.paths.root,
                resume=False,
            )
            harness.write_json(
                harness.paths.pid,
                self.marker_payload(stale_without_resume),
            )
            with (
                mock.patch.object(watcher, "marker_is_current_boot", return_value=True),
                mock.patch.object(
                    watcher.continuation,
                    "pid_is_running",
                    return_value=False,
                ),
                self.assertRaisesRegex(
                    watcher.ConfirmationWatcherError,
                    "stale PID marker requires --resume",
                ),
            ):
                stale_without_resume.__enter__()

            stale_with_resume = watcher.PidMarker(
                harness.paths.pid,
                harness.bound,
                harness.paths.root,
                resume=True,
            )
            with (
                mock.patch.object(watcher, "marker_is_current_boot", return_value=True),
                mock.patch.object(
                    watcher.continuation,
                    "pid_is_running",
                    return_value=False,
                ),
                stale_with_resume,
            ):
                current = watcher.read_json(harness.paths.pid, "PID marker")
                self.assertEqual(current["pid"], os.getpid())
                self.assertEqual(current["nonce"], stale_with_resume.nonce)
            self.assertFalse(harness.paths.pid.exists())

            wrong = watcher.PidMarker(
                harness.paths.pid,
                harness.bound,
                harness.paths.root,
                resume=True,
            )
            wrong_payload = self.marker_payload(wrong)
            wrong_payload["contract_sha256"] = "0" * 64
            harness.write_json(harness.paths.pid, wrong_payload)
            with self.assertRaisesRegex(
                watcher.ConfirmationWatcherError,
                "another sidecar identity",
            ):
                wrong.__enter__()
            self.assertTrue(harness.paths.pid.is_file())

    def test_sidecar_paths_and_lock_are_isolated_from_sealed_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            watcher.validate_paths(harness.paths, harness.bound)
            self.assertFalse(harness.paths.pid.is_relative_to(harness.paths.root))
            self.assertFalse(
                harness.paths.execution_lock.is_relative_to(harness.paths.root)
            )
            self.assertNotEqual(
                harness.paths.execution_lock,
                harness.bound.contract.lock_path,
            )

            overlapping = watcher.make_paths(
                harness.bound.contract.stage1.output_dir / "sidecar"
            )
            with self.assertRaisesRegex(
                watcher.ConfirmationWatcherError,
                "overlaps sealed pipeline state",
            ):
                watcher.validate_paths(overlapping, harness.bound)

            pid_inside = watcher.make_paths(
                Path(tmp) / "other-sidecar",
                Path(tmp) / "other-sidecar" / "pid.json",
            )
            with self.assertRaisesRegex(
                watcher.ConfirmationWatcherError,
                "must stay outside output root",
            ):
                watcher.validate_paths(pid_inside, harness.bound)


class OutputPrefixAndManifestTests(unittest.TestCase):
    def test_report_only_unknown_and_tampered_prefixes_fail_closed(self) -> None:
        invalid_prefixes = (
            (watcher.REPORT_NAME,),
            (watcher.LOCK_NAME, watcher.COMPLETION_NAME),
            (watcher.LOCK_NAME, watcher.REPORT_NAME, "unexpected.json"),
        )
        for index, prefix in enumerate(invalid_prefixes):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as tmp:
                harness = WatcherHarness(Path(tmp))
                harness.write_prefix(*prefix)
                with self.assertRaisesRegex(
                    watcher.ConfirmationWatcherError,
                    "exact supported prefix",
                ):
                    watcher.output_state(harness.paths)

    def test_lock_tamper_is_terminal_and_never_launches_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            harness.write_prefix(watcher.LOCK_NAME)
            with (
                mock.patch.object(
                    watcher,
                    "inspect_readiness",
                    return_value=harness.ready(),
                ),
                mock.patch.object(
                    watcher,
                    "audit_lock_only",
                    side_effect=watcher.ConfirmationWatcherError(
                        "confirmation lock audit failed: tampered"
                    ),
                ),
                mock.patch.object(watcher, "run_child") as child,
                self.assertRaisesRegex(
                    watcher.ConfirmationWatcherError,
                    "lock audit failed",
                ),
            ):
                watcher.inspect_sidecar(harness.bound, harness.paths, n_jobs=3)
            child.assert_not_called()

    def test_completion_manifest_binds_all_source_input_and_output_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            readiness = harness.ready()
            report = {"status": "negative_confirmation"}
            completion = watcher.completion_document(
                harness.bound,
                harness.paths,
                readiness,
                report,
                "a" * 64,
                "b" * 64,
            )
            self.assertEqual(completion["contract"]["file_sha256"], harness.bound.contract_file_sha256)
            self.assertEqual(completion["data"]["sha256"], readiness.data_sha256)
            self.assertEqual(completion["data"]["rows"], 2)
            self.assertEqual(
                completion["confirmation_lock"]["sha256"],
                "a" * 64,
            )
            self.assertEqual(
                completion["confirmation_report"],
                {
                    "path": str(harness.paths.report),
                    "sha256": "b" * 64,
                    "status": "negative_confirmation",
                },
            )
            self.assertEqual(
                set(completion["sources"]),
                set(harness.bound.sources),
            )
            self.assertEqual(
                set(completion["inputs"]),
                set(harness.bound.inputs.as_mapping()),
            )
            unsigned = dict(completion)
            unsigned.pop("completion_sha256")
            self.assertEqual(
                completion["completion_sha256"],
                watcher.canonical_sha256(unsigned),
            )

            changed_bound = copy.copy(harness.bound)
            changed_hashes = dict(changed_bound.source_sha256)
            changed_hashes["watcher"] = "f" * 64
            object.__setattr__(changed_bound, "source_sha256", changed_hashes)
            changed = watcher.completion_document(
                changed_bound,
                harness.paths,
                readiness,
                report,
                "a" * 64,
                "b" * 64,
            )
            self.assertNotEqual(
                changed["completion_sha256"],
                completion["completion_sha256"],
            )


class ExecuteLifecycleTests(unittest.TestCase):
    @staticmethod
    def execute_patches(
        harness: WatcherHarness,
        child: object,
    ) -> tuple[object, ...]:
        report = {"status": "negative_confirmation"}
        return (
            mock.patch.object(watcher, "assert_bound_context"),
            mock.patch.object(
                watcher,
                "inspect_readiness",
                return_value=harness.ready(),
            ),
            mock.patch.object(
                watcher,
                "audit_lock_and_report",
                return_value=(report, "a" * 64, "b" * 64),
            ),
            mock.patch.object(
                watcher,
                "audit_completion",
                return_value=report,
            ),
            mock.patch.object(watcher, "run_child", side_effect=child),
        )

    def test_fresh_execution_launches_child_once_and_publishes_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))

            def publish_pair(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                harness.write_prefix(watcher.LOCK_NAME, watcher.REPORT_NAME)
                return subprocess.CompletedProcess(list(args[0]), 0, "", "")

            patches = self.execute_patches(harness, publish_pair)
            with patches[0], patches[1], patches[2], patches[3], patches[4] as child:
                result = watcher.execute_sidecar(
                    harness.bound,
                    harness.paths,
                    harness.args(resume=False),
                )
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["confirmation_status"], "negative_confirmation")
            child.assert_called_once()
            argv = child.call_args.args[0]
            self.assertEqual(argv[0], sys.executable)
            self.assertNotIn("--resume", argv)
            self.assertEqual(child.call_args.kwargs["timeout_seconds"], 17.0)
            self.assertEqual(
                watcher.output_state(harness.paths),
                frozenset(
                    {
                        watcher.LOCK_NAME,
                        watcher.REPORT_NAME,
                        watcher.COMPLETION_NAME,
                    }
                ),
            )

    def test_lock_only_resume_passes_resume_to_exactly_one_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            harness.write_prefix(watcher.LOCK_NAME)

            def publish_report(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                harness.write_json(harness.paths.report, {"status": "negative_confirmation"})
                return subprocess.CompletedProcess(list(args[0]), 0, "", "")

            patches = self.execute_patches(harness, publish_report)
            lock_audit = {"context": {}, "lock_file_sha256": "a" * 64}
            with (
                patches[0],
                patches[1],
                mock.patch.object(
                    watcher,
                    "audit_lock_only",
                    return_value=lock_audit,
                ),
                patches[2],
                patches[3],
                patches[4] as child,
            ):
                result = watcher.execute_sidecar(
                    harness.bound,
                    harness.paths,
                    harness.args(resume=True),
                )
            self.assertEqual(result["status"], "complete")
            child.assert_called_once()
            self.assertEqual(child.call_args.args[0][-1], "--resume")

    def test_lock_and_report_publish_completion_without_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            harness.write_prefix(watcher.LOCK_NAME, watcher.REPORT_NAME)
            patches = self.execute_patches(harness, None)
            with patches[0], patches[1], patches[2], patches[3], patches[4] as child:
                result = watcher.execute_sidecar(
                    harness.bound,
                    harness.paths,
                    harness.args(resume=True),
                )
            self.assertEqual(result["status"], "complete")
            child.assert_not_called()
            self.assertTrue(harness.paths.completion.is_file())

    def test_complete_state_is_idempotent_without_child_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            harness.write_prefix(
                watcher.LOCK_NAME,
                watcher.REPORT_NAME,
                watcher.COMPLETION_NAME,
            )
            before = harness.paths.completion.read_bytes()
            patches = self.execute_patches(harness, None)
            with patches[0], patches[1], patches[2], patches[3], patches[4] as child:
                result = watcher.execute_sidecar(
                    harness.bound,
                    harness.paths,
                    harness.args(resume=False),
                )
            self.assertEqual(result["status"], "already_complete")
            self.assertEqual(harness.paths.completion.read_bytes(), before)
            child.assert_not_called()

    def test_readiness_change_during_final_audit_blocks_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = WatcherHarness(Path(tmp))
            harness.write_prefix(watcher.LOCK_NAME, watcher.REPORT_NAME)
            initial = harness.ready()
            changed = watcher.Readiness(
                True,
                "ready",
                "audited",
                data_sha256="e" * 64,
                gate_decision="skip_stage2",
                gate_passed=True,
            )
            patches = self.execute_patches(harness, None)
            with (
                patches[0],
                mock.patch.object(
                    watcher,
                    "inspect_readiness",
                    side_effect=[initial, initial, initial, changed],
                ),
                patches[2],
                patches[3],
                patches[4] as child,
                self.assertRaisesRegex(
                    watcher.ConfirmationWatcherError,
                    "changed during completion audit",
                ),
            ):
                watcher.execute_sidecar(
                    harness.bound,
                    harness.paths,
                    harness.args(resume=True),
                )
            child.assert_not_called()
            self.assertFalse(harness.paths.completion.exists())


if __name__ == "__main__":
    unittest.main()
