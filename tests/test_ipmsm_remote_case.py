"""Local subprocess fixtures validate transport; no AEDT or scheduler is used."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from pyaedt_motor import remote_case as remote


RUNNER = r'''
import argparse,csv,hashlib,json,os,sys,time
from pathlib import Path
p=argparse.ArgumentParser()
for name in ['cases','max-cases','workers','cores','simulation-dir','result-csv','aedt-backend']:
    p.add_argument('--'+name,required=True)
p.add_argument('--keep-projects',action='store_true',required=True)
p.add_argument('--non-graphical',action='store_true',required=True)
a=p.parse_args()
row=next(csv.DictReader(Path(a.cases).open(encoding='utf-8-sig')))
behavior=row.get('behavior','good')
root=Path(__file__).parent
files=['run_ipmsm_batch.py','module/ipmsm_geometry.py','module/ipmsm_ppt_setup.py',
       'module/ipmsm_waveform_quality.py','module/variable.py']
source={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}
producer='producer_v3:sha256:'+hashlib.sha256(json.dumps(source,sort_keys=True,separators=(',',':')).encode()).hexdigest()
project=Path(a.simulation_dir)/'simulation0'
project.mkdir(parents=True)
exports=project/'exports';exports.mkdir()
report=exports/'Physical_Voltages.csv';report.write_bytes(b'Time,Va,Vb,Vc\n0,1,-1,0\n')
(project/'simulation0.aedt').write_bytes(b'untouched AEDT project\x00\x01')
audit=project/'setup_audit.json';audit.write_text('{"analysis":true}')
(exports/'execution.json').write_text('{"nested_evidence":true}')
solution=project/'simulation0.aedtresults';solution.mkdir()
(solution/'current.stats').write_text('0|123|region\n')
(solution/'fields.bin').write_bytes(b'large reproducible solved fields\x00')
(solution/'solver.log').write_text('solver evidence\n')
(Path(os.environ['TMPDIR'])/'solver_aux.log').write_text('temporary solver log\n')
(root/'simulation_num.txt').write_text('1')
print('fixture runner, not an FEA',flush=True)
if behavior=='missing':sys.exit(2)
output={'case_id':row['case_id'],'status':'failed' if behavior=='failed' else 'ok',
        'input_producer_fingerprint':producer,'input_setup_fingerprint':'same-setup',
        'input_source_result_path':'/original/reference.csv','output_torque_last_avg_nm':'123.45600000000001',
        'output_voltage_span_last_peak_v':'188.760','project_path':str(project),
        'artifact_setup_audit':str(audit),'artifact_physical_voltage_waveform':str(report),
        'artifact_report_optional':'skipped: unavailable diagnostic', 'elapsed_s':'1.25'}
if behavior=='escape':output['artifact_physical_voltage_waveform']=str(Path(os.environ['TMPDIR'])/'solver_aux.log')
if behavior=='tamper':(root/'module/variable.py').write_text('# changed after solve')
with Path(a.result_csv).open('w',encoding='utf-8-sig',newline='') as stream:
    writer=csv.DictWriter(stream,list(output));writer.writeheader();writer.writerow(output)
if behavior=='timeout':time.sleep(20)
sys.exit(3 if behavior=='failed' else 0)
'''


class RemoteCaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source, self.library, self.scratch, self.durable = [self.root / name for name in ["source", "library", "scratch", "durable"]]
        for path in [self.source, self.library, self.scratch, self.durable]:
            path.mkdir()
        for name in remote.PRODUCER_FILES:
            path = self.source / name; path.parent.mkdir(exist_ok=True)
            path.write_text(RUNNER if name == "run_ipmsm_batch.py" else "# sealed producer\n", encoding="utf-8")
        (self.source / "material").mkdir()
        (self.source / "material/steel.csv").write_text("B,H\n1,20\n")
        (self.library / "pyaedt_module").mkdir()
        (self.library / "pyaedt_module/__init__.py").write_text("# sealed library\n")
        self.manifest = self.root / "source_manifest.json"
        self.manifest.write_bytes(remote.json_bytes(remote.seal_source(self.source, self.library)))
        self.cases = self.root / "case.csv"
        self.write_case()

    def tearDown(self):
        self.temporary.cleanup()

    def write_case(self, behavior="good", case_id="case-001"):
        self.cases.write_text(f"case_id,behavior\n{case_id},{behavior}\n", encoding="utf-8")

    def arguments(self):
        return dict(source_root=self.source, library_root=self.library, source_manifest=self.manifest,
                    source_sha256=remote.digest(self.manifest), cases=self.cases, cases_sha256=remote.digest(self.cases),
                    case_id="case-001", scratch_root=self.scratch, durable_root=self.durable, cores=2)

    def run_case(self, **changes):
        return remote.execute_case(**dict(self.arguments(), **changes))

    def read_result(self, path):
        return next(csv.DictReader(path.open(encoding="utf-8-sig", newline="")))

    def test_full_preservation_relocates_only_artifacts_and_cleans_owned_scratch(self):
        before_source = remote.seal_source(self.source, self.library)
        result = self.run_case()
        self.assertTrue(result["success"], result)
        case = Path(result["durable_case"])
        self.assertTrue((case / "COMPLETE").is_file())
        manifest = json.loads((case / "manifest.json").read_bytes())
        self.assertEqual(manifest["omitted_solution_files"], {})
        actual = {path.relative_to(case).as_posix() for path in case.rglob("*")
                  if path.is_file() and path not in {case / "execution.json", case / "manifest.json", case / "COMPLETE"}}
        self.assertEqual(set(manifest["files"]), actual)
        self.assertTrue((case / "simulation0/simulation0.aedtresults/fields.bin").is_file())
        self.assertTrue((case / "simulation0/simulation0.aedtresults/current.stats").is_file())
        self.assertTrue((case / "scratch_aux/solver_aux.log").is_file())
        self.assertFalse(Path(result["scratch"]).exists())
        self.assertEqual(before_source, remote.seal_source(self.source, self.library))
        self.assertFalse((self.source / "simulation_num.txt").exists())
        raw = self.read_result(case / "raw_result.csv")
        relocated = self.read_result(Path(result["result_csv"]))
        for name, value in raw.items():
            if name == "project_path" or name.startswith("artifact_") and not value.startswith("skipped:"):
                self.assertTrue(Path(relocated[name]).exists())
                self.assertIn(str(case), relocated[name])
            else:
                self.assertEqual(value, relocated[name], name)
        for name, record in manifest["files"].items():
            self.assertEqual(remote._record(case / name), record)
        self.assertEqual(remote.digest(case / "manifest.json"), (case / "COMPLETE").read_text())
        self.assertEqual((case / "provenance/input.csv").read_bytes(), self.cases.read_bytes())
        self.assertIn(b"fixture runner", (case / "runner.log").read_bytes())

    def test_training_solution_omission_is_explicit_and_keeps_mesh_logs_reports(self):
        result = self.run_case(retention="training-no-solution")
        self.assertTrue(result["success"], result)
        case = Path(result["durable_case"])
        manifest = json.loads((case / "manifest.json").read_bytes())
        fields = "simulation0/simulation0.aedtresults/fields.bin"
        self.assertEqual(list(manifest["omitted_solution_files"]), [fields])
        self.assertFalse((case / fields).exists())
        self.assertEqual(manifest["omitted_solution_files"][fields]["sha256"], hashlib.sha256(b"large reproducible solved fields\x00").hexdigest())
        for name in ["current.stats", "solver.log"]:
            self.assertTrue((case / "simulation0/simulation0.aedtresults" / name).is_file())
        self.assertTrue((case / "simulation0/exports/Physical_Voltages.csv").is_file())
        self.assertTrue((case / "simulation0/setup_audit.json").is_file())

    def test_failed_solver_row_is_preserved_and_scratch_is_retained(self):
        self.write_case("failed")
        result = self.run_case()
        self.assertFalse(result["success"])
        self.assertEqual(result["phase"], "preserved_failure")
        self.assertEqual(result["runner_exit_code"], 3)
        self.assertTrue(Path(result["scratch"]).is_dir())
        self.assertEqual(self.read_result(Path(result["result_csv"]))["status"], "failed")
        self.assertTrue((Path(result["durable_case"]) / "COMPLETE").is_file())

    def test_missing_csv_is_visible_partial_failure_without_success_sentinel(self):
        self.write_case("missing")
        result = self.run_case()
        case = Path(result["durable_case"])
        self.assertEqual(result["phase"], "failed")
        self.assertIn("no result CSV", result["error"])
        self.assertTrue(Path(result["scratch"]).is_dir())
        self.assertTrue((case / "runner.log").is_file())
        self.assertTrue((case / "simulation0/simulation0.aedt").is_file())
        self.assertFalse((case / "COMPLETE").exists())
        self.assertFalse(Path(result["result_csv"]).exists())

    def test_timeout_does_not_publish_an_ok_row_or_clean_potentially_active_scratch(self):
        self.write_case("timeout")
        result = self.run_case(timeout_seconds=1)
        self.assertEqual(result["phase"], "failed")
        self.assertIn("timeout", result["runner_error"])
        self.assertTrue(Path(result["scratch"]).is_dir())
        self.assertTrue((Path(result["durable_case"]) / "raw_result.csv").is_file())
        self.assertFalse(Path(result["result_csv"]).exists())

    def test_csv_source_and_library_seals_are_checked_before_running(self):
        for changes in [dict(cases_sha256="0" * 64), dict(source_sha256="0" * 64)]:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "seal mismatch"):
                self.run_case(**changes)
        (self.library / "pyaedt_module/__init__.py").write_text("# changed\n")
        with self.assertRaisesRegex(ValueError, "changed from seal"):
            self.run_case()
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse((self.durable / "simulation").exists())

    def test_source_changes_and_artifact_escape_during_run_refuse_publication(self):
        for index, behavior in enumerate(["tamper", "escape"], start=1):
            case_id = f"case-{index:03d}"
            self.write_case(behavior, case_id=case_id)
            result = self.run_case(case_id=case_id)
            self.assertEqual(result["phase"], "failed")
            self.assertTrue(Path(result["scratch"]).exists())
            self.assertFalse(Path(result["result_csv"]).exists())
            self.assertFalse((Path(result["durable_case"]) / "COMPLETE").exists())

    def test_copy_verification_failure_preserves_scratch_and_partial_state(self):
        original = remote._copy_verified
        def fail_aedt(source, target, expected=None):
            if target.is_relative_to(self.durable) and target.suffix == ".aedt":
                raise RuntimeError("injected copy/hash verification failure")
            return original(source, target, expected)
        with mock.patch.object(remote, "_copy_verified", side_effect=fail_aedt):
            result = self.run_case()
        self.assertEqual(result["phase"], "failed")
        self.assertIn("copy/hash", result["error"])
        self.assertTrue(Path(result["scratch"]).is_dir())
        self.assertEqual(json.loads((Path(result["durable_case"]) / "execution.json").read_bytes())["phase"], "failed")
        self.assertFalse(Path(result["result_csv"]).exists())

    def test_duplicate_attempts_and_existing_result_are_never_overwritten(self):
        result = self.run_case()
        previous = Path(result["result_csv"]).read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_case()
        self.assertEqual(Path(result["result_csv"]).read_bytes(), previous)

    def test_unsafe_identifiers_relative_manifest_paths_and_nested_roots_are_rejected(self):
        for value in ["../escape", "a/b", "a\\b", "..", "case:stream"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_case(case_id=value)
        seal = json.loads(self.manifest.read_bytes())
        seal["files"]["../outside.py"] = {"bytes": 1, "sha256": "0" * 64}
        self.manifest.write_bytes(remote.json_bytes(seal))
        with self.assertRaisesRegex(ValueError, "relative manifest"):
            self.run_case()
        with self.assertRaisesRegex(ValueError, "separate"):
            self.run_case(scratch_root=self.source / "scratch")

    def test_duplicate_or_multiple_case_rows_are_rejected(self):
        for payload in [b"case_id,case_id\ncase-001,case-001\n", b"case_id\ncase-001\ncase-002\n"]:
            self.cases.write_bytes(payload)
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "one exact"):
                self.run_case()

    def test_symlinks_and_windows_reparse_ancestors_are_rejected(self):
        with mock.patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)):
            with self.assertRaisesRegex(ValueError, "symlink/reparse"):
                remote._safe(self.source)
        link = self.source / "module/linked.py"
        try:
            link.symlink_to(self.source / "module/variable.py")
        except OSError:
            return  # Windows can prohibit symlink creation; reparse guard is tested above.
        with self.assertRaisesRegex(ValueError, "symlink/reparse"):
            remote.seal_source(self.source, self.library)


if __name__ == "__main__":
    unittest.main()
