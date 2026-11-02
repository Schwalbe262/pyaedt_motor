"""Spec-bound production winding origin. Pure FS replay; never runs a solver.

Research documents are inputs only. Issuance requires original Linux evidence;
portable inert fixtures produce a different, explicitly non-production identity.
"""
from pathlib import Path
from dataclasses import asdict
import hashlib,json,math,os,re

SCHEMA='ipmsm_spec_bound_production_zero_v1'
EVIDENCE_SCHEMA='ipmsm_production_zero_evidence_v1'
PREFIX='production-zero:sha256:'
UNIT_PREFIX='inert-production-zero:sha256:'
REPORTS=('PPT_Phase_Currents','PPT_Torque','PPT_Cogging_Torque','PPT_Back_EMF','PPT_Inductance_Matrix','PPT_PhaseA_Voltage_Limit','PPT_Phase_Voltages','PPT_Losses','PPT_Flux_Linkages')
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def need(value,message):
    if not value:raise ValueError(message)
def record(raw):return {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
def load_spec(raw):
    from ipmsm_optimization import optimization_spec_from_mapping
    value=dict(raw);value.setdefault('beta_calibration',{'electrical_zero_deg':0.,'calibration_id':'pending-production-zero','convention':'dq_current_advance_v2'})
    return optimization_spec_from_mapping(value)
def spec_context(spec):
    from ipmsm_optimization import OptimizationSpec,fixed_case_context_fields
    need(type(spec)is OptimizationSpec,'explicit typed OptimizationSpec required')
    result=fixed_case_context_fields(spec.material_context,spec.winding)
    need((spec.winding.coils_per_phase,spec.winding.parallel_branches)==(4,1),'only four coils/one branch supported')
    need(spec.winding.winding_temperature_c==100 and spec.winding.fill_factor<=.45,'existing resistance/fill contract required')
    return result
def _spec_without_zero(spec):
    value=asdict(spec);value.pop('beta_calibration',None);return value

def _require_sdk_origin(proof,*,inert):
    need(type(inert)is bool,'typed origin evidence mode required')
    need(proof['mode']=='node_smoke'and proof['production_eligible']is False and proof['receipt']['stage']=='diagnostic'and proof['observed_cores']==4,'fresh two-speed original SDK diagnostic receipt required')
    if not inert:need(proof.get('actual_observation_eligible')is True,'actual production zero rejects inert SDK origin observations')

def generate_production_zero_rows(spec, *, geometry_seed=42, quality_profile='reference_ultra', protocol_id=None, geometry=None, stack_length_mm=None):
    """New spec-bound zero plan; blank IDs require fresh two-speed raw evidence."""
    if protocol_id is not None:
        from . import declared_winding_study as declared
        need(protocol_id==declared.PROTOCOL_ID,'explicit supported declared-winding protocol required')
        return declared.generate_zero_rows(spec,geometry=geometry,stack_length_mm=stack_length_mm,geometry_seed=geometry_seed,quality_profile=quality_profile)
    need(geometry is None and stack_length_mm is None,'explicit geometry requires the declared-winding protocol')
    context=spec_context(spec)
    need((spec.winding.series_turns_per_phase,spec.winding.turns_per_coil_side)==(40,10),'this new origin protocol currently supports explicit Ns40/10 only')
    from .doe import sample_feasible_geometries
    from ipmsm_optimization import phase_resistance_100c_ohm
    from calibrate_ipmsm_beta import quality_profile_values
    geometry,depth,geometry_hash=sample_feasible_geometries(spec,1,geometry_seed)[0]
    identity=digest({'spec':_spec_without_zero(spec),'geometry_seed':geometry_seed,'quality_profile':quality_profile})[:20]
    common={'geometry_group_id':'prodZeroGeom_'+identity,'design_hash':geometry_hash,
        'doe_split':'calibration','repeat_of_case_id':'','source_case_id':'prodZeroGeom_'+identity,
        'dataset_schema_version':'ipmsm_v2','model_extent':'full_360','symmetry_factor':1,
        'use_periodic_boundary':False,'beta_convention':'dq_current_advance_v2',
        'initial_position_deg':-22.5,'slot_num':spec.slot_number,'pole_num':spec.pole_number,
        'stack_length_mm':depth,'phase_resistance_ohm':phase_resistance_100c_ohm(geometry,depth,spec.winding,slot_number=spec.slot_number),
        'vdc_v':spec.inverter.vdc_v,**context,**geometry,**quality_profile_values(quality_profile),
        'analysis_purpose':'screening','strict_setup':True,'compute_inductance':True,'require_steady_state':True}
    return [{**common,'case_id':f'prodZero_{identity}_{rpm}','operating_point_id':'production_zero_no_load',
        'beta_calibration_id':'','electrical_zero_deg':0.,'beta_dq_deg':0.,'beta_deg':0.,
        'base_rpm':rpm,'i_peak_a':0.,'operation':'no_load'}for rpm in (1200,5000)]

class VerifiedProductionZero:
    """Opaque validated origin; every use rehashes the complete retained authority."""
    def __init__(self):self._verified=False;self._pins={}
    def read(self,ref):
        need(type(ref)is dict and set(ref)=={'path','bytes','sha256'},'exact origin file ref required')
        need(type(ref['bytes'])is int and ref['bytes']>0 and type(ref['sha256'])is str and re.fullmatch('[0-9a-f]{64}',ref['sha256']),'typed original file seal required')
        p=Path(ref['path']);need(p.is_absolute()and p.is_file()and not p.is_symlink(),'regular absolute origin file required')
        for parent in (p,*p.parents):need(not getattr(parent.lstat(),'st_file_attributes',0)&0x400,'reparse origin refused')
        key=str(p.resolve());want={k:ref[k]for k in ('bytes','sha256')};need(self._pins.get(key,want)==want,'conflicting first origin authority')
        self._pins.setdefault(key,want);a=p.stat();raw=p.read_bytes();b=p.stat()
        need((a.st_size,a.st_ino,a.st_mtime_ns)==(b.st_size,b.st_ino,b.st_mtime_ns)and record(raw)==want,'origin bytes changed')
        return raw
    @classmethod
    def from_plan(cls,path,sha256,*,spec=None):
        obj=cls();path=Path(path).absolute();raw=path.read_bytes();need(record(raw)['sha256']==sha256,'explicit production evidence plan SHA required')
        from . import declared_winding_study as declared
        plan=json.loads(obj.read({'path':str(path),**record(raw)}));need(plan['schema']in(EVIDENCE_SCHEMA,declared.EVIDENCE_SCHEMA),'production evidence schema required')
        declared_mode=plan['schema']==declared.EVIDENCE_SCHEMA
        need(plan.get('protocol_id')==declared.PROTOCOL_ID if declared_mode else 'protocol_id'not in plan,'explicit outer declared protocol required; legacy protocol cannot be relabeled')
        mode=plan.get('evidence_mode','linux_original');need(mode in ('linux_original','inert_fixture'),'unknown origin mode')
        if mode=='linux_original':need(os.name=='posix','production origin issuance requires original Linux filesystem')
        obj.inert=mode=='inert_fixture';obj.plan=plan;obj.spec=load_spec(json.loads(obj.read(plan['spec'])))
        if spec is not None:need(asdict(spec)==asdict(obj.spec),'supplied spec differs from sealed declared spec')
        expected=spec_context(obj.spec)
        study_raw=obj.read(plan['study'])
        if declared_mode:
            obj.study=declared.VerifiedDeclaredWindingStudy.from_plan(Path(plan['study']['path']),record(study_raw)['sha256'],declared_spec=obj.spec,evidence_mode=mode,declared_spec_ref=plan['spec'])
        else:
            from research_ns40_zero import VerifiedNs40Study
            obj.study=VerifiedNs40Study.from_plan(Path(plan['study']['path']),record(study_raw)['sha256'])
        study_plan=json.loads(study_raw);obj.study_plan=study_plan
        need(len(obj.study._rows)==2 and sorted(float(r['input_base_rpm'])for r in obj.study._rows)==[1200.,5000.],'exact two original speeds required')
        from run_ipmsm_batch import build_spec,summarize_transient_outputs
        from module.ipmsm_waveform_quality import analyze_exported_waveforms
        from .solver_profile import audit_solver_profile
        from . import winding_native
        from . import reference_frames
        reference_frames.implementation_sha256()
        observe_native=winding_native.observe_native
        obj._observer_path=Path(winding_native.__file__).resolve()
        obj._observer_pin=record(obj._observer_path.read_bytes())
        obj._parser_path=Path(reference_frames.__file__).resolve()
        obj._parser_pin=record(obj._parser_path.read_bytes())
        byid={entry['input_case']['case_id']:entry for entry in study_plan['cases']};observed=[]
        need(set(plan['raw_cases'])==set(byid),'complete raw reports for both original cases required')
        for row in obj.study._rows:
            case=byid[row['case_id']]['input_case'];case_spec=build_spec(case)
            from ipmsm_optimization import phase_resistance_100c_ohm
            from run_ipmsm_batch import extract_fixed_geometry
            resistance=phase_resistance_100c_ohm(extract_fixed_geometry(case),case_spec.stack_length_mm,obj.spec.winding,slot_number=obj.spec.slot_number)
            need(math.isclose(case_spec.phase_resistance_ohm,resistance,rel_tol=1e-12,abs_tol=1e-12),'actual zero resistance differs from complete declared winding')
            need((case_spec.slot_number,case_spec.pole_number,case_spec.vdc_v)==(obj.spec.slot_number,obj.spec.pole_number,obj.spec.inverter.vdc_v),'actual slots/poles/inverter differ from declared spec')
            for key,value in expected.items():
                actual=row.get('input_'+key)
                need(actual is not None,'actual winding/material input missing: '+key)
                if isinstance(value,str):need(actual==value,'actual spec context differs: '+key)
                else:need(math.isclose(float(actual),value,rel_tol=1e-12,abs_tol=1e-12),'actual spec context differs: '+key)
            rawcase=plan['raw_cases'][row['case_id']];need(set(rawcase['reports'])==set(REPORTS),'all nine original reports required')
            exported={}
            project=Path(row['project_path']).resolve()
            if not obj.inert:need(str(project).startswith(study_plan['result_root'].rstrip('/')+'/')and '\\'not in str(project),'original Linux row artifacts required')
            for report,ref in rawcase['reports'].items():
                obj.read(ref);need(Path(row['artifact_report_'+report]).resolve()==Path(ref['path']).resolve(),'raw report/actor path differs')
                need(Path(ref['path']).resolve().is_relative_to(project),'raw report escaped same case')
                exported['artifact_report_'+report]=ref['path']
            # No physical_voltage output is requested: this replays existing bytes only.
            numeric=summarize_transient_outputs(exported,case_spec,operation='no_load')
            numeric.update(analyze_exported_waveforms(exported,case_spec,'no_load'))
            keys=('output_back_emf_phasea_h1_cos_peak_v','output_back_emf_phasea_h1_sin_peak_v')
            for key in keys:
                need(key in numeric and math.isclose(float(row[key]),float(numeric[key]),rel_tol=1e-10,abs_tol=1e-10),'raw signed H1 replay differs: '+key)
            need(numeric['output_steady_state_passed']is True,'actual raw waveform steady-state rejected')
            profile=obj.read(rawcase['native_profile']);acf=obj.read(rawcase['own_acf'])
            for ref in (rawcase['native_profile'],rawcase['own_acf']):need(Path(ref['path']).resolve().is_relative_to(project),'profile/ACF escaped case')
            audit=audit_solver_profile(profile,requested_cores=4,acf=acf)
            need(audit['profile_format']=='saved_native'and audit['normal_completion']is True and set(audit['machine_core_counts'])=={'4'}and set(audit['matrix_core_counts'])=={'4'}and audit['all_matrix_records_match_request']is True and audit['matrix_core_records_complete']is True and audit['acf']['requested_core_counts']==[4],'actual native four-core profile/own ACF required')
            saved=obj.read(byid[row['case_id']]['saved_AEDT']);native=observe_native(saved,turns_per_coil_side=obj.spec.winding.turns_per_coil_side)
            observed.append({'case_id':row['case_id'],'raw_h1':{key:numeric[key]for key in keys},'native_frame':native,'profile':record(profile),'acf':record(acf)})
        obj._sdk_evidence=None;obj._sdk_pin=None
        if not obj.inert or plan.get('owned_sdk_evidence')is not None:
            from .owned_sdk_production import OwnedSDKProductionEvidence
            from .reference_frames import _verify_collected_row
            authority=plan['owned_sdk_evidence'];obj.read(authority)
            sdk=OwnedSDKProductionEvidence.from_declaration(authority['path'],authority['sha256'])
            for row in obj.study._rows:
                proof=sdk.verify_case(row['case_id'],expected_producer=obj.study.scope['source_producer'])
                _require_sdk_origin(proof,inert=obj.inert)
                _verify_collected_row(row,proof['original_row'],{})
                if declared_mode:need(canonical(proof['case'])==canonical(byid[row['case_id']]['input_case']),'declared origin must retain the exact original SDK input case, including protocol marker')
                need(canonical(proof['full_spec42'])==canonical(asdict(build_spec(byid[row['case_id']]['input_case']))),'original SDK zero fullspec differs')
            obj._sdk_evidence=sdk;obj._sdk_pin=digest(sdk.cases)
        obj.observations=observed;obj.expected=expected;obj._spec_pin=digest(asdict(obj.spec));obj._rows_pin=digest(obj.study._rows)
        obj._plan_pin=digest(plan);obj._observations_pin=digest(observed);obj._mode_pin=obj.inert
        obj._scope_pin=digest(obj.study.scope);obj._study_plan_pin=digest(study_plan)
        obj._study_identity=id(obj.study);obj._sdk_identity=id(obj._sdk_evidence)
        obj._retained_pin=digest(obj._retained_authority());obj._verified=True;obj.assert_unchanged();return obj
    def _retained_authority(self):
        value={'origin_files':self._pins,'study_files':self.study._pins,
            'observer':{'path':str(self._observer_path),**self._observer_pin},
            'native_parser':{'path':str(self._parser_path),**self._parser_pin}}
        from . import declared_winding_study as declared
        if self.plan['schema']==declared.EVIDENCE_SCHEMA:
            value['declared_study']={'class':id(type(self.study)),'memory_pin':self.study._memory_pin,'implementation':self.study._implementation_pin}
        return value
    def assert_memory_unchanged(self):
        """Check retained authority only; checked search owns the FS boundaries."""
        need(type(self)is VerifiedProductionZero and self._verified,'verified typed production origin required')
        need(id(self.study)==self._study_identity and self.study._validated is True and id(self._sdk_evidence)==self._sdk_identity,'production origin evidence object changed')
        need(digest(asdict(self.spec))==self._spec_pin and digest(self.study._rows)==self._rows_pin and digest(self.study.scope)==self._scope_pin,'production origin context changed')
        need(digest(self.plan)==self._plan_pin and digest(self.study_plan)==self._study_plan_pin and digest(self.observations)==self._observations_pin and self.inert is self._mode_pin,'production origin plan/observations/mode changed')
        from . import declared_winding_study as declared
        if self.plan['schema']==declared.EVIDENCE_SCHEMA:declared.require_context(self.study)
        need(digest(self._retained_authority())==self._retained_pin,'production origin retained file authority changed')
        if self._sdk_evidence is not None:
            need(digest(self._sdk_evidence.cases)==self._sdk_pin,'original SDK origin declaration changed')
            self._sdk_evidence.assert_memory_unchanged()
    def assert_unchanged(self):
        self.assert_memory_unchanged()
        implementation_sha256()
        need(record(self._observer_path.read_bytes())==self._observer_pin,'winding observer changed after verification')
        need(record(self._parser_path.read_bytes())==self._parser_pin,'winding Native parser dependency changed')
        if self._sdk_evidence is not None:
            self._sdk_evidence.assert_unchanged()
        self.study.verify_rows(self.study._rows)
        for path,want in self._pins.items():need(record(Path(path).read_bytes())==want,'production origin changed after verification')
    def manifest(self):
        self.assert_unchanged()
        if not self.inert:need(os.name=='posix','production identity requires original Linux evidence')
        from . import declared_winding_study as declared
        if self.plan['schema']==declared.EVIDENCE_SCHEMA:
            measured=declared.analyze_zero_rows(self.study)
        else:
            from calibrate_ipmsm_beta import analyze_zero_calibration_rows
            measured=analyze_zero_calibration_rows(self.study._rows,_research_context=self.study)
        # Construct a fresh protocol payload; never modify or rehash a research manifest.
        value={key:measured[key]for key in ('method','convention','electrical_zero_deg','initial_position_deg','successful_rows','successful_speeds_rpm','circular_resultant','max_circular_deviation_deg','observations','material_context','winding_context','material_fingerprint','metric_contract','producer_fingerprint','back_emf_source','design_hash','quality_profile')}
        value.update(schema_version=SCHEMA,protocol_implementation_sha256=implementation_sha256(),winding_spec=asdict(self.spec.winding),
            origin_evidence_sha256=digest({'plan':self.plan,'observations':self.observations}),origin_scope=self.study.scope,
            evidence_mode='inert_fixture'if self.inert else'linux_original',training_admission=False)
        if self.plan['schema']==declared.EVIDENCE_SCHEMA:
            value.update(declared_study_protocol=declared.PROTOCOL_ID,declared_study_schema=declared.SCHEMA,declared_spec_canonical_sha256=declared.digest(asdict(self.spec)),declared_protocol_implementation_sha256=declared.implementation_sha256())
        inferred=measured['electrical_zero_deg'];literal=format(inferred,'g')+'deg';effective=float(format(inferred,'g'))
        value['inferred_electrical_zero_deg']=inferred;value['electrical_zero_deg']=effective
        value['native_angle_binding']={'policy':'frozen_ppt_g_formatter_v1','ppt_source_sha256':self.study_plan['source_files']['module/ipmsm_ppt_setup.py']['sha256'],
            'inferred_deg':inferred,'requested_deg':effective,'effective_native_literal':literal,'effective_native_deg':effective}
        from calibrate_ipmsm_beta import wrap_degrees
        value['effective_max_circular_deviation_deg']=max(abs(wrap_degrees(obs['inferred_zero_deg']-effective))for obs in measured['observations'])
        need(value['effective_max_circular_deviation_deg']<=3.,'effective Native angle exceeds existing max3deg policy')
        value['native_observer_sha256']=self._observer_pin['sha256']
        value['native_observer_dependencies']={'reference_frames.py':self._parser_pin}
        value['calibration_id']=(UNIT_PREFIX if self.inert else PREFIX)+digest(value);return value

def implementation_sha256():
    current=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();need(current==_IMPORTED_SHA,'production zero code changed after import');return current
def require_context(context):
    need(type(context)is VerifiedProductionZero,'a boolean/mapping/research context is not production evidence');context.assert_unchanged()
def analyze_production_zero(spec,context):
    require_context(context);need(_spec_without_zero(spec)==_spec_without_zero(context.spec),'production zero is bound to the complete declared spec');return context.manifest()
def validate_production_zero(manifest,spec,context):
    need(isinstance(manifest,dict)and manifest.get('schema_version')==SCHEMA,'production-zero protocol required')
    expected=analyze_production_zero(spec,context);need(canonical(manifest)==canonical(expected),'production zero must replay exact actual evidence')
    return expected['electrical_zero_deg'],expected['calibration_id']
def apply_production_zero_to_spec(spec,manifest,context):
    parsed=load_spec(spec);validate_production_zero(manifest,parsed,context)
    result=dict(spec);result['beta_calibration']={key:manifest[value]for key,value in [('electrical_zero_deg','electrical_zero_deg'),('calibration_id','calibration_id'),('convention','convention')]};return result

_IMPORTED_SHA=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
