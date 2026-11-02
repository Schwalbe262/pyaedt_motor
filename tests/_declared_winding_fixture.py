"""Explicit INERT copies only. No Ns32/36/48 simulation or actual authority."""
from dataclasses import asdict
from pathlib import Path
import copy,csv,io,json,re
from tests._ns40_production_fixture import materialize,rec,write
def declared_fixture(root,*,turns=36,area=20.):
    from pyaedt_motor import declared_winding_study as declared,production_zero as zero
    from ipmsm_optimization import phase_resistance_100c_ohm
    import run_ipmsm_batch as runner
    base=materialize(root);raw=copy.deepcopy(base['spec_raw']);raw['winding'].update(series_turns_per_phase=turns,turns_per_coil_side=turns//4,strand_area_mm2=area,strands_per_turn=1)
    geometry=base['study']['geometry17']
    # A fixture declares the actual retained geometry; never claim a different CAD.
    for bound in base['spec'].geometry_design_space:
        value=float(geometry[bound.name]);raw['design_space'][bound.name]=[min(bound.lower,value),max(bound.upper,value)]
    spec=zero.load_spec(raw);spec_ref=write(Path(root)/'spec.json',raw);study=copy.deepcopy(base['study']);rows=[];raw_cases={}
    for entry,oldrow in zip(study['cases'],base['rows']):
        oldid=entry['input_case']['case_id'];case=dict(entry['input_case']);case.update(series_turns_per_phase=str(turns),turns_per_coil_side=str(turns//4),conductor_area_per_branch_mm2=str(area),slot_fill_limit=str(spec.winding.fill_factor),zero_study_protocol=declared.PROTOCOL_ID)
        case['case_id']='INERT_DECLARED_N'+str(turns)+'_'+case['base_rpm'];case['phase_resistance_ohm']=str(phase_resistance_100c_ohm(geometry,float(case['stack_length_mm']),spec.winding,slot_number=12));case_spec=runner.build_spec(case)
        saved=Path(entry['saved_AEDT']['path']);text=saved.read_text(encoding='utf8');text,count=re.subn(r"('Conductor number'=')10(')",lambda m:m[1]+str(turns//4)+m[2],text);assert count==24
        text,count=re.subn(r"(VariableProp\('R_phase', '[^']*', '[^']*', ')[^']*(')",lambda m:m[1]+f'{case_spec.phase_resistance_ohm:g}ohm'+m[2],text);assert count==1;saved.write_text(text,encoding='utf8',newline='\n')
        audit=json.loads(Path(entry['setup_audit']['path']).read_bytes());audit['requested_spec']=asdict(case_spec);entry['setup_audit']=write(Path(entry['setup_audit']['path']),audit)
        marker=json.loads(Path(entry['solver_state']['path']).read_bytes());marker['case_id']=case['case_id'];entry['solver_state']=write(Path(entry['solver_state']['path']),marker);entry['saved_AEDT']=rec(saved);entry['input_case']=case
        row={**oldrow,**{'input_'+k:str(v)for k,v in case.items()},'case_id':case['case_id'],'input_setup_fingerprint':runner.setup_fingerprint(case_spec,case['quality_profile'],'no_load',False)};rows.append(row);raw_cases[case['case_id']]=base['plan_data']['raw_cases'][oldid]
    output=io.StringIO(newline='');writer=csv.DictWriter(output,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows);result=Path(study['result_csv']['path']);result.write_bytes(output.getvalue().encode());study['result_csv']=rec(result);study['material_reference']=study['cases'][0]['saved_AEDT']
    source=Path(study['source_root'])
    for name in declared.EXTRA_CODE:study['source_files'][name]={k:v for k,v in rec(source/name).items()if k!='path'}
    study['validation_code'][declared.MODULE]={k:v for k,v in rec(source/declared.MODULE).items()if k!='path'}
    study.update(schema=declared.SCHEMA,protocol_id=declared.PROTOCOL_ID,evidence_mode='inert_fixture',declared_spec=spec_ref,declared_spec_canonical_sha256=declared.digest(asdict(spec)),declared_winding=asdict(spec.winding))
    study_ref=write(Path(root)/'study.json',study);plan={**base['plan_data'],'schema':declared.EVIDENCE_SCHEMA,'protocol_id':declared.PROTOCOL_ID,'spec':spec_ref,'study':study_ref,'raw_cases':raw_cases,'synthetic_provenance':'INERT ONLY: separate copied Ns40 model boundary/metadata retargeted to declared turns/R; no new physical solve or production authority.'};plan_ref=write(Path(root)/'production_origin.json',plan)
    return {'plan':Path(plan_ref['path']),'sha256':plan_ref['sha256'],'spec':spec,'spec_raw':raw,'study':study,'plan_data':plan,'rows':rows}
