"""TEST ONLY complete profile graph. No verifier stubs, Native or authority issue.

The9 CSV bytes retain their genuine Source11/570 provenance. Case authority,
process history, resource observations and declared native mesh/time graph are
explicitly synthetic regression inputs, never actual307b convergence evidence.
"""
from pathlib import Path, PurePosixPath
from dataclasses import asdict
import ast,base64,copy,csv,gzip,hashlib,io,json,math,re,sys

D=Path(__file__).resolve().parent
SOURCE=D.parent
ASSETS=D/"fixtures/owned_cli_upstream"
sys.path[:0]=[str(SOURCE/"src"),str(SOURCE),str(D)]
import run_ipmsm_batch as runner
from pyaedt_motor import execution_evidence as e,execution_compatibility as ec,public_batch_pilot as pilot,reference_frames as frames,quality
from pyaedt_motor.solver_profile import audit_solver_profile
from tests.test_ipmsm_execution_evidence import Bundle
from tests._actual_family_fixture import materialize

def rec(raw):return {"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}
def canonical(value):return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def write(path,value):
    raw=canonical(value);path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("xb")as stream:stream.write(raw)
    return {"path":str(path.resolve()).replace("\\","/"),**rec(raw)}
def asset(name):
    m=json.loads((ASSETS/"PROVENANCE.json").read_bytes())["files"][name]
    z=(ASSETS/(name+".gz")).read_bytes();assert rec(z)==m["gzip"]
    raw=gzip.decompress(z);assert rec(raw)==m["raw"];return raw
def doc(name):return json.loads(asset(name))

def synthetic_native(raw,steps,mesh):
    """Transform copies only. Changes describe an inert synthetic Native graph."""
    text=raw.decode("utf-8-sig")
    for name,value in (("StepsPerPeriod",str(steps)),("TimeStep","StopTime/"+str(6*steps))):
        text,count=re.subn(r"(VariableProp\('"+name+r"', 'UD', '', ')[^']+(')",lambda m:m[1]+value+m[2],text)
        assert count==1,(name,count)
    for group,value in mesh.items():
        pattern=r"(\$begin 'Mesh_"+group+r"'.*?NumMaxElem=')[^']+('.*?\$end 'Mesh_"+group+r"')"
        text,count=re.subn(pattern,lambda m:m[1]+str(value)+m[2],text,flags=re.S)
        assert count==1,(group,count)
    return text.encode("utf-8")

def synthetic_profile(elements,steps):
    # Generated unit-resource record, not edited/relabeled actual Native output.
    return ("$begin 'Profile'\nMajorVer=2025\nMinorVer=2\n$begin 'ProfileGroup'\nName='HPC'\n"
      "I(1, 'Type', 'Disabled')\nProfileItem('Machine', 'Cores', 4, 'Tasks', 1,)\n$end 'ProfileGroup'\n"
      +f"ProfileItem('Mesh', 'Triangles', {elements//2},)\nProfileItem('Manual Refine', 'Triangles', {elements},)\n"
      +"ProfileItem('Matrix Solve', 'Cores', 4, 'Calls', 1,)\nI(1, 'Status', 'Normal Completion')\n$end 'Profile'\n").encode()

class ProfileCase(Bundle):
    def __init__(self,root,label,steps,scale,elements,family_root,manifest):
        root=Path(root);root.mkdir(parents=True,exist_ok=False);super().__init__(root)
        base=copy.deepcopy(self.data["builder_result"])
        self.case_id="INERT_PROFILE_"+label;self.original_root="/recorded/test_profile/"+label
        self.project=self.original_root+"/scene/simulation1/simulation1.aedt"
        case=doc("m2/case.json");case.update(case_id=self.case_id,beta_calibration_id="",source_case_id="",source_result_path="")
        case["steps_per_period"]=steps;case["quality_profile"]="test_only_profile_graph"
        for group in runner.MESH_ELEMENT_KEYS:case["mesh_"+group+"_elements"]=int(case["mesh_"+group+"_elements"])//2*scale
        self.data["case"]=case;self.original["case"]=self.original_root+"/case.json"
        spec=runner.build_spec(case);self.spec=spec
        # Genuine library23 from the real family fixture, not placeholder files.
        mapping=json.loads((family_root/"mapping.json").read_bytes())
        for name in list(self.data):
            if name.startswith("library/"):self.data.pop(name);self.original.pop(name)
        libraries={}
        for name,item in mapping["library_files"].items():
            raw=(family_root/item["path"]).read_bytes();assert rec(raw)=={k:item[k]for k in("bytes","sha256")}
            self.add("library/"+name,raw,"/recorded/library/"+name);libraries[name]=rec(raw)
        contract=self.data["contract"];contract.update(case_id=self.case_id,library_files=libraries,library_root="/recorded/library")
        # Live test setup uses the exact imported C source; packed archives keep
        # these original path strings only as provenance keys.
        contract["source_root"]=str(self.source).replace("\\","/")
        for name in contract["source_files"]:self.original["source/"+name]=contract["source_root"]+"/"+name
        contract["account"]="r1jae262"
        contract["options"].update(beta_convention="dq_current_advance_v2",use_periodic_boundary=False)
        install=doc("m2/installation_binding.json");self.data["installation_binding"]=install
        self.original["installation_binding"]=self.original_root+"/installation_binding.json"
        self.add("installation_before",doc("m2/installation_before.json"),self.original_root+"/installation_before.json")
        self.add("installation_after",doc("m2/installation_after.json"),self.original_root+"/installation_after.json")
        # A synthetic material-file authority is deliberately not a claim to
        # possess/validate installed Native binaries or original Linux syslibs.
        contract["system_material_files"]={self.original["syslib"]:self.rec("syslib")}
        self.data["project"]=synthetic_native(asset("m2/after.aedt"),steps,spec.mesh_elements)
        self.original["project"]=self.project
        self.add("prepared_full",synthetic_native(asset("m2/before.aedt"),steps,spec.mesh_elements),self.original_root+"/prepared_unsolved.aedt")
        for role,name in (("prepared_full","before.aedt"),("project","after.aedt")):
            path=root/name;path.write_bytes(self.raw(role))
            self.add("prepared"if role=="prepared_full"else"solved",pilot.saved_native(path),self.original_root+"/"+role+"_native_closure.json")
        audit=doc("m2/setup_audit.json");audit["requested_spec"]=asdict(spec)
        self.data["setup_audit"]=audit;self.original["setup_audit"]=self.original_root+"/scene/setup_audit.json"
        builder=doc("m2/builder.json");builder.update(base)
        builder.update(case_id=self.case_id,project_path=str(PurePosixPath(self.project).parent),simulation_name="simulation1",
            artifact_setup_audit=self.original["setup_audit"],input_producer_fingerprint=self.producer)
        cleanup=json.loads(builder["standalone_cleanup"]);cleanup["client_retirement"]["sdk_version"]="0.22.0"
        builder["standalone_cleanup"]=json.dumps(cleanup)
        builder.update(e._production_expected_inputs(runner,case,spec))
        # Retain actual public CSV-style nested representations.
        for key,value in list(builder.items()):
            if key.startswith("input_")and isinstance(value,(dict,list,tuple)):builder[key]=str(value)
        for key in runner.CASE_METADATA_COLUMNS:builder[key]=case.get(key,"")
        builder["timing_solve_s"]=float(1000+scale*100)
        self.data["builder_result"]=builder;self.original["builder_result"]=self.original_root+"/builder_result.json"
        self.add("profile",synthetic_profile(elements,steps),self.original_root+"/synthetic_native.profile")
        command=pilot.batch_command(install["wrapper_path"],Path(self.project),Path(self.original_root),"n113",spec.setup_name)
        command=[str(x).replace("\\","/")for x in command];self.data["command"]=command
        self.original["command"]=self.original_root+"/command.json";self.original["batch_options"]=self.original_root+"/options.txt"
        owner=self.data["owner"];native=owner["Native_processes"][0]
        native.update(exe=install["native_exe"],cmdline=[install["native_exe"],*command[1:]])
        wrapper=owner["owned_processes"][0];identity=copy.deepcopy(wrapper["last_verified_identity"])
        identity.update(exe=install["interpreter"],cmdline=[install["interpreter"],install["wrapper_path"],*command[1:]])
        owner["owned_processes"]=[self.process(identity),self.process(native)]
        self.original["owner"]=self.original_root+"/solver_owner_result.json"
        self.original["cli_intent"]=self.original_root+"/owned_cli_solver_state.json"
        self.original["cli_completion"]=self.original_root+"/owned_cli_solver_completion.json"
        contract.update(case=self.ref("case"),installation_binding=self.ref("installation_binding"))
        self.data["receipt"].update(case_id=self.case_id,case_sha256=self.rec("case")["sha256"])
        self.data["receipt"]["execution_contract_sha256"]=pilot.execution_contract_sha256(contract)
        self.original["receipt"]=self.original_root+"/receipt.json";contract["reservation_receipt"]=self.ref("receipt")
        self.original["contract"]=self.original_root+"/contract.json"
        before_rec=self.rec("prepared_full")
        self.data["cli_intent"].update(case_id=self.case_id,command=self.rec("command"),reservation_receipt=contract["reservation_receipt"],
            prepared_project=before_rec,prepared_unsolved_project=self.ref("prepared_full"))
        self.data["cli_completion"].update(case_id=self.case_id)
        mapping_reports={};report_records={};local_mapping={}
        reports=root/"reports";reports.mkdir()
        for report in __import__('module.ipmsm_ppt_setup',fromlist=['PPT_REPORT_DEFS']).PPT_REPORT_DEFS:
            if report=="PPT_EnergyError":continue
            key="artifact_report_"+report;role="report/"+report;raw=asset(f"numeric{steps}/{report}.csv")
            self.add(role,raw,self.original_root+"/reports/"+report+".csv")
            path=reports/(report+".csv");path.write_bytes(raw);local_mapping[key]=str(path)
            mapping_reports[key]=self.original[role];report_records[key]=rec(raw)
        from module.ipmsm_waveform_quality import analyze_exported_waveforms
        summary=runner.summarize_transient_outputs(local_mapping,spec,operation="sin_current")
        summary.update(analyze_exported_waveforms(local_mapping,spec,"sin_current"));runner.add_derived_motor_metrics(summary,spec)
        pin,power,loss=[runner.finite_float(summary[k])for k in("output_electrical_input_last_w","output_mech_power_last_w","output_total_loss_last_avg_w")]
        summary.update(output_power_balance_residual_last_w=pin-power-loss,output_power_balance_residual_last_pct=100*(pin-power-loss)/max(abs(pin),1.))
        parsed=audit_solver_profile(self.raw("profile"),requested_cores=4);parsed["observed_v252"]=True
        self.data["pilot_result"].update(case_id=self.case_id,errors=[],status="solved_diagnostic",
            training_allowed=False,public_training_admission=False,
            prepared_project={"path":self.project,**before_rec},prepared_unsolved_project=self.ref("prepared_full"),
            installation_observation_before=self.ref("installation_before"),installation_observation_after=self.ref("installation_after"),
            saved_serialization_comparison=pilot.compare_saved(self.data["prepared"],self.data["solved"]),
            public_setup_evidence=self.rec("setup_audit"),
            postprocess={"report_mapping":mapping_reports,"report_records":report_records,"summary":summary,
                "native_profile_record":self.rec("profile"),"native_profile":parsed})
        self.original["pilot_result"]=self.original_root+"/pilot_result.json"
        self.data["source_inputs"]={"source/"+n:r for n,r in contract["source_files"].items()}|{"library/"+n:r for n,r in libraries.items()}
        self.data["source_inputs"].update({"syslib/"+n:r for n,r in contract["system_material_files"].items()})
        self.data["source_inputs"].update({k:contract[k]for k in("case","installation_binding","reservation_receipt")})
        self.original["source_inputs"]=self.original_root+"/source_and_inputs_before.json"
        self.add("semantic_checker_source",Path(__import__('pyaedt_motor.native_state_semantics',fromlist=['__file__']).__file__).read_bytes(),self.original_root+"/native_state_semantics.py")
        self.add("semantic_policy",asset("m2/Root_policy.json"),self.original_root+"/semantic_policy.json")
        self.add("task_status",{"id":700000+scale,"status":"completed","exit_code":0,"scope":"INERT_SYNTHETIC_TEST_NO_API"},self.original_root+"/task_status.json")
        authority={"schema":"Root_CLI_profile_evidence_authority_v1","purpose":"profile_evidence_only","case_id":self.case_id,
            "producer_fingerprint":self.producer,"execution_contract_sha256":pilot.execution_contract_sha256(contract),
            "semantic_policy":self.ref("semantic_policy"),"prepared_raw":self.ref("prepared_full"),"solved_raw":self.rec("project"),
            "original_task_status":self.rec("task_status"),"profile_reuse_authorized":True,"training_admission_authorized":False,
            "scope":"INERT_TEST_AUTHORITY_NOT_ACTUALLY_ISSUED"}
        self.add("profile_authority",authority,self.original_root+"/profile_authority.json")
        self.reseal()

    def reseal(self):
        super().reseal()
        builder=copy.deepcopy(self.data["builder_result"]);paths=builder.pop("serialization_unavailable_numeric_paths",[])
        self.data["pilot_result"]["builder_result"]=builder
        self.data["pilot_result"]=pilot.result_json(self.data["pilot_result"])
        self.data["pilot_result"]["serialization_unavailable_numeric_paths"].extend([["builder_result",*p]for p in paths])

    def write(self):
        records={};common=SOURCE.parent.parent
        for number,name in enumerate(self.data):
            if name.startswith("source/")and not getattr(self,"archive_layout",False):path=self.source/name.removeprefix("source/");assert path.read_bytes()==self.raw(name)
            else:path=self.root/("file"+str(number)+".bin");path.write_bytes(self.raw(name))
            records[name]={"path":path.relative_to(common).as_posix(),"original_path":self.original[name],**self.rec(name)}
        index={"schema":e.INDEX_SCHEMA,"backend":e.CLI,"case_id":self.case_id,"producer_fingerprint":self.producer,
            "files":records,"verifier_sha256":rec(Path(e.__file__).read_bytes())["sha256"],**getattr(self,"index_extra",{})}
        path=self.root/"index.json";path.write_bytes(canonical(index));return path,rec(path.read_bytes())["sha256"],common

    def verify(self):
        path,sha,root=self.write()
        value=e.verify_cli_profile_evidence(path,sha,root,case_id=self.case_id,producer=self.producer,
            profile_authority=self.ref("profile_authority"),semantic_policy=self.ref("semantic_policy"))
        write(self.root/"profile_replay.json",pilot.result_json(value));return value

    def verify_production(self):
        path,sha,root=self.write()
        return e.verify_production_cli_evidence(path,sha,root,case_id=self.case_id,producer=self.producer)

def prepare(root):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=False)
    mapping,origin=materialize(root/"family");evidence=frames.LocalEvidence(mapping,mapping.parent)
    manifest=json.loads(frames.canonical(frames.draft_reference(origin,evidence,assign_identity=True)))
    write(root/"TEST_ONLY_family_manifest.json",manifest)
    cases=[]
    for label,steps,scale,elements in (("candidate",300,1,10000),("reference",300,2,20000),("refined",360,4,40000)):
        case=ProfileCase(root/label,label,steps,scale,elements,mapping.parent,manifest)
        result=case.verify();print(label,result.get("status"),result.get("errors"))
        if result["errors"]:raise ValueError(label+": "+repr(result["errors"]))
        cases.append(case)
    return cases,manifest,evidence

