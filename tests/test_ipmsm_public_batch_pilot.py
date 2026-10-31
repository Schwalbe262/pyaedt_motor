"""Portable inert public builder/owned CLI separation; no Native constructors."""
from dataclasses import asdict, replace
import ast,copy,json,os,sys,tempfile,types,unittest
import math
from pathlib import Path
from unittest.mock import patch
SOURCE=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(SOURCE),str(SOURCE/'src')]
import run_ipmsm_batch as public
from module.ipmsm_ppt_setup import IPMSMPPTSpec,PPT_REPORT_DEFS
from pyaedt_motor import public_batch_pilot as pilot

class Fixture:
    def __init__(self, root, mode="solve"):
        self.root = root
        self.events = []
        self.case = {"case_id": "synthetic_public_fresh", "aedt_version": "2025.2"}
        self.spec = IPMSMPPTSpec(solver_output_error=False)
        self.case_file = root / "case.json"
        self.case_file.write_bytes(pilot.canonical_json(self.case))
        self.install = root / "installation.json"
        self.install.write_bytes(b"{}")
        self.library = root / "library"
        self.library.mkdir()
        for i in range(23):
            (self.library / str(i)).write_bytes(str(i).encode())
        self.syslib = root / "system.amat"
        self.syslib.write_bytes(b"synthetic test material; not manufacturer data")
        names = [*pilot.REQUIRED_SOURCE, "src/pyaedt_motor/io.py"]
        files = {name: pilot.record(SOURCE / name) for name in names}
        producer = public.stable_fingerprint("producer_v3", {n: files[n]["sha256"] for n in pilot.PRODUCER_FILES})
        self.receipt = root / "root_receipt.json"
        self.receipt.write_bytes(pilot.canonical_json({"case_id": self.case["case_id"], "reserved_attempt": 1,
                                                      "producer_fingerprint": producer}))
        seal = lambda p: {"path": str(p), **pilot.record(p)}
        self.contract = {"schema": pilot.SCHEMA, "mode": mode, "case_id": self.case["case_id"],
            "source_root": str(SOURCE), "source_files": files, "library_root": str(self.library),
            "library_files": {p.name: pilot.record(p) for p in self.library.iterdir()},
            "system_material_files": {str(self.syslib): pilot.record(self.syslib)},
            "case": seal(self.case_file), "installation_binding": seal(self.install), "node": "n113",
            "expected_producer": producer, "reservation_receipt": seal(self.receipt),
            "options": asdict(public.RunnerOptions("unused", "unused", False, True, False, 1, False, 4))}
        if mode == "solve":
            self.receipt.write_bytes(pilot.canonical_json({"case_id": self.case["case_id"], "reserved_attempt": 1,
                "budget_attempt": 1, "category": "diagnostic", "stage": "diagnostic", "execution_authorized": True,
                "producer_fingerprint": producer, "case_sha256": self.contract["case"]["sha256"],
                "execution_contract_sha256": pilot.execution_contract_sha256(self.contract)}))
            self.contract["reservation_receipt"] = seal(self.receipt)
        else:
            self.contract["reservation_receipt"] = None
        self.output = root / "output"
        self.saved = {"science": {k: {"synthetic": k} for k in pilot.SCIENCE_KEYS},
                      "materials": {"M19": {"BH": [0, 1]}}, "raw_variable_rows": ["VariableProp('TimeStep',0,...)"],
                      "report_definitions": {k: {"TraceDef": k} for k in PPT_REPORT_DEFS if k != "PPT_EnergyError"},
                      "solutions": {}}
        self.saved['actual_reports']={name:{'Report2D':{'name':name}} for name in self.saved['report_definitions']}
        self.saved['full_report_setup']={'ReportManager':{'Reports':copy.deepcopy(self.saved['report_definitions'])},
                                         'Reports':copy.deepcopy(self.saved['actual_reports'])}
        self.after = copy.deepcopy(self.saved)
        self.cleanup_change = {}
        self.builder_intent = False
        self.launch_result = {"returncode": 0, "solver_uncertain": False, "launcher_calls": 1,
                              "Native_process_count": 1, "single_native_invocation_verified": True,
                              "all_observed_owned_processes_gone": True,
                              "Native_processes": [{"pid": 922, "create_time": 2.0,
                                  "selected_environment": {pilot.FEATURE: "0"}}]}
        self.runner = types.SimpleNamespace(__file__=str(SOURCE / "run_ipmsm_batch.py"),
            run_one_case=self.build, build_spec=lambda *a, **kw: self.spec)
        self.owner = types.SimpleNamespace(validate_installation=lambda *a: {"wrapper_path": "/vendor/ansysedt", "known_file_records": {}},
            prepare_environment=lambda *a: ({}, {}), owned_batch=self.launch,
            known_normal_exit=lambda r: r["returncode"] == 0 and r["solver_uncertain"] is False)

    def build(self, payload):
        case, options = payload
        self.events.append("public_builder")
        public.validate_batch_report_preparation(public.RunnerOptions(**options))
        assert options["analyze"] is False and options["prepare_batch_reports"] is True
        assert options["cleanup_linux"] is False and options["aedt_backend"] == "standalone"
        scene = Path(options["simulation_dir"]) / "fresh_cad"
        scene.mkdir(parents=True)
        (scene / "fresh_cad.aedt").write_bytes(b"new public CAD only")
        audit = scene / "setup_audit.json"
        depth = {"passed": True, "model_units_live": "mm", "native_depth_SI_m": float(f"{self.spec.stack_length_mm:g}")*1e-3}
        audit.write_bytes(pilot.canonical_json({"requested_spec": asdict(self.spec), "setup_issues": [], "validation": 1,
            "native_model_depth_audit": depth, "native_model_depth_audit_post_validation": depth,
            "winding_geometry_audit": {"passed": True, "count": 24}}))
        cleanup = {"verified": True, "owned_pid": 911, "process_exited": True, "errors": [],
                   "client_retirement": {"client_retired": True}, "SDK_release_desktop_called": False,
                   **self.cleanup_change}
        if self.builder_intent:
            (scene / "standalone_solver_state.json").write_bytes(b"{}")
        self.events.append("public_builder_exit")
        return {"status": "ok", "analyze": False, "standalone_solver_uncertain": False,
            "standalone_solver_state_contract": "ipmsm_standalone_solver_state_v1",
            "standalone_owner_identity": json.dumps([911, 1.0, "posix", [1000,1000], "ansysedt"]),
            "standalone_owned_pid": 911, "standalone_cleanup": json.dumps(cleanup), "project_path": str(scene),
            "simulation_name": "fresh_cad", "artifact_setup_audit": str(audit), "input_aedt_version": "2025.2",
            "input_producer_fingerprint": self.contract["expected_producer"]}

    def read(self, path):
        self.events.append("read_saved")
        return copy.deepcopy(self.after if "solver" in self.events else self.saved)

    def launch(self, command, output, **kwargs):
        assert self.events.index("public_builder_exit") < len(self.events)
        assert command.count("-BatchSolve") == 1
        assert "-ValidateOnly" not in command
        assert command[-1] == str(self.output / "scene/fresh_cad/fresh_cad.aedt")
        self.events.append("solver")
        return copy.deepcopy(self.launch_result)

    def process(self, *args):
        self.events.append("pure_postprocess")
        return {"physical_passed": True, "normal_completion": True, "summary": {"sentinel": 12.0}}

    def run(self):
        env = {pilot.FEATURE: "0", "PYAEDT_LIBRARY_ROOT": str(self.library), "SLURMD_NODENAME": "n113"}
        with patch.object(pilot, "validate_execution", side_effect=lambda c: self.validate(c, env)):
            return pilot.execute(self.contract, self.output, runner=self.runner, owner=self.owner,
                                 reader=self.read, processor=self.process)

    def validate(self, contract, env):
        return VALIDATE(contract, platform_name="posix", environ=env, hostname="n113")

