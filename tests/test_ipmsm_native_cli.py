"""Native source snapshots and production CLI routing, without an FEA solve."""
from contextlib import contextmanager
import hashlib
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from pyaedt_motor import cli, runtime


ROOT = Path(__file__).resolve().parents[1]


def small_repository(root):
    content = {"run_ipmsm_batch.py": b"# original runner\n", "ipmsm_optimization.py": b"# pure helpers\n",
               "module/geometry.py": b"# geometry\n", "material/steel.csv": b"B,H\n1,2\n",
               "configs/motor.json": b"{}\n", "src/pyaedt_motor/__init__.py": b"",
               "src/pyaedt_motor/train_physical.py": b"# physical train\n",
               "src/pyaedt_motor/optimize_physical.py": b"# physical optimize\n",
               "src/pyaedt_motor/nested/helper.py": b"# nested package helper\n"}
    for relative, payload in content.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return content


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class NativeSourceTests(unittest.TestCase):
    def test_freeze_copies_packaged_source_and_vendored_library_with_checked_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            expected = small_repository(root)
            library = Path(tmp) / "library" / "src" / "pyaedt_module"
            library.mkdir(parents=True)
            (library / "__init__.py").write_bytes(b"# vendor\n")
            (library / "core.py").write_bytes(b"value = 42\n")
            (library / "__pycache__").mkdir()
            (library / "__pycache__/ignored.pyc").write_bytes(b"ignored")
            frozen = runtime.freeze_source(root, Path(tmp) / "run", library.parent.parent)
            manifest = json.loads((frozen.parent / "source_manifest.json").read_text(encoding="utf-8"))
            records = {record["path"]: record for record in manifest["files"]}
            for relative, payload in expected.items():
                self.assertEqual((frozen / relative).read_bytes(), payload)
                self.assertEqual(records[relative]["sha256"], hashlib.sha256(payload).hexdigest())
            for relative in ["vendor/pyaedt_module/__init__.py", "vendor/pyaedt_module/core.py"]:
                payload = (frozen.parent / relative).read_bytes()
                self.assertEqual(records[relative]["sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(records[relative]["bytes"], len(payload))
            self.assertNotIn("vendor/pyaedt_module/__pycache__/ignored.pyc", records)
            (root / "src/pyaedt_motor/train_physical.py").write_bytes(b"# later changed source\n")
            (library / "core.py").write_bytes(b"value = 99\n")
            self.assertEqual((frozen / "src/pyaedt_motor/train_physical.py").read_bytes(), expected["src/pyaedt_motor/train_physical.py"])
            self.assertEqual((frozen.parent / "vendor/pyaedt_module/core.py").read_bytes(), b"value = 42\n")

    def test_freeze_refuses_a_packaged_source_edit_during_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            small_repository(root)
            changing = root / "src/pyaedt_motor/train_physical.py"
            original_read = Path.read_bytes
            mutated = False
            def read(path):
                nonlocal mutated
                payload = original_read(path)
                if path == changing and not mutated:
                    mutated = True
                    path.write_bytes(b"# edited while copying\n")
                return payload
            with mock.patch.object(Path, "read_bytes", read):
                with self.assertRaisesRegex(RuntimeError, "Source changed while freezing"):
                    runtime.freeze_source(root, Path(tmp) / "run")
            self.assertFalse((Path(tmp) / "run/source_manifest.json").exists())

    def test_freeze_rechecks_vendored_original_after_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir()
            small_repository(root)
            library = Path(tmp) / "library/pyaedt_module"
            library.mkdir(parents=True)
            (library / "core.py").write_bytes(b"# original vendor\n")
            original_copy = runtime.shutil.copytree
            def copy(*args, **kwargs):
                result = original_copy(*args, **kwargs)
                (library / "core.py").write_bytes(b"# changed while copying\n")
                return result
            with mock.patch.object(runtime.shutil, "copytree", copy):
                with self.assertRaisesRegex(RuntimeError, "Source changed while freezing"):
                    runtime.freeze_source(root, Path(tmp) / "run", library.parent)

    def test_calibration_sampler_avoids_retired_campaign_imports_and_recomputes_resistance(self):
        import calibrate_ipmsm_beta as calibration
        from ipmsm_optimization import WindingSpec, phase_resistance_100c_ohm
        import builtins
        original_import = builtins.__import__
        def importing(name, *args, **kwargs):
            if name.startswith(("generate_ipmsm_v2", "ipmsm_campaign_v4")):
                raise AssertionError("calibration reached a retired campaign: " + name)
            return original_import(name, *args, **kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "motor.json"
            raw = json.loads((ROOT / "configs/ipmsm_7p5kw.json").read_text(encoding="utf-8"))
            path.write_text(json.dumps(raw), encoding="utf-8-sig")
            with mock.patch.object(builtins, "__import__", importing):
                first = calibration.calibration_source_from_spec(path, geometry_seed=17)
                second = calibration.calibration_source_from_spec(path, geometry_seed=17)
        self.assertEqual(first, second)
        self.assertEqual(first["stator_shoe_thick"], 1.5)
        self.assertNotIn("electrical_zero_deg", first)
        self.assertNotIn("beta_calibration", first)
        self.assertAlmostEqual(first["phase_resistance_ohm"], phase_resistance_100c_ohm(
            first, first["stack_length_mm"], WindingSpec(**raw["winding"])))


@unittest.skipUnless(os.name == "nt", "Windows native CLI path semantics")
class NativeCLITests(unittest.TestCase):
    def test_train_and_optimize_start_fresh_supported_python_with_absolute_argv(self):
        commands = {
            "train": ["--data", "input cases.csv", "--spec=motor.json", "--output", "model output"],
            "optimize": ["--bundle", "model/artifact", "--spec=motor.json", "--expected-fingerprints", "expected.json",
                         "--calibration-manifest=zero.json", "--selected-profile", "quality/selected.json",
                         "--baseline", "baseline.json", "--output=proposals.json"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"; root.mkdir(); small_repository(root)
            invoking = Path(tmp) / "invoking folder"; invoking.mkdir()
            frozen = Path(tmp) / "native/source"; (frozen / "src").mkdir(parents=True)
            for command, arguments in commands.items():
                with self.subTest(command=command), working_directory(invoking), \
                        mock.patch.object(cli, "PROJECT_ROOT", root), \
                        mock.patch.object(runtime, "freeze_source", return_value=frozen) as freeze, \
                        mock.patch.object(cli.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as launch, \
                        mock.patch.object(cli.importlib, "import_module", side_effect=AssertionError("parent imported production module")), \
                        mock.patch.dict(os.environ, {"IPMSM_RUNTIME_ROOT": str(Path(tmp) / "runs"), "PYTHONPATH": "old-live-source"}):
                    self.assertEqual(cli.main([command, *arguments]), 7)
                argv = launch.call_args.args[0]
                module = "pyaedt_motor.train_physical" if command == "train" else "pyaedt_motor.optimize_physical"
                self.assertEqual(argv[:3], [sys.executable, "-m", module])
                self.assertEqual(launch.call_args.kwargs["cwd"], frozen)
                self.assertEqual(launch.call_args.kwargs["env"]["PYTHONPATH"], os.pathsep.join([str(frozen / "src"), str(frozen)]))
                values = argv[3:]
                for index, original in enumerate(arguments):
                    if original.startswith("--") and "=" in original:
                        flag, value = original.split("=", 1)
                        self.assertEqual(values[index], flag + "=" + str((invoking / value).resolve()))
                    elif not original.startswith("--") and index > 0 and arguments[index - 1].startswith("--"):
                        self.assertEqual(values[index], str((invoking / original).resolve()))
                self.assertEqual(freeze.call_args.args[0], root)
                self.assertEqual(freeze.call_args.args[1].drive.casefold(), "c:")

    def test_native_guards_refuse_mapped_outputs_roots_and_python314_before_freezing(self):
        for command in ["train", "optimize", "simulate"]:
            with self.subTest(command=command), mock.patch.object(runtime.sys, "version_info", (3, 14, 0)), \
                    mock.patch.object(runtime, "freeze_source") as freeze:
                with self.assertRaisesRegex(RuntimeError, "pyaedt2026v1.*Python3.11"):
                    cli.main([command, "--output", "C:/unused"])
            freeze.assert_not_called()
        for command in ["train", "optimize"]:
            for output in [["--output", "Y:/must_not_be_created"], ["--output=Y:/must_not_be_created"]]:
                with self.subTest(command=command, output=output), mock.patch.object(runtime, "freeze_source") as freeze:
                    with self.assertRaisesRegex(ValueError, "output must be on the native C"):
                        cli.main([command, *output])
                freeze.assert_not_called()
        with mock.patch.dict(os.environ, {"IPMSM_RUNTIME_ROOT": "Y:/must_not_be_created"}), \
                mock.patch.object(runtime, "freeze_source") as freeze:
            with self.assertRaisesRegex(ValueError, "runtime must be on the native C"):
                cli.main(["train", "--output", "C:/unused"])
        freeze.assert_not_called()

    def test_native_root_guard_checks_reparse_ancestors_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ancestor = Path(tmp)
            original_stat = Path.lstat
            def attributes(path):
                actual = original_stat(path)
                if path == ancestor:
                    return SimpleNamespace(st_mode=actual.st_mode,
                        st_file_attributes=actual.st_file_attributes | stat.FILE_ATTRIBUTE_REPARSE_POINT)
                return actual
            with mock.patch.object(Path, "lstat", attributes):
                with self.assertRaisesRegex(ValueError, "reparse ancestor"):
                    runtime.native_runtime_path(ancestor / "new_directory" / "result.json", label="output")
            self.assertFalse((ancestor / "new_directory").exists())

    def test_help_routes_to_new_modules_without_freezing(self):
        for command in ["train", "optimize"]:
            module = mock.Mock(); module.main.return_value = 0
            with self.subTest(command=command), mock.patch.object(runtime, "freeze_source") as freeze, \
                    mock.patch.object(cli.importlib, "import_module", return_value=module) as imported:
                self.assertEqual(cli.main([command, "--help"]), 0)
            imported.assert_called_once_with("pyaedt_motor." + ("train_physical" if command == "train" else "optimize_physical"))
            module.main.assert_called_once_with(["--help"])
            freeze.assert_not_called()

    def test_real_native_training_refuses_historical_rows_and_imports_only_frozen_sources(self):
        if sys.version_info[:2] != (3, 11):
            self.skipTest("run this integration check with pyaedt2026v1 Python3.11")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "historical.csv"
            old.write_text("case_id,input_base_rpm\nold_case,1200\n", encoding="utf-8")
            output = root / "must_not_be_created"
            environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]),
                               IPMSM_RUNTIME_ROOT=str(root / "runs"))
            result = subprocess.run([sys.executable, "-m", "pyaedt_motor", "train", "--data", str(old),
                                     "--spec", str(ROOT / "configs/ipmsm_7p5kw.json"), "--output", str(output)],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=90)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("complete new motor_metrics_v3 rows", result.stderr)
            self.assertFalse(output.exists())
            runs = list((root / "runs").glob("training_*"))
            self.assertEqual(len(runs), 1)
            frozen = runs[0] / "source"
            self.assertIn(str(frozen), result.stderr)
            manifest = json.loads((runs[0] / "source_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["environment_versions"]["python"].split(".")[:2], ["3", "11"])
            records = {record["path"]: record["sha256"] for record in manifest["files"]}
            self.assertEqual(records["src/pyaedt_motor/model_bundle.py"], hashlib.sha256((frozen / "src/pyaedt_motor/model_bundle.py").read_bytes()).hexdigest())
            frozen_env = dict(environment, PYTHONPATH=os.pathsep.join([str(frozen / "src"), str(frozen)]))
            code = ("import json, pathlib; from pyaedt_motor import model_bundle as b; import ipmsm_optimization as o; "
                    "print(json.dumps({'bundle':b.__file__,'helpers':o.__file__, "
                    "'pin_matches':b.source_contract()==b._IMPORTED_PREDICTION_SOURCES}))")
            check = subprocess.run([sys.executable, "-c", code], cwd=frozen, env=frozen_env,
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(check.returncode, 0, check.stderr[-1000:])
            evidence = json.loads(check.stdout)
            self.assertTrue(evidence["pin_matches"])
            self.assertTrue(Path(evidence["bundle"]).is_relative_to(frozen))
            self.assertTrue(Path(evidence["helpers"]).is_relative_to(frozen))


if __name__ == "__main__":
    unittest.main()
