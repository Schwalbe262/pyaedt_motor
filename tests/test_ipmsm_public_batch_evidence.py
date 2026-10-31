"""Inert evidence-retention tests; the stand-in solver never invokes Native."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_ipmsm_public_batch_pilot import Fixture, pilot


class EvidenceRetentionTests(unittest.TestCase):
    def test_setup_only_retains_exact_preimage_and_full_installation_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = Fixture(Path(temporary), "setup_only")
            observed = {"wrapper_path": "/vendor/ansysedt", "wrapper_resolved": "/vendor/.answrapper",
                        "known_file_records": {"/vendor/ansysedt.exe": {"bytes": 17, "sha256": "a" * 64}},
                        "ELF_class_inputs": [{"path": "/vendor/libngcore.so", "ELF_class": 2}], "Native_calls": 0}
            calls = []
            f.owner.validate_installation = lambda *args: calls.append(True) or copy.deepcopy(observed)
            paths = []
            reader = f.read
            f.read = lambda path: paths.append(Path(path)) or reader(path)
            result = f.run()
            self.assertEqual(result["status"], "passed_settings_only", result["errors"])
            self.assertEqual(result["retained_inputs_contract"], "public_cli_retained_inputs_v1")
            self.assertEqual(len(calls), 2)
            self.assertEqual(result["BatchSolve_launch_attempts"], 0)
            preimage = result["prepared_unsolved_project"]
            self.assertEqual(paths, [Path(preimage["path"])])
            self.assertEqual(pilot.read_sealed(preimage), b"new public CAD only")
            self.assertEqual({k: preimage[k] for k in ("bytes", "sha256")},
                             {k: result["prepared_project"][k] for k in ("bytes", "sha256")})
            import json
            for key in ("installation_observation_before", "installation_observation_after"):
                self.assertEqual(json.loads(pilot.read_sealed(result[key])), observed)
            self.assertNotIn("batch_options", result)
            self.assertFalse((f.output / "owned_cli_solver_state.json").exists())

    def test_solve_intent_binds_preimage_full_installation_and_actual_fsynced_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = Fixture(Path(temporary))
            launch = f.owner.owned_batch
            def solve(command, output, **kwargs):
                import json
                intent = json.loads((output / "owned_cli_solver_state.json").read_bytes())
                self.assertEqual(intent["retained_inputs_contract"], "public_cli_retained_inputs_v1")
                self.assertEqual(pilot.read_sealed(intent["prepared_unsolved_project"]), b"new public CAD only")
                self.assertEqual(pilot.read_sealed(intent["batch_options"]),
                    b"$begin 'Config'\n'Desktop/Settings/ProjectOptions/NumberOfProcessors'=4\n"
                    b"'Maxwell 2D/NumCoresPerDistributedTask'=4\n$end 'Config'\n")
                self.assertEqual(Path(intent["batch_options"]["path"]), Path(command[8]))
                pilot.read_sealed(intent["installation_observation_before"])
                Path(command[-1]).write_bytes(b"stand-in solved project; expected to differ")
                return launch(command, output, **kwargs)
            f.owner.owned_batch = solve
            result = f.run()
            self.assertEqual(result["status"], "solved_diagnostic", result["errors"])
            self.assertEqual(f.events.count("solver"), 1)
            self.assertEqual(pilot.read_sealed(result["prepared_unsolved_project"]), b"new public CAD only")
            self.assertEqual(Path(result["prepared_project"]["path"]).read_bytes(), b"stand-in solved project; expected to differ")
            self.assertTrue(result["installation_files_unchanged"])

    def test_post_intent_mutation_of_any_sealed_execution_input_prevents_solver(self):
        for artifact in ("prepared_unsolved_project", "installation_observation_before", "batch_options"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as temporary:
                f = Fixture(Path(temporary))
                original_write = pilot.write_new
                def intercept(path, value):
                    original_write(path, value)
                    if Path(path).name == "owned_cli_solver_state.json":
                        Path(value[artifact]["path"]).write_bytes(b"tampered after durable intent")
                with patch.object(pilot, "write_new", side_effect=intercept):
                    result = f.run()
                self.assertEqual(result["BatchSolve_launch_attempts"], 0)
                self.assertNotIn("solver", f.events)
                self.assertTrue(result["solver_state_uncertain"])
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["errors"])

    def test_original_project_drift_during_preimage_parse_is_not_relabelled_as_new_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = Fixture(Path(temporary))
            reader = f.read
            def change_original(path):
                value = reader(path)
                (f.output / "scene/fresh_cad/fresh_cad.aedt").write_bytes(b"different project")
                return value
            f.read = change_original
            result = f.run()
            self.assertEqual(result["status"], "failed")
            self.assertNotIn("solver", f.events)
            self.assertEqual(result["BatchSolve_launch_attempts"], 0)
            self.assertFalse((f.output / "owned_cli_solver_state.json").exists())

    def test_after_observation_drift_and_post_solve_options_drift_remain_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            f = Fixture(Path(temporary), "setup_only")
            calls = []
            def observe(*args):
                calls.append(True)
                return {"wrapper_path": "/vendor/ansysedt", "known_file_records": {}, "generation": len(calls)}
            f.owner.validate_installation = observe
            result = f.run()
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["installation_files_unchanged"])
            self.assertNotEqual(pilot.read_sealed(result["installation_observation_before"]),
                                pilot.read_sealed(result["installation_observation_after"]))
        with tempfile.TemporaryDirectory() as temporary:
            f = Fixture(Path(temporary))
            launch = f.owner.owned_batch
            def solve(command, output, **kwargs):
                value = launch(command, output, **kwargs)
                (output / "options.txt").write_bytes(b"changed after solve")
                return value
            f.owner.owned_batch = solve
            result = f.run()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(f.events.count("solver"), 1)
            self.assertFalse(result["solver_state_uncertain"])
            self.assertFalse(result["automatic_retry_allowed"])

    def test_verified_copy_is_lossless_and_never_overwrites_an_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target = Path(temporary) / "original.aedt", Path(temporary) / "retained.aedt"
            raw = b"\xff\xfeA\x00\r\n\x00\x80untouched"
            source.write_bytes(raw)
            item = pilot.copy_new_verified(source, target)
            self.assertEqual(pilot.read_sealed(item), raw)
            self.assertEqual(source.read_bytes(), raw)
            with self.assertRaises(FileExistsError): pilot.copy_new_verified(source, target)
            self.assertEqual(target.read_bytes(), raw)


if __name__ == "__main__": unittest.main()