VALIDATE=pilot.validate_execution

class PublicBatchPilotTests(unittest.TestCase):
    def test_actual_empty_summary_builder_NaNs_saved_as_null_with_paths_live_values_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            fixture=Fixture(Path(d),'setup_only')
            empty=public.summarize_transient_outputs({},fixture.spec)
            missing=[key for key,value in empty.items() if isinstance(value,float) and math.isnan(value)]
            self.assertEqual(len(missing),22)
            original=fixture.build
            live=[]
            def build_with_actual_empty_summary(payload):
                row=original(payload)
                row.update(empty)
                live.append(row)
                return row
            fixture.runner.run_one_case=build_with_actual_empty_summary
            result=fixture.run()
            self.assertEqual(result['status'],'passed_settings_only',result['errors'])
            self.assertEqual(result['BatchSolve_launch_attempts'],0)
            persisted=json.loads((fixture.output/'builder_result.json').read_bytes())
            final=json.loads((fixture.output/'pilot_result.json').read_bytes())
            self.assertIs(result['builder_result'],live[0])
            for key in missing:
                self.assertTrue(math.isnan(live[0][key]),key)
                self.assertIsNone(persisted[key],key)
                self.assertIn([key],persisted['serialization_unavailable_numeric_paths'])
                self.assertIsNone(final['builder_result'][key],key)
                self.assertIn(['builder_result',key],final['serialization_unavailable_numeric_paths'])
            self.assertFalse((fixture.output/'owned_cli_solver_state.json').exists())

    def test_actual_public_argparse_requires_explicit_preparation_combination(self):
            with patch.object(sys, "argv", ["run_ipmsm_batch.py", "--prepare-batch-reports", "--setup-only", "--keep-projects", "--aedt-backend", "standalone"]):
                args = public.parse_args()
            self.assertTrue(args.prepare_batch_reports)
            self.assertFalse(args.analyze)
            self.assertFalse(args.cleanup_linux)
            tree = ast.parse((SOURCE / "run_ipmsm_batch.py").read_bytes())
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run_one_case")
            config = next(n.value for n in ast.walk(function) if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
                isinstance(t, ast.Name) and t.id == "configure_kwargs" for t in (n.targets if isinstance(n, ast.Assign) else [n.target])))
            expression = next(v for k, v in zip(config.keys, config.values) if isinstance(k, ast.Constant) and k.value == "create_reports")
            for analyze, prepare, expected in ((False, False, False), (False, True, True), (True, False, True)):
                options = types.SimpleNamespace(analyze=analyze, prepare_batch_reports=prepare)
                actual = eval(compile(ast.Expression(expression), "public_runner_exact_kwarg", "eval"), {"options": options})
                self.assertIs(actual, expected)
    def test_setup_only_has_no_intent_no_solver_and_requires_public_quiet(self):
            with tempfile.TemporaryDirectory() as d:
                f = Fixture(Path(d), "setup_only")
                r = f.run()
                self.assertEqual(r["status"], "passed_settings_only")
                self.assertEqual(r["BatchSolve_launch_attempts"], 0)
                self.assertNotIn("solver", f.events)
                self.assertFalse((f.output / "owned_cli_solver_state.json").exists())
                self.assertIsNone(r["F903_scope"]["solver_observed_environment"])
    def test_one_fresh_batch_has_separate_builder_solver_and_no_fake_Analyze(self):
            with tempfile.TemporaryDirectory() as d:
                f = Fixture(Path(d))
                r = f.run()
                self.assertEqual(r["status"], "solved_diagnostic")
                self.assertEqual(f.events.count("public_builder"), 1)
                self.assertEqual(f.events.count("solver"), 1)
                self.assertEqual(f.events.count("pure_postprocess"), 1)
                self.assertFalse(r["standalone_Analyze_return_ABI_claimed"])
                self.assertEqual(r["builder_result"]["standalone_owned_pid"], 911)
                self.assertEqual(r["solver_owner_result"]["Native_processes"][0]["pid"], 922)
                self.assertTrue((f.output / "owned_cli_solver_completion.json").exists())
                self.assertFalse(r["training_allowed"])
    def test_unknown_exit_and_exception_do_not_postprocess_or_retry(self):
            for returncode in (None, 1):
                with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as d:
                    f = Fixture(Path(d))
                    f.launch_result.update(returncode=returncode, solver_uncertain=True)
                    r = f.run()
                    self.assertEqual(r["status"], "failed")
                    self.assertTrue(r["solver_state_uncertain"])
                    self.assertNotIn("pure_postprocess", f.events)
                    self.assertEqual(f.events.count("solver"), 1)
                    self.assertFalse((f.output / "owned_cli_solver_completion.json").exists())
            with tempfile.TemporaryDirectory() as d:
                f = Fixture(Path(d))
                f.owner.owned_batch = unittest.mock.Mock(side_effect=RuntimeError("observer failure"))
                r = f.run()
                self.assertTrue(r["solver_state_uncertain"])
                self.assertNotIn("pure_postprocess", f.events)
                self.assertEqual(f.owner.owned_batch.call_count, 1)
    def test_nonquiet_or_builder_intent_never_launches_second_native(self):
            for key in ("exit", "retirement", "intent"):
                with self.subTest(key=key), tempfile.TemporaryDirectory() as d:
                    f = Fixture(Path(d))
                    if key == "exit": f.cleanup_change["process_exited"] = False
                    if key == "retirement": f.cleanup_change["client_retirement"] = {"client_retired": False}
                    if key == "intent": f.builder_intent = True
                    r = f.run()
                    self.assertEqual(r["status"], "failed")
                    self.assertEqual(r["BatchSolve_launch_attempts"], 0)
                    self.assertNotIn("solver", f.events)
    def test_migration_is_recorded_and_rejected_without_discarding_raw_metrics(self):
            with tempfile.TemporaryDirectory() as d:
                f = Fixture(Path(d))
                f.after["science"]["ModelSetup"] = {"SurfaceMaterialValue": ""}
                f.after["report_definitions"]["PPT_Torque"]["NextVersID"] = 2
                r = f.run()
                self.assertEqual(r["status"], "solved_saved_closure_unproven")
                self.assertFalse(r["solver_state_uncertain"])
                self.assertEqual(r["postprocess"]["summary"]["sentinel"], 12.0)
                self.assertFalse(r["saved_serialization_comparison"]["normalization_applied"])
                self.assertFalse(r["saved_serialization_comparison"]["physical_accuracy_criterion"])
    def test_receipt_and_explicit_version_rejected_before_builder(self):
            for key in ("receipt", "version", "old_calibration_id"):
                with self.subTest(key=key), tempfile.TemporaryDirectory() as d:
                    f = Fixture(Path(d))
                    if key == "receipt": f.receipt.write_bytes(b"{}")
                    else:
                        f.case["aedt_version" if key == "version" else "beta_calibration_id"] = "2026.1" if key == "version" else "old-producer-origin-ID"
                        f.case_file.write_bytes(pilot.canonical_json(f.case))
                        f.contract["case"].update(pilot.record(f.case_file))
                    with self.assertRaises(ValueError): f.run()
                    self.assertEqual(f.events, [])

    def test_default_flag_is_false_and_optin_cannot_authorize_analyze(self):
        options=public.RunnerOptions('x','y',False,True,False,1,False,4)
        self.assertFalse(options.prepare_batch_reports)
        public.validate_batch_report_preparation(replace(options,prepare_batch_reports=True))
        for changes in ({'analyze':True},{'aedt_backend':'pooled'},{'cleanup_linux':True}):
            with self.assertRaises(ValueError):
                public.validate_batch_report_preparation(replace(options,prepare_batch_reports=True,**changes))

    def test_default_dry_validates_pins_without_builder_or_solver_import(self):
        with tempfile.TemporaryDirectory() as d:
            fixture=Fixture(Path(d),'setup_only')
            path=Path(d)/'contract.json'
            path.write_bytes(pilot.canonical_json(fixture.contract))
            with patch.object(pilot.importlib,'import_module',side_effect=AssertionError('no execution imports in dry mode')):
                code=pilot.main(['--contract',str(path),'--contract-sha256',pilot.record(path)['sha256'],
                                 '--output-root',str(fixture.output)])
            self.assertEqual(code,0)
            self.assertEqual(fixture.events,[])
            self.assertFalse(fixture.output.exists())

    def test_malformed_builder_identity_and_cleanup_cannot_launch_cli(self):
        for kind in ('cleanup_fraction','token_PID','contract'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as d:
                fixture=Fixture(Path(d))
                original=fixture.build
                def invalid(payload):
                    row=original(payload)
                    if kind=='cleanup_fraction':
                        cleanup=json.loads(row['standalone_cleanup'])
                        cleanup['owned_pid']=911.00000001
                        row['standalone_cleanup']=json.dumps(cleanup)
                    elif kind=='token_PID':
                        row['standalone_owner_identity']=json.dumps([912,1.0,'posix',[1000,1000],'ansysedt'])
                    else:
                        row['standalone_solver_state_contract']=''
                    return row
                fixture.runner.run_one_case=invalid
                result=fixture.run()
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['BatchSolve_launch_attempts'],0)
                self.assertNotIn('solver',fixture.events)

    def test_root_receipt_authority_and_contract_binding_fail_before_builder(self):
        mutations=({'execution_authorized':False},{'reserved_attempt':1.5},{'budget_attempt':2},
                   {'budget_attempt':True},{'case_sha256':'0'*64},
                   {'execution_contract_sha256':'0'*64},{'category':'training'},{'stage':'design'})
        for change in mutations:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as d:
                fixture=Fixture(Path(d))
                receipt=json.loads(fixture.receipt.read_bytes())
                receipt.update(change)
                fixture.receipt.write_bytes(pilot.canonical_json(receipt))
                fixture.contract['reservation_receipt'].update(pilot.record(fixture.receipt))
                with self.assertRaises(ValueError): fixture.run()
                self.assertEqual(fixture.events,[])
        with tempfile.TemporaryDirectory() as d:
            fixture=Fixture(Path(d))
            fixture.contract['options']['electrical_zero_deg']=2.0
            with self.assertRaises(ValueError): fixture.run()
            self.assertEqual(fixture.events,[])

    def test_OS_hostname_and_optional_Slurm_node_must_both_match(self):
        with tempfile.TemporaryDirectory() as d:
            fixture=Fixture(Path(d),'setup_only')
            env={pilot.FEATURE:'0','PYAEDT_LIBRARY_ROOT':str(fixture.library),'SLURMD_NODENAME':'n113'}
            with self.assertRaises(ValueError):
                VALIDATE(fixture.contract,platform_name='posix',environ=env,hostname='n111.cluster')
            env['SLURMD_NODENAME']='n111'
            with self.assertRaises(ValueError):
                VALIDATE(fixture.contract,platform_name='posix',environ=env,hostname='n113.cluster')
            env.pop('SLURMD_NODENAME')
            self.assertEqual(VALIDATE(fixture.contract,platform_name='posix',environ=env,hostname='n113.cluster'),'n113')

