"""Synthetic serialized projects plus immutable local bundles; no AEDT imports or calls."""
import copy
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

import calibrate_ipmsm_beta as calibration
from pyaedt_motor import reference_frames as frames
from pyaedt_motor.doe import physical_geometry_hash
from tests.test_calibrate_ipmsm_beta import source_row, zero_result, reference_context


def block(tag, rows=(), children=()):
    return "\n".join([f"$begin '{tag}'", *rows, *children, f"$end '{tag}'"])


def properties(values):
    return [f"{repr(key) if ' ' in key else key}={str(value).lower() if type(value) is bool else repr(value)}"
            for key, value in values.items()]


def saved_project(planned):
    parts, coils, coordinate_systems = [], [], []
    materials = {"stator": "M19_29G_reference_y2", "rotator": "M19_29G_reference_y2", "shaft": "vacuum",
                 "Region": "air", "Band": "air", "magnet_space": "air"}
    for i in range(1, 25):
        name = f"winding{i}"; materials[name] = "copper"
        phase, polarity = frames.SECTOR[(i-1) % 6]
        coils.append(block(f"{phase}_{polarity}_{i:02d}", properties({"BoundType": "Coil", "Conductor number": "12",
            "Winding": {"PhaseA": 25, "PhaseB": 26, "PhaseC": 27}[phase], "PolarityType": polarity}) + [f"Objects({i})"]))
    for pole in "NS":
        for i in range(1, 5):
            name = f"magnet_{pole}_{i:02d}"; materials[name] = "NdFeB_1.25T"
            angle = math.radians((i-1)*90 + (45 if pole == "S" else 0)); sign = -1 if pole == "S" else 1
            x, y = sign*math.cos(angle), sign*math.sin(angle)
            values = {"OriginX": f"{40*math.cos(angle)}mm", "OriginY": f"{40*math.sin(angle)}mm", "OriginZ": "0mm",
                      "XAxisXvec": f"{x}mm", "XAxisYvec": f"{y}mm", "XAxisZvec": "0mm",
                      "YAxisXvec": f"{-y}mm", "YAxisYvec": f"{x}mm", "YAxisZvec": "0mm"}
            coordinate_systems.append(block("Operation", properties({"ID": len(materials)+100,
                "ReferenceCoordSystemID": 1}), [block("Attributes", properties({"Name": name+"_PM_CS"})),
                block("RelativeCSParameters", properties(values))]))
    body_ids = {name: int(name[7:]) if name.startswith("winding") else index+1000
                for index, name in enumerate(materials)}
    for name, material in materials.items():
        cs = len(list(materials)[:list(materials).index(name)+1])+100 if name.startswith("magnet_") and name != "magnet_space" else 1
        parts.append(block("GeometryPart", children=[block("Attributes", properties({"Name": name,
            "MaterialValue": '"'+material+'"', "PartCoordinateSystem": cs})), block("Operations", children=[
            block("Operation", properties({"ParentPartID": body_ids[name]}), [
                block("OperationIdentity", properties({"BodyID": body_ids[name]}))])])]))
    phase_nodes = [block(name, properties({"BoundType": "Winding Group", "ID": 25+i, "IsSolid": False,
                  "Current": expression, "ParallelBranchesNum": "1"})) for i, (name, expression) in enumerate(frames.PHASE_CURRENT.items())]
    variables = {key: planned[key[6:]] for key in source_row() if key.startswith("input_") and key[6:] in planned}
    variables = {key[6:]: value for key, value in variables.items()}
    variables.update(pole_num=8, slot_num=12, NumPoles="pole_num", NumSlots="slot_num", SymmetryFactor=1,
        ElectricalZero=f"{float(planned['electrical_zero_deg']):g}deg", InitialPositionMD="-22.5deg",
        InitialPositionWrapped="337.5deg", StackLength=f"{float(planned['stack_length_mm']):g}mm")
    var_rows = [f"VariableProp('{key}', 'UD', '', '{value}')" for key, value in variables.items()]
    geometry = block("GeometryCore", properties({"Units": "mm"}), [block("GeometryOperations", children=[
        block("CoordinateSystems", children=coordinate_systems), block("ToplevelParts", children=parts)])])
    motion = block("MotionSetupList", children=[block("MotionSetup1", properties({**frames.MOTION, "Coordinate System": 1, "ID": 0})),
        block("Moving1", properties({"BandName": 0}) + ["Objects("+", ".join(str(body_ids[name]) for name in materials
            if name in {"shaft", "rotator", "magnet_space"} or name.startswith("magnet_"))+")"])])
    pm = block("NdFeB_1.25T", properties({"permeability": "1.05", "conductivity": "909090", "mass_density": "7500"}), [
        block("magnetic_coercivity", properties({"Magnitude": "947000A_per_meter", "DirComp1": "1", "DirComp2": "0", "DirComp3": "0"}))])
    model = block("Maxwell2DModel", properties({"Name": "IPMSM", "SolutionType": "Transient", "GeometryMode": "XY",
        "Multiplier": "SymmetryFactor", "ModelDepth": "StackLength"}), [block("ModelSetup", children=[block("Properties", var_rows), geometry, motion]),
        block("BoundarySetup", children=[block("Boundaries", children=coils+phase_nodes)])])
    return block("AnsoftProject", children=[model, block("Definitions", children=[block("Materials", children=[pm])])]).encode()


