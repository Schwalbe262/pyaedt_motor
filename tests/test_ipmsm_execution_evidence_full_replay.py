"""Portable full-entry replay: real numeric fixture, explicitly inert OS context.

No Native, HTTP, external C paths, guard evaluation, or function mocking.
The old helper fails this positive test at JSON tuple/list spec equality.
"""
from dataclasses import asdict
import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import run_ipmsm_batch as runner
from pyaedt_motor import execution_evidence as evidence, public_batch_pilot as pilot
from test_ipmsm_execution_evidence import Bundle

try:
    from ansys.aedt.core.internal import load_aedt_file
except ImportError:
    HAS_AEDT_FILE_PARSER = False
else:
    HAS_AEDT_FILE_PARSER = True

FIXTURE = Path(__file__).with_name("fixtures") / "public_cli_p3_numeric"


def raw_fixture(name):
    manifest = json.loads((FIXTURE / "PROVENANCE.json").read_bytes())["files"][name]
    compressed = (FIXTURE / (name + ".gz")).read_bytes()
    if evidence._record(compressed) != manifest["gzip"]:
        raise AssertionError("gzip fixture changed")
    raw = gzip.decompress(compressed)
    if evidence._record(raw) != manifest["raw"]:
        raise AssertionError("raw fixture changed")
    return raw


class FullReplayBundle(Bundle):
    def __init__(self, root):
        super().__init__(root)
        synthetic_builder = copy.deepcopy(self.data["builder_result"])
        actual_case = json.loads(raw_fixture("case.json"))
        actual_case["case_id"] = self.case_id
        self.data["case"] = actual_case
        raw_builder = json.loads(raw_fixture("builder.json"))
        raw_builder.update(synthetic_builder)
        raw_builder["artifact_setup_audit"] = "/private/test/scene/setup_audit.json"
        raw_builder["input_producer_fingerprint"] = self.producer
        self.data["builder_result"] = raw_builder
        self.data["setup_audit"] = json.loads(raw_fixture("setup_audit.json"))
        self.data["project"] = raw_fixture("solved.aedt")
        self.add("profile", raw_fixture("saved.profile"), "/private/test/native.profile")
        self.add("prepared", json.loads(raw_fixture("prepared.json")), "/private/test/prepared_native_closure.json")
        # Actual installed Native-free parser; the test never creates or opens a
        # Desktop. Raw bytes are those of the already collected user project.
        temporary_project = self.root / "parse_fixture.aedt"
        temporary_project.write_bytes(self.data["project"])
        solved = pilot.saved_native(temporary_project)
        self.add("solved", solved, "/private/test/solved_native_closure.json")
        contract = self.data["contract"]
        contract["case"] = self.ref("case")
        receipt = self.data["receipt"]
        receipt["case_sha256"] = self.rec("case")["sha256"]
        receipt["execution_contract_sha256"] = pilot.execution_contract_sha256(contract)
        contract["reservation_receipt"] = self.ref("receipt")
        self.data["source_inputs"].update({k: contract[k] for k in ("case", "installation_binding", "reservation_receipt")})
        self.data["cli_intent"].update(reservation_receipt=contract["reservation_receipt"], prepared_project=self.rec("project"))
        self.data["pilot_result"].update(prepared_project={"path": self.project, **self.rec("project")},
            status="solved_saved_closure_unproven", saved_serialization_comparison=pilot.compare_saved(self.data["prepared"], solved))
        mapping = {}
        report_records = {}
        from module.ipmsm_ppt_setup import PPT_REPORT_DEFS
        for report in PPT_REPORT_DEFS:
            if report == "PPT_EnergyError":
                continue
            logical = "report/" + report
            self.add(logical, raw_fixture(report + ".csv"), "/private/test/reports/" + report + ".csv")
            key = "artifact_report_" + report
            mapping[key] = self.original[logical]
            report_records[key] = self.rec(logical)
        targets = json.loads((FIXTURE / "expected_targets.json").read_bytes())["summary"]
        self.data["pilot_result"]["postprocess"] = {"report_mapping": mapping, "report_records": report_records,
            "summary": targets, "native_profile_record": self.rec("profile")}
        self.reseal()

    def reseal(self):
        # Bind raw and embedded builder representations through the actual
        # public serialization contract, not a fake SDK Analyze return.
        super().reseal()
        raw = self.data["builder_result"]
        embedded = copy.deepcopy(raw)
        paths = embedded.pop("serialization_unavailable_numeric_paths", [])
        self.data["pilot_result"]["builder_result"] = embedded
        self.data["pilot_result"]["serialization_unavailable_numeric_paths"] = [["builder_result", *p] for p in paths]
        self.data["pilot_result"]["public_setup_evidence"] = self.rec("setup_audit")

    def write(self):
        records = {}
        # Keep source entries at the actual imported module paths. The common
        # test root and artifacts are derived only from this package's paths.
        for i, name in enumerate(self.data):
            if name.startswith("source/"):
                path = self.source / name.removeprefix("source/")
                if path.read_bytes() != self.raw(name):
                    raise AssertionError("source fixture bytes changed")
            else:
                path = self.root / ("file" + str(i) + ".bin")
                path.write_bytes(self.raw(name))
            records[name] = {"path": path.relative_to(self.source).as_posix(),
                             "original_path": self.original[name], **self.rec(name)}
        value = {"schema": evidence.INDEX_SCHEMA, "backend": evidence.CLI, "case_id": self.case_id,
                 "producer_fingerprint": self.producer, "files": records,
                 "verifier_sha256": hashlib.sha256(Path(evidence.__file__).read_bytes()).hexdigest()}
        index = self.root / "index.json"
        index.write_bytes(json.dumps(value, sort_keys=True).encode())
        return index, hashlib.sha256(index.read_bytes()).hexdigest()

    def verify(self):
        index, digest = self.write()
        return evidence.verify_cli_evidence(index, digest, self.source, case_id=self.case_id, producer=self.producer)