def csv_bytes(rows):
    stream=io.StringIO(newline="");writer=csv.DictWriter(stream,fieldnames=sorted(set().union(*(row.keys()for row in rows))))
    writer.writeheader();writer.writerows(rows);return stream.getvalue().encode("utf-8")

def complete_profile(root):
    """Return a real-verifier-positive TEST graph, not an actual certificate."""
    root=Path(root).resolve();cases,manifest,origin=prepare(root)
    fs=root/"INERT_FS_TRANSCRIPT";fs.mkdir()
    verifier=Path(e.__file__).read_bytes();worker=b'"""Inert FS-authority fixture; no actual job or network execution."""\n'
    verifier_ref=write(fs/"verifier_source_record.json",rec(verifier))
    (fs/"execution_evidence.py").write_bytes(verifier);(fs/"replay_actual_pair.py").write_bytes(worker)
    tasks=[];records={};replays={}
    def published(name,raw):
        path=fs/name;path.write_bytes(raw);records[name]={"path":str(path).replace("\\","/"),**rec(raw)}
    for ordinal,case in enumerate(cases,1):
        task_id=case.data["task_status"]["id"];index_path,index_sha,index_root=case.write();index=json.loads(index_path.read_bytes())
        result=json.loads((case.root/"profile_replay.json").read_bytes());assert result["errors"]==[]
        name="evidence_index_"+str(task_id)+".json";published(name,index_path.read_bytes())
        published("replay_"+str(task_id)+".json",canonical(result));replays[case.case_id]=result
        tasks.append({"task_id":task_id,"case_id":case.case_id,"index_name":name,"index_record":rec(index_path.read_bytes()),
            "expected_records":index["files"],"profile_authority":case.ref("profile_authority"),"semantic_policy":case.ref("semantic_policy")})
    config={"schema":"INERT_TEST_FS_REPLAY_CONFIGURATION","tasks":tasks,
        "payload_records":{"execution_evidence.py":rec(verifier),"replay_actual_pair.py":rec(worker)}}
    config_ref=write(fs/"config.json",config)
    payload={"execution_evidence.py":{"data":base64.b64encode(verifier).decode(),**rec(verifier)},
             "replay_actual_pair.py":{"data":base64.b64encode(worker).decode(),**rec(worker)},
             "config.json":{"data":base64.b64encode((fs/"config.json").read_bytes()).decode(),**rec((fs/"config.json").read_bytes())}}
    request={"scope":"INERT_TEST_NO_REAL_REQUEST","command":"set -e\npython -I -B inert_FS.py --execute-FS --config-sha256 "+config_ref["sha256"]+"\n",
        "env_setup":"python -I -B - <<'DB1_FS_REPLAY_BOOTSTRAP'\nFILES="+repr(payload)+"\nDB1_FS_REPLAY_BOOTSTRAP\n"}
    request_ref=write(fs/"synthetic_request.json",request)
    task_ref=write(fs/"synthetic_task.json",{"id":790000,"status":"completed","exit_code":0,"scope":"INERT_NO_API"})
    receipt_ref=write(fs/"synthetic_submission_receipt.json",{"task_id":790000,"POSTed_once":True,"request_sha256":request_ref["sha256"],"scope":"INERT_CONTROL_GRAPH_NO_REAL_POST"})
    summaries=[]
    for task in tasks:
        replay=replays[task["case_id"]]
        summaries.append({"task_id":task["task_id"],**{k:replay[k]for k in ("case_id","original_status","solver_context_known","solver_state_uncertain",
            "physical_quality_passed","full_saved_closure_passed","resource_4core_proven","training_eligible")}})
    state={"scope":"INERT_TEST_COMPUTED_REPLAYS_NOT_REAL_FS_TASK","Root_index_authority":config_ref["sha256"],
        "verifier_sha256":rec(verifier)["sha256"],"replay_completed":True,"errors":[],"cases":summaries,
        "all_read_whitelisted_input_files_unchanged":True,"original_pair_failed_statuses_preserved":True,"Native_calls":0,"Analyze_calls":0,"FEAs":0}
    published("FS_replay_result.json",canonical(state))
    entries=[]
    for name in list(records):
        raw=(fs/name).read_bytes();gz=gzip.compress(raw,mtime=0);ascii_raw=base64.b64encode(gz)
        published(name+".gz",gz);published(name+".gz.b64",ascii_raw)
        entry={"raw_relative_path":name,"gzip_relative_path":name+".gz","ASCII_relative_path":name+".gz.b64"}
        for label,value in (("raw",raw),("gzip",gz),("ASCII",ascii_raw)):
            entry[label+"_bytes"]=len(value);entry[label+"_sha256"]=rec(value)["sha256"]
        entries.append(entry)
    seal_ref=write(fs/"collection_seal.json",{"files":{k:{x:v[x]for x in("bytes","sha256")}for k,v in records.items()}})
    transport_ref=write(fs/"ASCII_transport_manifest.json",{"entries":entries,"scope":"INERT_LOCAL_LOSSLESS_TRANSPORT"})
    collection_ref=write(fs/"collection_proof.json",{"task_id":790000,"collection_complete":True,"errors":[],"state_errors":[],"scope":"INERT_NO_NETWORK"})
    authority={"task_id":790000,"task":task_ref,"submission":receipt_ref,"request":request_ref,"seal":seal_ref,"transport":transport_ref,
        "published":records,"config":config_ref,"verifier":{"path":str(fs/"execution_evidence.py").replace("\\","/"),**rec(verifier)},
        "worker":{"path":str(fs/"replay_actual_pair.py").replace("\\","/"),**rec(worker)},"collection_proof":collection_ref,
        "scope":"INERT_TEST_AUTHORITY_NOT_ISSUED"}
    binding=write(root/"INERT_capture_binding.json",authority)
    checked=ec.verify_fs_capture(binding,binding["sha256"])
    local_roles={};derived={};views={}
    for case in cases:
        index_path,sha,common=case.write();index=json.loads(index_path.read_bytes())
        local_roles[case.case_id]={name:{"path":str(common/item["path"]).replace("\\","/"),**{k:item[k]for k in("bytes","sha256")}}for name,item in index["files"].items()}
        value=ec.derive_case(checked[case.case_id],local_roles[case.case_id]);value["frame_binding"]=ec.verify_cli_frame(value,manifest,origin)
        derived[case.case_id]=value;views[case.case_id]=ec.comparison_view(value)
    keys={x["physics_key"]for x in derived.values()};assert len(keys)==1,keys;key=next(iter(keys))
    declaration=write(root/"INERT_profile_inputs.json",{"FS_capture":binding,"local_roles":local_roles})
    plan=[views[c.case_id]["plan"]for c in cases];rows=[views[c.case_id]["row"]for c in cases]
    for name,data in (("comparison_plan.csv",csv_bytes(plan)),("comparison_rows.csv",csv_bytes(rows))):(root/name).write_bytes(data)
    ids={c.case_id:views[c.case_id]["profile_id"]for c in cases}
    report=quality.compare_profiles(rows,plan,reference_profile=ids[cases[1].case_id],refined_reference_profile=ids[cases[2].case_id],
        frame_manifest=manifest,frame_evidence=origin,cli_execution=declaration,cli_expected_key=key)
    # Add the same explicit source/input snapshots emitted by the real CLI.
    report["case_plan_file"]={"snapshot":str(root/"comparison_plan.csv"),"sha256":rec((root/"comparison_plan.csv").read_bytes())["sha256"]}
    report["result_files"]=[{"snapshot":str(root/"comparison_rows.csv"),"sha256":rec((root/"comparison_rows.csv").read_bytes())["sha256"]}]
    report["comparison_source_snapshot"]=str(Path(quality.__file__));report["comparison_source_sha256"]=rec(Path(quality.__file__).read_bytes())["sha256"]
    gate=Path(quality.v3_training_row_issues.__code__.co_filename)
    report["metric_gate_source"]={"snapshot":str(gate),"sha256":rec(gate.read_bytes())["sha256"]}
    report_ref=write(root/"INERT_quality_report.json",report)
    selected=quality.selected_profile_manifest(report,root/"INERT_quality_report.json",ids[cases[0].case_id])
    selected_ref=write(root/"INERT_selected_profile.json",selected)
    verified=ec.verify_selected_profile(root/"INERT_selected_profile.json")
    write(root/"INERT_SCOPE.json",{"actual_profile_certification":False,"actual_training_admission":False,"Native_calls":0,"API_calls":0,
        "scope":"full real-validator unit regression using synthetic OS/authority/native graph and genuine declared raw CSV inputs",
        "numerical_source_provenance":str(ASSETS/"PROVENANCE.json"),"synthetic_resource_N":[10000,20000,40000],
        "original_numeric_raw_modified":False,"verified_code_result":verified})
    return {"selected_profile":selected_ref,"selected":selected,"manifest":manifest,"origin_evidence":origin,"family_mapping":origin.path,
        "family_root":origin.root,"cases":cases,"candidate":cases[0],"declaration":declaration,"physics_key":key,"root":root,"verified":verified}

