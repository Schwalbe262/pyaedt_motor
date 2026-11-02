"""Explicit owned SDK production authority and replay. Import is inert.

This backend retains the actual SDK Analyze=True row. It never calls the CLI
verifier, promotes a diagnostic row, creates a reservation, or starts Native.
All transported files retain their original path in EvidenceFiles.
"""
from __future__ import annotations

from dataclasses import asdict
import csv
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex

from . import sdk_launch as launch

BACKEND = "public_owned_sdk_v1"
SCHEMA = "owned_sdk_single_case_contract_v1"
PROFILE_SCHEMA = "private_sdk022_owned_production_launch_profile_v1"
DECLARATION_SCHEMA = "owned_sdk_production_evidence_v1"
NODE_SCHEMA = "owned_sdk_explicit_node_policy_v1"
GATES = ("source_reviewed", "case_plan_reviewed", "node_evidence_reviewed",
         "same_instance_save_reviewed", "budget_authorized", "execution_authorized")
RUNTIME_KEYS = ('node', 'cores', 'sdk_execution_identity', 'sdk_identity', 'sdk_import_proof',
                'source_commit', 'source_files', 'install_root', 'installation_files', 'library_root', 'library_files')
EXTRA_SOURCE = ("src/pyaedt_motor/owned_sdk_production.py", "src/pyaedt_motor/native_state_semantics.py",
                "src/pyaedt_motor/public_batch_pilot.py", "src/pyaedt_motor/execution_evidence.py",
                "src/pyaedt_motor/batch_ownership.py", "src/pyaedt_motor/io.py",
                "configs/material_m19_29g_reference_y2.json", "src/pyaedt_motor/sdk_case_worker.py",
                "src/pyaedt_motor/sdk_production_cluster.py", "tools/run_owned_sdk_case.py",
                "src/pyaedt_motor/quality.py", "src/pyaedt_motor/doe.py", "src/pyaedt_motor/train_physical.py",
                "src/pyaedt_motor/winding_frames.py", "src/pyaedt_motor/winding_native.py", "src/pyaedt_motor/production_zero.py",
                "research_ns40_zero.py", "src/pyaedt_motor/sdk_node_fs.py", "calibrate_ipmsm_beta.py", "ipmsm_optimization.py")


def need(value, message):
    if not value:
        raise ValueError(message)