@unittest.skipUnless(HAS_AEDT_FILE_PARSER, "requires optional fea pyaedt==0.22.0 pure-file parser")
class FullSerializedSpecReplayTests(unittest.TestCase):
    def bundle(self):
        # Same filesystem as imported source; works on Windows drive letters
        # and Linux without absolute runtime/test-machine paths.
        return tempfile.TemporaryDirectory(prefix="inert_cli_", dir=Path(__file__).resolve().parent)

    def test_current_spec_json_roundtrip_in_explicit_inert_bundle(self):
        with self.bundle() as temporary:
            bundle = FullReplayBundle(temporary)
            spec = runner.build_spec(bundle.data["case"], default_electrical_zero_deg=0.0)
            self.assertEqual(asdict(spec)["core_material_fallbacks"], ())
            self.assertEqual(bundle.data["setup_audit"]["requested_spec"]["core_material_fallbacks"], [])
            # Explicit synthetic current-schema metadata on an inert bundle.
            # Archived gzip/raw bytes remain unchanged and are tested below.
            legacy_spec = bundle.data["setup_audit"]["requested_spec"]
            current_spec = json.loads(json.dumps(asdict(spec)))
            self.assertEqual(set(current_spec) - set(legacy_spec), {
                "save_fields_start_step", "save_fields_stop_step", "save_fields_every_n_steps"})
            self.assertEqual({key: current_spec[key] for key in legacy_spec}, legacy_spec)
            bundle.data["setup_audit"]["requested_spec"] = current_spec
            bundle.reseal()
            result = bundle.verify()
            self.assertEqual(result["errors"], [])
            self.assertTrue(result["solver_context_known"])
            self.assertTrue(result["provenance_passed"])
            self.assertTrue(result["physical_quality_passed"])
            self.assertTrue(result["resource_4core_proven"])
            self.assertEqual(result["raw_grid_points"], 901)
            self.assertFalse(result["full_saved_closure_passed"])
            self.assertFalse(result["training_eligible"])
            self.assertEqual(result["status"], "HELD_ACTUAL_BACKEND_VALIDATION")

    def test_archived_spec_missing_save_controls_is_rejected_unchanged(self):
        with self.bundle() as temporary:
            bundle = FullReplayBundle(temporary)
            archived_spec = bundle.data["setup_audit"]["requested_spec"]
            self.assertEqual(len(archived_spec), 39)
            self.assertFalse(any(key.startswith("save_fields_") for key in archived_spec))
            result = bundle.verify()
            self.assertTrue(result["solver_context_known"])
            self.assertEqual(result["errors"], [{
                "phase": "admission_evidence", "error": "public builder setup/NoAnalyze differs"}])
            self.assertNotIn("physical_quality_passed", result)

    def test_changed_serialized_spec_rejected_by_same_full_entrypoint(self):
        for field in ("steps_per_period", "core_material_fallbacks"):
            with self.subTest(field=field), self.bundle() as temporary:
                bundle = FullReplayBundle(temporary)
                if field == "steps_per_period":
                    bundle.data["setup_audit"]["requested_spec"][field] += 1
                else:
                    bundle.data["setup_audit"]["requested_spec"][field] = ["PNF-Core"]
                bundle.reseal()
                result = bundle.verify()
                self.assertTrue(result["solver_context_known"])
                self.assertFalse(result["solver_state_uncertain"])
                self.assertEqual(result["errors"], [{"phase": "admission_evidence", "error": "public builder setup/NoAnalyze differs"}])
                self.assertNotIn("physical_quality_passed", result)


if __name__ == "__main__":
    unittest.main()
