"""Explicit complete-Spec 8p/12s winding study. Pure files; no solver/network API.

This protocol never relaxes the legacy Ns40 research or default Ns48 paths.
Structural unit fixtures receive inert identities; actual issuance remains in
production_zero and requires original Linux SDK evidence and diagnostic receipts.
"""
from collections import Counter
from dataclasses import asdict
from pathlib import Path
import argparse,ast,csv,hashlib,io,json,math,os,re,sys

if not __package__:
    sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[2])]
    __package__='pyaedt_motor'

SCHEMA='ipmsm_declared_winding_two_speed_study_v1'
EVIDENCE_SCHEMA='ipmsm_declared_winding_production_zero_evidence_v1'
PROTOCOL_ID='declared_complete_spec_8p12s_four_coils_one_branch_v1'
MODULE='src/pyaedt_motor/declared_winding_study.py'
PHYSICAL=('run_ipmsm_batch.py','module/ipmsm_geometry.py','module/ipmsm_ppt_setup.py','module/ipmsm_waveform_quality.py','module/variable.py')
HELPERS=('src/pyaedt_motor/public_batch_pilot.py','src/pyaedt_motor/native_state_semantics.py','ipmsm_optimization.py','generate_ipmsm_quality_cases.py')
EXTRA_CODE=(MODULE,'src/pyaedt_motor/production_zero.py','src/pyaedt_motor/winding_native.py')
MM={'stator_outer_radius','stator_shoe_thick','stator_gap','rotator_gap','magnet_shield_thick'}
def need(value,message):
    if not value:raise ValueError(message)
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def record(raw):return {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
def implementation_sha256():
    current=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    need(current==_IMPORTED_SHA,'declared winding protocol implementation changed after import')
    return current
def validate_declared_spec(spec):
    from ipmsm_optimization import OptimizationSpec,WindingSpec,fixed_case_context_fields
    need(type(spec)is OptimizationSpec and type(spec.winding)is WindingSpec,'complete typed OptimizationSpec/WindingSpec required')
    w=spec.winding
    need(type(spec.slot_number)is int and type(spec.pole_number)is int and(spec.slot_number,spec.pole_number)==(12,8),'explicit12-slot/8-pole topology required')
    need(all(type(x)is int and x>0 for x in(w.series_turns_per_phase,w.turns_per_coil_side,w.coils_per_phase,w.parallel_branches,w.strands_per_turn)),'typed positive winding counts required')
    need((w.coils_per_phase,w.parallel_branches)==(4,1)and w.series_turns_per_phase==4*w.turns_per_coil_side,'declared Ns must equal four series coils times coil turns, one branch')
    need(w.winding_temperature_c==100 and 0<w.fill_factor<=.45,'unchanged100C resistance/fill contract required')
    need(all(type(x)in(int,float)and math.isfinite(x)and x>0 for x in(w.strand_area_mm2,w.conductor_area_per_branch_mm2,w.copper_resistivity_20c_ohm_m,w.end_turn_factor)),'explicit finite physical conductor/resistance values required')
    need(spec.material_context.core_material_model=='m19_29g_reference_y2_v1'and spec.material_context.core_material=='M19_29G_reference_y2','current declared M19 material context required')
    canonical(asdict(spec))
    return fixed_case_context_fields(spec.material_context,w)
def validate_geometry(spec,geometry,stack_length_mm):
    from ipmsm_optimization import GEOMETRY_VARIABLE_NAMES,geometry_metrics
    validate_declared_spec(spec)
    need(type(geometry)is dict and set(geometry)in(set(GEOMETRY_VARIABLE_NAMES),set(GEOMETRY_VARIABLE_NAMES)|{'slot_num','pole_num'}),'complete explicit15/17 geometry required')
    if 'slot_num'in geometry:need(type(geometry['slot_num'])is int and type(geometry['pole_num'])is int and(geometry['slot_num'],geometry['pole_num'])==(12,8),'declared geometry topology differs')
    values={}
    for bound in spec.geometry_design_space:
        value=geometry[bound.name]
        need(type(value)in(int,float)and math.isfinite(value)and bound.lower<=value<=bound.upper,'geometry outside complete declared spec: '+bound.name)
        values[bound.name]=float(value)
    need(values['stator_shoe_thick']==1.5,'frozen inactive shoe must remain1.5')
    need(type(stack_length_mm)in(int,float)and math.isfinite(stack_length_mm)and spec.stack_length_bounds.lower<=stack_length_mm<=spec.stack_length_bounds.upper,'stack outside complete declared spec')
    metrics=geometry_metrics(values,stack_length_mm,spec.winding,slot_number=12)
    need(metrics.slot_fill_ratio<=spec.winding.fill_factor,'declared geometry/conductor slot fill exceeded')
    return {**values,'slot_num':12,'pole_num':8},metrics
def validate_case(case,case_spec,declared_spec,geometry):
    from ipmsm_optimization import phase_resistance_100c_ohm
    expected=validate_declared_spec(declared_spec);validate_geometry(declared_spec,geometry,case_spec.stack_length_mm)
    need(case.get('zero_study_protocol')==PROTOCOL_ID,'explicit new protocol case marker required; no legacy row relabeling')
    for name,value in expected.items():
        actual=getattr(case_spec,name)
        need(actual==value if isinstance(value,str)else math.isclose(actual,value,rel_tol=1e-12,abs_tol=1e-12),'case differs from full declared winding/material: '+name)
    need((case_spec.slot_number,case_spec.pole_number,case_spec.vdc_v)==(12,8,declared_spec.inverter.vdc_v),'declared slots/poles/DC bus differ')
    wanted=phase_resistance_100c_ohm(geometry,case_spec.stack_length_mm,declared_spec.winding,slot_number=12)
    need(math.isclose(case_spec.phase_resistance_ohm,wanted,rel_tol=1e-12,abs_tol=1e-12),'case resistance differs from complete declared winding/geometry')

class VerifiedDeclaredWindingStudy:
    def __init__(self):self._validated=False;self._pins={}
    def read(self,ref):
        need(type(ref)is dict and set(ref)=={'path','bytes','sha256'}and type(ref['bytes'])is int and ref['bytes']>0 and type(ref['sha256'])is str and re.fullmatch('[0-9a-f]{64}',ref['sha256']),'exact typed sealed file ref required')
        path=Path(ref['path']);need(path.is_absolute()and path.is_file()and not path.is_symlink(),'regular absolute evidence file required')
        for part in (path,*path.parents):need(not getattr(part.lstat(),'st_file_attributes',0)&0x400,'reparse evidence forbidden')
        key=str(path.resolve(strict=True));want={k:ref[k]for k in ('bytes','sha256')}
        need(key not in self._pins or self._pins[key]==want,'conflicting first file authority')
        self._pins.setdefault(key,want);a=path.stat();raw=path.read_bytes();b=path.stat()
        need((a.st_size,a.st_mtime_ns,a.st_ino)==(b.st_size,b.st_mtime_ns,b.st_ino)and record(raw)==want,'evidence bytes changed')
        return raw
    @classmethod
    def from_plan(cls,path,sha256,*,declared_spec,evidence_mode,declared_spec_ref):
        obj=cls();raw=Path(path).read_bytes();need(record(raw)['sha256']==sha256,'explicit plan SHA required');plan=json.loads(obj.read({'path':str(Path(path).absolute()),**record(raw)}))
        need(plan['schema']==SCHEMA and plan.get('protocol_id')==PROTOCOL_ID and len(plan['cases'])==2,'explicit declared-winding two-case protocol required')
        need(evidence_mode in ('linux_original','inert_fixture')and plan.get('evidence_mode')==evidence_mode,'outer/study evidence modes must agree')
        if evidence_mode=='linux_original':need(os.name=='posix','original Linux declared study required')
        validate_declared_spec(declared_spec)
        need(canonical(plan['declared_spec'])==canonical(declared_spec_ref),'same sealed complete spec file required')
        from .production_zero import load_spec
        need(canonical(asdict(load_spec(json.loads(obj.read(declared_spec_ref)))))==canonical(asdict(declared_spec)),'complete declared spec bytes differ')
        need(plan.get('declared_spec_canonical_sha256')==digest(asdict(declared_spec))and canonical(plan.get('declared_winding'))==canonical(asdict(declared_spec.winding)),'explicit full Spec/winding declaration differs')
        obj.spec=declared_spec;obj.plan=plan;obj.evidence_mode=evidence_mode;obj._implementation_pin=implementation_sha256()
        root=Path(plan['source_root']);library=Path(plan['library_root']);need(set(PHYSICAL)|set(HELPERS)|set(EXTRA_CODE)|{'configs/material_m19_29g_reference_y2.json'}<=set(plan['source_files']),'physical/verification source and M19 pack closure required')
        need(len(plan['library_files'])==23,'private library23 required')
        for base,files in ((root,plan['source_files']),(library,plan['library_files'])):
            for name,ref in files.items():
                member=Path(name);need(not member.is_absolute()and '..'not in member.parts,'source member escape')
                obj.read({'path':str(base/member),**ref})
        code=Path(__file__).resolve().parents[2]
        for name in ('calibrate_ipmsm_beta.py',MODULE):obj.read({'path':str(code/name),**plan['validation_code'][name]})
        sys.path[:0]=[str(code),str(root),str(root/'src')]
        import calibrate_ipmsm_beta as calibration
        need(Path(calibration.__file__).resolve()==code/'calibrate_ipmsm_beta.py','explicit reviewed calibration implementation required')
        import run_ipmsm_batch as runner
        import ipmsm_optimization as optimization,generate_ipmsm_quality_cases as quality
        from module import ipmsm_ppt_setup as ppt
        from pyaedt_motor import public_batch_pilot as pilot,native_state_semantics as semantic
        for name,used in zip(HELPERS,(pilot,semantic,optimization,quality)):
            need(Path(used.__file__).resolve()==(root/name).resolve(),'actual helper import origin differs: '+name)
            obj.read({'path':used.__file__,**plan['source_files'][name]})
        need(calibration.fixed_case_context_fields is optimization.fixed_case_context_fields and calibration.MaterialContextSpec is optimization.MaterialContextSpec and calibration.QUALITY_PROFILES is quality.QUALITY_PROFILES and calibration.MESH_ELEMENT_KEYS is quality.MESH_ELEMENT_KEYS,'calibrator imported helper bindings differ')
        from ansys.aedt.core.internal import load_aedt_file as parser
        obj.read({'path':parser.__file__,**plan['pure_parser']})
        need(Path(runner.__file__).resolve()==(root/'run_ipmsm_batch.py').resolve(),'actual runner origin differs')
        need(Path(ppt.__file__).resolve()==(root/'module/ipmsm_ppt_setup.py').resolve(),'actual PPT origin differs')
        producer=runner.stable_fingerprint('producer_v3',{n:plan['source_files'][n]['sha256']for n in PHYSICAL})
        obj.read(plan['material_reference']);reference_materials=semantic.read_state(plan['material_reference']['path'])['project']['Definitions']['Materials']
        rows=list(csv.DictReader(obj.read(plan['result_csv']).decode('utf-8-sig').splitlines()))
        need(len(rows)==2 and len({x['case_id']for x in rows})==2,'all two original rows required; no filtering/relabeling')
        byid={x['case_id']:x for x in rows};geometry=plan['geometry17'];initials=[];depths=[];speeds=[];specs=[]
        for entry in plan['cases']:
            case=entry['input_case'];row=byid.get(case['case_id']);need(row is not None,'actual case absent')
            spec=runner.build_spec(case);validate_case(case,spec,declared_spec,geometry)
            need((spec.slot_number,spec.pole_number)==(12,8)and spec.analysis_purpose=='screening'and 40<=spec.stack_length_mm<=70 and float(geometry['stator_outer_radius'])<=150,'8p12s screened diameter/stack bounds')
            need(spec.i_peak_a==0 and spec.beta_deg==0 and spec.electrical_zero_deg==0 and case['operation']=='no_load'and case.get('beta_calibration_id','')=='','NoLoad0/zero0/blank-ID required')
            need(spec.core_material_model=='m19_29g_reference_y2_v1'and spec.core_material=='M19_29G_reference_y2'and spec.model_extent=='full_360'and spec.symmetry_factor==1 and spec.compute_inductance is True and spec.strict_setup is True and spec.require_steady_state is True,'full M19 strict research study only')
            need(runner.extract_fixed_geometry(case)==geometry==runner.extract_fixed_geometry(row),'whole geometry17 input binding')
            actual_spec=runner.build_spec({k[len('input_'):]:v for k,v in row.items()if k.startswith('input_')});need(pilot.canonical_json(asdict(actual_spec))==pilot.canonical_json(asdict(spec)),'actual row Spec differs from case')
            need(row.get('input_beta_calibration_id','')=='','old calibration ID cannot enter Ns40 zero solve')
            need(row['status']=='ok'and row['analyze']=='True'and row['standalone_solver_uncertain']=='False'and row['input_producer_fingerprint']==producer,'successful actual same-source Analyze required')
            need(row['input_setup_fingerprint']==runner.setup_fingerprint(spec,case['quality_profile'],'no_load',False),'actual setup fingerprint differs')
            need(row['standalone_analyze_return_received']=='True'and row['standalone_analyze_return_type']=='builtins.bool'and row['standalone_analyze_return_repr']=='True','exact SDK boolTrue required')
            token=json.loads(row['standalone_owner_identity']);cleanup=json.loads(row['standalone_cleanup'])
            need(type(token)is list and len(token)==5 and type(token[0])is int and token[0]>0 and type(token[1])in(int,float)and math.isfinite(token[1])and token[1]>0,'typed original owner identity')
            need((token[2]=='nt'and type(token[3])is str and bool(token[3]))or(token[2]=='posix'and type(token[3])is list and len(token[3])==2 and all(type(x)is int and x>=0 for x in token[3])),'actual Windows/POSIX principal')
            need(row['standalone_solver_state_contract']=='ipmsm_standalone_solver_state_v1','actual SDK row ABI required')
            need(token[4]in {'ansysedt','ansysedt.exe'}and type(cleanup['owned_pid'])is int and cleanup['owned_pid']==token[0]and cleanup['verified']is True and cleanup['process_exited']is True and cleanup['native_quit_requested']is True and cleanup['errors']==[]and cleanup['client_retirement']['client_retired']is True and cleanup['client_retirement']['errors']==[],'original known owned exit/retirement required')
            retirement=cleanup['client_retirement'];need(retirement['sdk_version']=='0.22.0'and retirement['new_python_client']is True and retirement['Native_exit_implied']is False and cleanup['SDK_release_desktop_called']is False and cleanup['SDK_ReleaseAll_called']is False and cleanup['atexit_function_replaced']is False,'strict client retirement separate from actual exit')
            result_root=Path(plan['result_root']).resolve(strict=True)
            for role in ('setup_audit','saved_AEDT','solver_state'):need(Path(entry[role]['path']).resolve(strict=True).is_relative_to(result_root),'artifact outside own declared result root')
            audit=json.loads(obj.read(entry['setup_audit']));need(Path(row['artifact_setup_audit']).resolve()==Path(entry['setup_audit']['path']).resolve(),'audit row binding')
            project=Path(row['project_path'])/(row['simulation_name']+'.aedt');need(project.resolve()==Path(entry['saved_AEDT']['path']).resolve(),'saved row binding');obj.read(entry['saved_AEDT'])
            marker=json.loads(obj.read(entry['solver_state']));need(Path(row['artifact_standalone_solver_state']).resolve()==Path(entry['solver_state']['path']).resolve(),'marker row binding')
            need(marker['schema']=='ipmsm_standalone_solver_state_v1'and marker['case_id']==case['case_id']and marker['producer_fingerprint']==producer and canonical(marker['owner_identity'])==canonical(token)and type(marker['owned_pid'])is int and marker['owned_pid']==token[0]and marker['stage']=='analyze_returned_true'and marker['solver_state_uncertain']is False and marker['Analyze_return_received']is True and marker['Analyze_return_type']=='builtins.bool'and marker['Analyze_return_repr']=='True','actual solver-state source/case/owner binding')
            pilot.require_setup_evidence(row,spec);need(audit['analysis']is True and audit['native_model_depth_audit_pre_analysis']['passed']is True,'actual pre-Analyze depth boundary')
            depth=audit['native_model_depth_audit_pre_analysis'];need(depth['model_units_live']=='mm'and type(depth['native_depth_SI_m'])in(int,float)and math.isclose(depth['native_depth_SI_m'],float(f'{spec.stack_length_mm:g}')*1e-3,rel_tol=1e-9,abs_tol=1e-12),'actual third depth SI')
            state=semantic.read_state(project);model=state['project']['Maxwell2DModel'];variables={}
            for rawvar in state['raw_variable_rows']:
                node=ast.parse(rawvar.strip(),mode='eval').body;need(isinstance(node,ast.Call)and getattr(node.func,'id',None)=='VariableProp'and len(node.args)>=4,'saved variable syntax')
                prefix=node.args[:4];need(all(isinstance(x,ast.Constant)and type(x.value)is str for x in prefix),'saved variable prefix')
                name,value=prefix[0].value,prefix[3].value;need(name not in variables,'duplicate variable');variables[name]=value
            for name,value in geometry.items():need(variables[name]==(str(int(value))if name in {'slot_num','pole_num'}else format(float(value),'.15g')+('mm'if name in MM else'')),'exact saved geometry15g/same units: '+name)
            expected={'StackLength':f'{spec.stack_length_mm:g}mm','R_phase':f'{spec.phase_resistance_ohm:g}ohm','Imax':'0A','Beta':'0deg','ElectricalZero':'0deg','InitialPositionMD':f'{spec.initial_position_deg:g}deg','BaseRPM':f'{spec.base_rpm:g}','StepsPerPeriod':str(spec.steps_per_period),'TransientPeriods':str(spec.transient_periods),'StopTime':'TransientPeriods/frq','TimeStep':'StopTime/'+str(spec.transient_periods*spec.steps_per_period),'SymmetryFactor':'1'}
            need(all(variables.get(k)==v for k,v in expected.items()),'saved commanded depth/R/frame/time differs')
            need(model['ModelDepth']=='StackLength'and model['GeometryMode']=='XY'and model['Multiplier']=='SymmetryFactor'and model['SolveFraction']is False,'saved fullXY depth/Sym1')
            setup=model['AnalysisSetup']['SolveSetups']['PPT_Transient']
            need(setup['NonlinearSolverResidual']==f'{spec.solver_nonlinear_residual:g}'and setup['OutputError']is spec.solver_output_error and setup['TimeStep']=='TimeStep'and setup['StopTime']=='StopTime'and setup['SaveFieldsType']=='None'and setup['UseAdaptiveTimeStep']is False and setup['UseControlProgram']is False,'actual saved residual/output/time/retention/control settings')
            need(model['ComputeTransientInductance']is True and model['ComputeIncrementalMatrix']is False,'saved apparent-L controls')
            need(not spec.mesh_refinement_modes and not spec.mesh_maximum_lengths_mm,'this minimal study retains the existing budget-mesh mode')
            for group,count in spec.mesh_elements.items():
                op=model['MeshSetup']['MeshOperations']['Mesh_'+group];need(op['Enabled']is True and op['RefineInside']is True and op['RestrictElem']is True and op['RestrictLength']is False and op['NumMaxElem']==str(count),'saved budget-mesh request differs: '+group)
            boundaries=model['BoundarySetup']['Boundaries'];coils=[v for v in boundaries.values()if type(v)is dict and v.get('BoundType')=='Coil'];groups={k:v for k,v in boundaries.items()if type(v)is dict and v.get('BoundType')=='Winding Group'}
            need(len(coils)==24 and set(groups)=={'PhaseA','PhaseB','PhaseC'}and all(v['Conductor number']==str(declared_spec.winding.turns_per_coil_side)for v in coils),'saved24 coils must match declared turns')
            ids=[v['ID']for v in groups.values()];need(len(set(ids))==3 and all(type(x)is int and x>0 for x in ids),'three unique actual phase IDs')
            counts=Counter(v['Winding']for v in coils);need(counts=={x:8 for x in ids}and all(v['ParentBndID']==v['Winding']and len(v['Objects'])==1 and type(v['Objects'][0])is int and v['Objects'][0]>0 for v in coils)and len({v['Objects'][0]for v in coils})==24,'saved phase8 coil sides / actual independent parts')
            currents=ppt.phase_current_expressions(spec)
            need(all(v['Type']=='Current'and v['ParallelBranchesNum']=='1'and v['Current']==currents[k]for k,v in groups.items()),'saved current-source/onebranch/frame expressions')
            material=audit['material_definitions'];need(material['native_readback_verified']is True and material['bh_curve_points']==23 and material['composition_live']=='Solid','actual material native audit')
            wanted=[x for pair in ppt.load_reference_core_material_pack()['bh']['points_H_B']for x in pair]
            need(state['project']['Definitions']['Materials']['M19_29G_reference_y2']['permeability']['BHCoordinates']['Points']==wanted,'saved genuine M19 BH23')
            materials=state['project']['Definitions']['Materials'];need(set(materials)==set(reference_materials),'bulk material set differs from sealed reference')
            for name,value in materials.items():need(canonical({k:v for k,v in value.items()if k!='ModTime'})==canonical({k:v for k,v in reference_materials[name].items()if k!='ModTime'}),'all bulk material inputs exact except explicit ModTime: '+name)
            parts=model['ModelSetup']['GeometryCore']['GeometryOperations']['ToplevelParts']['GeometryPart'];attrs={p['Attributes']['Name']:p['Attributes']for p in parts};need(len(attrs)==len(parts),'duplicate saved body names')
            need(attrs['stator']['MaterialValue']==attrs['rotator']['MaterialValue']=='"M19_29G_reference_y2"','actual core bulk assignments')
            need(Counter(a['MaterialValue']for a in attrs.values())=={'"M19_29G_reference_y2"':2,'"copper"':24,'"vacuum"':1,'"air"':3,'"NdFeB_1.25T"':8},'actual8magnet/24copper/material assignments')
            initials.append(spec.initial_position_deg);depths.append(spec.stack_length_mm);speeds.append(spec.base_rpm);specs.append(asdict(spec))
        need(sorted(speeds)==[1200.,5000.]and len(set(initials))==len(set(depths))==1,'exact two speeds/same initial angle/depth')
        need(canonical({k:v for k,v in specs[0].items()if k!='base_rpm'})==canonical({k:v for k,v in specs[1].items()if k!='base_rpm'}),'only speed may differ across the two NoLoad Specs')
        obj._rows=rows;obj.scope={'kind':PROTOCOL_ID,'series_turns_per_phase':declared_spec.winding.series_turns_per_phase,'turns_per_coil_side':declared_spec.winding.turns_per_coil_side,'coils_per_phase':4,'parallel_branches':1,'geometry17':geometry,'initial_position_deg':initials[0],'stack_length_mm':depths[0],'source_producer':producer,'plan_sha256':sha256,'result_csv':plan['result_csv'],'production_training_or_model_approval':False}
        obj.scope.update({k:specs[0][k]for k in ('conductor_area_per_branch_mm2','phase_resistance_ohm','slot_fill_limit','core_material_model','core_material')});obj.scope['material_reference']=plan['material_reference']
        obj.scope['shared_spec']={k:v for k,v in specs[0].items()if k not in {'base_rpm','i_peak_a','beta_deg','electrical_zero_deg'}}
        obj.scope.update(declared_spec_canonical_sha256=digest(asdict(declared_spec)),declared_winding=asdict(declared_spec.winding),declared_protocol_implementation_sha256=obj._implementation_pin,evidence_mode=evidence_mode)
        obj._rows_canonical=canonical(rows);obj._scope_canonical=canonical(obj.scope)
        obj._validated=True;obj._memory_pin=obj._memory_snapshot();obj.verify_rows(rows);return obj
    def verify_rows(self,rows):
        require_context(self);need(canonical(list(rows))==self._rows_canonical and canonical(self.scope)==self._scope_canonical,'original two authoritative rows/scope changed/filtered/reordered')
        for path,want in self._pins.items():need(record(Path(path).read_bytes())==want,'source/evidence changed at analysis boundary')

    def _memory_snapshot(self):
        return digest({'attributes':sorted(k for k in vars(self)if k!='_memory_pin'),
            'rows':[id(self._rows),self._rows],'scope':[id(self.scope),self.scope],
            'plan':[id(self.plan),self.plan],'spec':[id(self.spec),asdict(self.spec)],
            'pins':[id(self._pins),self._pins],'mode':self.evidence_mode,
            'implementation':self._implementation_pin,
            'row_canonical_sha256':hashlib.sha256(self._rows_canonical).hexdigest(),
            'scope_canonical_sha256':hashlib.sha256(self._scope_canonical).hexdigest(),
            'class_methods':{n:id(getattr(type(self),n))for n in ('read','verify_rows','assert_memory_unchanged','_memory_snapshot')}})
    def assert_memory_unchanged(self):
        need(type(self)is VerifiedDeclaredWindingStudy and self._validated is True,'verified declared study required')
        need(_MEMORY_SNAPSHOT(self)==self._memory_pin,'declared study retained memory authority changed')

_MEMORY_SNAPSHOT=VerifiedDeclaredWindingStudy._memory_snapshot

def require_context(value):
    need(type(value)is VerifiedDeclaredWindingStudy and value._validated is True,'verified declared-winding study required')
    VerifiedDeclaredWindingStudy.assert_memory_unchanged(value)
def generate_zero_rows(spec,*,geometry=None,stack_length_mm=None,geometry_seed=42,quality_profile='reference_ultra'):
    """Explicit geometry is preserved exactly; sampling occurs only when both are absent."""
    implementation_sha256();context=validate_declared_spec(spec)
    from .doe import sample_feasible_geometries,physical_geometry_hash
    from ipmsm_optimization import phase_resistance_100c_ohm
    from calibrate_ipmsm_beta import quality_profile_values
    need((geometry is None)is(stack_length_mm is None),'explicit geometry and stack must be supplied together')
    if geometry is None:geometry,stack_length_mm,_=sample_feasible_geometries(spec,1,geometry_seed)[0]
    geometry,_=validate_geometry(spec,geometry,stack_length_mm)
    physical_hash=physical_geometry_hash(geometry,stack_length_mm)
    complete=asdict(spec);complete.pop('beta_calibration',None)
    identity=digest({'protocol_id':PROTOCOL_ID,'spec_without_zero':complete,'geometry17':geometry,'stack_length_mm':stack_length_mm,'quality_profile':quality_profile})[:20]
    common={'geometry_group_id':'declaredZeroGeom_'+identity,'design_hash':physical_hash,'doe_split':'calibration','repeat_of_case_id':'','source_case_id':'declaredZeroGeom_'+identity,
        'dataset_schema_version':'ipmsm_v2','model_extent':'full_360','symmetry_factor':1,'use_periodic_boundary':False,'beta_convention':'dq_current_advance_v2','initial_position_deg':-22.5,
        'slot_num':12,'pole_num':8,'stack_length_mm':stack_length_mm,'phase_resistance_ohm':phase_resistance_100c_ohm(geometry,stack_length_mm,spec.winding,slot_number=12),'vdc_v':spec.inverter.vdc_v,
        **context,**geometry,**quality_profile_values(quality_profile),'analysis_purpose':'screening','strict_setup':True,'compute_inductance':True,'require_steady_state':True,'zero_study_protocol':PROTOCOL_ID}
    return [{**common,'case_id':f'declZero_{identity}_{rpm}','operating_point_id':'declared_winding_zero_no_load','beta_calibration_id':'','electrical_zero_deg':0.,'beta_dq_deg':0.,'beta_deg':0.,'base_rpm':rpm,'i_peak_a':0.,'operation':'no_load'}for rpm in(1200,5000)]
def analyze_zero_rows(context):
    """Same signed-H1/circular math, on this verified protocol; no legacy context spoof."""
    require_context(context);context.verify_rows(context._rows);rows=context._rows
    from calibrate_ipmsm_beta import circular_mean_degrees,wrap_degrees,ZERO_CALIBRATION_METHOD,BETA_CONVENTION
    need(len(rows)==2 and all(row.get('status')=='ok'for row in rows),'exact two successful original rows; no filtering')
    observations=[];homogeneous={name:set()for name in('design_hash','input_quality_profile','input_setup_fingerprint','input_material_fingerprint','input_aedt_version','input_metric_contract_version','input_producer_fingerprint')};initials=[]
    expected=validate_declared_spec(context.spec)
    for row in rows:
        need(row.get('input_dataset_schema_version')=='ipmsm_v2'and row.get('input_metric_contract_version')=='motor_metrics_v3'and row.get('output_back_emf_source')=='periodic_flux_derivative','original v3 physical no-load EMF required')
        need(str(row.get('validation'))=='True'and str(row.get('output_steady_state_passed'))=='True','original validated steady-state required')
        for key,value in expected.items():
            actual=row.get('input_'+key);need(actual is not None,'actual explicit context missing: '+key)
            need(actual==value if isinstance(value,str)else math.isclose(float(actual),value,rel_tol=1e-12,abs_tol=1e-12),'actual declared context differs: '+key)
        for key,values in homogeneous.items():
            value=str(row.get(key,'')).strip();need(bool(value),'nonblank homogeneous original '+key+' required');values.add(value)
        need(row['input_operation']=='no_load'and float(row['input_i_peak_a'])==float(row['input_electrical_zero_deg'])==0. and not row.get('input_beta_calibration_id',''),'original NoLoad0/zero0/blankID required')
        initial=float(row['input_initial_position_deg']);speed=float(row['input_base_rpm']);cosine=float(row['output_back_emf_phasea_h1_cos_peak_v']);sine=float(row['output_back_emf_phasea_h1_sin_peak_v'])
        need(all(math.isfinite(v)for v in(initial,speed,cosine,sine))and speed>0 and math.hypot(cosine,sine)>0,'finite speed and signed nonzero H1 required')
        initials.append(initial);observations.append({'speed_rpm':speed,'cos_peak_v':cosine,'sin_peak_v':sine,'amplitude_peak_v':math.hypot(cosine,sine),'inferred_zero_deg':wrap_degrees(math.degrees(math.atan2(-cosine,-sine)))})
    need(all(len(v)==1 for v in homogeneous.values())and all(math.isclose(v,initials[0],rel_tol=0,abs_tol=1e-9)for v in initials),'same source/material/setup/geometry/frame rows required')
    speeds=sorted(x['speed_rpm']for x in observations);need(speeds==[1200.,5000.],'exact original1200/5000 speeds required')
    angle,resultant=circular_mean_degrees(x['inferred_zero_deg']for x in observations);deviation=max(abs(wrap_degrees(x['inferred_zero_deg']-angle))for x in observations);need(deviation<=3.,'existing circular max3deg policy exceeded')
    first=rows[0];context.verify_rows(rows)
    return {'method':ZERO_CALIBRATION_METHOD,'convention':BETA_CONVENTION,'electrical_zero_deg':angle,'initial_position_deg':initials[0],'successful_rows':2,'successful_speeds_rpm':speeds,'circular_resultant':resultant,'max_circular_deviation_deg':deviation,'observations':observations,
        'material_context':{k:expected[k]for k in('core_material_model','core_material')},'winding_context':{k:expected[k]for k in('series_turns_per_phase','turns_per_coil_side','conductor_area_per_branch_mm2','slot_fill_limit')},
        'material_fingerprint':first['input_material_fingerprint'],'metric_contract':first['input_metric_contract_version'],'producer_fingerprint':first['input_producer_fingerprint'],'back_emf_source':first['output_back_emf_source'],'design_hash':first['design_hash'],'quality_profile':first['input_quality_profile']}
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='action')
    g=sub.add_parser('generate');g.add_argument('--spec',type=Path,required=True);g.add_argument('--geometry',type=Path);g.add_argument('--stack-mm',type=float);g.add_argument('--geometry-seed',type=int,default=42);g.add_argument('--quality-profile',default='reference_ultra');g.add_argument('--output',type=Path,required=True)
    a=sub.add_parser('analyze');a.add_argument('--spec',type=Path,required=True);a.add_argument('--evidence-plan',type=Path,required=True);a.add_argument('--evidence-sha256',required=True);a.add_argument('--output',type=Path,required=True)
    args=p.parse_args(argv)
    if args.action is None:print('HELD_DECLARED_WINDING_NO_INPUTS_NO_NATIVE_NO_ID');return 0
    from . import production_zero as zero
    spec=zero.load_spec(json.loads(args.spec.read_bytes()));validate_declared_spec(spec)
    need(not args.output.exists(),'new exclusive output required')
    if args.action=='generate':
        geometry=None if args.geometry is None else json.loads(args.geometry.read_bytes());rows=generate_zero_rows(spec,geometry=geometry,stack_length_mm=args.stack_mm,geometry_seed=args.geometry_seed,quality_profile=args.quality_profile)
        args.output.mkdir(parents=True)
        with(args.output/'cases.csv').open('x',encoding='utf8',newline='')as f:w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
        context={'schema':SCHEMA,'protocol_id':PROTOCOL_ID,'declared_spec':{'path':str(args.spec.resolve()),**record(args.spec.read_bytes())},'declared_spec_canonical_sha256':digest(asdict(spec)),'declared_winding':asdict(spec.winding),'geometry17':{k:rows[0][k]for k in(*__import__('ipmsm_optimization').GEOMETRY_VARIABLE_NAMES,'slot_num','pole_num')},'stack_length_mm':rows[0]['stack_length_mm'],'case_ids':[r['case_id']for r in rows],'fresh_two_speed_raw_evidence_required':True,'Native_FEAs':0,'calibration_ID_issued':False}
        with(args.output/'DECLARED_CONTEXT_HELD.json').open('x',encoding='utf8')as f:json.dump(context,f,indent=2,sort_keys=True,allow_nan=False)
        print('HELD_TWO_SPEED_CASES_WRITTEN_NO_NATIVE_NO_ID');return 0
    plan=json.loads(args.evidence_plan.read_bytes());need(plan.get('schema')==EVIDENCE_SCHEMA and plan.get('protocol_id')==PROTOCOL_ID,'analyze requires this explicit new outer evidence protocol')
    context=zero.VerifiedProductionZero.from_plan(args.evidence_plan,args.evidence_sha256,spec=spec);manifest=zero.analyze_production_zero(spec,context)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf8')as f:json.dump(manifest,f,indent=2,sort_keys=True,allow_nan=False)
    print('DECLARED_WINDING_ZERO_WRITTEN_'+manifest['evidence_mode']);return 0

_IMPORTED_SHA=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
if __name__=='__main__':raise SystemExit(main())