build=complete_profile

def production(profile_result,new_root, *, legal_model_constant=False):
    """NEW inert training graph, never promotion of an actual diagnostic row."""
    from pyaedt_motor import execution_contracts as contracts,doe
    obj=copy.deepcopy(profile_result["candidate"]);obj.root=Path(new_root).resolve();obj.root.mkdir(parents=True,exist_ok=False)
    old_root=obj.original_root;obj.case_id="INERT_LEGAL_SHOE_PRODUCTION_CASE"if legal_model_constant else"INERT_NEW_PRODUCTION_TRAINING_CASE"
    obj.original_root="/recorded/inert_new_production";obj.project=obj.project.replace(old_root,obj.original_root)
    obj.archive_layout=True
    for role,original in list(obj.original.items()):
        if original.startswith(old_root):obj.original[role]=original.replace(old_root,obj.original_root,1)
    case=obj.data["case"];case.update(case_id=obj.case_id,beta_calibration_id=profile_result["manifest"]["reference_frame_id"],
        electrical_zero_deg=profile_result["manifest"]["electrical_zero_deg"],initial_position_deg=profile_result["manifest"]["initial_position_deg"],
        **frames.frame_fields(profile_result["manifest"]))
    if legal_model_constant:
        # A NEW explicitly inert production input, never relabel the genuine
        # diagnostic G1 row. Source geometry does not reference this inactive
        # saved variable; the public model's fixed1.5 gate is retained.
        original_active=doe.physical_geometry_hash(case)
        case["stator_shoe_thick"]=1.5
        # This NEW synthetic declared input is representable by the optimizer's
        # existing g6 model-depth boundary. The genuine diagnostic case is not.
        original_stack=case["stack_length_mm"]
        case["stack_length_mm"]=float(f"{float(original_stack):.6g}")
        from ipmsm_optimization import WindingSpec,phase_resistance_100c_ohm
        winding=WindingSpec(**json.loads((SOURCE/"configs/ipmsm_7p5kw.json").read_bytes())["winding"])
        case["phase_resistance_ohm"]=phase_resistance_100c_ohm(case,case["stack_length_mm"],winding)
        assert doe.physical_geometry_hash(case)==original_active
        for role in ("prepared_full","project"):
            text=obj.raw(role).decode("utf-8-sig")
            text,count=re.subn(r"(VariableProp\('stator_shoe_thick', 'UD', '', ')[^']+(')",lambda m:m[1]+"1.5"+m[2],text)
            assert count==1
            obj.data[role]=text.encode("utf-8")
        for role,parsed in (("prepared_full","prepared"),("project","solved")):
            path=obj.root/(role+".aedt");path.write_bytes(obj.raw(role))
            obj.data[parsed]=pilot.saved_native(path)
        obj.fixture_inactive_constant_scope={"old_genuine_value":profile_result["candidate"].data["case"]["stator_shoe_thick"],
            "new_test_only_value":1.5,"active_geometry_hash_unchanged":True,
            "original_declared_stack_mm":original_stack,"new_declared_stack_mm":case["stack_length_mm"],
            "genuine_numeric_bytes_unchanged":True,"actual_original_case_relabelled":False}
    settings=profile_result["selected"]["settings"]
    for name,value in settings.items():
        if name=="mesh_elements":
            for group,count in value.items():case["mesh_"+group+"_elements"]=count
        else:case[name]=value
    design=doe.physical_geometry_hash(case);case.update(geometry_group_id="v3geo_"+design,design_hash=design,doe_split="train")
    obj.spec=runner.build_spec(case)
    if legal_model_constant:
        # Recompute this synthetic case's derived evidence from unchanged CSVs.
        # No actual output is patched or relabelled as the new case.
        mapping={}
        report_root=obj.root/"reports";report_root.mkdir()
        for key,original in obj.data["pilot_result"]["postprocess"]["report_mapping"].items():
            role="report/"+key.removeprefix("artifact_report_")
            path=report_root/(role.rsplit("/",1)[1]+".csv");path.write_bytes(obj.raw(role));mapping[key]=str(path)
        from module.ipmsm_waveform_quality import analyze_exported_waveforms
        summary=runner.summarize_transient_outputs(mapping,obj.spec,operation="sin_current")
        summary.update(analyze_exported_waveforms(mapping,obj.spec,"sin_current"));runner.add_derived_motor_metrics(summary,obj.spec)
        pin,power,loss=[runner.finite_float(summary[k])for k in("output_electrical_input_last_w","output_mech_power_last_w","output_total_loss_last_avg_w")]
        summary.update(output_power_balance_residual_last_w=pin-power-loss,output_power_balance_residual_last_pct=100*(pin-power-loss)/max(abs(pin),1.))
        obj.data["pilot_result"]["postprocess"]["summary"]=summary
    obj.data["setup_audit"]["requested_spec"]=asdict(obj.spec)
    builder=obj.data["builder_result"]
    builder.update(e._production_expected_inputs(runner,case,obj.spec))
    for key,value in list(builder.items()):
        if key.startswith("input_")and isinstance(value,(dict,list,tuple)):builder[key]=str(value)
    builder.update(case_id=obj.case_id,project_path=str(PurePosixPath(obj.project).parent),simulation_name="simulation1",
        artifact_setup_audit=obj.original["setup_audit"],**frames.frame_fields(profile_result["manifest"]))
    for key in runner.CASE_METADATA_COLUMNS:builder[key]=case.get(key,"")
    cleanup=json.loads(builder["standalone_cleanup"])
    cleanup["client_retirement"].update(new_python_client=True,sdk_version="0.22.0",client_retired=True,errors=[],Native_exit_implied=False)
    builder["standalone_cleanup"]=json.dumps(cleanup)
    # Full ordinary numeric dependency closure and explicitly required public
    # modules. This is a recorded test source snapshot, not a live-path lease.
    source_records={};source_root="/recorded/inert_producer_source"
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file()or"__pycache__"in path.parts:continue
        name=path.relative_to(SOURCE).as_posix()
        if "tests"in path.relative_to(SOURCE).parts:continue
        if not(name.endswith(".py")or name.startswith("configs/")or name.startswith("module/")and name.endswith(".csv")):continue
        raw=path.read_bytes();obj.add("source/"+name,raw,source_root+"/"+name);source_records[name]=rec(raw)
        # Profile comparison metadata deliberately retains the current C
        # provenance strings. Archive aliases are separate byte copies.
        obj.add("source_alias/"+name,raw,str(path.resolve()).replace("\\","/"))
        if str(path.resolve())!=str(path.resolve()).replace("\\","/"):
            obj.add("source_alias_backslash/"+name,raw,str(path.resolve()))
    assert set(contracts.REQUIRED_SOURCE).issubset(source_records)
    before=obj.data["installation_before"];install=obj.data["installation_binding"]
    roles={"interpreter":before["interpreter_exe"],"static_registry":before["static_registry"],
        "setup_mwuser":install["install_root"]+"/mainwin561/Linux64/mw/setup-mwuser",
        "setup_mwarch":install["install_root"]+"/mainwin561/Linux64/mw/lib/setup-mwarch.sh"}
    inventory={name:{"path":path,**before["known_file_records"][path]}for name,path in roles.items()}
    inventory["core_libraries"]=[{"path":path,**record}for path,record in before["known_file_records"].items()
        if PurePosixPath(path).parent==PurePosixPath(install["install_root"])and PurePosixPath(path).name.startswith("libngcore")]
    obj.add("installation_inventory",inventory,obj.original_root+"/installation_inventory.json")
    # Capture the complete profile/origin closure by file, preserving all path
    # spellings present in immutable documents; aliases use distinct byte copies.
    profile_root=profile_result["root"]
    closure={}
    for path in profile_root.rglob("*"):
        if path.is_file():
            closure[str(path.resolve())]=path
            closure[str(path.resolve()).replace("\\","/")]=path
    for number,(original,path)in enumerate(sorted(closure.items())):
        obj.add("profile_closure/"+str(number),path.read_bytes(),original)
    manifest_ref={"path":str(profile_root/"TEST_ONLY_family_manifest.json").replace("\\","/"),**rec((profile_root/"TEST_ONLY_family_manifest.json").read_bytes())}
    mapping_ref={"path":str(profile_result["family_mapping"]).replace("\\","/"),**rec(Path(profile_result["family_mapping"]).read_bytes())}
    contract={"schema":contracts.SCHEMA,"mode":"solve","purpose":"training","stage":"training","backend":e.CLI,
        "case_id":obj.case_id,"case":obj.ref("case"),"source_root":source_root,"source_files":source_records,
        "library_root":"/recorded/library","library_files":obj.data["contract"]["library_files"],
        "system_material_files":obj.data["contract"]["system_material_files"],"installation_binding":obj.ref("installation_binding"),
        "installation_inventory":obj.ref("installation_inventory"),"expected_producer":obj.producer,"cli_policy":copy.deepcopy(contracts.CLI_POLICY),
        "options":copy.deepcopy(contracts.OPTION_POLICY),"node":"n113","account":"r1jae262",
        "reference_frame":{"manifest":manifest_ref,"local_evidence":{"mapping":mapping_ref,"root":str(profile_result["family_root"])}},
        "selected_profile":profile_result["selected_profile"],"reservation_receipt":None,"required_observed_cores":[4],"semantic_policy":obj.ref("semantic_policy")}
    assert set(contract)==contracts.CONTRACT_KEYS
    ledger_row={"case_id":obj.case_id,"stage":"training","budget_attempt":1,"campaign_id":"INERT_TEST_CAMPAIGN",
        "reservation_token":"INERT_NOT_A_REAL_RESERVATION","expected_producer":obj.producer,"input_case_sha256":obj.rec("case")["sha256"],
        "scope":"INERT_REGRESSION_ONLY_NO_REAL_LEDGER"}
    obj.add("reservation_ledger_snapshot",canonical(ledger_row)+b"\n",obj.original_root+"/reservation_ledger.jsonl")
    receipt={"execution_authorized":True,"production_admission_authorized":True,"case_id":obj.case_id,"producer_fingerprint":obj.producer,
        "category":"training","stage":"training","reserved_attempt":1,"budget_attempt":1,"case_sha256":obj.rec("case")["sha256"],
        "execution_contract_sha256":contracts.contract_sha256(contract),"reservation_ledger_snapshot":obj.ref("reservation_ledger_snapshot"),
        "ledger_record_sha256":contracts._hash(ledger_row),"campaign_id":ledger_row["campaign_id"],"reservation_token":ledger_row["reservation_token"],
        "scope":"INERT_SYNTHETIC_AUTHORITY_NOT_ACTUALLY_ISSUED"}
    obj.data["receipt"]=receipt;contract["reservation_receipt"]=obj.ref("receipt");obj.data["contract"]=contract
    command=pilot.batch_command(install["wrapper_path"],Path(obj.project),Path(obj.original_root),"n113",obj.spec.setup_name)
    command=[str(x).replace("\\","/")for x in command];obj.data["command"]=command
    owner=obj.data["owner"];native=owner["Native_processes"][0];native["cmdline"]=[install["native_exe"],*command[1:]]
    identity=copy.deepcopy(owner["owned_processes"][0]["last_verified_identity"])
    identity["cmdline"]=[install["interpreter"],install["wrapper_path"],*command[1:]]
    owner["owned_processes"]=[obj.process(identity),obj.process(native)]
    obj.data["cli_intent"].update(case_id=obj.case_id,command=obj.rec("command"),reservation_receipt=contract["reservation_receipt"],
        prepared_project=obj.rec("prepared_full"),prepared_unsolved_project=obj.ref("prepared_full"))
    obj.data["cli_completion"]["case_id"]=obj.case_id
    obj.data["pilot_result"].update(case_id=obj.case_id,prepared_project={"path":obj.project,**obj.rec("prepared_full")},
        prepared_unsolved_project=obj.ref("prepared_full"),installation_observation_before=obj.ref("installation_before"),
        installation_observation_after=obj.ref("installation_after"),public_setup_evidence=obj.rec("setup_audit"),
        execution_code_identity={"coordinator":source_records[ec.COORDINATOR],"OS_owner":source_records[ec.OWNER]})
    for key,original in obj.data["pilot_result"]["postprocess"]["report_mapping"].items():
        obj.data["pilot_result"]["postprocess"]["report_mapping"][key]=original.replace(old_root,obj.original_root,1)
    # Semantic truth is recomputed on the same declared before/after graph.
    from pyaedt_motor import native_state_semantics as semantics
    left=obj.root/"before.aedt";right=obj.root/"after.aedt";left.write_bytes(obj.raw("prepared_full"));right.write_bytes(obj.raw("project"))
    closure=semantics.compare_states(semantics.read_state(left),semantics.read_state(right),prepared_record=obj.rec("prepared_full"),
        solved_record=obj.rec("project"),renderer_producer=obj.producer,policy_bytes=obj.raw("semantic_policy"),policy_sha256=obj.rec("semantic_policy")["sha256"])
    assert closure["semantic_equivalence_proven"]is True and closure["errors"]==[]
    obj.data["pilot_result"]["raw_saved_serialization_comparison"]=obj.data["pilot_result"]["saved_serialization_comparison"]
    obj.data["pilot_result"]["saved_serialization_comparison"]={**obj.data["pilot_result"]["saved_serialization_comparison"],"semantic_equivalence_proven":True}
    obj.data["pilot_result"]["same_instance_semantic_comparison"]=closure
    obj.data["source_inputs"]={"source/"+n:r for n,r in source_records.items()}|{"library/"+n:r for n,r in contract["library_files"].items()}
    obj.data["source_inputs"].update({"syslib/"+n:r for n,r in contract["system_material_files"].items()})
    obj.data["source_inputs"].update({k:contract[k]for k in("case","installation_binding","reservation_receipt")})
    obj.data["task_status"]={"id":780000,"status":"completed","exit_code":0,"scope":"INERT_PRODUCTION_CASE_NO_API"}
    obj.index_extra={"archive_verifier_sources":{name:rec((SOURCE/"src/pyaedt_motor"/name).read_bytes())for name in("execution_archive.py","archive_numeric.py")}}
    obj.reseal();return obj

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--output",type=Path,required=True)
    complete_profile(parser.parse_args().output)
