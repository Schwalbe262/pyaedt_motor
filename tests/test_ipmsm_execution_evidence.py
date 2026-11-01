"""Inert contract tests. Synthetic OS evidence is never actual Native proof."""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pyaedt_motor import execution_evidence as e, public_batch_pilot as pilot, remote_case


class Bundle:
    def __init__(self, root):
        self.root, self.data, self.original = Path(root), {}, {}
        self.source = Path(e.__file__).resolve().parents[2]
        self.case_id = "inert_owned_cli_loaded"
        self.original_root = "/private/test"
        self.project = "/private/test/scene/IPMSM/IPMSM.aedt"
        self.add("case", {"case_id": self.case_id, "aedt_version": "2025.2", "operation": "sin_current"}, "/control/case.json")
        self.add("installation_binding", {"wrapper_path": "/install/ansysedt", "native_exe": "/install/ansysedt.exe"}, "/control/install.json")
        source = {}
        for name in pilot.REQUIRED_SOURCE:
            self.add("source/" + name, (self.source / name).read_bytes(), "/source/" + name)
            source[name] = self.rec("source/" + name)
        libraries = {}
        for i in range(23):
            name = f"library{i}.py"
            self.add("library/" + name, b"# inert library\n", "/library/" + name)
            libraries[name] = self.rec("library/" + name)
        self.add("syslib", b"inert system material", "/install/syslib.amat")
        self.producer = "producer_v3:sha256:" + hashlib.sha256(e._canonical({k: source[k]["sha256"] for k in remote_case.PRODUCER_FILES})).hexdigest()
        contract = {"schema": pilot.SCHEMA, "mode": "solve", "case_id": self.case_id,
                    "expected_producer": self.producer, "source_root": "/source", "library_root": "/library",
                    "source_files": source, "library_files": libraries, "system_material_files": {"/install/syslib.amat": self.rec("syslib")},
                    "case": self.ref("case"), "installation_binding": self.ref("installation_binding"), "node": "n113",
                    "options": {"electrical_zero_deg": 0, "cores": 4, "aedt_backend": "standalone", "non_graphical": True,
                                "cleanup_linux": False, "model_extent": "full_360", "symmetry_factor": 1}}
        receipt = {"execution_authorized": True, "case_id": self.case_id, "producer_fingerprint": self.producer,
                   "category": "diagnostic", "stage": "diagnostic", "reserved_attempt": 1, "budget_attempt": 1,
                   "case_sha256": self.rec("case")["sha256"],
                   "execution_contract_sha256": hashlib.sha256(e._canonical(contract)).hexdigest()}
        self.add("receipt", receipt, "/control/receipt.json")
        contract["reservation_receipt"] = self.ref("receipt")
        self.add("contract", contract, "/control/contract.json")
        token = [100, 1.25, "posix", [1455, 1455], "ansysedt.exe"]
        builder = {"case_id": self.case_id, "input_producer_fingerprint": self.producer, "status": "ok", "analyze": False,
                   "standalone_solver_uncertain": False, "standalone_solver_state_contract": remote_case.SOLVER_CONTRACT,
                   "standalone_owner_identity": json.dumps(token), "standalone_owned_pid": 100,
                   "standalone_cleanup": json.dumps({"verified": True, "owned_pid": 100, "process_exited": True, "errors": [],
                        "SDK_release_desktop_called": False, "SDK_ReleaseAll_called": False, "atexit_function_replaced": False,
                        "client_retirement": {"client_retired": True, "Native_exit_implied": False}}),
                   "project_path": "/private/test/scene/IPMSM", "simulation_name": "IPMSM"}
        self.add("builder_result", builder, "/private/test/builder_result.json")
        self.add("project", b"inert saved bytes", self.project)
        self.add("setup_audit", {"requested_spec": {"setup_name": "PPT_Transient"}}, "/private/test/scene/setup_audit.json")
        command = pilot.batch_command("/install/ansysedt", Path(self.project), Path(self.original_root), "n113", "PPT_Transient")
        command = [value.replace("\\", "/") for value in command]
        self.add("command", command, "/private/test/command.json")
        self.add("batch_options", ("$begin 'Config'\n'Desktop/Settings/ProjectOptions/NumberOfProcessors'=4\n"
                 "'Maxwell 2D/NumCoresPerDistributedTask'=4\n$end 'Config'\n").encode("ascii"), "/private/test/options.txt")
        controller = self.identity(200, 2, 1, "/python", ["/python", "pilot.py"])
        wrapper = self.identity(300, 3, 200, "/bin/bash", ["/bin/bash", *command])
        native = self.identity(400, 4, 300, "/install/ansysedt.exe", ["/install/ansysedt.exe", *command[1:]])
        native.update(shell_startup_hooks_absent=True, selected_environment={e.FEATURE: "0", "ANS_MWREG": "static", "MWRT_MODE": "classic"})
        owner = {"controller_snapshot_before_Popen": True, "controller_identity": controller, "returncode": 0,
                 "solver_uncertain": False, "launcher_calls": 1, "Native_process_count": 1, "single_native_invocation_verified": True,
                 "all_observed_owned_processes_gone": True, "all_observed_processes_positively_identified": True,
                 "errors": [], "OS_signals": 0, "Native_API_calls": 0, "automatic_retry": False, "launcher_pid": 300,
                 "Native_processes": [native], "owned_processes": [self.process(wrapper), self.process(native)]}
        self.add("owner", owner, "/private/test/solver_owner_result.json")
        prepared = {"path": self.project, **self.rec("project")}
        self.add("cli_intent", {"schema": pilot.SCHEMA, "stage": "before_cli_intent", "case_id": self.case_id,
                 "producer": self.producer, "solver_state_uncertain": True, "automatic_retry_allowed": False,
                 "command": self.rec("command"), "reservation_receipt": contract["reservation_receipt"],
                 "prepared_project": self.rec("project")}, "/private/test/owned_cli_solver_state.json")
        self.add("cli_completion", {"schema": pilot.SCHEMA, "stage": "known_normal_cli_exit", "case_id": self.case_id,
                 "producer": self.producer, "solver_state_uncertain": False, "SDK_Analyze_return_claimed": False,
                 "intent_record": self.rec("cli_intent"), "solver_owner_record": self.rec("owner")}, "/private/test/owned_cli_solver_completion.json")
        self.add("pilot_result", {"schema": pilot.SCHEMA, "case_id": self.case_id, "mode": "solve", "status": "solved_quality_rejected",
                 "source_producer": self.producer, "BatchSolve_launch_attempts": 1, "solver_state_uncertain": False,
                 "automatic_retry_allowed": False, "standalone_Analyze_return_ABI_claimed": False, "builder_PID_is_solver_PID": False,
                 "Native_API_postprocess_calls": 0, "builder_result": builder, "solver_owner_result": owner,
                 "execution_code_identity": {"coordinator": source["src/pyaedt_motor/public_batch_pilot.py"], "OS_owner": source["src/pyaedt_motor/batch_ownership.py"]},
                 "prepared_project": prepared, "inputs_unchanged": True, "installation_files_unchanged": True}, "/private/test/pilot_result.json")
        before = {f"source/{k}": v for k, v in source.items()} | {f"library/{k}": v for k, v in libraries.items()}
        before["syslib//install/syslib.amat"] = self.rec("syslib")
        before.update({k: contract[k] for k in ("case", "installation_binding", "reservation_receipt")})
        self.add("source_inputs", before, "/private/test/source_and_inputs_before.json")

    @staticmethod
    def identity(pid, start, ppid, exe, argv):
        return {"pid": pid, "create_time": start, "ppid": ppid, "uid": 1455, "effective_uid": 1455,
                "identity_complete": True, "identity_changed_during_snapshot": False, "exe": exe, "cmdline": argv}

    @staticmethod
    def process(info):
        return {"pid": info["pid"], "create_time": info["create_time"], "state": "gone",
                "last_verified_identity": copy.deepcopy(info), "identity_events": [copy.deepcopy(info)]}

    def add(self, name, value, original):
        self.data[name], self.original[name] = copy.deepcopy(value), original

    def raw(self, name):
        value = self.data[name]
        return value if isinstance(value, bytes) else json.dumps(value, sort_keys=True).encode()

    def rec(self, name):
        return e._record(self.raw(name))

    def ref(self, name):
        return {"path": self.original[name], **self.rec(name)}

    def reseal(self):
        self.data["pilot_result"]["solver_owner_result"] = copy.deepcopy(self.data["owner"])
        self.data["pilot_result"]["builder_result"] = copy.deepcopy(self.data["builder_result"])
        if "cli_completion" in self.data:
            self.data["cli_completion"]["solver_owner_record"] = self.rec("owner")
            self.data["cli_completion"]["intent_record"] = self.rec("cli_intent")

    def write(self):
        records = {}
        for i, name in enumerate(self.data):
            path = f"file{i}.bin"
            (self.root / path).write_bytes(self.raw(name))
            records[name] = {"path": path, "original_path": self.original[name], **self.rec(name)}
        value = {"schema": e.INDEX_SCHEMA, "backend": e.CLI, "case_id": self.case_id,
                 "producer_fingerprint": self.producer, "files": records,
                 "verifier_sha256": hashlib.sha256(Path(e.__file__).read_bytes()).hexdigest()}
        index = self.root / "index.json"
        index.write_bytes(json.dumps(value, sort_keys=True).encode())
        return index, hashlib.sha256(index.read_bytes()).hexdigest()

    def verify(self):
        index, sha = self.write()
        return e.verify_cli_evidence(index, sha, self.root, case_id=self.case_id, producer=self.producer)


