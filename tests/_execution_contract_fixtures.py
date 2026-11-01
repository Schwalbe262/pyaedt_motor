"""Inert fixture receipts and outputs only; these are not actual reservations."""
import copy
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from pyaedt_motor import execution_contracts as ec, public_batch_pilot as pilot, quality
import calibrate_ipmsm_beta as beta

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pure_quality_fixtures", SOURCE / "tests/test_ipmsm_v3_quality.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def seal(path):
    return {"path": str(path.resolve()), **pilot.record(path)}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pilot.canonical_json(value))
    return seal(path)


def csv_write(path, rows):
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=sorted(set().union(*(row.keys() for row in rows))))
    writer.writeheader(); writer.writerows(rows)
    path.write_bytes(buf.getvalue().encode())
    return {"snapshot": str(path.resolve()), "path": str(path.resolve()), "sha256": pilot.record(path)["sha256"]}


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="execution_contract_fixture_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        library = self.root / "library"
        library.mkdir()
        for n in range(23):
            (library / f"fixture_{n}.py").write_text(f"# inert library {n}\n", encoding="utf8")
        syslib = self.root / "syslib.amat"; syslib.write_bytes(b"inert material fixture")
        install = self.root / "install"
        paths = ["ansysedt", "ansysedt.exe", ".setup_runtime", "product.info", "libngcore.so",
                 "mainwin561/Linux64/GenericClassicRegistry/mainwin.classic.hklm.bin",
                 "mainwin561/Linux64/mw/setup-mwuser", "mainwin561/Linux64/mw/lib/setup-mwarch.sh",
                 ".prodsetup", "mainwin561/Linux64/mw/setmwruntime"]
        for relative in paths:
            path = install / relative; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("inert fixture " + relative).encode())
        interpreter = self.root / "bash"; interpreter.write_bytes(b"inert interpreter content")
        known = [seal(install / name) for name in [*paths[:4], *paths[8:]]]
        binding = {"install_root": str(install), "wrapper_path": str(install / "ansysedt"),
                   "native_exe": str(install / "ansysedt.exe"), "interpreter": str(interpreter),
                   "shebang": "#!/bin/bash", "static_registry_relative": paths[5],
                   "known_installed_files": known}
        inventory = {"interpreter": seal(interpreter), "static_registry": seal(install / paths[5]),
                     "setup_mwuser": seal(install / paths[6]), "setup_mwarch": seal(install / paths[7]),
                     "core_libraries": [seal(install / "libngcore.so")]}
        source = {n: pilot.record(SOURCE / n) for n in ec.REQUIRED_SOURCE}
        producer = "producer_v3:sha256:" + ec._hash({n: source[n]["sha256"] for n in pilot.PRODUCER_FILES})
        self.case = {"case_id": "fixture_case_1", "aedt_version": "2025.2"}
        self.contract = {"schema": ec.SCHEMA, "mode": "solve", "purpose": "diagnostic", "stage": "diagnostic",
                         "backend": ec.BACKEND, "case_id": self.case["case_id"],
                         "case": write(self.root / "case.json", self.case), "source_root": str(SOURCE),
                         "source_files": source, "library_root": str(library),
                         "library_files": {p.name: pilot.record(p) for p in library.iterdir()},
                         "system_material_files": {str(syslib): pilot.record(syslib)},
                         "installation_binding": write(self.root / "install.json", binding),
                         "installation_inventory": write(self.root / "inventory.json", inventory),
                         "expected_producer": producer, "cli_policy": copy.deepcopy(ec.CLI_POLICY),
                         "options": copy.deepcopy(ec.OPTION_POLICY), "node": "n113", "account": "fixture_user",
                         "reference_frame": None, "selected_profile": None, "reservation_receipt": None}
        self.authorize(self.contract)

    def authorize(self, contract, attempt=1, prefix=None):
        root = self.root / ("authority_" + contract["case_id"])
        root.mkdir(exist_ok=True)
        rows = [] if prefix is None else copy.deepcopy(prefix)
        row = {"case_id": contract["case_id"], "stage": contract["stage"], "budget_attempt": attempt,
               "campaign_id": "synthetic_fixture_only", "reservation_token": "fixture_" + str(attempt)}
        rows.append(row)
        ledger = root / "snapshot.jsonl"
        ledger.write_bytes(b"".join(pilot.canonical_json(item) + b"\n" for item in rows))
        receipt = {"case_id": contract["case_id"], "stage": contract["stage"], "category": contract["purpose"],
                   "budget_attempt": attempt, "reserved_attempt": attempt, "execution_authorized": True,
                   "producer_fingerprint": contract["expected_producer"], "case_sha256": contract["case"]["sha256"],
                   "execution_contract_sha256": ec.contract_sha256(contract),
                   "reservation_ledger_snapshot": seal(ledger), "ledger_record_sha256": ec._hash(row),
                   "campaign_id": row["campaign_id"], "reservation_token": row["reservation_token"]}
        contract["reservation_receipt"] = write(root / "receipt.json", receipt)
        return rows

    def rewrite_case(self, case=None):
        self.contract["case"] = write(self.root / "case.json", self.case if case is None else case)

    def production(self, stage="training"):
        c = self.contract
        c.update(stage=stage, purpose=stage)
        zero = {"method": beta.ZERO_CALIBRATION_METHOD, "convention": ec.frames.CONVENTION,
                "electrical_zero_deg": -90.0, "initial_position_deg": -22.5,
                "producer_fingerprint": c["expected_producer"],
                "material_fingerprint": "materials_v2:sha256:" + "b" * 64,
                "core_material_model": "legacy_pnf_hybrid_v1", "core_material": "30PNF1600",
                "series_turns_per_phase": 48, "turns_per_coil_side": 12}
        zero["calibration_id"] = "beta-calibration:sha256:" + ec._hash(zero)
        c["reference_frame"] = {"manifest": write(self.root / "frame.json", zero), "local_evidence": None}
        plans = fixtures.cases(refined=True)
        for row in plans:
            row.update(beta_calibration_id=zero["calibration_id"], require_steady_state=True)
        self.case = copy.deepcopy(plans[1]); self.case.update(case_id=c["case_id"], aedt_version="2025.2")
        self.rewrite_case()
        cohort = ec.cohort_fingerprint(c)
        rows = [fixtures.result(plan) for plan in plans]
        for row in rows:
            row.update(input_producer_fingerprint=c["expected_producer"], input_execution_backend="standalone",
                       input_execution_cohort_fingerprint=cohort)
        plan_ref = csv_write(self.root / "plan.csv", plans)
        result_ref = csv_write(self.root / "results.csv", rows)
        # Replay must use precisely the serialized CSV values, as the real CLI does.
        parse = lambda p: list(csv.DictReader(io.StringIO(p.read_text(encoding="utf8"))))
        mesh = {plan["case_id"]: count for plan, count in zip(plans, (200, 100, 400))}
        report = quality.compare_profiles(parse(self.root / "results.csv"), parse(self.root / "plan.csv"),
                                          reference_profile="reference", refined_reference_profile="refined", mesh_counts=mesh)
        mesh_files = []
        for cid, count in mesh.items():
            ref = write(self.root / (cid + "_mesh.json"), [{"elements": count}])
            mesh_files.append({"case_id": cid, "snapshot": ref["path"], "path": ref["path"], "sha256": ref["sha256"]})
        comparison = Path(quality.__file__)
        gate = Path(quality.v3_training_row_issues.__code__.co_filename)
        report.update(case_plan_file=plan_ref, case_plan_sha256=plan_ref["sha256"], result_files=[result_ref],
                      mesh_files=mesh_files, comparison_source_sha256=pilot.record(comparison)["sha256"],
                      comparison_source_snapshot=str(comparison),
                      metric_gate_source={"snapshot": str(gate), "sha256": pilot.record(gate)["sha256"]})
        self.report_path = self.root / "report.json"; write(self.report_path, report)
        selected = quality.selected_profile_manifest(report, self.report_path, "fast")
        c["selected_profile"] = write(self.root / "selected.json", selected)
        self.authorize(c)
        return c

    def test_diagnostic_contract_and_acyclic_receipt(self):
        result = ec.validate_contract(self.contract)
        self.assertTrue(result["launch_contract_validated"])
        self.assertFalse(result["training_admission"])
        old = ec.contract_sha256(self.contract)
        c = copy.deepcopy(self.contract); c["reservation_receipt"] = None
        self.assertEqual(old, ec.contract_sha256(c))
        c["reservation_receipt"] = self.contract["reservation_receipt"]
        c["node"] = "n111"
        self.assertNotEqual(old, ec.contract_sha256(c))
        self.assertEqual(ec.cohort_fingerprint(c), ec.cohort_fingerprint(self.contract))
        with self.assertRaisesRegex(ValueError, "whole contract"):
            ec.validate_contract(c)

    def test_training_and_design_replay_actual_validators(self):
        self.production()
        r = ec.validate_contract(self.contract)
        self.assertEqual(r["profile_context"]["profile"], "fast")
        self.assertTrue(r["frame_id"].startswith("beta-calibration:"))
        self.assertFalse(r["training_admission"])
        self.contract.update(purpose="design", stage="design"); self.authorize(self.contract)
        self.assertEqual(ec.validate_contract(self.contract)["purpose"], "design")

    def test_no_missing_contract_or_sdk_fallback(self):
        for bad in (None, {}, {**self.contract, "backend": "sdk"}, {**self.contract, "schema": pilot.SCHEMA}):
            with self.subTest(bad=bad and bad.get("backend")), self.assertRaises(ValueError):
                ec.validate_contract(bad)
        self.assertEqual(pilot.SCHEMA, "public_owned_cli_batch_pilot_v1")
        with self.assertRaisesRegex(ValueError, "initial diagnostic pilot"):
            c = copy.deepcopy(self.contract); self.case["beta_calibration_id"] = "fixture_nonblank"
            self.rewrite_case(); c["case"] = self.contract["case"]
            pilot.verify_inputs({**c, "schema": pilot.SCHEMA})

    def test_purpose_and_typed_attempts(self):
        c = copy.deepcopy(self.contract); c["purpose"] = "training"
        with self.assertRaisesRegex(ValueError, "purpose"):
            ec.validate_contract(c)
        for key, value in (("reserved_attempt", True), ("budget_attempt", True), ("category", "design"),
                           ("producer_fingerprint", "producer_v3:sha256:" + "0" * 64)):
            receipt = json.loads(pilot.read_sealed(self.contract["reservation_receipt"]))
            receipt[key] = value
            c = copy.deepcopy(self.contract); c["reservation_receipt"] = write(self.root / "bad_receipt.json", receipt)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "actual reservation"):
                ec.validate_contract(c)

    def test_training_missing_frame_or_profile_and_forged_pass_rejected(self):
        c = copy.deepcopy(self.contract); c.update(stage="training", purpose="training")
        with self.assertRaisesRegex(ValueError, "frame"):
            ec.validate_contract(c)
        self.production()
        c = copy.deepcopy(self.contract); c["selected_profile"] = None
        with self.assertRaisesRegex(ValueError, "selected profile"):
            ec.validate_contract(c)
        report = json.loads(self.report_path.read_bytes()); report["reference_certification"][0]["certified"] = False
        write(self.report_path, report)
        selected = json.loads(pilot.read_sealed(self.contract["selected_profile"]))
        selected["evidence"][0]["sha256"] = pilot.record(self.report_path)["sha256"]
        self.contract["selected_profile"] = write(self.root / "selected.json", selected)
        self.authorize(self.contract)
        with self.assertRaisesRegex(ValueError, "recomputed quality decision"):
            ec.validate_contract(self.contract)

    def test_invalid_frame_identity_and_missing_family_evidence(self):
        self.production()
        frame = json.loads(pilot.read_sealed(self.contract["reference_frame"]["manifest"]))
        frame["electrical_zero_deg"] = -80
        self.contract["reference_frame"]["manifest"] = write(self.root / "frame.json", frame)
        with self.assertRaisesRegex(ValueError, "content does not match"):
            ec.validate_contract(self.contract)
        self.contract["reference_frame"] = {"manifest": write(self.root / "family.json", {"schema_version": ec.frames.SCHEMA}), "local_evidence": None}
        with self.assertRaises((ValueError, TypeError)):
            ec.validate_contract(self.contract)

    def test_runtime_drift_and_policy_rejection(self):
        old = ec.cohort_fingerprint(self.contract)
        path = Path(self.contract["library_root"]) / "fixture_0.py"; path.write_bytes(b"changed")
        with self.assertRaises(ValueError): ec.validate_contract(self.contract)
        self.contract["library_files"][path.name] = pilot.record(path)
        self.assertNotEqual(old, ec.cohort_fingerprint(self.contract))
        for key, value in (("F903_requested_environment", "1"), ("cores", True), ("solve", "SDK")):
            c = copy.deepcopy(self.contract); c["cli_policy"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "CLI policy"):
                ec.validate_contract(c)

    def test_cohort_excludes_case_receipt_node_account_and_rejects_mixed_duplicate(self):
        c = copy.deepcopy(self.contract)
        old = ec.cohort_fingerprint(c)
        prefix = json.loads(pilot.read_sealed(c["reservation_receipt"]))
        first_rows = [json.loads(line) for line in Path(prefix["reservation_ledger_snapshot"]["path"]).read_bytes().splitlines()]
        c.update(case_id="fixture_case_2", account="another_fixture_account", node="n111")
        c["case"] = write(self.root / "case2.json", {"case_id": c["case_id"], "aedt_version": "2025.2"})
        self.authorize(c, 2, first_rows)
        self.assertEqual(old, ec.cohort_fingerprint(c))
        self.assertEqual(len(ec.validate_cohort([self.contract, c])), 2)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ec.validate_cohort([self.contract, self.contract])
        other = self.root / "otherlib"; other.mkdir()
        for path in Path(c["library_root"]).iterdir(): (other / path.name).write_bytes(path.read_bytes())
        (other / "fixture_0.py").write_bytes(b"different")
        c["library_root"] = str(other); c["library_files"] = {p.name: pilot.record(p) for p in other.iterdir()}
        self.authorize(c, 2, first_rows)
        with self.assertRaisesRegex(ValueError, "mixed execution cohort"):
            ec.validate_cohort([self.contract, c])

    def test_full_ledger_row_tamper_and_budget_limit(self):
        receipt = json.loads(pilot.read_sealed(self.contract["reservation_receipt"]))
        ledger = Path(receipt["reservation_ledger_snapshot"]["path"])
        row = json.loads(ledger.read_bytes()); row["unprojected_extra_authority"] = "tamper"
        ledger.write_bytes(pilot.canonical_json(row) + b"\n")
        receipt["reservation_ledger_snapshot"] = seal(ledger)
        self.contract["reservation_receipt"] = write(self.root / "receipt_tamper.json", receipt)
        with self.assertRaisesRegex(ValueError, "full reserved ledger row"):
            ec.validate_contract(self.contract)
        c = copy.deepcopy(self.contract); c["case_id"] = "fixture_over_budget"
        c["case"] = write(self.root / "overcase.json", {"case_id": c["case_id"], "aedt_version": "2025.2"})
        prefix = [{"case_id": f"fixture_prev_{n}", "stage": "diagnostic", "budget_attempt": n + 1} for n in range(300)]
        self.authorize(c, 301, prefix)
        with self.assertRaisesRegex(ValueError, "authorized budget"):
            ec.validate_contract(c)

    def test_profile_cannot_reuse_sdk_or_mixed_cohort_rows(self):
        self.production()
        rows = list(csv.DictReader(io.StringIO((self.root / "results.csv").read_text())))
        rows[0]["input_execution_backend"] = "sdk"
        report = json.loads(self.report_path.read_bytes()); report["result_files"] = [csv_write(self.root / "results.csv", rows)]
        write(self.report_path, report)
        selected = json.loads(pilot.read_sealed(self.contract["selected_profile"]))
        selected["evidence"][0]["sha256"] = pilot.record(self.report_path)["sha256"]
        self.contract["selected_profile"] = write(self.root / "selected.json", selected); self.authorize(self.contract)
        with self.assertRaisesRegex(ValueError, "no SDK fallback"):
            ec.validate_contract(self.contract)

    def test_selected_settings_and_actual_mesh_proof_cannot_be_faked(self):
        self.production()
        self.case["steps_per_period"] += 1
        self.rewrite_case(); self.authorize(self.contract)
        with self.assertRaisesRegex(ValueError, "case selected profile setting"):
            ec.validate_contract(self.contract)
        self.case["steps_per_period"] -= 1
        self.rewrite_case(); self.authorize(self.contract)
        report = json.loads(self.report_path.read_bytes())
        item = next(item for item in report["mesh_files"] if item["case_id"] == "g1_refined")
        ref = write(Path(item["snapshot"]), [{"elements": 200}])
        item["sha256"] = ref["sha256"]
        write(self.report_path, report)
        selected = json.loads(pilot.read_sealed(self.contract["selected_profile"]))
        selected["evidence"][0]["sha256"] = pilot.record(self.report_path)["sha256"]
        self.contract["selected_profile"] = write(self.root / "selected.json", selected); self.authorize(self.contract)
        with self.assertRaisesRegex(ValueError, "recomputed quality decision"):
            ec.validate_contract(self.contract)

    def test_installation_content_and_missing_native_binary_change_cohort(self):
        old = ec.cohort_fingerprint(self.contract)
        binding = json.loads(pilot.read_sealed(self.contract["installation_binding"]))
        binary = Path(binding["native_exe"]); binary.write_bytes(b"different inert binary")
        with self.assertRaises(ValueError): ec.cohort_fingerprint(self.contract)
        for item in binding["known_installed_files"]:
            if item["path"] == str(binary): item.update(pilot.record(binary))
        self.contract["installation_binding"] = write(self.root / "install.json", binding)
        self.assertNotEqual(old, ec.cohort_fingerprint(self.contract))
        binding["known_installed_files"] = [x for x in binding["known_installed_files"] if x["path"] != str(binary)]
        self.contract["installation_binding"] = write(self.root / "install.json", binding)
        with self.assertRaisesRegex(ValueError, "Native content binding"):
            ec.cohort_fingerprint(self.contract)

    def test_collected_row_does_not_gain_admission_from_launch_contract(self):
        validated = ec.validate_contract(self.contract)
        row = {"case_id": self.contract["case_id"], "input_execution_backend": ec.BACKEND,
               "input_execution_contract_sha256": validated["contract_sha256"],
               "input_execution_cohort_fingerprint": validated["cohort_fingerprint"],
               "input_execution_purpose": "diagnostic", "input_producer_fingerprint": self.contract["expected_producer"],
               "public_training_admission": False}
        self.assertFalse(ec.validate_row_contract(row, self.contract)["training_admission"])
        row["input_execution_backend"] = "standalone"
        with self.assertRaisesRegex(ValueError, "row contract mismatch"):
            ec.validate_row_contract(row, self.contract)


if __name__ == "__main__":
    unittest.main()