def jb(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def document(raw):
    from .cluster import _strict
    return _strict(raw)


def exact(actual, expected, label):
    need(jb(actual) == jb(expected), label + " differs")


def contract_sha256(contract):
    """Receipt-free immutable intent; final payload SHA is a separate identity."""
    return sha(jb({k: v for k, v in contract.items() if k != "reservation_receipt"}))


def _one_case(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    rows = list(reader)
    need(reader.fieldnames and len(set(reader.fieldnames)) == len(reader.fieldnames) and len(rows) == 1
         and None not in rows[0] and all(v is not None for v in rows[0].values()), "one original complete CSV row required")
    return rows[0]


def _reservation(contract, read):
    from . import cluster
    receipt = document(read(contract["reservation_receipt"]))
    attempt = receipt.get("budget_attempt")
    need(type(attempt) is int and attempt > 0 and receipt.get("reserved_attempt") == attempt
         and type(receipt.get("reserved_attempt")) is int, "typed actual reservation required")
    for key, expected in {"schema": "owned_sdk_stage_receipt_v1", "backend": BACKEND,
                          "case_id": contract["case_id"], "stage": contract["purpose"],
                          "producer_fingerprint": contract["expected_producer"],
                          "case_sha256": contract["case"]["sha256"],
                          "case_intent_sha256": contract_sha256(contract),
                          "request_identity_kind": "owned_sdk_case_intent_v1"}.items():
        exact(receipt.get(key), expected, "actual receipt " + key)
    need(receipt.get("execution_authorized") is True, "actual execution reservation authority required")
    raw = read(receipt["reservation_ledger_snapshot"])
    # Parse the exact snapshot with the shared ledger validator, without writes.
    rows, counts, cases = [], dict.fromkeys(cluster.STAGE_LIMITS, 0), set()
    need(not raw or raw.endswith(b"\n"), "torn reservation snapshot")
    for line in raw.splitlines():
        row = document(line)
        need(row.get("stage") in counts and row.get("case_id") not in cases
             and type(row.get("budget_attempt")) is int and row["budget_attempt"] == len(rows) + 1,
             "invalid contiguous reservation ledger snapshot")
        cases.add(row["case_id"]); rows.append(row); counts[row["stage"]] += 1
    need(len(rows) == attempt and attempt <= cluster.TOTAL_LIMIT
         and all(counts[k] <= cluster.STAGE_LIMITS[k] for k in counts), "actual stage/total budget exceeded")
    row = rows[-1]
    for key in ("case_id", "stage", "campaign_id", "reservation_token", "request_sha256"):
        need(bool(receipt.get(key)) and row.get(key) == receipt[key], "actual reservation row differs: " + key)
    need(row["case_id"] == contract["case_id"] and row["stage"] == contract["purpose"]
         and sha(jb(row)) == receipt.get("ledger_record_sha256"), "case/stage/ledger hash differs")
    return receipt


def validate_contract(contract, read=None, *, require_receipt=True):
    read = launch.read_ref if read is None else read
    required = {"schema", "backend", "mode", "purpose", "case_id", "case", "node", "node_policy",
                "source_files", "expected_producer", "root_approval", "reservation_receipt",
                "semantic_policy", "reference_frame", "selected_profile", "launch_runtime"}
    need(type(contract) is dict and set(contract) == required and contract["schema"] == SCHEMA
         and contract["backend"] == BACKEND, "explicit complete SDK contract required")
    mode, purpose = contract["mode"], contract["purpose"]
    need((mode == "production" and purpose in {"training", "design"}) or
         (mode in {"node_native0", "node_smoke"} and purpose == "diagnostic"), "SDK mode/stage mismatch")
    need(re.fullmatch(r"[A-Za-z0-9_-]+", str(contract["node"])) is not None, "one explicit node required")
    case = _one_case(read(contract["case"]))
    need(case.get("case_id") == contract["case_id"], "SDK contract CSV identity differs")
    need(type(contract['case_id']) is str and re.fullmatch('[A-Za-z0-9_.-]+',contract['case_id'])
         and contract['case_id'] not in ('.','..') and len(contract['case_id']) <= 96, 'safe SDK singlecase identity required')
    names = set(launch.PHYSICAL + launch.ENTRIES + EXTRA_SOURCE)
    need(names <= set(contract["source_files"]), "SDK physical/validator source closure missing")
    producer = "producer_v3:sha256:" + sha(jb({n: contract["source_files"][n]["sha256"] for n in launch.PHYSICAL}))
    exact(producer, contract["expected_producer"], "SDK physical producer")
    runtime = contract['launch_runtime']
    need(set(runtime) == set(RUNTIME_KEYS) and runtime['node'] == contract['node'] and type(runtime['cores']) is int and runtime['cores'] == 4
         and runtime['source_files'] == contract['source_files'] and len(runtime['installation_files']) == 11 and len(runtime['library_files']) == 23,
         'complete authority-bound SDK runtime identity required')
    need(re.fullmatch('[0-9a-f]{40}',str(runtime['source_commit'])) is not None, 'full promoted SDK source commit required')
    approval = document(read(contract["root_approval"]))
    need(approval.get("schema") == "Root_owned_sdk_case_authority_v1"
         and all(approval.get(g) is True for g in GATES), "six explicit Root gates required")
    for key, expected in {"case_intent_sha256": contract_sha256(contract), "case_id": contract["case_id"],
                          "mode": mode, "purpose": purpose, "node": contract["node"]}.items():
        # The Root approval is referenced by its record. Its intent excludes its
        # own reference, avoiding a self-hash as well as the reservation cycle.
        if key == "case_intent_sha256":
            expected = authority_sha256(contract)
        exact(approval.get(key), expected, "Root authority " + key)
    if mode == "production":
        need(contract["reference_frame"] is not None and contract["selected_profile"] is not None
             and contract["semantic_policy"] is not None, "production requires actual frame/profile/semantic policy")
    if mode == "node_native0":
        need(contract["reservation_receipt"] is None, "Native0 qualification must not consume a FEA reservation")
    elif require_receipt:
        _reservation(contract, read)
    return case


def authority_sha256(contract):
    return sha(jb({k: v for k, v in contract.items() if k not in {"reservation_receipt", "root_approval"}}))


def _fs_proof(policy, read):
    proof = document(read(policy["filesystem"]))
    need(proof.get("schema") == "owned_sdk_node_filesystem_observation_v1"
         and proof.get("node") == policy["node"] and type(proof.get("UID")) is int and proof["UID"] == 1641
         and proof.get("account") == "dw16" and proof.get("sdk_identity_sha256") == launch.IDENTITY_SHA,
         "actual node/UID/private SDK filesystem proof required")
    need(proof.get("source_files") == policy["source_files"] and proof.get("imports_fresh") is True,
         "node source/import proof differs")
    need(type(proof.get('Native_calls')) is int and proof['Native_calls'] == 0
         and type(proof.get('FEAs')) is int and proof['FEAs'] == 0 and proof.get('training_admission') is False,
         "FS proof must have zero Native/FEA and no training admission")
    need(type(proof.get("module_origins")) is dict and proof["module_origins"]
         and all(type(p) is str and p.startswith(proof["private_root"] + "/") for p in proof["module_origins"].values()),
         "actual private module origins required")
    for key, count in (("installation_files", 11), ("library_files", 23)):
        need(type(proof.get(key)) is dict and len(proof[key]) == count, "complete node " + key + " required")
        for path, record in proof[key].items():
            need(PurePosixPath(path).is_absolute() and set(record) == {'bytes','sha256'} and type(record['bytes']) is int
                 and record['bytes'] > 0 and re.fullmatch('[0-9a-f]{64}', str(record['sha256'])), 'typed observed FS file record required')
            if key == 'library_files': read({"path": path, **record})
            # Installed Native binaries are observed/rehashed by the actual FS
            # job and Context at run boundaries. Never copy large binaries into
            # a raw-result archive merely to replay their observation records.
    # Preserve/replay the raw FS observation, not a hand-written passed flag.
    raw = document(read(proof["raw_observation"]))
    exact(raw, {k: v for k, v in proof.items() if k != "raw_observation"}, "original FS observation")
    from . import _sdk_launch_resource as resource
    gate = proof['resource_gate']
    selected = resource.evaluate(gate['raw_job'].encode(), gate['raw_step'].encode(),
               {k:v for k,v in gate['Slurm_whitelist'].items() if v is not None}, uid=1641, node=policy['node'], policy=policy)
    exact(gate['selected'], selected, 'original FS own Slurm4 allocation')
    return proof


def validate_node_policy(ref, *, node, source_files, mode, read=None, runtime=None, retain=None):
    read = launch.read_ref if read is None else read
    policy = document(read(ref))
    need(policy.get("schema") == NODE_SCHEMA and policy.get("node") == node
         and type(policy.get("minimum_job_CPUs")) is int and policy["minimum_job_CPUs"] >= 4
         and policy.get("UID") == 1641 and type(policy.get("UID")) is int and policy.get("account") == "dw16",
         "explicit typed node policy required")
    exact(policy.get("source_files"), source_files, "node policy source")
    fs = _fs_proof(policy, read)
    if runtime is not None:
        exact(fs['installation_files'], runtime['installation_files'], 'same-runtime node installation')
        exact(fs['library_files'], {str(PurePosixPath(runtime['library_root']) / n): rec for n, rec in runtime['library_files'].items()}, 'same-runtime node library')
    if mode == "production":
        for role, expected_mode in (("native0", "node_native0"), ("smoke", "node_smoke")):
            declaration = document(read(policy[role]))
            evidence = OwnedSDKProductionEvidence.from_document(declaration)
            need(len(evidence.cases) == 1, "single-case node qualification proof required")
            result = evidence.verify_case(next(iter(evidence.cases)))
            need(result["mode"] == expected_mode and result["node"] == node and result["source_files"] == source_files
                 and result["production_eligible"] is False and result["qualification_passed"] is True,
                 "same-source node Native0/FEA smoke proof required")
            if runtime is not None: exact(result['launch_runtime'], runtime, 'same-runtime prior node qualification')
            if retain is not None: retain.append(evidence)
    return {"node": node, "UID": 1641, "account": "dw16", "minimum_job_CPUs": policy["minimum_job_CPUs"]}


class LaunchAuthority:
    def __init__(self, profile):
        self.profile = profile
        self.contract_ref = profile["owned_sdk_contract"]
        self.contract = document(launch.read_ref(self.contract_ref))
        validate_contract(self.contract)
        approval = document(launch.read_ref(self.contract['root_approval']))
        need(approval.get('evidence_mode') == 'linux_original' and 'inert' not in str(approval.get('scope','')).casefold(),
             'actual Native entry rejects inert/test/unspecified Root authority')
        if self.contract['mode'] == 'production':
            frame = document(launch.read_ref(self.contract['reference_frame']))
            need(frame.get('evidence_mode') == 'linux_original' and not str(frame.get('reference_frame_id','')).startswith('inert-'),
                 'actual Native entry rejects inert production origin/frame')
            selected = document(launch.read_ref(self.contract['selected_profile']))
            for key in ('case_evidence','frame_evidence'):
                ref = selected[key]
                raw = launch.read_once(ref['path']); need(sha(raw) == ref['sha256'], 'actual proof declaration SHA differs')
                need(document(raw).get('evidence_mode') == 'linux_original', 'actual Native entry rejects inert proof declarations')
        exact(self.contract["source_files"], profile["source_files"], "profile/contract source")
        exact(self.contract['launch_runtime'], {k: profile[k] for k in RUNTIME_KEYS}, 'Root-authorized launch runtime')
        self.before = None
        self.node_evidence = []
        self.selected_evidence = None

    def activate(self):
        # Called only after Context.activate's fresh SDK guard and complete
        # private SDK verification. Pure saved-file parsing may then import SDK.
        profile = self.profile
        self.policy = validate_node_policy(self.contract["node_policy"], node=profile["node"],
                                          source_files=profile["source_files"], mode=self.contract["mode"], runtime=self.contract['launch_runtime'], retain=self.node_evidence)
        policy = document(launch.read_ref(self.contract['node_policy']))
        fs = document(launch.read_ref(policy['filesystem']))
        need(fs.get('evidence_mode') == 'linux_original', 'actual Native entry requires original node FS observation')
        if self.contract['mode'] == 'production':
            for evidence in self.node_evidence:
                proof = evidence.verify_case(next(iter(evidence.cases)))
                need(proof['actual_observation_eligible'] is True, 'actual Native entry rejects inert node qualification')
        if self.contract["mode"] == "production":
            selected = document(launch.read_ref(self.contract["selected_profile"]))
            declaration = selected["case_evidence"]
            evidence = OwnedSDKProductionEvidence.from_declaration(declaration["path"], declaration["sha256"])
            self.selected_evidence = evidence
            verify_selected_profile(selected, evidence, self.contract["expected_producer"])
            need(document(launch.read_ref(self.contract["reference_frame"])) == selected["reference_frame"], "selected profile/production frame differs")

    def validate_cases(self, args, cases):
        exact(launch.record(args.cases), {k: self.contract["case"][k] for k in ("bytes", "sha256")}, "original case CSV")
        need(len(cases) == 1 and cases[0]["case_id"] == self.contract["case_id"], "one bound SDK case required")
        need(args.analyze is (self.contract["mode"] != "node_native0"), "Native0/FEA mode differs")

    def capture_inputs(self, context, name):
        for evidence in self.node_evidence: evidence.assert_unchanged()
        if self.selected_evidence is not None:self.selected_evidence.assert_unchanged()
        observed = {}
        for prefix, root, records in (("source/", context.root, self.profile["source_files"]),
                                      ("library/", context.library, self.profile["library_files"])):
            for relative, expected in records.items():
                observed[prefix + relative] = launch.record(root / relative)
                exact(observed[prefix + relative], expected, "actual immutable " + prefix + relative)
        # Context has just replayed the complete existing SDK manifest. Retain
        # its immutable identity, not another1189-entry vector per case/phase.
        observed['private_sdk'] = {'identity_sha256': launch.IDENTITY_SHA, 'manifest_sha256': sha(jb(context.manifest)),
                                   'full_files_verified': len(context.manifest)}
        for path, expected in self.profile["installation_files"].items():
            observed["install/" + path] = launch.record(Path(path).resolve(strict=True))
            exact(observed["install/" + path], expected, "actual installed input")
        context.write(name, observed)

    def before_analyze(self, context, app, owner, spec, row, case, project_dir):
        need(self.contract["mode"] != "node_native0" and self.before is None and owner.verified is True
             and owner.solver_uncertain is False and not owner.analysis_return_received, "same owned fresh pre-Analyze only")
        bound_case = validate_contract(self.contract)
        import run_ipmsm_batch as runner
        exact(asdict(spec), asdict(runner.build_spec(bound_case)), 'same-case pre-Analyze requested spec')
        need(case.get('beta_calibration_id','') == bound_case.get('beta_calibration_id',''), 'same-case pre-Analyze calibration ID differs')
        context.write('initial_save_intent.json', {'schema':'owned_sdk_initial_save_intent_v1', 'case_id':self.contract['case_id'],
                      'case_intent_sha256':contract_sha256(self.contract),'owned_identity':owner.identity,
                      'Native_call_entered':False,'automatic_retry':False})
        # All contract/spec/file IO precedes these immediate Native guards.
        context.verify_owned(owner, runner.standalone_process_identity)
        runner.assert_standalone_owned(owner, app.desktop_class)
        runner.assert_standalone_project(owner)
        runner.assert_standalone_owned(owner, app.desktop_class)
        owner.solver_uncertain = True
        row['sdk_initial_save_state'] = 'unknown_native_save_call'
        try:
            returned = app.save_project()
        except BaseException as exc:
            row['sdk_initial_save_reason'] = type(exc).__name__ + ': ' + str(exc)
            raise  # Unknown Native call: original cleanup suppresses Save/export/Quit.
        if returned is not True:
            row['sdk_initial_save_reason'] = type(returned).__module__ + '.' + type(returned).__qualname__ + ':' + repr(returned)
            raise ValueError('initial same-instance Save must return exact True; Native state remains unknown')
        row['sdk_initial_save_state'] = 'True_return_pending_identity_project_guards'
        try:
            runner.assert_standalone_owned(owner, app.desktop_class)
            runner.assert_standalone_project(owner)
            context.verify_owned(owner, runner.standalone_process_identity)
        except BaseException as exc:
            row['sdk_initial_save_reason'] = type(exc).__name__ + ': ' + str(exc)
            raise
        owner.solver_uncertain = False
        row['sdk_initial_save_state'] = 'known_exactTrue_owned'
        # Project filename comes from this one fresh design, never glob/fallback.
        source = Path(project_dir) / (str(app.project_name) + ".aedt")
        raw = launch.read_once(source)
        target = context.control / "initial_before_analyze.aedt"
        with target.open("xb") as stream:
            stream.write(raw); stream.flush(); __import__("os").fsync(stream.fileno())
        need(launch.read_once(source) == raw, "owned saved project changed during exclusive snapshot")
        context.verify_owned(owner, runner.standalone_process_identity)
        self.before = {"schema": "owned_sdk_before_analyze_v1", "case_id": self.contract["case_id"],
                       "case_intent_sha256": contract_sha256(self.contract), "owned_identity": list(owner.identity),
                       "full_spec": asdict(spec), "initial_save_return_type": "builtins.bool", "initial_save_return_repr": "True",
                       "source_project": {"path": str(source), **launch.raw_record(raw)},
                       "before_image": {"path": str(target), **launch.record(target)},
                       "Analyze_calls_before_snapshot": 0, "automatic_retry": False}
        context.write("before_analyze.json", self.before)
        row["artifact_sdk_before_analyze"] = str(context.control / "before_analyze.json")

    def stamp_attempt(self, row):
        row["input_execution_backend"] = BACKEND
        row["input_execution_purpose"] = self.contract["purpose"]
        row["input_sdk_production_mode"] = self.contract["mode"]
        row["input_sdk_case_intent_sha256"] = contract_sha256(self.contract)
        row["artifact_sdk_contract"] = self.contract_ref["path"]

    def after_cleanup(self, context, row):
        self.stamp_attempt(row)
        need(row.get("status") == "ok", "failed SDK row cannot pass production closure")
        identity = json.loads(row["standalone_owner_identity"])
        need(strict_exit(row, identity, analyze=self.contract["mode"] != "node_native0"), "original owned15s quit/gone/retired receipt required")
        if self.contract["mode"] != "node_native0":
            need(self.before is not None, "same-instance initial snapshot missing")
        context.write("production_closure.json", {"schema": "owned_sdk_closed_publication_v1",
                      "case_id": self.contract["case_id"], "contract": self.contract_ref,
                      "before_analyze": self.before, "production_admission": False,
                      "original_row_before_csv": row})


def strict_exit(row, identity, *, analyze):
    """Only the original cleanup receipt. A late OS absence never repairs it."""
    if analyze:
        return launch.owned_exit_proven(row, identity)
    try:
        cleanup = json.loads(row["standalone_cleanup"])
        return (json.loads(row["standalone_owner_identity"]) == identity and len(identity) == 5
                and type(identity[0]) is int and identity[0] > 0 and identity[2:] == ["posix", [1641, 1641], identity[4]]
                and identity[4] in ("ansysedt", "ansysedt.exe") and row["standalone_owned_pid"] == identity[0]
                and row["standalone_solver_uncertain"] is False and row["standalone_analyze_return_received"] is False
                and cleanup["owned_pid"] == identity[0] and cleanup["verified"] is True
                and cleanup["native_quit_requested"] is True and cleanup["process_exited"] is True
                and cleanup["errors"] == [] and not cleanup.get("suppressed")
                and cleanup["client_retirement"]["client_retired"] is True and cleanup["client_retirement"]["errors"] == [])
    except (KeyError, TypeError, ValueError):
        return False


def _typed_row(row):
    """Local typed view only. Original CSV bytes and paths stay authoritative."""
    view = dict(row)
    for key in ("analyze", "standalone_solver_uncertain", "standalone_analyze_return_received", "analysis_returned_false"):
        need(view.get(key) in ("True", "False"), "original strict bool CSV field required: " + key)
        view[key] = view[key] == "True"
    view["standalone_owned_pid"] = int(view["standalone_owned_pid"])
    return view


def _loaded_sources(files, contract):
    import run_ipmsm_batch as runner
    from module import variable, ipmsm_ppt_setup as ppt, ipmsm_waveform_quality as wave
    from . import public_batch_pilot, native_state_semantics, solver_profile
    modules = {"run_ipmsm_batch.py": runner, "module/variable.py": variable, "module/ipmsm_ppt_setup.py": ppt,
               "module/ipmsm_waveform_quality.py": wave, "src/pyaedt_motor/public_batch_pilot.py": public_batch_pilot,
               "src/pyaedt_motor/native_state_semantics.py": native_state_semantics,
               "src/pyaedt_motor/solver_profile.py": solver_profile, "src/pyaedt_motor/sdk_launch.py": launch,
               "src/pyaedt_motor/owned_sdk_production.py": __import__(__name__, fromlist=["_"])}
    for name, module in modules.items():
        need(launch.record(module.__file__) == contract["source_files"][name], "loaded SDK verifier/producer origin differs: " + name)
    for name, rec in contract["source_files"].items():
        # Original-path source copies are indexed independently of loaded origin.
        matches = [key for key in files.files if files.files[key].get("source_relative") == name]
        need(len(matches) == 1 and files.rec(matches[0]) == rec, "transported source closure differs: " + name)
    return runner, ppt, variable


def verify_saved_settings(files, contract, case, row):
    """Rebuild controls from the actual source and parse the unmodified saved AEDT."""
    runner, ppt, variable = _loaded_sources(files, contract)
    from . import native_state_semantics as semantics, public_batch_pilot as pilot
    from ansys.aedt.core.internal import load_aedt_file
    parser_pin = files.json('sdk_identity')['private_manifest']['ansys/aedt/core/internal/load_aedt_file.py']
    exact(launch.record(load_aedt_file.__file__), parser_pin, 'loaded pure file parser origin')
    spec = runner.build_spec(case)
    from .execution_evidence import _verify_production_builder_inputs
    _verify_production_builder_inputs(runner, case, spec, row)
    audit = files.json("setup_audit")
    expected_spec = asdict(spec)
    differences = [key for key in set(audit.get('requested_spec', {})) | set(expected_spec)
                   if jb(audit.get('requested_spec', {}).get(key)) != jb(expected_spec.get(key))]
    need(not differences, 'source fullspec differs: ' + ','.join(sorted(differences)))
    need(audit.get("setup_issues") == [] and (audit.get("validation") is True or
         type(audit.get("validation")) is int and audit["validation"] == 1), "actual setup validation failed")
    state = semantics.read_state(files.path("project")); native = pilot.saved_native(files.path("project"))
    need(state["record"] == files.rec("project"), "saved file changed during parse")
    model = state["project"]["Maxwell2DModel"]
    need(state['project']['Desktop']['Version'] == [2025,2] and model.get('ComputeIncrementalMatrix') is False
         and model.get('UseSkewModel') is False and model.get('EnableTranTranLinkWithSimplorer') is False,
         'saved v252/nonincremental/no-skew/local transient scope differs')
    actual = {}
    for line in state["raw_variable_rows"]:
        match = re.search(r"VariableProp\('([^']+)', '[^']*', '[^']*', '([^']*)'", line)
        need(match is not None and match[1] not in actual, "unparsed/duplicate variable")
        actual[match[1]] = match[2]
    class Design(dict):
        @property
        def variables(self): return self
    design = Design()
    variable._set_fixed_geometry(type("Sim", (), {})(), design, case)
    ppt.apply_ppt_design_variables(design, spec)
    need(actual.keys() == design.keys(), "saved/source variable names differ")
    for name, want in design.items():
        got = actual[name]
        if got == want: continue
        a = re.fullmatch(r"([-+0-9.eE]+)([a-zA-Z]*)", got)
        b = re.fullmatch(r"([-+0-9.eE]+)([a-zA-Z]*)", want)
        need(a and b and a[2] == b[2] and math.isclose(float(a[1]), float(b[1]), rel_tol=1e-12, abs_tol=1e-12),
             "saved source variable differs: " + name)
    setup = model["AnalysisSetup"]["SolveSetups"][spec.setup_name]
    from types import SimpleNamespace
    proposed = SimpleNamespace(props={}, update=lambda: True)
    spy = SimpleNamespace(change_symmetry_multiplier=lambda value: True, create_setup=lambda **kwargs: proposed)
    ppt.create_ppt_transient_setup(spy, spec)
    for key, value in proposed.props.items():
        need(setup.get(key) == value or key == 'NonlinearSolverResidual' and float(setup[key]) == float(value),
             'source-derived saved transient property differs: ' + key)
    for key, want in audit["setup"].items():
        if key in setup:
            need(setup[key] == want or key == "NonlinearSolverResidual" and float(setup[key]) == float(want),
                 "saved setup/audit differs: " + key)
    for key, want in {"SetupType": "Transient", "StopTime": "StopTime", "TimeStep": "TimeStep",
                      "OutputError": spec.solver_output_error}.items(): exact(setup.get(key), want, "saved setup " + key)
    need(model["ModelDepth"] == "StackLength" and model["Multiplier"] == "SymmetryFactor"
         and model["GeometryMode"] == "XY" and model["SolutionType"] == "Transient"
         and model["ComputeTransientInductance"] is spec.compute_inductance, "saved full transient controls differ")
    for key in ("native_model_depth_audit", "native_model_depth_audit_post_validation"):
        depth = audit[key]
        need(depth["passed"] is True and depth["model_units_live"] == "mm" and
             math.isclose(depth["native_depth_SI_m"], float(f"{spec.stack_length_mm:g}") / 1000, rel_tol=1e-9, abs_tol=1e-12),
             "saved depth/live audit differs")
    from . import winding_native
    observed = winding_native.observe_native(files.raw("project"), turns_per_coil_side=spec.turns_per_coil_side)
    need(observed, "source-bound winding/material observation missing")
    need(spec.series_turns_per_phase == 4 * spec.turns_per_coil_side,
         'declared series turns differ from actual24 coils/three phases/one branch')
    pack_role = next(k for k, rec in files.files.items() if rec.get("source_relative") == "configs/material_m19_29g_reference_y2.json")
    pack = files.json(pack_role); material = native["materials"]["M19_29G_reference_y2"]
    need(material["permeability"]["BHCoordinates"]["Points"] == [v for pair in pack["bh"]["points_H_B"] for v in pair], "saved BH values differ")
    for key, field in (("Kh", "core_loss_kh"), ("Kc", "core_loss_kc"), ("Ke", "core_loss_ke"), ("Kdc", "core_loss_kdc"), ("Y", "core_loss_y")):
        need(float(material[field]) == pack["core_loss"][key], "saved core-loss material differs")
    need(float(material['conductivity']) == pack['properties']['conductivity_s_per_m']
         and float(material['mass_density']) == pack['properties']['mass_density_kg_per_m3']
         and material['stacking_type']['Choice'] == 'Solid', 'saved source M19 scalar properties differ')
    assigned = {name: value.strip('"') for name, value in observed['material_assignments'].items()}
    exact(assigned, audit['materials']['material_readback'], 'saved actual material assignments')
    for name, material_name in assigned.items():
        expected = spec.core_material if name in ('stator','rotator') else spec.shaft_material if name == 'shaft' else \
                   spec.magnet_material if name.startswith('magnet_') and name != 'magnet_space' else \
                   spec.winding_material if re.fullmatch('winding[0-9]+',name) else spec.air_material
        need(material_name.casefold() == expected.casefold(), 'saved source material assignment differs: ' + name)
    required = [name for name in ppt.PPT_REPORT_DEFS if not (name == "PPT_EnergyError" and not spec.solver_output_error)
                and not (name == "PPT_Inductance_Matrix" and not spec.compute_inductance)]
    need(set(native["actual_reports"]) == set(native["report_definitions"]) == set(audit["reports"]) == set(required), "saved required reports differ")
    for group, count in spec.mesh_elements.items():
        mesh = model["MeshSetup"]["MeshOperations"]["Mesh_" + group]
        length_only = spec.mesh_refinement_modes.get(group) == 'length_only'
        wanted = {'Enabled': True, 'RefineInside': True, 'RestrictElem': not length_only,
                  'RestrictLength': group in spec.mesh_maximum_lengths_mm, 'ApplyToInitialMesh': False,
                  'NumMaxElem': str(1000 if length_only else count), 'Type': 'LengthBased',
                  'MaxLength': f'{spec.mesh_maximum_lengths_mm[group]:g}mm' if group in spec.mesh_maximum_lengths_mm else '1mm'}
        for key, value in wanted.items(): exact(mesh.get(key), value, 'saved source mesh ' + group + '.' + key)
    return spec, state, native, required


class OwnedSDKProductionEvidence:
    def __init__(self, value, *, authority=None):
        need(value.get("schema") == DECLARATION_SCHEMA and type(value.get("cases")) is dict and value["cases"], "typed SDK evidence declaration required")
        self.cases, self.authority, self.used = value["cases"], authority, []
        self.dependencies = []
        self.memory_sha256 = sha(jb(self.cases))
        self.evidence_mode = value.get('evidence_mode','unspecified')
        self._memory_pin = self._memory_snapshot()

    @classmethod
    def from_declaration(cls, path, digest):
        ref = {"path": str(Path(path).resolve(strict=True)), **launch.record(path)}
        need(ref["sha256"] == digest, "SDK evidence declaration SHA differs")
        return cls(document(launch.read_ref(ref)), authority=ref)

    @classmethod
    def from_document(cls, value): return cls(value)

    def _memory_snapshot(self):
        files = []
        for item in self.used:
            files.append({'object':id(item),'class':id(type(item)),'attributes':sorted(vars(item)),
                'root':[id(item.root),str(item.root)],'index_path':[id(item.index_path),str(item.index_path)],
                'index':[id(item.index),item.index],'files':[id(item.files),item.files],
                'remote':[id(item.remote),item.remote],
                'used':[id(item.used),sorted((str(k),v) for k,v in item.used.items())],
                'aliases':[id(item.aliases),sorted((str(k),str(v)) for k,v in item.aliases.items())]})
        return sha(jb({'cases':[id(self.cases),self.cases],'authority':self.authority,'mode':self.evidence_mode,
                       'used_list':id(self.used),'files':files,
                       'dependencies':[id(self.dependencies),[(id(e),e._memory_pin) for e in self.dependencies]]}))

    def assert_memory_unchanged(self):
        need(sha(jb(self.cases)) == self.memory_sha256 and self._memory_snapshot() == self._memory_pin,
             'SDK evidence in-memory authority/files/references changed')
        for evidence in self.dependencies:evidence.assert_memory_unchanged()

    def assert_unchanged(self):
        self.assert_memory_unchanged()
        need(sha(jb(self.cases)) == self.memory_sha256, "SDK evidence in-memory case authority changed")
        if self.authority: launch.read_ref(self.authority)
        for files in self.used: files.assert_unchanged()
        for evidence in self.dependencies:evidence.assert_unchanged()

    def verify_case(self, case_id, expected_producer=None):
        self.assert_memory_unchanged()
        from .execution_evidence import EvidenceFiles, _replay_metrics
        item = self.cases[case_id]
        files = EvidenceFiles(item["root"], item["index_path"], item["index_sha256"])
        read = lambda ref: files.raw(files.reference(ref))
        contract = files.json("contract")
        case = validate_contract(contract, read)
        need(contract["case_id"] == case_id and (expected_producer is None or expected_producer == contract["expected_producer"]), "SDK evidence case/producer differs")
        original = _one_case(files.raw("result")); row = _typed_row(original)
        analyze = contract["mode"] != "node_native0"
        need(row.get("status") == "ok" and row.get("case_id") == case_id and row["analyze"] is analyze
             and row.get("input_producer_fingerprint") == contract["expected_producer"]
             and row.get("input_execution_backend") == BACKEND and row.get("input_sdk_production_mode") == contract["mode"]
             and row.get("input_execution_purpose") == contract["purpose"]
             and row.get("input_sdk_case_intent_sha256") == contract_sha256(contract), "original SDK row is failed/diagnostic/misbound")
        identity = json.loads(row["standalone_owner_identity"])
        need(strict_exit(row, identity, analyze=analyze), "original15s owned exit failed or uncertain")
        terminal = files.json("terminal_task")
        need(type(terminal.get("id")) is int and terminal["id"] > 0 and terminal.get("status") == "completed"
             and type(terminal.get("exit_code")) is int and terminal["exit_code"] == 0
             and terminal.get("actual_node_name") == contract["node"], "original task/placement/terminal status differs")
        owned = files.json("owned"); before = files.json("launch_before"); after = files.json("launch_after")
        profile = files.json(files.reference(before["profile"]))
        exact(contract['launch_runtime'], {k: profile[k] for k in RUNTIME_KEYS}, 'actual authority-bound runtime')
        inputs = files.json("inputs_before")
        exact(files.json("inputs_after"), inputs, "before/after immutable inputs")
        for prefix, records in (("source/", contract["source_files"]), ("library/", profile["library_files"]),
                                ("install/", profile["installation_files"])):
            for name, expected in records.items(): exact(inputs.get(prefix + name), expected, "actual source/library/install input")
        identity_doc = document(read(profile["sdk_identity"]))
        need(profile["sdk_identity"]["sha256"] == launch.IDENTITY_SHA and len(identity_doc["private_manifest"]) == 1189,
             "actual immutable private SDK1189 identity required")
        exact(inputs.get('private_sdk'), {'identity_sha256': launch.IDENTITY_SHA,
              'manifest_sha256': sha(jb(identity_doc['private_manifest'])), 'full_files_verified': 1189}, 'actual SDK verification identity')
        import_raw = read(profile["sdk_import_proof"])
        exact(launch.raw_record(import_raw), identity_doc['import_proof'], 'actual private import proof')
        defaults = document(import_raw)["default_aedt_environment_variables"]
        need(len(defaults) == 12 and all(v == '1' for v in defaults.values()), "actual Linux SDK12 defaults differ")
        binding = files.json("task_binding"); request = files.json("final_request")
        need(binding.get("task_id") == terminal["id"] and type(binding.get("task_id")) is int
             and binding.get("case_id") == case_id and binding.get("case_intent_sha256") == contract_sha256(contract)
             and binding.get("final_payload_sha256") == sha(jb(request)), "actual task/request/intent binding differs")
        from .cluster import Controller
        Controller._verify_task(None, terminal, request)
        for key in ('command', 'env_setup', 'max_workers_per_node'):
            if key in terminal:exact(terminal[key], request.get(key), 'exposed task field ' + key)
        need(type(request.get('max_workers_per_node')) is int and 1 <= request['max_workers_per_node'] <= 12
             and request.get('entrypoint') == 'tools/run_owned_sdk_case.py', 'explicit SDK singlecase worker/node cap required')
        command = shlex.split(request['command'])
        need(command[:4] == ['python', '-I', '-B', 'tools/run_owned_sdk_case.py'], 'actual SDK worker entry differs')
        entry = before['worker_entry']
        need(entry.get('schema') == 'owned_sdk_original_worker_entry_v1' and entry.get('isolated') is True
             and entry.get('dont_write_bytecode') is True and entry.get('cwd') == request['remote_cwd'], 'original isolated worker observation differs')
        need(type(entry.get('argv')) is list and len(entry['argv']) >= 1 and entry['argv'][1:] == command[4:],
             'sealed final command differs from original worker argv')
        script = PurePosixPath(entry['argv'][0]); expected_script = PurePosixPath(request['remote_cwd']) / command[3]
        need('..' not in script.parts and str(script if script.is_absolute() else PurePosixPath(request['remote_cwd']) / script) == str(expected_script),
             'original worker script differs from final payload')
        required_flags = {'--cases': contract['case']['path'], '--workers': '1', '--cores': '4',
                          '--aedt-backend': 'standalone', '--max-cases': '1',
                          '--sdk-launch-profile': files.original(files.reference(before['profile'])),
                          '--sdk-launch-profile-sha256': before['profile']['sha256']}
        for flag, value in required_flags.items():
            need(command.count(flag) == 1 and command[command.index(flag) + 1] == value, 'original worker argument differs: ' + flag)
        need(command.count('--non-graphical') == command.count('--keep-projects') == 1
             and all(flag not in command for flag in ('--electrical-zero-deg','--beta-convention','--periodic-boundary','--graphical','--cleanup-linux')),
             'SDK control override or unsafe retention requested')
        need(('--setup-only' in command) is (not analyze) and ('--prepare-batch-reports' in command) is (not analyze), 'original Native0/FEA command differs')
        valued = set(required_flags) | {'--simulation-dir', '--result-csv'}
        switches = {'--non-graphical', '--keep-projects'} | ({'--setup-only', '--prepare-batch-reports'} if not analyze else set())
        offset = 4
        while offset < len(command):
            flag = command[offset]
            need(flag in valued or flag in switches, 'unknown SDK worker argument: ' + flag)
            need(command.count(flag) == 1, 'duplicate SDK worker argument')
            offset += 2 if flag in valued else 1
        for flag in ('--simulation-dir', '--result-csv'):
            need(command.count(flag) == 1 and command.index(flag) + 1 < len(command), 'one explicit output path required')
        def absolute(value):
            path = PurePosixPath(value)
            need('..' not in path.parts, 'unsafe worker output path')
            return str(path if path.is_absolute() else PurePosixPath(request['remote_cwd']) / path)
        simulation = absolute(command[command.index('--simulation-dir') + 1])
        need(absolute(command[command.index('--result-csv') + 1]) == files.original('result')
             and str(PurePosixPath(simulation) / original['simulation_name']) == original['project_path'],
             'original worker output/project path differs')
        need(files.original('project') == str(PurePosixPath(original['project_path']) / (original['simulation_name'] + '.aedt'))
             and files.original('setup_audit') == original['artifact_setup_audit']
             and files.original('launch_after') == original['artifact_sdk_launch_provenance'], 'original output artifact pointer differs')
        exact(profile["source_files"], contract["source_files"], "launch/evidence source")
        need(profile["schema"] == PROFILE_SCHEMA and profile["execution_enabled"] is True and profile["node"] == contract["node"]
             and profile["cores"] == 4 and type(profile["cores"]) is int, "actual SDK production launch profile differs")
        need(files.reference(profile["owned_sdk_contract"]) == "contract", "actual profile/contract differs")
        exact(owned["owned_identity"], identity, "owned launch identity")
        from ._sdk_launch_binding import production_arguments
        exact(owned["argv_tail4"], production_arguments(contract["node"])[0], "actual owned argv")
        need(owned["SDK12_matches_defaults"] is True and owned["Ansys_resource_readback"]["owned_Native_observed"] == "4", "owned resource readback differs")
        exact(owned["owned_SDK12_environment"], defaults, "raw owned SDK12 environment")
        gate = before["resource_gate"]
        from ._sdk_launch_resource import evaluate
        raw_job, raw_step = gate["raw_job"].encode("utf-8"), gate["raw_step"].encode("utf-8")
        policy = document(read(contract["node_policy"]))
        dependencies = []
        validate_node_policy(contract["node_policy"], node=contract["node"], source_files=contract["source_files"], mode=contract["mode"], read=read, runtime=contract['launch_runtime'], retain=dependencies)
        selected = evaluate(raw_job, raw_step, {k: v for k, v in gate["Slurm_whitelist"].items() if v is not None}, uid=1641, node=contract["node"], policy=policy)
        exact(selected, gate["selected"], "original current Slurm observation")
        need(after["environment"]["Slurm_whitelist_unchanged"] is True and after["environment"]["Ansys_resource_input_restored"] is True,
             "resource restoration failed")
        spec, state, native, required = verify_saved_settings(files, contract, case, row)
        setup_audit = files.json('setup_audit')
        need(setup_audit.get('analysis') is True if analyze else 'analysis' not in setup_audit,
             'actual setup audit Analyze mode differs')
        need(not analyze or row["standalone_analyze_return_type"] == "builtins.bool" and row["standalone_analyze_return_repr"] == "True", "strict original Analyze True missing")
        if not analyze:
            need(native["solutions"] == {}, "Native0 has a saved solution")
        else:
            need(files.original('solver_state') == original['artifact_standalone_solver_state']
                 and files.original('before_analyze') == original['artifact_sdk_before_analyze'], 'original SDK state pointer differs')
            solver = files.json('solver_state')
            import run_ipmsm_batch as runner
            for key, expected in {'schema': runner.STANDALONE_SOLVER_STATE_CONTRACT, 'case_id': case_id,
                                  'stage': 'analyze_returned_true', 'owned_pid': identity[0], 'owner_identity': identity,
                                  'solver_state_uncertain': False, 'producer_fingerprint': contract['expected_producer'],
                                  'Analyze_return_received': True, 'Analyze_return_type': 'builtins.bool', 'Analyze_return_repr': 'True',
                                  'automatic_retry_allowed': False, 'NativeQuit_allowed': True}.items():
                exact(solver.get(key), expected, 'original SDK solver state ' + key)
            marker = files.json("before_analyze")
            need(marker["case_id"] == case_id and marker["case_intent_sha256"] == contract_sha256(contract)
                 and marker["Analyze_calls_before_snapshot"] == 0 and type(marker["Analyze_calls_before_snapshot"]) is int
                 and marker["initial_save_return_type"] == "builtins.bool" and marker["initial_save_return_repr"] == "True"
                 and marker["automatic_retry"] is False, "same-instance SaveTrue beforeimage intent missing")
            exact(marker["full_spec"], asdict(spec), "beforeimage fullspec")
            exact(marker["owned_identity"], identity, "same-instance beforeimage owner")
            need(files.reference(marker["before_image"]) == "initial_project" and marker["source_project"]["bytes"] == files.rec("initial_project")["bytes"]
                 and marker["source_project"]["sha256"] == files.rec("initial_project")["sha256"], "exclusive beforeimage bytes differ")
            need(marker["source_project"]["path"] == str(PurePosixPath(original["project_path"]) / (original["simulation_name"] + ".aedt"))
                 and files.original("project") == marker["source_project"]["path"], "original same-owned project path differs")
            from . import native_state_semantics as semantics
            initial = semantics.read_state(files.path("initial_project"))
            need(initial["project"]["Maxwell2DModel"].get("Solutions") == {}, "beforeimage was already solved")
            policy_raw = read(contract["semantic_policy"])
            comparison = semantics.compare_states(initial, state, prepared_record=files.rec("initial_project"), solved_record=files.rec("project"),
                         renderer_producer=contract["expected_producer"], policy_bytes=policy_raw, policy_sha256=contract["semantic_policy"]["sha256"])
            need(comparison["semantic_equivalence_proven"] is True, "full saved same-instance input semantics failed: " + str(comparison["errors"]))
            from .solver_profile import audit_solver_profile
            resource_audit = files.json('profile4_audit')
            need(files.reference(resource_audit['profile_record']) == 'profile' and files.reference(resource_audit['acf_record']) == 'acf'
                 and files.reference(after['resource_profile_audit']) == 'profile4_audit', 'original profile/ACF publication differs')
            project = PurePosixPath(original['project_path'])
            need(PurePosixPath(files.original('profile')).parent == project / (original['simulation_name']+'.aedtresults') / 'IPMSM.results'
                 and files.original('acf') == str(project / (original['simulation_name']+'.pyaedt') / 'IPMSM/pyaedt_config.acf'), 'original owned profile/ACF path differs')
            audit = audit_solver_profile(files.raw("profile"), requested_cores=4, acf=files.raw("acf"))
            need(audit["profile_format"] == "saved_native" and audit["normal_completion"] is True
                 and set(audit["machine_core_counts"]) == set(audit["matrix_core_counts"]) == {"4"}
                 and audit["matrix_core_records_complete"] is True and audit["all_matrix_records_match_request"] is True
                 and audit["acf"]["requested_core_counts"] == [4], "actual saved-native/own ACF four cores not proven")
        production = contract["mode"] == "production"
        summary = None
        mesh_count = None
        if analyze:
            values = [count for counts in audit["mesh_triangle_counts"].values() for count in counts]
            need(values and all(type(v) is int and v > 0 for v in values), "actual saved-native global mesh missing")
            mesh_count = values[-1]
        if analyze and case.get("operation", "sin_current") == "sin_current":
            post = {"report_mapping": {}, "report_records": {}, "summary": original, "native_profile_record": files.rec("profile")}
            for report in required:
                role = "report/" + report
                post["report_mapping"]["artifact_report_" + report] = files.original(role)
                need(original.get('artifact_report_' + report) == files.original(role), 'original raw report pointer differs')
                post["report_records"]["artifact_report_" + report] = files.rec(role)
            import run_ipmsm_batch as runner
            result = _replay_metrics(files, runner, spec, required, row, post, case.get("operation", "sin_current"))
            need(result["physical_quality_passed"] is True and result["native_normal_completion"] is True
                 and result["resource_4core_proven"] is True, "raw SDK numeric/physics replay failed")
            summary = result["summary"]
        elif analyze:
            # Zero/no-load qualification has different numerical eligibility
            # from loaded training. It still requires every original raw report
            # and the same complete solver time grid; it cannot certify a profile.
            import run_ipmsm_batch as runner
            total = spec.transient_periods * spec.steps_per_period
            period = runner.transient_setup_metadata(spec)['electrical_period_s']
            for report in required:
                role = 'report/' + report
                need(original.get('artifact_report_' + report) == files.original(role), 'original no-load raw report pointer differs')
                raw_frame = runner.read_report_csv(files.path(role))
                columns = [c for c in raw_frame if re.fullmatch(r'Time(?:\s*\[[^\]]*\])?', str(c).strip())]
                need(len(columns) == 1 and runner.extract_column_unit(columns[0]) in runner.TIME_UNITS_TO_SECONDS,
                     'no-load raw time column/unit missing')
                times = [runner.parse_time_seconds(v, runner.extract_column_unit(columns[0])) for v in raw_frame[columns[0]]]
                need(len(times) == total + 1 and all(math.isfinite(t) and math.isclose(t,i*period/spec.steps_per_period,rel_tol=1e-9,abs_tol=1e-12)
                     for i,t in enumerate(times)), 'no-load raw full solver grid differs')
        if production:
            from . import winding_frames as frames
            frame = document(read(contract["reference_frame"]))
            selected_profile = document(read(contract["selected_profile"]))
            # This production profile's raw case coverage is replayed below by
            # the dedicated verifier, never trusted from a passed flag.
            verify_selected_profile(selected_profile, self, contract["expected_producer"], files=files)
            frame_evidence = frame_evidence_from_declaration(selected_profile["frame_evidence"], sdk_evidence=self)
            frames.validate_case_frame(asdict(spec), frame_evidence.origin.spec, frame, frame_evidence)
            need(summary is not None, "production requires loaded apparent-inductance raw replay")
        approval = document(read(contract['root_approval']))
        fs_observation = document(read(policy['filesystem']))
        actual = (self.evidence_mode == 'linux_original' and approval.get('evidence_mode') == 'linux_original'
                  and fs_observation.get('evidence_mode') == 'linux_original'
                  and 'inert' not in str(approval.get('scope','')).casefold() and policy.get('schema') == NODE_SCHEMA)
        if production:
            actual = actual and frame.get('evidence_mode') == 'linux_original' and not str(frame.get('reference_frame_id','')).startswith('inert-')
        files.assert_unchanged()
        for evidence in dependencies:evidence.assert_unchanged()
        # Only a successful full replay may enroll fresh EvidenceFiles memory.
        self.assert_memory_unchanged()
        self.used.append(files)
        self.dependencies.extend(dependencies)
        self._memory_pin = self._memory_snapshot()
        return {"backend": BACKEND, "case_id": case_id, "mode": contract["mode"], "node": contract["node"],
                "receipt": None if not analyze else document(read(contract["reservation_receipt"])),
                "source_files": contract["source_files"], "full_spec42": asdict(spec), "saved_state": state,
                'launch_runtime': contract['launch_runtime'],
                "original_row": original, "production_row": dict(original), "summary": summary,
                "case": case, "actual_mesh_elements": mesh_count,
                "frame_manifest": None if contract["reference_frame"] is None else document(read(contract["reference_frame"])),
                "selected_profile": contract["selected_profile"], "observed_cores": 4 if analyze else None,
                "physics_key": "owned-sdk-physics:sha256:" + sha(jb({"source_files": contract["source_files"],
                        "frame": contract["reference_frame"], "selected_profile": contract["selected_profile"]})),
                "qualification_passed": not production, "production_eligible": production,
                'raw_replay_passed': analyze,
                'actual_observation_eligible': actual,
                'actual_data_eligible': production and actual,
                'task_API_observed_fields':{key:key in terminal for key in ('command','env_setup','max_workers_per_node')},
                'worker_argv_matches_sealed_final_command':True,'task_API_environment_text_verified':False,
                "profile_evidence_eligible": contract["mode"] == "node_smoke" and summary is not None,
                "training_eligible": production and contract["purpose"] == "training", "errors": []}

    def verify_frame_rows(self, rows, manifest):
        proofs = []
        for row in rows:
            proof = self.verify_case(row["case_id"], expected_producer=manifest["producer_fingerprint"])
            need(proof["production_eligible"] is True, "qualification is never a production frame row")
            exact(proof["frame_manifest"], manifest, "actual SDK case frame")
            from .reference_frames import _verify_collected_row
            _verify_collected_row(row, proof["production_row"], {})
            proofs.append(proof)
        return proofs


def frame_evidence_from_declaration(value, *, sdk_evidence):
    from . import winding_frames as frames, production_zero as zero
    return frames.ProductionWindingEvidence(zero.VerifiedProductionZero.from_plan(value["path"], value["sha256"]), sdk_evidence=sdk_evidence)


def verify_selected_profile(profile, evidence, producer, *, files=None, frame_evidence=None):
    """Replay explicitly enumerated qualification cases; no self-certified flag."""
    need(profile.get("schema") == "owned_sdk_selected_profile_v1" and profile.get("backend") == BACKEND
         and profile.get("producer_fingerprint") == producer and type(profile.get("cases")) is list
         and profile["cases"] and len(set(profile["cases"])) == len(profile["cases"]), "actual SDK selected profile proof required")
    results, plans, mesh = [], [], {}
    for case_id in profile["cases"]:
        proof = evidence.verify_case(case_id, expected_producer=producer)
        need(proof["profile_evidence_eligible"] is True, "selected SDK profile requires original four-core raw-replayed qualification cases")
        results.append(proof["original_row"]); plans.append(proof["case"]); mesh[case_id] = proof["actual_mesh_elements"]
    from . import quality, winding_frames as frames
    if frame_evidence is None:
        frame_evidence = frame_evidence_from_declaration(profile["frame_evidence"], sdk_evidence=evidence)
    frame = profile["reference_frame"]
    report = quality.compare_profiles(results, plans, reference_profile=profile["reference_profile"],
              refined_reference_profile=profile["refined_reference_profile"], mesh_counts=mesh,
              frame_manifest=frame, frame_evidence=frame_evidence)
    ref = profile["comparison_report"]
    path = Path(ref["path"]) if files is None else files.path(files.reference(ref))
    exact(document(launch.read_once(path)), report, "recomputed SDK convergence report")
    need(launch.record(path) == {k: ref[k] for k in ("bytes", "sha256")}, "SDK convergence report SHA differs")
    selected = quality.selected_profile_manifest(report, path, profile["selected_profile"])
    exact(selected["settings"], profile["settings"], "actual SDK selected profile settings")
    evidence.assert_unchanged()
    return selected


def evidence_arguments(parser):
    parser.add_argument("--owned-sdk-production-evidence", type=Path)
    parser.add_argument("--owned-sdk-production-evidence-sha256")


def evidence_from_arguments(args):
    path = getattr(args, "owned_sdk_production_evidence", None)
    digest = getattr(args, "owned_sdk_production_evidence_sha256", None)
    need(bool(path) == bool(digest), "SDK production evidence path and SHA required together")
    return OwnedSDKProductionEvidence.from_declaration(path, digest) if path else None


def verify_selected_profile_document(path, expected_producer=None, expected_sha256=None):
    """New SDK selected document admission; existing CLI/legacy APIs are separate."""
    ref = {"path": str(Path(path).resolve(strict=True)), **launch.record(path)}
    need(expected_sha256 is None or ref['sha256'] == expected_sha256, 'selected SDK initial read SHA differs')
    selected = document(launch.read_ref(ref))
    proof = document(launch.read_ref(selected["owned_sdk_profile_proof"]))
    producer = proof["producer_fingerprint"]
    need(expected_producer is None or producer == expected_producer, "selected SDK producer differs")
    declaration = proof["case_evidence"]
    evidence = OwnedSDKProductionEvidence.from_declaration(declaration["path"], declaration["sha256"])
    recomputed = verify_selected_profile(proof, evidence, producer)
    for key in ("schema_version", "metric_contract_version", "producer_fingerprint", "settings", "material_context",
                "material_fingerprint", "winding_context", "material_context_provenance"):
        exact(selected.get(key), recomputed.get(key), "actual SDK selected document " + key)
    need(any(item['sha256'] == proof['comparison_report']['sha256'] for item in selected.get('evidence', [])),
         "selected SDK report proof missing")
    evidence.assert_unchanged(); launch.read_ref(ref); launch.read_ref(selected["owned_sdk_profile_proof"])
    return recomputed
