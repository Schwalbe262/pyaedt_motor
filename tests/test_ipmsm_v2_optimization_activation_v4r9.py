from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import build_ipmsm_v2_optimization_activation_v4r9 as builder
import continue_ipmsm_v2_optimization_v4r9 as runner
import supervise_ipmsm_v2_pipeline as v3


def _section(fields: set[str], argv_name: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {name: f"legacy/{name}.dat" for name in fields}
    if argv_name is not None:
        value[argv_name] = [
            "python.exe",
            "legacy.py",
            "--cases",
            "legacy/cases.csv",
            "--remote-cases-dir",
            "remote/leave-relative",
        ]
    return value


class OptimizationActivationBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / "sealed-runtime"
        self.source = self.root / "detached-source"
        self.output = self.runtime / builder.ACTIVATION_RELATIVE_ROOT
        self.runtime.mkdir()
        self.source.mkdir()
        self.output.mkdir(parents=True)

    def pipeline(self) -> dict[str, object]:
        pipeline = {
            "workdir": str(self.runtime),
            "lock_path": "legacy/pipeline.lock",
            "immutable_inputs": [{"path": "legacy/input.json", "sha256": "a" * 64}],
            "external_pid_files": [],
            "stage1": _section(builder.PIPELINE_PATH_FIELDS["stage1"], "campaign_argv"),
            "stage2": _section(builder.PIPELINE_PATH_FIELDS["stage2"], "argv"),
            "stage3": _section(builder.PIPELINE_PATH_FIELDS["stage3"], "continuation_argv"),
            "optimization": _section(
                builder.PIPELINE_PATH_FIELDS["optimization"], "argv_template"
            ),
            "speed": _section(builder.PIPELINE_PATH_FIELDS["speed"], "campaign_argv"),
        }
        speed = pipeline["speed"]
        assert isinstance(speed, dict)
        speed["plan_argv"] = ["python.exe", "generate_ipmsm_second_pass_cases.py"]
        speed["campaign_argv"] = ["python.exe", "run_ipmsm_v2_campaign.py"]
        speed["rank_argv"] = ["python.exe", "rank_ipmsm_second_pass_profiles.py"]
        return pipeline

    def test_source_root_requires_detached_commit_and_no_untracked_files(self) -> None:
        repository = self.root / "exact-source"
        repository.mkdir()
        (repository / "source.py").write_bytes(b"# exact LF source\n")
        (repository / ".gitignore").write_bytes(b"__pycache__/\n")
        subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repository,
            check=True,
        )
        subprocess.run(["git", "add", "source.py", ".gitignore"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "exact"], cwd=repository, check=True)
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip()
        subprocess.run(
            ["git", "checkout", "--quiet", "--detach", revision],
            cwd=repository,
            check=True,
        )
        builder.audit_source_root(repository, revision)
        cache = repository / "__pycache__"
        cache.mkdir()
        stale = cache / "source.cpython-313.pyc"
        stale.write_bytes(b"stale ignored bytecode")
        with self.assertRaisesRegex(builder.OptimizationActivationBuildError, "ignored"):
            builder.audit_source_root(repository, revision)
        stale.unlink()
        cache.rmdir()
        (repository / "untracked.py").write_bytes(b"# must fail closed\n")
        with self.assertRaisesRegex(builder.OptimizationActivationBuildError, "untracked"):
            builder.audit_source_root(repository, revision)

    def test_runner_and_child_keep_detached_source_cache_free(self) -> None:
        repository = self.root / "cache-free-source"
        repository.mkdir()
        project_root = Path(runner.__file__).resolve(strict=True).parent
        activation = repository / "activation.json"
        activation.write_bytes(b"{}\n")
        (repository / ".gitignore").write_bytes(b"__pycache__/\n")
        (repository / "helper.py").write_bytes(b"VALUE = 7\n")
        (repository / "child.py").write_bytes(
            b"import helper\nassert helper.VALUE == 7\n"
        )
        (repository / "probe.py").write_text(
            "\n".join(
                (
                    "from pathlib import Path",
                    "import sys",
                    "import continue_ipmsm_v2_optimization_v4r9 as activation_runner",
                    "root = Path(sys.argv[1]).resolve(strict=True)",
                    "activation_runner._run_child(",
                    "    (sys.executable, str(root / 'child.py')),",
                    "    workdir=root,",
                    "    label='cache-free child',",
                    "    capture_output=True,",
                    "    child_environment=activation_runner.activation_builder.child_environment(",
                    "        root / 'activation.json'",
                    "    ),",
                    ")",
                    "",
                )
            ),
            encoding="utf-8",
            newline="\n",
        )
        subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repository,
            check=True,
        )
        subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "cache-free"], cwd=repository, check=True)
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip()
        subprocess.run(
            ["git", "checkout", "--quiet", "--detach", revision],
            cwd=repository,
            check=True,
        )
        builder.audit_source_root(repository, revision)
        environment = os.environ.copy()
        environment.pop("PYTHONDONTWRITEBYTECODE", None)
        environment.pop("PYTHONPYCACHEPREFIX", None)
        environment["PYTHONPATH"] = str(project_root)
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-X",
                f"pycache_prefix={builder.runner_pycache_prefix(activation)}",
                str(repository / "probe.py"),
                str(repository),
            ],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        builder.audit_source_root(repository, revision)
        self.assertFalse((repository / "__pycache__").exists())

    def test_remote_task_allows_runtime_csv_but_rejects_code_shadow_files(self) -> None:
        repository = self.root / "remote-task-source"
        repository.mkdir()
        (repository / "tracked.py").write_bytes(b"# exact source\n")
        (repository / ".gitignore").write_bytes(b"*.so\n__pycache__/\n")
        subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repository,
            check=True,
        )
        subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "exact"], cwd=repository, check=True)
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip()
        runtime_csv = repository / "remote" / "run" / "cases.csv"
        runtime_csv.parent.mkdir(parents=True)
        runtime_csv.write_bytes(b"case_id\ncase-1\n")

        def shadows(*, ignored: bool) -> list[str]:
            command = ["git", "ls-files", "--others"]
            if ignored:
                command.append("--ignored")
            command.extend(["--exclude-standard", "--", "*.py", "*.pyd", "*.so"])
            return subprocess.check_output(
                command,
                cwd=repository,
                text=True,
            ).splitlines()

        self.assertEqual(shadows(ignored=False), [])
        self.assertEqual(shadows(ignored=True), [])
        (repository / "rogue.py").write_bytes(b"# shadow\n")
        self.assertEqual(shadows(ignored=False), ["rogue.py"])
        (repository / "rogue.py").unlink()
        (repository / "native.so").write_bytes(b"shadow")
        self.assertEqual(shadows(ignored=True), ["native.so"])

        setup = builder.remote_task_env_setup(revision)
        self.assertNotIn("git status", setup)
        self.assertIn("git diff --quiet", setup)
        self.assertIn("git diff --cached --quiet", setup)
        self.assertIn("git ls-files --others --exclude-standard", setup)
        self.assertIn("git ls-files --others --ignored --exclude-standard", setup)

    def test_accepted_numbers_are_exact_not_relatively_close(self) -> None:
        points = [
            SimpleNamespace(
                name="rated_torque",
                speed_rpm=1200.0,
                target_kind="torque",
                target_torque_nm=65.1,
                target_power_w=None,
                duty_weight=0.5,
            ),
            SimpleNamespace(
                name="rated_power_at_max_speed",
                speed_rpm=5000.0,
                target_kind="power",
                target_torque_nm=None,
                target_power_w=7500.0,
                duty_weight=0.5,
            ),
        ]
        spec = SimpleNamespace(
            operating_points=points,
            inverter=SimpleNamespace(vdc_v=200.0, phase_peak_current_limit_a=137.8),
            winding=SimpleNamespace(series_turns_per_phase=48),
            nsga2=SimpleNamespace(
                population_size=160,
                max_generations=300,
                seeds=(42, 43, 44),
                max_fea_candidates=12,
            ),
        )
        builder.assert_accepted_spec(spec)
        spec.inverter.phase_peak_current_limit_a = 137.80000001
        with self.assertRaisesRegex(builder.OptimizationActivationBuildError, "137.8 A"):
            builder.assert_accepted_spec(spec)

    def test_fresh_passed_decision_is_not_pinned_to_stale_parent_stage3_path(self) -> None:
        stale = self.runtime / "v4r4" / "foundation_stage3_decision.json"
        fresh = self.runtime / "v4r10" / "adaptive_stage3_decision.json"
        fresh.parent.mkdir(parents=True)
        fresh.write_bytes(b"fresh completed decision")
        parent = SimpleNamespace(
            base_contract=SimpleNamespace(stage3=SimpleNamespace(decision=stale))
        )
        audited = object()

        with mock.patch.object(
            builder,
            "_legacy_inputs",
            return_value=(audited, {"optimization_spec": self.runtime / "spec.json"}),
        ) as audit:
            actual, _ = builder._audit_fresh_passed_decision(
                parent,
                fresh.resolve(strict=True),
                self.runtime.resolve(strict=True),
            )

        self.assertIs(actual, audited)
        audit.assert_called_once_with(parent, fresh.resolve(strict=True))

    def test_arbitrary_complete_decision_without_stage3_provenance_is_rejected(self) -> None:
        passed = self.runtime / "arbitrary-complete.json"
        completion = self.runtime / "completion.json"
        passed.write_bytes(b"passed")
        completion.write_bytes(b"completion")
        passed_binding = builder._binding(passed)
        completion_binding = builder._binding(completion)
        decision = {"execution_contract": {"stage2": {}}}
        with (
            mock.patch.object(
                builder,
                "_audit_stage3_acquisition_completion",
                return_value=(completion_binding, {}, {}),
            ),
            mock.patch.object(
                builder,
                "_load_lineage_decision",
                return_value=(passed_binding, decision),
            ),
            self.assertRaisesRegex(
                builder.OptimizationActivationBuildError,
                "neither direct precollected nor adaptive-chained",
            ),
        ):
            builder.audit_passed_decision_provenance(
                passed,
                completion,
                self.runtime,
            )

    def test_adaptive_provenance_rejects_noncontiguous_failed_decision_chain(self) -> None:
        artifacts = []
        for name in ("passed", "baseline", "intermediate", "completion"):
            path = self.runtime / f"{name}.json"
            path.write_bytes(name.encode("ascii"))
            artifacts.append(builder._binding(path))
        passed_binding, baseline_binding, intermediate_binding, completion_binding = artifacts
        records = [
            {"decision": baseline_binding.as_mapping()},
            {"decision": intermediate_binding.as_mapping()},
        ]
        fixed = {"path": str(self.runtime / "fixed.csv"), "sha256": "f" * 64}
        final_batch = {
            "batch_index": 2,
            "history_records": records,
            "failed_decision": intermediate_binding.as_mapping(),
            "fixed_audit_case_plan": fixed,
            "r2_history": {"path": str(self.runtime / "history.json"), "sha256": "e" * 64},
        }
        intermediate_batch = {
            "batch_index": 1,
            "history_records": records[:1],
            "failed_decision": intermediate_binding.as_mapping(),
            "fixed_audit_case_plan": fixed,
        }
        passed = {"execution_contract": {"stage2": {"case_manifest": {}}}}
        completion = {"effective_plan": fixed}
        with (
            mock.patch.object(
                builder,
                "_audit_stage3_acquisition_completion",
                return_value=(completion_binding, completion, {}),
            ),
            mock.patch.object(
                builder,
                "_load_lineage_decision",
                side_effect=[
                    (passed_binding, passed),
                    (baseline_binding, {}),
                    (intermediate_binding, {}),
                ],
            ),
            mock.patch.object(
                builder,
                "_adaptive_manifest",
                side_effect=[final_batch, intermediate_batch],
            ),
            mock.patch.object(builder, "_audit_direct_precollected_decision"),
            self.assertRaisesRegex(
                builder.OptimizationActivationBuildError,
                "not contiguous",
            ),
        ):
            builder.audit_passed_decision_provenance(
                passed_binding.path,
                completion_binding.path,
                self.runtime,
            )

    def test_absolutize_moves_workdir_but_keeps_sealed_data_paths(self) -> None:
        pipeline = self.pipeline()
        optimization = pipeline["optimization"]
        assert isinstance(optimization, dict)
        optimization["argv_template"] = [
            "python.exe",
            "continue_ipmsm_v2_optimization.py",
            "--stage2-decision",
            v3.UPSTREAM_PLACEHOLDER,
            "--optimization-spec",
            "legacy/spec.json",
            "--remote-cases-dir",
            "remote/leave-relative",
        ]

        builder._absolutize_legacy_pipeline(
            pipeline,
            runtime_root=self.runtime,
            source_root=self.source,
        )

        self.assertEqual(pipeline["workdir"], str(self.source))
        immutable = pipeline["immutable_inputs"]
        assert isinstance(immutable, list)
        self.assertEqual(immutable[0]["path"], str(self.runtime / "legacy/input.json"))
        for section_name, fields in builder.PIPELINE_PATH_FIELDS.items():
            section = pipeline[section_name]
            assert isinstance(section, dict)
            for field in fields:
                self.assertTrue(Path(str(section[field])).is_absolute())
                self.assertTrue(str(section[field]).startswith(str(self.runtime)))
        transformed_optimization = pipeline["optimization"]
        assert isinstance(transformed_optimization, dict)
        argv = transformed_optimization["argv_template"]
        assert isinstance(argv, list)
        self.assertEqual(
            argv[argv.index("--stage2-decision") + 1],
            v3.UPSTREAM_PLACEHOLDER,
        )
        self.assertEqual(
            argv[argv.index("--optimization-spec") + 1],
            str(self.runtime / "legacy/spec.json"),
        )
        self.assertEqual(
            argv[argv.index("--remote-cases-dir") + 1],
            "remote/leave-relative",
        )

    def test_successor_base_uses_unique_scheduler8002_optimization_only(self) -> None:
        pipeline = self.pipeline()
        stage3 = pipeline["stage3"]
        assert isinstance(stage3, dict)
        stage3["continuation_argv"] = [
            "python.exe",
            "continue_ipmsm_v2_stage2.py",
            "--decision-output",
            "legacy/stage3.json",
        ]
        optimization = pipeline["optimization"]
        assert isinstance(optimization, dict)
        flags = {
            "--stage2-decision": v3.UPSTREAM_PLACEHOLDER,
            "--optimization-spec": "legacy/spec.json",
            "--beta-summary": "legacy/beta.json",
            "--beta-case-plan": "legacy/beta.csv",
            "--beta-results": "legacy/beta-results.csv",
            "--beta-calibration-manifest": "legacy/beta-manifest.json",
            "--output-dir": "legacy/output",
            "--checkpoint-dir": "legacy/checkpoints",
            "--decision-output": "legacy/decision.json",
            "--project": "OLD",
            "--scheduler-url": "http://127.0.0.1:8000",
            "--project-active-cap": "1",
            "--max-fea-candidates": "1",
            "--task-prefix": "old-task",
            "--remote-cases-dir": "remote/old",
            "--result-dir": "simul_log/old",
            "--simulation-dir": "simulation/old",
            "--log-dir": "logs/old",
        }
        optimization["argv_template"] = [
            "old-python",
            "continue_ipmsm_v2_optimization.py",
            *(item for pair in flags.items() for item in pair),
        ]
        parent_path = self.root / "parent-base.json"
        parent_path.write_bytes(
            builder._canonical_bytes(
                {"schema_version": v3.CONTRACT_SCHEMA_VERSION, "pipeline": pipeline}
            )
        )
        legacy_implementation = self.runtime / "legacy/input.json"
        legacy_implementation.parent.mkdir(parents=True)
        legacy_implementation.write_bytes(b"input")
        implementation = self.source / "ipmsm_optimization.py"
        implementation.write_bytes(b"implementation")
        legacy_runner = self.source / "continue_ipmsm_v2_optimization.py"
        legacy_runner.write_bytes(b"runner")
        passed = self.runtime / "passed-stage3.json"
        python = self.source / "python.exe"
        python.write_bytes(b"python")
        paths = self.paths()
        parent = SimpleNamespace(
            base_contract=SimpleNamespace(source=parent_path, workdir=self.runtime)
        )
        source_binding = builder.FileBinding(implementation, builder._file_sha256(implementation))

        with mock.patch.object(builder, "_source_binding", return_value=source_binding):
            document = builder._build_base_document(
                parent,
                source_root=self.source,
                source_revision="a" * 40,
                python_executable=python,
                passed_decision=passed,
                paths=paths,
                scheduler_project="PYAEDT_MOTOR_IPMSM_V2",
            )

        successor = document["pipeline"]
        self.assertEqual(successor["workdir"], str(self.source))
        self.assertEqual(successor["external_pid_files"], [])
        self.assertEqual(successor["stage3"]["decision"], str(passed))
        continuation = successor["stage3"]["continuation_argv"]
        self.assertEqual(
            continuation[continuation.index("--decision-output") + 1],
            str(passed),
        )
        argv = successor["optimization"]["argv_template"]
        expected = {
            "--scheduler-url": "http://127.0.0.1:8002",
            "--project-active-cap": "50",
            "--max-fea-candidates": "12",
            "--task-prefix": builder.NAMESPACE["task_prefix"],
            "--remote-cases-dir": builder.NAMESPACE["remote_cases_dir"],
            "--result-dir": builder.NAMESPACE["result_dir"],
            "--simulation-dir": builder.NAMESPACE["simulation_dir"],
            "--log-dir": builder.NAMESPACE["log_dir"],
        }
        for flag, value in expected.items():
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertEqual(argv[argv.index("--output-dir") + 1], str(paths.optimization_output))
        self.assertEqual(argv[argv.index("--decision-output") + 1], str(paths.decision))
        task_setup = argv[argv.index("--env-setup") + 1]
        self.assertEqual(task_setup, builder.remote_task_env_setup("a" * 40))
        self.assertIn("${SLURM_SCHED_TASK_ID}", task_setup)
        self.assertIn("git rev-parse HEAD", task_setup)
        self.assertNotIn("${SLURM_JOB_ID}", task_setup)
        self.assertNotIn("${SIMULATION_ID}", task_setup)
        self.assertNotIn("--execute", argv)
        self.assertNotIn("--resume", argv)
        speed = successor["speed"]
        for argv_name, filename in builder.SPEED_SCRIPT_FILENAMES.items():
            self.assertEqual(
                speed[argv_name][1],
                str(paths.activation_contract / filename),
            )

    def paths(self) -> builder.BuildPaths:
        return builder.BuildPaths(
            root=self.output,
            base_contract=self.output / builder.BASE_FILENAME,
            v4_contract=self.output / builder.V4_FILENAME,
            activation_contract=self.output / builder.ACTIVATION_FILENAME,
            declaration=self.output / builder.DECLARATION_FILENAME,
            confirmation=self.output / builder.CONFIRMATION_FILENAME,
            receipt=self.output / builder.RECEIPT_FILENAME,
            decision=self.output / builder.DECISION_FILENAME,
            shared_lock=self.output / "optimization.lock",
            stage1_workspace=self.output / "stage1_official",
            optimization_output=self.runtime / builder.OPTIMIZATION_RELATIVE_ROOT,
            checkpoint_dir=self.runtime / builder.CHECKPOINT_RELATIVE_ROOT,
        )

    def test_v4_document_is_standard_and_expands_only_at_activation(self) -> None:
        paths = self.paths()
        base = {
            "schema_version": v3.CONTRACT_SCHEMA_VERSION,
            "pipeline": {
                "workdir": str(self.source),
                "optimization": {
                    "argv_template": [
                        "python.exe",
                        str(self.source / "continue_ipmsm_v2_optimization.py"),
                        "--stage2-decision",
                        v3.UPSTREAM_PLACEHOLDER,
                        "--decision-output",
                        str(paths.decision),
                    ]
                },
            },
            "contract_sha256": "b" * 64,
        }
        pins: dict[str, builder.FileBinding] = {}
        for name in (
            "stage1_publisher_v4",
            "optimization_authorizer_v4",
            "optimization_runner_v4",
        ):
            source = self.source / f"{name}.py"
            source.write_bytes(name.encode())
            pins[name] = builder.FileBinding(source, builder._file_sha256(source))

        document = builder._build_v4_document(
            base_document=base,
            base_path=paths.base_contract,
            v4_path=paths.v4_contract,
            python_executable=Path("python.exe"),
            source_pins=pins,
            paths=paths,
        )

        self.assertEqual(document["schema_version"], builder.v4.CONTRACT_SCHEMA_VERSION)
        self.assertEqual(document["pipeline"]["workdir"], str(self.source))
        wrapper = document["pipeline"]["optimization"]["wrapper_argv_template"]
        self.assertIn(v3.UPSTREAM_PLACEHOLDER, wrapper)
        self.assertNotIn("--execute", wrapper)
        unsigned = {
            "schema_version": document["schema_version"],
            "pipeline": document["pipeline"],
        }
        self.assertEqual(document["contract_sha256"], v3._canonical_sha256(unsigned))

    def test_activation_seals_expanded_argv_authority_and_stop_boundary(self) -> None:
        paths = self.paths()
        passed = self.runtime / "passed-stage3.json"
        passed.write_bytes(b"passed")
        python = self.source / "python.exe"
        python.write_bytes(b"python")
        config = self.output / "config.json"
        config.write_bytes(b"config")
        base = {
            "schema_version": v3.CONTRACT_SCHEMA_VERSION,
            "pipeline": {},
            "contract_sha256": "a" * 64,
        }
        envelope = {
            "schema_version": builder.v4.CONTRACT_SCHEMA_VERSION,
            "pipeline": {},
            "contract_sha256": "b" * 64,
        }
        plan = builder.BuildPlan(
            config_path=config,
            config_sha256=builder._file_sha256(config),
            source_root=self.source,
            source_revision="c" * 40,
            runtime_root=self.runtime,
            python_executable=python,
            parent_contract=SimpleNamespace(),
            passed_decision=builder.FileBinding(passed, builder._file_sha256(passed)),
            stage3_acquisition_completion=builder.FileBinding(
                passed,
                builder._file_sha256(passed),
            ),
            stage3_lineage={"kind": "direct_precollected"},
            remote_deployment_snapshot={
                "project": "PYAEDT_MOTOR_IPMSM_V2",
                "source_revision": "c" * 40,
                "max_active_tasks": 50,
                "validated_concurrency_limit": 100,
            },
            audited_inputs=SimpleNamespace(model_bundle_contract={"metadata": "bound"}),
            paths=paths,
            source_pins={},
            base_document=base,
            v4_document=envelope,
            authority={},
            scheduler_project="PYAEDT_MOTOR_IPMSM_V2",
        )
        wrapper_template = (
            str(python),
            str(self.source / "continue_ipmsm_v2_optimization_v4.py"),
            "--stage2-decision",
            v3.UPSTREAM_PLACEHOLDER,
        )

        def source_binding(
            root: Path, revision: str, relative: Path, label: str
        ) -> builder.FileBinding:
            del revision, label
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.as_posix().encode())
            return builder.FileBinding(path, builder._file_sha256(path))

        with (
            mock.patch.object(builder, "_source_binding", side_effect=source_binding),
            mock.patch.object(
                builder.v4,
                "load_contract",
                return_value=SimpleNamespace(
                    optimization=SimpleNamespace(wrapper_argv_template=wrapper_template)
                ),
            ),
        ):
            document = builder._build_activation_document(
                plan,
                SimpleNamespace(record={"binding_sha256": "d" * 64}),
            )

        activation = document["activation"]
        self.assertEqual(activation["passed_decision"]["path"], str(passed))
        self.assertEqual(
            activation["stage3_acquisition_completion"]["path"],
            str(passed),
        )
        self.assertEqual(activation["stage3_lineage"]["kind"], "direct_precollected")
        self.assertEqual(
            activation["optimization"]["wrapper_argv"],
            [*wrapper_template[:-1], str(passed)],
        )
        self.assertEqual(activation["optimization"]["stop_after"], "validated_pareto_fea")
        self.assertFalse(activation["optimization"]["legacy_speed_stage_authorized"])
        self.assertFalse(activation["optimization"]["target_load_stage_authorized"])
        self.assertEqual(
            activation["scheduler"],
            builder.activation_scheduler_policy("c" * 40),
        )
        self.assertEqual(activation["runner"]["argv"][-1], "--execute")
        self.assertEqual(activation["runner"]["argv"][1:3], ["-B", "-X"])
        self.assertEqual(
            activation["runner"]["child_environment"],
            builder.child_environment(paths.activation_contract),
        )
        self.assertEqual(
            set(activation["source"]["dynamic_sources"]),
            set(builder.DYNAMIC_SOURCE_MODULES),
        )
        unsigned = {
            "schema_version": builder.ACTIVATION_SCHEMA_VERSION,
            "activation": activation,
        }
        self.assertEqual(document["contract_sha256"], v3._canonical_sha256(unsigned))

    def test_remote_deployment_policy_rejects_timeout_field_and_one_ref_drift(self) -> None:
        revision = "a" * 40

        class Response:
            def __init__(self, value: dict[str, object]) -> None:
                self.payload = json.dumps(value).encode("utf-8")

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return self.payload

        def project() -> dict[str, object]:
            policy = builder.REMOTE_DEPLOYMENT_POLICY
            return {
                "name": policy["project"],
                "repos": [
                    {
                        "url": policy["repository_url"],
                        "ref": revision,
                        "subdir": policy["repository_subdir"],
                    }
                ],
                "deployments": [
                    {
                        "account_name": account,
                        "status": "deployed",
                        "deployed_refs": json.dumps({"pyaedt_motor": revision}),
                    }
                    for account in policy["accounts"]
                ],
                "auto_pull": False,
                "cleanup_globs": "*.aedtresults",
                "max_active_tasks": 50,
                "validated_concurrency_limit": 100,
                "aedt_backend": "standalone",
                "setup": policy["setup"],
                "entrypoints": [
                    {
                        "workdir": "pyaedt_motor",
                        "path": "run_ipmsm_batch.py",
                        "conda_env": "pyaedt2026v1",
                    },
                    {
                        "workdir": "pyaedt_motor",
                        "path": "subprocess_run.py",
                        "conda_env": "pyaedt2026v1",
                    },
                ],
                "simulation_policy": {
                    "project": policy["project"],
                    "name": policy["project"],
                    "desired_simulations": 50,
                    "effective_simulations": 50,
                    "validated_concurrency_limit": 50,
                    "min_desired_simulations": 0,
                    "max_desired_simulations": 50,
                    "scale_down_mode": "drain",
                    "control_enabled": True,
                },
            }

        with mock.patch.object(
            builder.url_request,
            "urlopen",
            return_value=Response(project()),
        ):
            snapshot = builder.audit_remote_deployment(revision)
        self.assertEqual(snapshot["source_revision"], revision)
        self.assertIs(snapshot["auto_pull"], False)
        self.assertEqual(snapshot["cleanup_globs"], "*.aedtresults")
        self.assertEqual(snapshot["validated_concurrency_limit"], 100)
        self.assertEqual(
            set(snapshot["entrypoints"]),
            {"run_ipmsm_batch.py", "subprocess_run.py"},
        )

        wrong_ref = project()
        deployments = wrong_ref["deployments"]
        assert isinstance(deployments, list)
        deployments[0]["deployed_refs"] = json.dumps({"pyaedt_motor": "b" * 40})
        with (
            mock.patch.object(
                builder.url_request,
                "urlopen",
                return_value=Response(wrong_ref),
            ),
            self.assertRaisesRegex(builder.OptimizationActivationBuildError, "status/ref"),
        ):
            builder.audit_remote_deployment(revision)

        wrong_cleanup = project()
        wrong_cleanup["cleanup_globs"] = "*.csv"
        with (
            mock.patch.object(
                builder.url_request,
                "urlopen",
                return_value=Response(wrong_cleanup),
            ),
            self.assertRaisesRegex(
                builder.OptimizationActivationBuildError,
                "cap/backend/setup policy",
            ),
        ):
            builder.audit_remote_deployment(revision)

        wrong_subprocess = project()
        entrypoints = wrong_subprocess["entrypoints"]
        assert isinstance(entrypoints, list)
        entrypoints[1]["workdir"] = "wrong"
        with (
            mock.patch.object(
                builder.url_request,
                "urlopen",
                return_value=Response(wrong_subprocess),
            ),
            self.assertRaisesRegex(
                builder.OptimizationActivationBuildError,
                "subprocess_run.py entrypoint",
            ),
        ):
            builder.audit_remote_deployment(revision)

        missing_setup = project()
        del missing_setup["setup"]
        with (
            mock.patch.object(
                builder.url_request,
                "urlopen",
                return_value=Response(missing_setup),
            ),
            self.assertRaisesRegex(builder.OptimizationActivationBuildError, "setup"),
        ):
            builder.audit_remote_deployment(revision)

        with (
            mock.patch.object(
                builder.url_request,
                "urlopen",
                side_effect=TimeoutError("timed out"),
            ),
            self.assertRaisesRegex(builder.OptimizationActivationBuildError, "preflight"),
        ):
            builder.audit_remote_deployment(revision)


class OptimizationActivationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.decision = self.root / "decision.json"
        self.activation = self.root / "activation.json"
        self.activation.write_bytes(b"activation blocker")
        self.completion = self.root / "completion.json"
        self.completion.write_bytes(b"completion")
        self.wrapper = (
            "python.exe",
            "continue_ipmsm_v2_optimization_v4.py",
            "--stage2-decision",
            str(self.root / "passed.json"),
        )
        self.context = runner.ActivationContext(
            source=self.activation,
            source_sha256="a" * 64,
            canonical_sha256="b" * 64,
            contract_sha256="c" * 64,
            source_root=self.source,
            source_revision="d" * 40,
            runtime_root=self.root / "runtime",
            v4_path=self.root / "contract.json",
            contract=SimpleNamespace(workdir=self.source),
            authorization=SimpleNamespace(
                record={"binding_sha256": "e" * 64},
                audit=SimpleNamespace(),
            ),
            passed_decision=self.root / "passed.json",
            stage3_acquisition_completion=self.completion,
            stage3_lineage={"kind": "direct_precollected"},
            remote_deployment_snapshot={
                "project": "PYAEDT_MOTOR_IPMSM_V2",
                "source_revision": "d" * 40,
                "max_active_tasks": 50,
                "validated_concurrency_limit": 100,
            },
            audited_inputs=SimpleNamespace(),
            wrapper_argv=self.wrapper,
            decision=self.decision,
            output_dir=self.root / "output",
            checkpoint_dir=self.root / "checkpoints",
            runner_argv=(
                "python.exe",
                "-B",
                "-X",
                f"pycache_prefix={builder.runner_pycache_prefix(self.activation)}",
                "runner.py",
                "--activation-contract",
                "activation.json",
                "--execute",
            ),
            child_environment=builder.child_environment(self.activation),
        )

    def _owner(self, mode: str) -> dict[str, object]:
        return {
            "hostname": "inactive-host",
            "pid": 12345,
            "mode": mode,
            "nonce": f"{mode}-nonce",
        }

    def _legacy_authority(self, *, resume: bool) -> tuple[object, object, dict[str, str]]:
        return (
            SimpleNamespace(resume=resume, decision_output=self.decision),
            SimpleNamespace(),
            {"contract_sha256": "1" * 64},
        )

    def test_scheduler_policy_is_exact_and_cap50(self) -> None:
        flags = {
            "--project": "PYAEDT_MOTOR_IPMSM_V2",
            "--scheduler-url": "http://127.0.0.1:8002",
            "--project-active-cap": "50",
            "--max-fea-candidates": "12",
            "--env-setup": builder.remote_task_env_setup("d" * 40),
        }
        argv = ["python", "wrapper", *(item for pair in flags.items() for item in pair)]
        scheduler = builder.activation_scheduler_policy("d" * 40)
        with mock.patch.object(builder, "_audit_campaign_defaults"):
            runner._audit_scheduler(scheduler, argv, "d" * 40)
            changed = dict(scheduler)
            changed["project_active_cap"] = 49
            with self.assertRaises(runner.OptimizationActivationError):
                runner._audit_scheduler(changed, argv, "d" * 40)

    def test_speed_commands_are_blocked_below_activation_file(self) -> None:
        blocker = self.root / "activation.json"
        blocker.write_bytes(b"activation")
        speed = SimpleNamespace(
            **{
                argv_name: (
                    "python.exe",
                    str(blocker / filename),
                )
                for argv_name, filename in builder.SPEED_SCRIPT_FILENAMES.items()
            }
        )
        contract = SimpleNamespace(
            workdir=self.source,
            base_contract=SimpleNamespace(speed=speed),
        )
        runner._audit_speed_hard_disabled(contract, blocker)
        speed.plan_argv = ("python.exe", str(self.source / "generate_ipmsm_second_pass_cases.py"))
        with self.assertRaisesRegex(runner.OptimizationActivationError, "hard-disabled"):
            runner._audit_speed_hard_disabled(contract, blocker)

    def test_dynamic_aedt_source_rejects_foreign_module_and_sha_drift(self) -> None:
        expected = self.source / "module" / "aedt_attach_client.py"
        expected.parent.mkdir()
        expected.write_bytes(b"# exact dynamic source\n")
        adaptive = self.source / "generate_ipmsm_v2_adaptive_batch.py"
        adaptive.write_bytes(b"# exact adaptive source\n")
        binding = {
            "adaptive_batch": {
                "path": str(adaptive),
                "sha256": builder._file_sha256(adaptive),
            },
            "aedt_attach_client": {
                "path": str(expected),
                "sha256": builder._file_sha256(expected),
            }
        }
        exact = SimpleNamespace(__file__=str(expected))
        modules = {
            "generate_ipmsm_v2_adaptive_batch": SimpleNamespace(__file__=str(adaptive)),
            "module.aedt_attach_client": exact,
        }
        source_bindings = {
            Path("generate_ipmsm_v2_adaptive_batch.py"): builder.FileBinding(
                adaptive,
                builder._file_sha256(adaptive),
            ),
            Path("module/aedt_attach_client.py"): builder.FileBinding(
                expected,
                builder._file_sha256(expected),
            ),
        }
        with (
            mock.patch.object(
                builder,
                "_source_binding",
                side_effect=lambda root, revision, relative, label: source_bindings[relative],
            ),
            mock.patch.object(
                runner.importlib,
                "import_module",
                side_effect=lambda name: modules[name],
            ),
        ):
            runner._audit_dynamic_sources(binding, self.source, "a" * 40)

        foreign = self.root / "foreign" / "aedt_attach_client.py"
        foreign.parent.mkdir()
        foreign.write_bytes(expected.read_bytes())
        with (
            mock.patch.object(
                builder,
                "_source_binding",
                side_effect=lambda root, revision, relative, label: source_bindings[relative],
            ),
            mock.patch.object(
                runner.importlib,
                "import_module",
                side_effect=lambda name: (
                    SimpleNamespace(__file__=str(foreign))
                    if name == "module.aedt_attach_client"
                    else modules[name]
                ),
            ),
            self.assertRaisesRegex(runner.OptimizationActivationError, "path/SHA"),
        ):
            runner._audit_dynamic_sources(binding, self.source, "a" * 40)

        changed = dict(binding)
        changed["aedt_attach_client"] = {
            "path": str(expected),
            "sha256": "0" * 64,
        }
        with (
            mock.patch.object(
                builder,
                "_source_binding",
                side_effect=lambda root, revision, relative, label: source_bindings[relative],
            ),
            mock.patch.object(
                runner.importlib,
                "import_module",
                side_effect=lambda name: modules[name],
            ),
            self.assertRaisesRegex(runner.OptimizationActivationError, "bytes changed"),
        ):
            runner._audit_dynamic_sources(changed, self.source, "a" * 40)

    def test_decision_state_selects_fresh_and_durable_resume(self) -> None:
        state, decision = runner._decision_state(self.context)
        self.assertEqual((state, decision), ("fresh", None))
        self.decision.write_text("{}", encoding="utf-8")
        active = {"status": "pareto_fea_started"}
        with (
            mock.patch.object(runner.v3, "audit_decision", return_value=active),
            mock.patch.object(runner.v4, "audit_optimization_decision_authorization"),
        ):
            state, decision = runner._decision_state(self.context)
        self.assertEqual(state, "resume")
        self.assertIs(decision, active)

    def test_orphan_fresh_claim_is_dry_run_preserved_then_execute_reconciled(self) -> None:
        owner = self._owner("execute")
        claim = runner.legacy_optimization._claim_path(self.decision)
        claim.write_bytes(
            runner.legacy_optimization._json_bytes(
                {
                    "schema_version": runner.legacy_optimization.SCHEMA_VERSION,
                    "decision_output": str(self.decision.resolve()),
                    "decision_sha256": "2" * 64,
                    "contract_sha256": "1" * 64,
                    "original_owner": owner,
                    "owner": owner,
                }
            )
        )
        with (
            mock.patch.object(
                runner,
                "_legacy_resume_authority",
                return_value=self._legacy_authority(resume=False),
            ),
            mock.patch.object(
                runner.legacy_optimization,
                "_require_owner_inactive",
            ),
            mock.patch.object(
                runner.legacy_optimization,
                "_assert_new_outputs_fresh",
            ),
        ):
            state, decision = runner._decision_state(self.context)
            self.assertEqual((state, decision), ("recover_fresh_claim", None))
            self.assertTrue(claim.is_file(), "dry inspection must not mutate the claim")
            reconciled, writes = runner._reconcile_durable_state(
                self.context,
                state,
                decision,
            )
        self.assertEqual((reconciled, writes), ("fresh", 1))
        self.assertFalse(claim.exists())

    def test_orphan_recovery_lock_is_dry_run_preserved_then_execute_reconciled(self) -> None:
        self.decision.write_bytes(b"{}\n")
        prior = {
            "status": "pareto_fea_started",
            "owner": self._owner("execute"),
        }
        recovery = runner.legacy_optimization._recovery_claim_path(self.decision)
        recovery.write_bytes(
            runner.legacy_optimization._json_bytes(
                {
                    "schema_version": runner.legacy_optimization.SCHEMA_VERSION,
                    "decision_output": str(self.decision.resolve()),
                    "decision_sha256": runner.legacy_optimization._sha256(self.decision),
                    "owner": self._owner("resume"),
                }
            )
        )
        with (
            mock.patch.object(runner.v3, "audit_decision", return_value=prior),
            mock.patch.object(runner.v4, "audit_optimization_decision_authorization"),
            mock.patch.object(
                runner,
                "_legacy_resume_authority",
                return_value=self._legacy_authority(resume=True),
            ),
            mock.patch.object(
                runner.legacy_optimization,
                "_validate_prior_decision",
                return_value=prior,
            ),
            mock.patch.object(
                runner.legacy_optimization,
                "_require_owner_inactive",
            ),
        ):
            state, decision = runner._decision_state(self.context)
            self.assertEqual(state, "recover_recovery_lock:resume")
            self.assertTrue(recovery.is_file(), "dry inspection must not mutate the lock")
            reconciled, writes = runner._reconcile_durable_state(
                self.context,
                state,
                decision,
            )
        self.assertEqual((reconciled, writes), ("resume", 1))
        self.assertFalse(recovery.exists())

    def test_failed_decision_is_not_restarted_fresh(self) -> None:
        self.decision.write_text("{}", encoding="utf-8")
        failed = {"status": "failed", "error": "strict FEA validation failed"}
        with (
            mock.patch.object(runner.v3, "audit_decision", return_value=failed),
            mock.patch.object(runner.v4, "audit_optimization_decision_authorization"),
            self.assertRaisesRegex(runner.OptimizationActivationError, "strict FEA"),
        ):
            runner._decision_state(self.context)

    def test_complete_decision_must_be_standard_target_load_consumable(self) -> None:
        self.decision.write_text("{}", encoding="utf-8")
        complete = {"status": "complete"}
        with (
            mock.patch.object(runner.v3, "audit_decision", return_value=complete),
            mock.patch.object(runner.v4, "audit_optimization_decision_authorization"),
            mock.patch.object(runner.target_authority, "audit_completed_upstream") as audit,
        ):
            state, _ = runner._decision_state(self.context)
        self.assertEqual(state, "complete")
        audit.assert_called_once_with(self.context.v4_path)

    def test_complete_commit_with_stale_claim_uses_durable_recovery(self) -> None:
        self.decision.write_text("{}", encoding="utf-8")
        runner.legacy_optimization._claim_path(self.decision).write_text("{}", encoding="utf-8")
        complete = {"status": "complete"}
        with (
            mock.patch.object(runner.v3, "audit_decision", return_value=complete),
            mock.patch.object(runner.v4, "audit_optimization_decision_authorization"),
            mock.patch.object(runner.target_authority, "audit_completed_upstream"),
        ):
            state, _ = runner._decision_state(self.context)
        self.assertEqual(state, "recover_complete")

    def test_wrapper_runs_exact_dry_then_execute_and_never_supervisor(self) -> None:
        calls: list[tuple[tuple[str, ...], Path, bool]] = []

        def child(
            argv: object,
            *,
            workdir: Path,
            label: str,
            capture_output: bool,
            child_environment: object,
        ) -> subprocess.CompletedProcess[str]:
            del label
            self.assertEqual(
                child_environment,
                builder.child_environment(self.activation),
            )
            command = tuple(argv)  # type: ignore[arg-type]
            calls.append((command, workdir, capture_output))
            stdout = (
                json.dumps({"mode": "resume-dry-run", "status": "pareto_fea_started"})
                if capture_output
                else ""
            )
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with mock.patch.object(runner, "_run_child", side_effect=child):
            runner._run_wrapper(self.context, "resume")

        self.assertEqual(
            calls[0],
            ((*self.wrapper, "--resume"), self.context.runtime_root, True),
        )
        self.assertEqual(
            calls[1],
            (
                (*self.wrapper, "--resume", "--execute"),
                self.context.runtime_root,
                False,
            ),
        )
        for command, _, _ in calls:
            self.assertFalse(any("speed" in item or "target_load" in item for item in command))

    def test_process_argv_must_equal_contract_byte_for_byte(self) -> None:
        with mock.patch.object(runner, "_actual_process_argv", return_value=self.context.runner_argv):
            runner._audit_process_argv(self.context, execute=True)
        changed = (*self.context.runner_argv[:-1], "--resume", "--execute")
        with (
            mock.patch.object(runner, "_actual_process_argv", return_value=changed),
            self.assertRaises(runner.OptimizationActivationError),
        ):
            runner._audit_process_argv(self.context, execute=True)

    def test_unknown_activation_field_fails_closed_before_side_effects(self) -> None:
        activation = {"unexpected": True}
        unsigned = {
            "schema_version": builder.ACTIVATION_SCHEMA_VERSION,
            "activation": activation,
        }
        document = {
            **unsigned,
            "contract_sha256": v3._canonical_sha256(unsigned),
        }
        path = self.root / "activation.json"
        path.write_bytes(builder._canonical_bytes(document))
        with (
            mock.patch.object(runner, "_absolute_path", return_value=path),
            self.assertRaisesRegex(runner.OptimizationActivationError, "activation fields changed"),
        ):
            runner.load_activation(path)

    def test_run_stops_at_complete_pareto_fea(self) -> None:
        self.decision.write_text("{}", encoding="utf-8")
        completed = {"status": "complete"}
        with (
            mock.patch.object(
                runner,
                "_decision_state",
                side_effect=[("fresh", None), ("complete", completed)],
            ),
            mock.patch.object(runner, "_run_wrapper") as run_wrapper,
            mock.patch.object(runner, "load_activation", return_value=self.context),
            mock.patch.object(builder, "_file_sha256", return_value="f" * 64),
            mock.patch.object(runner, "_audit_live_remote_deployment"),
        ):
            result = runner.run(self.context, execute=True)
        run_wrapper.assert_called_once_with(self.context, "fresh")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["stop_after"], "validated_pareto_fea")
        self.assertFalse(result["legacy_speed_stage_started"])
        self.assertFalse(result["target_load_stage_started"])


if __name__ == "__main__":
    unittest.main()
