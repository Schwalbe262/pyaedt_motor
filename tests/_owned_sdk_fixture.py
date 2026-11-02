"""Inert SDK graph, using raw numeric/saved fixtures, never actual authority.

Process/Slurm/task/Root/budget metadata is generated synthetic test evidence.
The SDK identity is read from the existing pinned external file: no SDK copy,
new SDK file vector, installation, Native, API or real ledger is created.
"""
from dataclasses import asdict
import copy
import csv
import gzip
import io
import json
import re
import shlex
from pathlib import Path

from pyaedt_motor import owned_sdk_production as sdk, sdk_launch as launch, execution_evidence as evidence
from tests._owned_cli_fixture import asset, doc, synthetic_profile
from tests.test_ipmsm_sdk_launch import quiet_row, IDENTITY
from module import ipmsm_ppt_setup as ppt
import run_ipmsm_batch as runner

SOURCE = Path(__file__).resolve().parents[1]


def csv_raw(row):
    stream = io.StringIO(newline=""); writer = csv.DictWriter(stream, list(row)); writer.writeheader(); writer.writerow(row)
    return stream.getvalue().encode()


class SDKCase:
    def __init__(self, root, identity_path, *, mode="node_smoke", node="n117", label="INERT_SDK", overrides=None, elements=20000, production=None):
        self.root = Path(root); self.root.mkdir(parents=True)
        self.data, self.original, self.source_roles = {}, {}, {}
        self.case_id, self.mode, self.node = label, mode, node
        self.base = "/inert/owned_sdk/" + label
        purpose = production['purpose'] if production is not None else 'diagnostic'
        self.additional_cases = production['case_evidence']['cases'] if production else {}
        sources = {}
        for name in dict.fromkeys(launch.PHYSICAL + launch.ENTRIES + sdk.EXTRA_SOURCE):
            raw = (SOURCE / name).read_bytes(); role = "source/" + name
            self.add(role, raw, "/inert/source/" + name); sources[name] = self.rec(role); self.source_roles[role] = name
        self.producer = "producer_v3:sha256:" + sdk.sha(sdk.jb({n: sources[n]["sha256"] for n in launch.PHYSICAL}))
        case = doc("m2/case.json"); case["case_id"] = label
        case.update(overrides or {})
        self.case, self.spec = case, runner.build_spec(case)
        self.add("case", csv_raw(case), self.base + "/case.csv")
        identity_raw = Path(identity_path).read_bytes()
        assert sdk.sha(identity_raw) == launch.IDENTITY_SHA
        identity = json.loads(identity_raw)
        self.add("sdk_identity", identity_raw, "/inert/sdk_identity.json")
        import_raw = Path(identity_path).with_name('import_proof.json').read_bytes()
        assert launch.raw_record(import_raw) == identity['import_proof']
        defaults = json.loads(import_raw)['default_aedt_environment_variables']
        self.add("sdk_import_proof", import_raw, "/inert/sdk_import.json")
        # Inert install/library files are explicitly not real installation claims.
        installed, libraries = {}, {}
        for i in range(11):
            role = "install/" + str(i); self.add(role, b"INERT_INSTALL_TEST", "/inert/install/" + str(i)); installed[self.original[role]] = self.rec(role)
        for i in range(23):
            role = "library/" + str(i); self.add(role, b"INERT_LIBRARY_TEST", "/inert/library/" + str(i)); libraries[str(i)] = self.rec(role)
        from pyaedt_motor import _sdk_launch_resource as resource, _sdk_launch_binding as binding
        env = {'SLURM_JOB_ID':'123','SLURM_STEP_ID':'0','SLURM_CPUS_PER_TASK':'4','SLURM_SCHEDULER_TASK_CPUS':'4'}
        job = f'JobId=123 UserId=dw16(1641) JobState=RUNNING NodeList={node} NumNodes=1 NumCPUs=64'
        step = f'StepId=123.0 UserId=1641 State=RUNNING NodeList={node} Nodes=1 CPUs=4 Tasks=1'
        gate = {'Slurm_whitelist': resource.snapshot(env), 'raw_job': job, 'raw_step': step,
                'selected': resource.evaluate(job.encode(),step.encode(),env,uid=1641,node=node,
                                             policy=dict(node=node,UID=1641,account='dw16',minimum_job_CPUs=64))}
        fs = {"schema": "owned_sdk_node_filesystem_observation_v1", "node": node, "UID": 1641, "account": "dw16",
              "source_files": sources, "sdk_identity_sha256": launch.IDENTITY_SHA,
              "private_root": "/inert/private_sdk", "module_origins": {"ansys.aedt.core": "/inert/private_sdk/ansys/aedt/core/__init__.py"},
              "imports_fresh": True, "installation_files": installed,
              "library_files": {"/inert/library/" + n: r for n, r in libraries.items()}, "Native_calls": 0, "FEAs": 0, "training_admission": False,
              'resource_gate': gate}
        self.add("raw_fs", fs, self.base + "/raw_fs.json")
        self.add("filesystem", dict(fs, raw_observation=self.ref("raw_fs")), self.base + "/filesystem.json")
        policy = {"schema": sdk.NODE_SCHEMA, "node": node, "UID": 1641, "account": "dw16", "minimum_job_CPUs": 64,
                  "source_files": sources, "filesystem": self.ref("filesystem")}
        if production:
            for key in ('native0', 'smoke'):
                self.add('prior_' + key, production[key], self.base + '/prior_' + key + '.json')
                policy[key] = self.ref('prior_' + key)
        self.add("node_policy", policy, self.base + "/node_policy.json")
        semantic = doc("m2/Root_policy.json"); semantic["renderer_producer"] = self.producer
        self.add("semantic_policy", semantic, self.base + "/semantic_policy.json")
        runtime = dict(node=node, cores=4, sdk_execution_identity='private_pyaedt022_launch_only_backport_v1',
                       sdk_identity=self.ref('sdk_identity'), sdk_import_proof=self.ref('sdk_import_proof'),
                       source_commit='a'*40, source_files=sources, install_root='/inert/install',
                       installation_files=installed, library_root='/inert/library', library_files=libraries)
        contract = {"schema": sdk.SCHEMA, "backend": sdk.BACKEND, "mode": mode, "purpose": purpose,
                    "case_id": label, "case": self.ref("case"), "node": node, "node_policy": self.ref("node_policy"),
                    "source_files": sources, "expected_producer": self.producer, "root_approval": None,
                    "reservation_receipt": None, "semantic_policy": self.ref("semantic_policy"), "reference_frame": None, "selected_profile": None,
                    'launch_runtime': runtime}
        if production:
            for key in ('reference_frame', 'selected_profile', 'comparison_report'):
                ref = production[key]; self.add(key, Path(ref['path']).read_bytes(), ref['path'])
                assert self.rec(key) == {k: ref[k] for k in ('bytes', 'sha256')}
                if key != 'comparison_report': contract[key] = self.ref(key)
        approval = dict.fromkeys(sdk.GATES, True)
        approval.update(schema="Root_owned_sdk_case_authority_v1", case_intent_sha256=sdk.authority_sha256(contract),
                        case_id=label, mode=mode, purpose=purpose, node=node, scope="INERT_SYNTHETIC_TEST_NO_AUTHORITY")
        self.add("root_approval", approval, self.base + "/Root.json"); contract["root_approval"] = self.ref("root_approval")
        if mode != "node_native0":
            ledger = {"case_id": label, "stage": purpose, "budget_attempt": 1, "campaign_id": "INERT", "reservation_token": "INERT",
                      "request_sha256": sdk.sha(b"INERT_INTENT"), "kind": "nominal", "reserved_at": "INERT"}
            self.add("ledger", sdk.jb(ledger) + b"\n", self.base + "/ledger.jsonl")
            receipt = {"schema": "owned_sdk_stage_receipt_v1", "backend": sdk.BACKEND, "case_id": label, "stage": purpose,
                       "producer_fingerprint": self.producer, "case_sha256": self.rec("case")["sha256"], "case_intent_sha256": sdk.contract_sha256(contract),
                       "request_identity_kind": "owned_sdk_case_intent_v1", "execution_authorized": True,
                       "reserved_attempt": 1, "budget_attempt": 1, "campaign_id": "INERT", "reservation_token": "INERT",
                       "request_sha256": ledger["request_sha256"], "ledger_record_sha256": sdk.sha(sdk.jb(ledger)), "reservation_ledger_snapshot": self.ref("ledger")}
            self.add("receipt", receipt, self.base + "/receipt.json"); contract["reservation_receipt"] = self.ref("receipt")
        self.contract = contract; self.add("contract", contract, self.base + "/contract.json")
        profile = {**runtime, "schema": sdk.PROFILE_SCHEMA, "execution_enabled": True, "node": node, "cores": 4, "purpose": purpose,
                   "sdk_execution_identity": "private_pyaedt022_launch_only_backport_v1", "source_commit": "a" * 40,
                   "source_files": sources, "source_root": "/inert/source", "library_root": "/inert/library", "library_files": libraries,
                   "installation_files": installed, "sdk_identity": self.ref("sdk_identity"), "sdk_import_proof": self.ref("sdk_import_proof"),
                   "owned_sdk_contract": self.ref("contract")}
        self.add("launch_profile", profile, self.base + "/launch_profile.json")
        env = {"SLURM_JOB_ID": "123", "SLURM_STEP_ID": "0", "SLURM_CPUS_PER_TASK": "4", "SLURM_SCHEDULER_TASK_CPUS": "4"}
        from pyaedt_motor import _sdk_launch_resource as resource, _sdk_launch_binding as binding
        env = resource.snapshot(env)
        job = f"JobId=123 UserId=dw16(1641) JobState=RUNNING NodeList={node} NumNodes=1 NumCPUs=64"
        step = f"StepId=123.0 UserId=1641 State=RUNNING NodeList={node} Nodes=1 CPUs=4 Tasks=1"
        gate = {"Slurm_whitelist": env, "raw_job": job, "raw_step": step,
                "selected": resource.evaluate(job.encode(), step.encode(), {k: v for k, v in env.items() if v is not None}, uid=1641, node=node, policy=policy)}
        self.add("launch_before", {"profile": self.ref("launch_profile"), "resource_gate": gate}, self.base + "/before.json")
        self.add("owned", {"owned_identity": IDENTITY, "argv_tail4": binding.production_arguments(node)[0], "SDK12_matches_defaults": True,
                           "owned_SDK12_environment": defaults, "Ansys_resource_readback": {"owned_Native_observed": "4"}}, self.base + "/owned.json")
        self.add("launch_after", {"environment": {"Slurm_whitelist_unchanged": True, "Ansys_resource_input_restored": True}}, self.base + "/after.json")
        inputs = {"source/" + n: r for n, r in sources.items()} | {"library/" + n: r for n, r in libraries.items()}
        inputs.update({"install/" + n: r for n, r in installed.items()})
        inputs['private_sdk'] = dict(identity_sha256=launch.IDENTITY_SHA,manifest_sha256=sdk.sha(sdk.jb(identity['private_manifest'])),full_files_verified=1189)
        self.add("inputs_before", inputs, self.base + "/production_inputs_before.json"); self.add("inputs_after", inputs, self.base + "/production_inputs_after.json")
        analyze = mode != "node_native0"
        def saved_asset(name):
            raw = asset('m2/' + name)
            if not overrides: return raw
            from module import variable
            class Design(dict):
                @property
                def variables(self): return self
            design = Design(); variable._set_fixed_geometry(type('Sim', (), {})(), design, case); ppt.apply_ppt_design_variables(design, self.spec)
            text = raw.decode('utf-8-sig')
            for key, value in design.items():
                text, count = re.subn(r"(VariableProp\('" + re.escape(key) + r"', '[^']*', '[^']*', ')[^']*(')", lambda m: m[1] + value + m[2], text)
                assert count == 1, key
            text, count = re.subn(r"('Conductor number'=')12(')", lambda m: m[1] + str(self.spec.turns_per_coil_side) + m[2], text)
            assert count == 24, count
            for group, value in self.spec.mesh_elements.items():
                text, count = re.subn(r"(\$begin 'Mesh_"+group+r"'.*?NumMaxElem=')[^']+(')", lambda m: m[1]+str(value)+m[2], text, flags=re.S)
                assert count == 1
            return text.encode('utf-8')
        self.add("project", saved_asset("after.aedt" if analyze else "before.aedt"), self.base + "/scene/simulation1/simulation1.aedt")
        audit = doc('m2/setup_audit.json'); audit['requested_spec'] = asdict(self.spec)
        if analyze: audit['analysis'] = True
        self.add("setup_audit", audit, self.base + "/scene/setup_audit.json")
        row = doc("m2/builder.json"); row.update(quiet_row(self.root)); row.update(evidence._production_expected_inputs(runner, case, self.spec))
        row.update({key: case.get(key, '') for key in runner.CASE_METADATA_COLUMNS})
        row.update(case_id=label, input_producer_fingerprint=self.producer, status="ok", analyze=analyze,
                   standalone_analyze_return_received=analyze, standalone_analyze_return_type="builtins.bool" if analyze else "",
                   standalone_analyze_return_repr="True" if analyze else "", analysis_returned_false=False,
                   project_path=self.base + "/scene/simulation1", simulation_name="simulation1", input_execution_backend=sdk.BACKEND,
                   input_execution_purpose=purpose, input_sdk_production_mode=mode, input_sdk_case_intent_sha256=sdk.contract_sha256(contract))
        row.update(artifact_setup_audit=self.original['setup_audit'], artifact_sdk_launch_provenance=self.original['launch_after'])
        self.row = row
        if analyze:
            self.add('solver_state', dict(schema=runner.STANDALONE_SOLVER_STATE_CONTRACT, case_id=label, stage='analyze_returned_true',
                     owned_pid=IDENTITY[0], owner_identity=IDENTITY, solver_state_uncertain=False, producer_fingerprint=self.producer,
                     Analyze_return_received=True, Analyze_return_type='builtins.bool', Analyze_return_repr='True',
                     automatic_retry_allowed=False, NativeQuit_allowed=True), self.base+'/standalone_solver_state.json')
            row['artifact_standalone_solver_state'] = self.original['solver_state']
            self.add("initial_project", saved_asset("before.aedt"), self.base + "/initial_before_analyze.aedt")
            marker = {"case_id": label, "case_intent_sha256": sdk.contract_sha256(contract), "owned_identity": IDENTITY,
                      "full_spec": asdict(self.spec), "initial_save_return_type": "builtins.bool", "initial_save_return_repr": "True",
                      "Analyze_calls_before_snapshot": 0, "automatic_retry": False,
                      "before_image": self.ref("initial_project"), "source_project": {"path": self.original["project"], **self.rec("initial_project")}}
            self.add("before_analyze", marker, self.base + "/before_analyze.json")
            row['artifact_sdk_before_analyze'] = self.original['before_analyze']
            self.add("profile", synthetic_profile(elements, self.spec.steps_per_period), self.base + "/scene/simulation1/simulation1.aedtresults/IPMSM.results/native.profile")
            self.add("acf", b"NumCores=4\nUseAutoSettings=False\n", self.base + "/scene/simulation1/simulation1.pyaedt/IPMSM/pyaedt_config.acf")
            self.add('profile4_audit', {'profile_record': self.ref('profile'), 'acf_record': self.ref('acf')}, self.base + '/profile4_audit.json')
            self.data['launch_after']['resource_profile_audit'] = self.ref('profile4_audit')
            local = {}
            for report in ppt.PPT_REPORT_DEFS:
                if report == "PPT_EnergyError": continue
                raw = asset("numeric" + str(self.spec.steps_per_period) + "/" + report + ".csv"); role = "report/" + report
                self.add(role, raw, self.base + "/reports/" + report + ".csv")
                path = self.root / (report + ".csv"); path.write_bytes(raw); local["artifact_report_" + report] = str(path)
                row["artifact_report_" + report] = self.original[role]
            from module.ipmsm_waveform_quality import analyze_exported_waveforms
            summary = runner.summarize_transient_outputs(local, self.spec, operation="sin_current")
            summary.update(analyze_exported_waveforms(local, self.spec, "sin_current")); runner.add_derived_motor_metrics(summary, self.spec)
            pin, power, loss = [runner.finite_float(summary[k]) for k in ("output_electrical_input_last_w", "output_mech_power_last_w", "output_total_loss_last_avg_w")]
            summary.update(output_power_balance_residual_last_w=pin-power-loss, output_power_balance_residual_last_pct=100*(pin-power-loss)/max(abs(pin), 1.))
            row.update(summary)
        else:
            self.add('profile4_audit',dict(status='not_requested_setup_only',actual_4core_proven=False),self.base+'/profile4_audit.json')
        self.add("result", csv_raw(row), self.base + "/result.csv")
        command = ['python','-I','-B','tools/run_owned_sdk_case.py','--cases',self.original['case'],'--workers','1','--cores','4',
                   '--non-graphical','--keep-projects','--aedt-backend','standalone','--max-cases','1',
                   '--simulation-dir',self.base+'/scene','--result-csv',self.original['result'],
                   '--sdk-launch-profile',self.original['launch_profile'],'--sdk-launch-profile-sha256',self.rec('launch_profile')['sha256']]
        if not analyze: command += ['--setup-only','--prepare-batch-reports']
        request = dict(project="INERT", name="INERT", dedupe_key="INERT", remote_cwd="/inert/source", env_profile="pyaedt2026v1",
                       required_capability="conda:pyaedt2026v1", entrypoint="tools/run_owned_sdk_case.py", scheduling_profile="fea_bursty", cpus=4,
                       memory_mb=8192, account_name="dw16", node_name=node, command=shlex.join(command), env_setup="INERT", max_workers_per_node=12)
        self.data['launch_before']['worker_entry'] = dict(schema='owned_sdk_original_worker_entry_v1',
            argv=['/inert/source/tools/run_owned_sdk_case.py',*command[4:]],cwd='/inert/source',
            python_executable='/inert/conda/bin/python',isolated=True,dont_write_bytecode=True)
        terminal = dict(request, id=700001, status="completed", exit_code=0, actual_node_name=node)
        self.add("final_request", request, self.base + "/final_request.json"); self.add("terminal_task", terminal, self.base + "/task.json")
        self.add("task_binding", {"task_id": 700001, "case_id": label, "case_intent_sha256": sdk.contract_sha256(contract),
                                  "final_payload_sha256": sdk.sha(sdk.jb(request))}, self.base + "/task_binding.json")

    def add(self, role, value, original):
        self.data[role] = copy.deepcopy(value); self.original[role] = original
    def raw(self, role):
        value = self.data[role]
        return value if type(value) is bytes else sdk.jb(value)
    def rec(self, role): return launch.raw_record(self.raw(role))
    def ref(self, role): return {"path": self.original[role], **self.rec(role)}
    def write(self):
        records = {}
        for i, role in enumerate(self.data):
            path = self.root / (str(i) + ".bin"); path.write_bytes(self.raw(role))
            records[role] = {"path": path.name, "original_path": self.original[role], **self.rec(role)}
            if role in self.source_roles: records[role]["source_relative"] = self.source_roles[role]
        index = self.root / "index.json"; index.write_bytes(sdk.jb({"schema": evidence.INDEX_SCHEMA, "files": records}))
        return {"schema": sdk.DECLARATION_SCHEMA, "cases": {**self.additional_cases, self.case_id: {"root": str(self.root), "index_path": str(index), "index_sha256": sdk.sha(index.read_bytes())}}}
    def verify(self):
        return sdk.OwnedSDKProductionEvidence.from_document(self.write()).verify_case(self.case_id)