class ExecutionEvidenceTests(unittest.TestCase):
    def test_default_sdk_is_exact_delegate_for_existing_success_failure_and_missing(self):
        rows = [None, {}, {"case_id": "x", "status": "failed"},
                {"input_execution_backend": e.SDK},
                {"input_producer_fingerprint": "p", "standalone_solver_state_contract": remote_case.SOLVER_CONTRACT}]
        for row in rows:
            self.assertEqual(e.solver_context(row, "p"), remote_case.solver_row_context(row, "p"))

    def test_cli_never_falls_back_to_sdk_absent_intent(self):
        row = {"case_id": "x", "input_producer_fingerprint": "p", "standalone_solver_state_contract": remote_case.SOLVER_CONTRACT,
               "input_execution_backend": e.CLI}
        self.assertTrue(remote_case.solver_row_context(row, "p")["solver_context_known"])
        self.assertFalse(e.solver_context(row, "p")["solver_context_known"])
        self.assertFalse(e.solver_context(row, "p", backend=e.CLI)["solver_context_known"])
        with patch.object(e, "verify_cli_evidence", side_effect=AssertionError("wrong backend row must not read evidence")):
            self.assertFalse(e.solver_context({**row,"input_execution_backend":e.SDK},"p",backend=e.CLI,cli_index={})["solver_context_known"])

    def test_builder_null_serialization_paths_are_cross_bound_not_ignored(self):
        raw = {"status": "ok", "output_unavailable": None,
               "serialization_unavailable_numeric_paths": [["output_unavailable"]]}
        final = {"builder_result": {"status": "ok", "output_unavailable": None},
                 "serialization_unavailable_numeric_paths": [["builder_result", "output_unavailable"], ["postprocess", "other"]]}
        original = copy.deepcopy(raw)
        self.assertEqual(e._builder_copy(raw, final), final["builder_result"])
        self.assertEqual(raw, original)
        for bad in ([], [["builder_result", "foreign"]], [["builder_result", "output_unavailable"]]*2):
            with self.assertRaises(ValueError): e._builder_copy(raw, {**final, "serialization_unavailable_numeric_paths": bad})
        with self.assertRaises(ValueError): e._builder_copy({**raw,"output_unavailable":0}, final)

    def test_known_quiet_quality_rejection_and_missing_physics_remain_held(self):
        with tempfile.TemporaryDirectory() as d:
            f = Bundle(d)
            with patch.object(e, "_science_and_metrics", return_value={"physical_quality_passed": False, "physics_issues": ["power_balance_residual"]}):
                result = f.verify()
            self.assertTrue(result["solver_context_known"])
            self.assertFalse(result["solver_state_uncertain"])
            self.assertFalse(result["physical_quality_passed"])
            self.assertEqual(result["original_status"], "solved_quality_rejected")
            self.assertFalse(result["training_eligible"])
            result = f.verify()
            self.assertTrue(result["solver_context_known"])
            self.assertTrue(result["errors"])
            self.assertFalse(result["training_eligible"])

    def test_uncertain_missing_completion_rc_zero_without_native_and_fake_sdk_return_rejected(self):
        changes = [lambda f: f.data.pop("cli_completion"),
                   lambda f: f.data["owner"].update(controller_snapshot_before_Popen=False),
                   lambda f: f.data["owner"].update(Native_process_count=0, Native_processes=[]),
                   lambda f: f.data["owner"].update(errors=[{"stage": "identity_binding"}]),
                   lambda f: f.data["pilot_result"].update(standalone_Analyze_return_ABI_claimed=True),
                   lambda f: f.data["builder_result"].update(analyze=True),
                   lambda f: f.data["owner"]["owned_processes"][0].update(state="alive"),
                   lambda f: f.data["owner"]["controller_identity"].update(create_time=True),
                   lambda f: f.data["owner"]["owned_processes"][1]["identity_events"][0].update(ppid=999),
                   lambda f: f.data["contract"]["options"].update(cores=True),
                   lambda f: f.data["builder_result"].update(standalone_analyze_return_received=0),
                   lambda f: f.data["owner"].update(launcher_pid=True),
                   lambda f: f.data["builder_result"].update(standalone_cleanup="null"),
                   lambda f: f.data.update(owner=[])]
        for mutate in changes:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as d:
                f = Bundle(d); mutate(f); f.reseal(); result = f.verify()
                self.assertFalse(result["solver_context_known"], result)
                self.assertTrue(result["solver_state_uncertain"])

    def test_marker_case_receipt_command_native_uid_and_owner_code_binding_rejected(self):
        changes = [lambda f: f.data["cli_intent"].update(case_id="foreign"),
                   lambda f: f.data["cli_completion"].update(intent_record={"bytes": 1, "sha256": "0" * 64}),
                   lambda f: f.data["receipt"].update(reserved_attempt=True),
                   lambda f: f.data["owner"]["Native_processes"][0].update(uid=0),
                   lambda f: f.data["owner"]["Native_processes"][0]["cmdline"].append("-other"),
                   lambda f: f.data["owner"]["Native_processes"][0]["selected_environment"].update({e.FEATURE: "1"}),
                   lambda f: f.data["contract"]["source_files"]["src/pyaedt_motor/batch_ownership.py"].update(sha256="0"*64)]
        for i, mutate in enumerate(changes):
            with self.subTest(i=i), tempfile.TemporaryDirectory() as d:
                f = Bundle(d); mutate(f)
                if i != 1: f.reseal()
                result = f.verify(); self.assertFalse(result["solver_context_known"], result)

    def test_source_mutation_blocks_provenance_without_falsely_creating_running_native(self):
        with tempfile.TemporaryDirectory() as d:
            f = Bundle(d); f.data["source_inputs"]["source/run_ipmsm_batch.py"]["sha256"] = "0" * 64
            result = f.verify()
            self.assertTrue(result["solver_context_known"])
            self.assertTrue(any(v["phase"] == "admission_evidence" for v in result["errors"]))
            self.assertFalse(result["training_eligible"])

    def test_first_authority_rehash_index_pin_and_path_escape(self):
        with tempfile.TemporaryDirectory() as d:
            f = Bundle(d); index, sha = f.write()
            files = e.EvidenceFiles(d, index, sha); files.json("case")
            (Path(d)/files.files["case"]["path"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "changed"): files.assert_unchanged()
            with self.assertRaisesRegex(ValueError, "changed"): e.EvidenceFiles(d, index, "0" * 64)
        with tempfile.TemporaryDirectory() as d:
            f = Bundle(d); index, _ = f.write(); value=json.loads(index.read_bytes())
            value["files"]["case"]["path"]="../escape";index.write_bytes(json.dumps(value).encode())
            with self.assertRaisesRegex(ValueError, "unsafe"): e.EvidenceFiles(d,index,hashlib.sha256(index.read_bytes()).hexdigest())

    def test_duplicate_json_and_conflicting_canonical_authority_rejected(self):
        with self.assertRaisesRegex(ValueError,"duplicate"): e._strict(b'{"case":1,"case":2}')
        with self.assertRaisesRegex(ValueError,"nonfinite"): e._strict(b'{"number":1e999}')
        with tempfile.TemporaryDirectory() as d:
            f=Bundle(d);index,sha=f.write();files=e.EvidenceFiles(d,index,sha);files.raw("case")
            with self.assertRaisesRegex(ValueError,"conflicting"):
                files._read(files.path("case"),{"bytes":1,"sha256":"0"*64})


if __name__ == "__main__": unittest.main()