class Fixture:
    def __init__(self, root):
        self.root = root
        self.mapping = {"schema_version": frames.EVIDENCE_SCHEMA, "origin_cases": {}, "case_audits": {}}
        raw_source = {name: b"# synthetic unit source\n" for name in frames.PHYSICAL_FILES}
        raw_source["run_ipmsm_batch.py"] = b"def build_spec(case):\n    return IPMSMPPTSpec(pole_number=8)\n"
        raw_source["module/ipmsm_ppt_setup.py"] = b"def apply_ppt_design_variables(design, spec):\n    return spec\n"
        self.source_seal = {"schema": "fixture", "files": {}, "library_files": {}}
        self.mapping["physical_files"] = {name: self.put("source/"+name, raw) for name, raw in raw_source.items()}
        for name, record in self.mapping["physical_files"].items():
            self.source_seal["files"][name] = self.semantic(record)
        pack = self.put("source/configs/material_m19_29g_reference_y2.json", b'{"unit_fixture":true}')
        self.mapping["material_pack"] = pack
        self.source_seal["files"]["configs/material_m19_29g_reference_y2.json"] = self.semantic(pack)
        self.mapping["library_files"] = {f"pyaedt_module/file{i}.py": self.put(f"library/pyaedt_module/file{i}.py", b"# fixture\n") for i in range(23)}
        self.source_seal["library_files"] = {name: self.semantic(record) for name, record in self.mapping["library_files"].items()}
        self.mapping["source_seal"] = self.put("source_seal.json", self.json(self.source_seal))
        self.producer = "producer_v3:sha256:" + frames.digest({name: value["sha256"] for name, value in self.mapping["physical_files"].items()})
        self.rows = []
        for speed in (1200, 5000):
            row = {**source_row(), **zero_result(speed, -91, speed/100), "input_source_case_id": "fixture_origin"}
            # Local mapping JSON sorts keys; measured low/high order deliberately differs.
            row["case_id"] = "z_low" if speed == 1200 else "a_high"
            row.update({"input_"+key: value for key, value in reference_context().items()})
            row.update(input_beta_dq_deg=0, input_beta_calibration_id="", input_metric_contract_version="motor_metrics_v3",
                input_producer_fingerprint=self.producer, output_back_emf_source="periodic_flux_derivative", output_steady_state_passed=True,
                validation=True, analyze=True, input_material_fingerprint="materials_v2:sha256:"+"b"*64)
            row.update({"input_"+key: value for key, value in {"magnet_material":"NdFeB_1.25T", "shaft_material":"vacuum",
                "winding_material":"copper", "air_material":"air"}.items()})
            self.add_case(row, "origin_cases", 100+speed)
            self.rows.append(row)
        self.origin = calibration.analyze_zero_calibration_rows(self.rows)

    @staticmethod
    def semantic(record):
        return {key: record[key] for key in ("bytes", "sha256")}

    @staticmethod
    def json(value):
        return json.dumps(value, sort_keys=True).encode()

    def put(self, relative, raw):
        path = self.root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        return {"path": relative, "bytes":len(raw), "sha256":hashlib.sha256(raw).hexdigest()}

    def csv(self, row):
        out = io.StringIO(newline=""); writer = csv.DictWriter(out, fieldnames=list(row)); writer.writeheader(); writer.writerow(row)
        return out.getvalue().encode()

    def add_case(self, row, section, task):
        planned = {key[6:]: value for key,value in row.items() if key.startswith("input_")}
        planned.update(case_id=row["case_id"], design_hash=physical_geometry_hash(planned))
        row["design_hash"] = planned["design_hash"]
        if section == "case_audits":
            planned.update(reference_frame_method=frames.METHOD, reference_frame_id=row["input_beta_calibration_id"])
        root = "cases/"+row["case_id"]; files = {}
        files["input"] = self.put(root+"/input.csv", self.csv(planned))
        identity = [123, 100.5, "posix", [1000,1000], "ansysedt"]
        row.update(standalone_solver_state_contract="ipmsm_standalone_solver_state_v1", standalone_solver_uncertain=False,
            standalone_analyze_return_received=True, standalone_analyze_return_type="builtins.bool", standalone_analyze_return_repr="True",
            artifact_standalone_solver_state="remote/intent.json", standalone_owned_pid="123", standalone_owner_identity=json.dumps(identity),
            standalone_cleanup=json.dumps({"verified":True,"owned_pid":123,"process_exited":True,"errors":[]}))
        files["result"] = self.put(root+"/result.csv", self.csv(row))
        spec = {**reference_context(), "model_extent":"full_360", "symmetry_factor":1, "pole_number":8,"slot_number":12,
            "beta_convention":frames.CONVENTION, "electrical_zero_deg":float(planned["electrical_zero_deg"]),
            "initial_position_deg":-22.5, "stack_length_mm":float(planned["stack_length_mm"]),
            **{key:planned[key] for key in ["shaft_material","winding_material","magnet_material","air_material"]}}
        depth = {"passed":True,"model_units_live":"mm","native_depth_expression":"StackLength",
                 "native_depth_SI_m":float(f"{spec['stack_length_mm']:g}")/1000}
        native = saved_project(planned); observed = frames.observe_native(native)
        setup = {"validation":True,"analysis":True,"setup_issues":[],"requested_spec":spec,
            "material_definitions":{"material_pack_sha256":self.mapping["material_pack"]["sha256"],"bh_curve_points":23,
                "native_readback_verified":True,"native_definition_unchanged_by_cache_bridge":True},
            "materials":{"errors":[],"material_readback":{name:value.strip('"') for name,value in observed["material_assignments"].items()}},
            "winding_geometry_audit":{"passed":True,"count":24,**{key:reference_context()[key] for key in
                ["turns_per_coil_side","conductor_area_per_branch_mm2","slot_fill_limit"]}}}
        setup.update({name:depth for name in ["native_model_depth_audit","native_model_depth_audit_post_validation","native_model_depth_audit_pre_analysis"]})
        files["setup"] = self.put(root+"/setup.json", self.json(setup)); files["project"] = self.put(root+"/project.aedt", native)
        files["source_seal"] = self.put(root+"/source_seal.json", self.json(self.source_seal))
        execution = {"case_id":row["case_id"],"producer_fingerprint":self.producer,"phase":"complete","runner_exit_code":0,
            "solver_context_known":True,"solver_state_uncertain":False,"cases_sha256":files["input"]["sha256"],
            "source_sha256":files["source_seal"]["sha256"]}
        files["execution"] = self.put(root+"/execution.json", self.json(execution))
        files["task"] = self.put(root+"/task.json", self.json({"task_id":task,"status":"completed","exit_code":0}))
        seal = {"files":{Path(record["path"]).relative_to(root).as_posix():self.semantic(record) for name,record in files.items() if name != "task"}}
        files["seal"] = self.put(root+"/collection_seal.json", self.json(seal))
        self.mapping[section][row["case_id"]] = {"collection_root":root,"task_id":task,"files":files}

    def evidence(self):
        self.put("mapping.json", self.json(self.mapping))
        return frames.LocalEvidence(self.root/"mapping.json", self.root)


class ReferenceFrameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.fixture = Fixture(Path(self.tmp.name))

    def issued_fixture(self):
        evidence = self.fixture.evidence()
        return frames.draft_reference(self.fixture.origin, evidence, assign_identity=True)

    def test_default_dry_origin_has_no_id_and_does_not_relabel_producer(self):
        draft = frames.draft_reference(self.fixture.origin, self.fixture.evidence())
        self.assertIsNone(draft["reference_frame_id"])
        self.assertEqual(draft["producer_fingerprint"], self.fixture.producer)
        self.assertFalse(any(draft["claims"].values()))

    def test_cross_geometry_frame_is_explicit_and_old_strict_path_rejects(self):
        manifest = self.issued_fixture()
        row = copy.deepcopy(self.fixture.rows[0]); row.update(case_id="target", input_stator_outer_radius="149",
            input_beta_calibration_id=manifest["reference_frame_id"], input_electrical_zero_deg=manifest["electrical_zero_deg"])
        self.fixture.add_case(row, "case_audits", 900)
        admission = frames.verify_rows([row], manifest, self.fixture.evidence())
        self.assertEqual(admission["admitted_cases"], 1)
        self.assertNotEqual(row["design_hash"], self.fixture.origin["design_hash"])
        with self.assertRaisesRegex(ValueError, "method"):
            calibration.validated_zero_manifest(manifest)
        from tests.test_calibrate_ipmsm_beta import reference_beta_fixture
        old, _, old_rows = reference_beta_fixture()
        old_rows[0]["design_hash"] = "different-geometry"
        with self.assertRaisesRegex(ValueError, "geometry"):
            calibration.analyze_beta_sweep_rows(old_rows, calibration_manifest=old)

    def test_scope_origin_pending_material_and_producer_mismatches_reject(self):
        for key, value in [("producer_fingerprint", "producer_v3:sha256:"+"c"*64),
                           ("material_fingerprint", "wrong"), ("successful_speeds_rpm", [1200,1200])]:
            with self.subTest(key=key):
                wrong=copy.deepcopy(self.fixture.origin);wrong[key]=value
                unhashed=dict(wrong);unhashed.pop("calibration_id");wrong["calibration_id"]="beta-calibration:sha256:"+frames.digest(unhashed)
                with self.assertRaises(ValueError): frames.draft_reference(wrong,self.fixture.evidence())
        entry = next(iter(self.fixture.mapping["origin_cases"].values()))
        task = self.fixture.root / entry["files"]["task"]["path"]
        entry["files"]["task"] = self.fixture.put(task.relative_to(self.fixture.root).as_posix(), self.fixture.json({"task_id":1300,"status":"queued","exit_code":None}))
        with self.assertRaisesRegex(ValueError,"completed"): frames.draft_reference(self.fixture.origin,self.fixture.evidence())

    def test_native_phase_motion_and_pm_assignment_are_observations(self):
        entry=next(iter(self.fixture.mapping["origin_cases"].values())); raw=(self.fixture.root/entry["files"]["project"]["path"]).read_bytes()
        for old,new in [(b"PolarityType='Negative'",b"PolarityType='Positive'"), (b"'Is Positive'=true",b"'Is Positive'=false"),
                        (b"Magnitude='947000A_per_meter'",b"Magnitude='-947000A_per_meter'"),
                        (b"Current='IdCommand*cos",b"Current='-IdCommand*cos")]:
            with self.subTest(old=old), self.assertRaises(ValueError): frames.observe_native(raw.replace(old,new,1))

    def test_mapping_drift_and_wrong_row_audit_fail_before_training(self):
        manifest=self.issued_fixture();row=copy.deepcopy(self.fixture.rows[0]);row.update(case_id="target",
            input_beta_calibration_id=manifest["reference_frame_id"],input_electrical_zero_deg=manifest["electrical_zero_deg"])
        self.fixture.add_case(row,"case_audits",900);evidence=self.fixture.evidence()
        wrong=copy.deepcopy(row);wrong["input_i_peak_a"]="100"
        with self.assertRaisesRegex(ValueError,"row is not"): frames.verify_rows([wrong],manifest,evidence)
        target=evidence.path;evidence.mapping["case_audits"]={}
        with self.assertRaisesRegex(ValueError,"mapping changed"): evidence.assert_unchanged()
        with self.assertRaisesRegex(ValueError,"explicit source-spec"): frames.verify_spec_rows({},[row],self.fixture.evidence())

    def test_renderer_transfer_requires_unchanged_coordinate_source_not_same_producer(self):
        renderer=copy.deepcopy({key:self.fixture.mapping[key] for key in ["source_seal","physical_files","library_files","material_pack"]})
        name="run_ipmsm_batch.py"; record=renderer["physical_files"][name]
        changed=(self.fixture.root/record["path"]).read_bytes()+b"\n# mesh metadata only\n"
        renderer["physical_files"][name]=self.fixture.put("renderer/run_ipmsm_batch.py",changed)
        seal=copy.deepcopy(self.fixture.source_seal);seal["files"][name]=self.fixture.semantic(renderer["physical_files"][name])
        renderer["source_seal"]=self.fixture.put("renderer/seal.json",self.fixture.json(seal));self.fixture.mapping["renderer"]=renderer
        draft=frames.draft_reference(self.fixture.origin,self.fixture.evidence())
        self.assertNotEqual(draft["origin_producer_fingerprint"],draft["producer_fingerprint"])
        self.assertIsNone(draft["reference_frame_id"])
        key="module/ipmsm_geometry.py";renderer["physical_files"][key]=self.fixture.put("renderer/geometry.py",b"phase_axis = 1\n")
        seal["files"][key]=self.fixture.semantic(renderer["physical_files"][key]);renderer["source_seal"]=self.fixture.put("renderer/seal.json",self.fixture.json(seal))
        with self.assertRaisesRegex(ValueError,"coordinate closure"): frames.draft_reference(self.fixture.origin,self.fixture.evidence())

    def test_doe_requires_frame_bound_profile_and_exports_distinct_flat_fields(self):
        from pyaedt_motor import doe
        from ipmsm_optimization import optimization_spec_from_mapping
        from tests.test_ipmsm_v3_doe import V3DoeTests
        fixture = V3DoeTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        manifest = self.issued_fixture(); evidence = self.fixture.evidence()
        raw = frames.apply_reference(fixture.raw, manifest, evidence)
        spec = optimization_spec_from_mapping(raw)
        document = copy.deepcopy(fixture.profile_document)
        document["producer_fingerprint"] = manifest["producer_fingerprint"]
        document["material_fingerprint"] = manifest["material_fingerprint"]
        with self.assertRaisesRegex(ValueError,"reference quality evidence"):
            frames.profile_context(fixture.profile, manifest)
        report_path = fixture.root/"comparison.json"
        report_path.write_text(json.dumps({"reference_frame_context":frames.semantic_context(manifest)}))
        document["evidence"][0]["sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
        fixture.profile_path.write_text(json.dumps(document))
        profile = doe.load_selected_profile(fixture.profile_path)
        rows, plan = doe.generate_training_plan(spec, manifest, profile, geometries=5, seed=123, frame_evidence=evidence)
        self.assertGreater(len({row["design_hash"] for row in rows}), 1)
        self.assertTrue(all(row["reference_frame_method"]==frames.METHOD and
            row["reference_frame_id"]==manifest["reference_frame_id"] and row["beta_calibration_id"]==manifest["reference_frame_id"] for row in rows))
        self.assertEqual(plan["reference_frame_context"],frames.semantic_context(manifest))
        wrong=copy.deepcopy(rows);wrong[0]["reference_frame_id"]="wrong"
        with self.assertRaisesRegex(ValueError,"plan family reference"):
            doe.write_training_plan(fixture.root/"must_not_publish.csv",wrong,plan)

    def test_quality_preserves_numerical_failure_and_training_rejects_wrong_audit_before_fit(self):
        import pandas as pd
        from pyaedt_motor import model_bundle, quality
        manifest=self.issued_fixture();row=copy.deepcopy(self.fixture.rows[0]);row.update(case_id="target",
            input_beta_calibration_id=manifest["reference_frame_id"],input_electrical_zero_deg=manifest["electrical_zero_deg"],
            input_stator_outer_radius="149",input_i_peak_a="100",input_beta_dq_deg="30",input_operation="sin_current")
        self.fixture.add_case(row,"case_audits",900);evidence=self.fixture.evidence()
        planned=evidence.bundle("case_audits","target")["input"][0]
        report=quality.compare_profiles([row],[planned],reference_profile="reference_ultra",frame_manifest=manifest,frame_evidence=evidence)
        self.assertEqual(report["reference_frame_context"]["admitted_cases"],1)
        self.assertFalse(any(profile["production_profile_eligible"] for profile in report["profiles"]))
        spec=json.loads((Path(__file__).parents[1]/"configs/ipmsm_7p5kw.json").read_bytes())
        spec.update(reference_frame=manifest,beta_calibration={"electrical_zero_deg":manifest["electrical_zero_deg"],
            "calibration_id":manifest["reference_frame_id"],"convention":frames.CONVENTION})
        wrong=copy.deepcopy(row);wrong["input_stator_outer_radius"]="148"
        with self.assertRaisesRegex(ValueError,"row is not"):
            model_bundle.domain_from_training(pd.DataFrame([wrong]),spec,case_frame=pd.DataFrame([wrong]),frame_evidence=evidence)
        context=frames.verify_spec_rows(spec,[row],evidence)
        with self.assertRaisesRegex(ValueError,"duplicate"):
            frames.verify_rows([row,row],manifest,evidence)
        frames.verify_domain({"reference_frame_context":context},manifest,evidence)
        with self.assertRaisesRegex(ValueError,"case-audit mapping"):
            frames.verify_domain({"reference_frame_context":{**context,"case_audits_sha256":"wrong"}},manifest,evidence)

    def test_optimizer_replays_family_profile_and_emits_explicit_reference_context(self):
        from pyaedt_motor import optimize_physical as optimization
        from tests.test_ipmsm_physical_optimizer import validation_profile, baseline
        from run_ipmsm_batch import build_spec, setup_fingerprint
        manifest=self.issued_fixture();row=copy.deepcopy(self.fixture.rows[0]);row.update(case_id="training_target",
            input_beta_calibration_id=manifest["reference_frame_id"],input_electrical_zero_deg=manifest["electrical_zero_deg"],
            input_stator_outer_radius="149",input_i_peak_a="100",input_beta_dq_deg="30",input_operation="sin_current")
        self.fixture.add_case(row,"case_audits",901);evidence=self.fixture.evidence()
        old,path,document=validation_profile(self.fixture.root)
        raw=frames.apply_reference(old.source_spec,manifest,evidence)
        context=frames.verify_spec_rows(raw,[row],evidence)
        bundle=old.bundle;fps=copy.deepcopy(old.fingerprints)
        fps.update(input_producer_fingerprint=manifest["producer_fingerprint"],input_beta_calibration_id=manifest["reference_frame_id"])
        settings=document["settings"];flat={key:value for key,value in settings.items() if key!="mesh_elements"}
        flat.update({f"mesh_{key}_elements":value for key,value in settings["mesh_elements"].items()})
        replay={**baseline(),**reference_context(),**flat,"electrical_zero_deg":manifest["electrical_zero_deg"]}
        fps["input_setup_fingerprint"]=setup_fingerprint(build_spec(replay),settings["quality_profile"],"sin_current",False)
        bundle.metadata.update(fingerprints=fps,spec_sha256=optimization._digest(raw))
        bundle.metadata["domain"]["reference_frame_context"]=context
        evidence_file=path.parent/document["evidence"][0]["path"]
        evidence_file.write_text(json.dumps({"reference_frame_context":frames.semantic_context(manifest)}))
        document["producer_fingerprint"]=manifest["producer_fingerprint"]
        document["evidence"][0]["sha256"]=hashlib.sha256(evidence_file.read_bytes()).hexdigest()
        path.write_text(json.dumps(document))
        bound=optimization.PhysicalBundleAdapter(bundle,raw,fps,manifest,frame_evidence=evidence)
        report=optimization.search_designs(bound,[baseline()],selected_profile=path)
        self.assertTrue(report["fea_validation_rows"])
        self.assertTrue(all(value["reference_frame_id"]==manifest["reference_frame_id"] for value in report["fea_validation_rows"]))
        bad=copy.deepcopy(bundle.metadata["domain"]["reference_frame_context"])
        bundle.metadata["domain"]["reference_frame_context"]["case_audits_sha256"]="wrong"
        with self.assertRaisesRegex(ValueError,"case-audit mapping"):
            optimization.PhysicalBundleAdapter(bundle,raw,fps,manifest,frame_evidence=evidence)

    def test_renderer_source_bytes_and_target_cleanup_are_not_trusted_from_row_labels(self):
        evidence=self.fixture.evidence()
        physical=self.fixture.root/self.fixture.mapping["physical_files"]["module/ipmsm_geometry.py"]["path"]
        original=physical.read_bytes();physical.write_bytes(original+b"# drift\n")
        with self.assertRaisesRegex(ValueError,"SHA256 mismatch"):
            frames.draft_reference(self.fixture.origin,evidence)
        physical.write_bytes(original)
        manifest=self.issued_fixture();row=copy.deepcopy(self.fixture.rows[0]);row.update(case_id="target",
            input_beta_calibration_id=manifest["reference_frame_id"],input_electrical_zero_deg=manifest["electrical_zero_deg"])
        self.fixture.add_case(row,"case_audits",900)
        entry=self.fixture.mapping["case_audits"]["target"];record=entry["files"]["result"]
        actual=next(csv.DictReader((self.fixture.root/record["path"]).open()))
        actual["standalone_cleanup"]=json.dumps({"verified":False,"owned_pid":123,"process_exited":True,"errors":[]})
        entry["files"]["result"]=self.fixture.put(record["path"],self.fixture.csv(actual))
        seal_path=self.fixture.root/entry["files"]["seal"]["path"];seal=json.loads(seal_path.read_bytes())
        seal["files"]["result.csv"]=self.fixture.semantic(entry["files"]["result"])
        entry["files"]["seal"]=self.fixture.put(seal_path.relative_to(self.fixture.root).as_posix(),self.fixture.json(seal))
        with self.assertRaisesRegex(ValueError,"quiescent"):
            frames.verify_rows([actual],manifest,self.fixture.evidence())


if __name__ == "__main__":
    unittest.main()
