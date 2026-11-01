"""Explicit pure Ns40 research recipe; default dry, never Native/FEA/production."""
from pathlib import Path
from dataclasses import asdict
from collections import Counter
import argparse,ast,csv,hashlib,json,math,sys

SCHEMA='ns40_research_zero_evidence_v1'
PHYSICAL=('run_ipmsm_batch.py','module/ipmsm_geometry.py','module/ipmsm_ppt_setup.py','module/ipmsm_waveform_quality.py','module/variable.py')
HELPERS=('src/pyaedt_motor/public_batch_pilot.py','src/pyaedt_motor/native_state_semantics.py','ipmsm_optimization.py','generate_ipmsm_quality_cases.py')
MM={'stator_outer_radius','stator_shoe_thick','stator_gap','rotator_gap','magnet_shield_thick'}
def need(value,message):
    if not value:raise ValueError(message)
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def record(raw):return {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
def require_context(value):
    need(type(value)is VerifiedNs40Study and value._validated is True,'verified explicit Ns40 study evidence required')

class VerifiedNs40Study:
    def __init__(self):self._validated=False;self._pins={}
    def read(self,ref):
        need(type(ref)is dict and {'path','bytes','sha256'}<=set(ref),'sealed file ref required')
        path=Path(ref['path']);need(path.is_absolute()and path.is_file()and not path.is_symlink(),'regular absolute evidence file required')
        for part in (path,*path.parents):need(not getattr(part.lstat(),'st_file_attributes',0)&0x400,'reparse evidence forbidden')
        key=str(path.resolve(strict=True));want={k:ref[k]for k in ('bytes','sha256')}
        need(key not in self._pins or self._pins[key]==want,'conflicting first file authority')
        self._pins.setdefault(key,want);a=path.stat();raw=path.read_bytes();b=path.stat()
        need((a.st_size,a.st_mtime_ns,a.st_ino)==(b.st_size,b.st_mtime_ns,b.st_ino)and record(raw)==want,'evidence bytes changed')
        return raw
    @classmethod
    def from_plan(cls,path,sha256):
        obj=cls();raw=Path(path).read_bytes();need(record(raw)['sha256']==sha256,'explicit plan SHA required');plan=json.loads(obj.read({'path':str(Path(path).absolute()),**record(raw)}))
        need(plan['schema']==SCHEMA and len(plan['cases'])==2,'exact two-case Ns40 evidence plan required')
        root=Path(plan['source_root']);library=Path(plan['library_root']);need(set(PHYSICAL)|set(HELPERS)|{'configs/material_m19_29g_reference_y2.json'}<=set(plan['source_files']),'physical/verification source and M19 pack closure required')
        need(len(plan['library_files'])==23,'private library23 required')
        for base,files in ((root,plan['source_files']),(library,plan['library_files'])):
            for name,ref in files.items():
                member=Path(name);need(not member.is_absolute()and '..'not in member.parts,'source member escape')
                obj.read({'path':str(base/member),**ref})
        code=Path(__file__).resolve().parent
        for name in ('calibrate_ipmsm_beta.py','research_ns40_zero.py'):obj.read({'path':str(code/name),**plan['validation_code'][name]})
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
            spec=runner.build_spec(case);need((spec.series_turns_per_phase,spec.turns_per_coil_side)==(40,10),'explicit40/10 required')
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
            need(len(coils)==24 and set(groups)=={'PhaseA','PhaseB','PhaseC'}and all(v['Conductor number']=='10'for v in coils),'saved24 coils/10 turns')
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
        obj._rows=rows;obj.scope={'kind':'explicit_ns40_research_zero_v1','series_turns_per_phase':40,'turns_per_coil_side':10,'coils_per_phase':4,'parallel_branches':1,'geometry17':geometry,'initial_position_deg':initials[0],'stack_length_mm':depths[0],'source_producer':producer,'plan_sha256':sha256,'result_csv':plan['result_csv'],'production_training_or_model_approval':False}
        obj.scope.update({k:specs[0][k]for k in ('conductor_area_per_branch_mm2','phase_resistance_ohm','slot_fill_limit','core_material_model','core_material')});obj.scope['material_reference']=plan['material_reference']
        obj.scope['shared_spec']={k:v for k,v in specs[0].items()if k not in {'base_rpm','i_peak_a','beta_deg','electrical_zero_deg'}}
        obj._rows_canonical=canonical(rows);obj._scope_canonical=canonical(obj.scope)
        obj._validated=True;obj.verify_rows(rows);return obj
    def verify_rows(self,rows):
        require_context(self);need(canonical(list(rows))==self._rows_canonical and canonical(self.scope)==self._scope_canonical,'original two authoritative rows/scope changed/filtered/reordered')
        for path,want in self._pins.items():need(record(Path(path).read_bytes())==want,'source/evidence changed at analysis boundary')

def validate_manifest(manifest,context):
    require_context(context);context.verify_rows(context._rows)
    need(canonical(manifest.get('research_context'))==canonical(context.scope),'exact same Ns40 study required')
    value=dict(manifest);identifier=value.pop('calibration_id',None)
    need(identifier=='research-ns40-zero:sha256:'+hashlib.sha256(canonical(value)).hexdigest(),'Ns40-only ID mismatch; old48 ID forbidden')
    need(manifest['winding_context']['series_turns_per_phase']==40 and manifest['winding_context']['turns_per_coil_side']==10,'exact research winding in manifest')
    from calibrate_ipmsm_beta import analyze_zero_calibration_rows
    expected=analyze_zero_calibration_rows(context._rows,min_distinct_speeds=2,max_circular_deviation_deg=3.,_research_context=context)
    need(canonical(manifest)==canonical(expected),'manifest must exactly replay the authoritative two-speed observations')
    return manifest['electrical_zero_deg'],identifier

def apply_to_loaded_cases(cases,manifest,context):
    """Pure, separately reservable research input rows; never model admission."""
    zero,identifier=validate_manifest(manifest,context)
    import run_ipmsm_batch as runner
    output=[]
    for original in cases:
        spec=runner.build_spec(original);scope=context.scope
        need((spec.series_turns_per_phase,spec.turns_per_coil_side)==(40,10)and runner.extract_fixed_geometry(original)==scope['geometry17'],'loaded case outside exact Ns40 geometry/winding')
        need(spec.initial_position_deg==scope['initial_position_deg']and spec.stack_length_mm==scope['stack_length_mm']and spec.conductor_area_per_branch_mm2==scope['conductor_area_per_branch_mm2']and spec.phase_resistance_ohm==scope['phase_resistance_ohm']and spec.core_material==scope['core_material']and spec.core_material_model==scope['core_material_model'],'loaded material/depth/wire/frame context differs')
        need(spec.beta_convention==manifest['convention']and spec.analysis_purpose=='screening'and original.get('beta_calibration_id','')=='','explicit unqualified research loaded inputs only')
        need(canonical({k:v for k,v in asdict(spec).items()if k not in {'base_rpm','i_peak_a','beta_deg','electrical_zero_deg'}})==canonical(scope['shared_spec']),'loaded numerical/setup/winding spec differs from calibrated study')
        row={**original,'electrical_zero_deg':zero,'beta_calibration_id':identifier};row.pop('case_id',None)
        row['case_id']='research_ns40_loaded_r'+str(int(spec.base_rpm))+'_'+hashlib.sha256(canonical(row)).hexdigest()[:12]
        runner.build_spec(row);output.append(row)
    need(sorted(runner.build_spec(x).base_rpm for x in output)==[1200.,5000.]and len({x['case_id']for x in output})==2,'one exact two-speed research pair')
    context.verify_rows(context._rows);return output

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--analyze-research-ns40',action='store_true');p.add_argument('--plan',type=Path);p.add_argument('--plan-sha256');p.add_argument('--output',type=Path);a=p.parse_args(argv)
    if not a.analyze_research_ns40:print('HELD_EXPLICIT_NS40_RESEARCH_NO_NATIVE_NO_ID_ISSUED');return 0
    need(a.plan is not None and a.plan_sha256 and a.output is not None and not a.output.exists(),'explicit actual evidence and exclusive output required')
    context=VerifiedNs40Study.from_plan(a.plan,a.plan_sha256)
    from calibrate_ipmsm_beta import analyze_zero_calibration_rows
    answer=analyze_zero_calibration_rows(context._rows,min_distinct_speeds=2,max_circular_deviation_deg=3.,_research_context=context)
    validate_manifest(answer,context)
    with a.output.open('x',encoding='utf8')as f:json.dump(answer,f,sort_keys=True,indent=2,allow_nan=False);f.write('\n')
    print('PURE_NS40_RESEARCH_ZERO_WRITTEN_NOT_PRODUCTION_APPROVAL');return 0

if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from research_ns40_zero import main as entry
    raise SystemExit(entry())
