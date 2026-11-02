"""Portable INERT FS graph. Genuine low-speed bytes + explicit synthetic high-speed transform.

No Native/FEA or actual high-speed/Source19 assertion. All origin files are new
temporary copies; original actor artifacts and research documents are untouched.
"""
from pathlib import Path
from dataclasses import asdict
import csv,gzip,hashlib,io,json,re,sys

SOURCE=Path(__file__).resolve().parents[1]
FIXTURE=Path(__file__).resolve().parent/'fixtures/ns40_production_origin'
def canonical(v):return json.dumps(v,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def rec(path):b=path.read_bytes();return {'path':str(path.absolute()),'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
def write(path,value):path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(canonical(value)+b'\n');return rec(path)
def csv_raw(row):
 out=io.StringIO(newline='');w=csv.DictWriter(out,fieldnames=list(row));w.writeheader();w.writerow(row);return out.getvalue().encode()
def materialize(root, *, source_root=None):
 source=Path(source_root).resolve()if source_root is not None else SOURCE
 root=Path(root).absolute();root.mkdir(parents=True,exist_ok=True);provenance=json.loads((FIXTURE/'PROVENANCE.json').read_bytes());data={}
 for name,want in provenance['records'].items():
  raw=gzip.decompress((FIXTURE/want['gzip']).read_bytes());assert len(raw)==want['bytes']and hashlib.sha256(raw).hexdigest()==want['sha256'];data[name]=raw
 library=root/'library';library.mkdir()
 for name,raw in data.items():
  if name.startswith('library/'):
   path=library/name.removeprefix('library/');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
 sys.path[:0]=[str(source),str(source/'src')]
 from pyaedt_motor.production_zero import load_spec
 import run_ipmsm_batch as runner
 from module.ipmsm_waveform_quality import analyze_exported_waveforms
 from ansys.aedt.core.internal import load_aedt_file as parser
 from research_ns40_zero import PHYSICAL,HELPERS
 spec_raw=json.loads((source/'configs/ipmsm_7p5kw_ns40.json').read_bytes());spec=load_spec(spec_raw);spec_ref=write(root/'spec.json',spec_raw)
 producer=runner.stable_fingerprint('producer_v3',{n:hashlib.sha256((source/n).read_bytes()).hexdigest()for n in PHYSICAL})
 original=json.loads(data['original_row.json'])['original_row'];case_base=next(csv.DictReader(io.StringIO(data['case.csv'].decode('utf-8-sig'))))
 rows=[];cases=[];raw_cases={}
 for rpm in (1200,5000):
  case={**case_base,'case_id':'INERT_NS40_'+str(rpm),'base_rpm':str(rpm)};case_spec=runner.build_spec(case)
  project=root/'results'/str(rpm)/'simulation';project.mkdir(parents=True)
  saved=data['saved.aedt'].decode('utf-8-sig')
  pattern=r"(VariableProp\('BaseRPM', '[^']*', '[^']*', ')[^']*(')"
  saved,count=re.subn(pattern,lambda m:m[1]+str(rpm)+m[2],saved);assert count==1
  saved_path=project/'simulation.aedt';saved_path.write_text(saved,encoding='utf-8',newline='\n')
  audit=json.loads(data['setup.json']);audit['requested_spec']=asdict(case_spec)
  setup_ref=write(project/'setup_audit.json',audit)
  exported={}
  for name,raw in data.items():
   if not name.startswith('reports/'):continue
   report=Path(name).stem;text=raw.decode('utf-8-sig');lines=text.splitlines();delimiter=';'if lines[0].count(';')>lines[0].count(',')else','
   header=next(csv.reader([lines[0]],delimiter=delimiter));result_raw=raw
   if rpm!=1200:
    time_index=next(i for i,v in enumerate(header)if v.startswith('Time '));factor=rpm/1200
    changed=[lines[0]]
    for textline in lines[1:]:
     if not textline.strip():continue
     sep=';'if textline.count(';')>textline.count(',')else',';line=next(csv.reader([textline],delimiter=sep))
     line[time_index]=format(float(line[time_index])/factor,'.17g')
     if report in ('PPT_Back_EMF','PPT_Phase_Voltages','PPT_PhaseA_Voltage_Limit'):
      for i in range(len(line)):
       if i!=time_index:line[i]=format(float(line[i])*factor,'.17g')
     one=io.StringIO(newline='');csv.writer(one,delimiter=sep,lineterminator='').writerow(line);changed.append(one.getvalue())
    result_raw=('\n'.join(changed)+'\n').encode()
   p=project/'exports'/(report+'.csv');p.parent.mkdir(exist_ok=True);p.write_bytes(result_raw);exported['artifact_report_'+report]=str(p)
  profile=project/'native.profile';profile.write_bytes(data['native.profile']);acf=project/'own.acf';acf.write_bytes(data['own.acf'])
  numeric=runner.summarize_transient_outputs(exported,case_spec,operation='no_load');numeric.update(analyze_exported_waveforms(exported,case_spec,'no_load'))
  row={**original,**{'input_'+k:v for k,v in case.items()},**numeric,**exported,'case_id':case['case_id'],'simulation_name':'simulation','project_path':str(project),'artifact_setup_audit':setup_ref['path'],'input_producer_fingerprint':producer}
  row['input_setup_fingerprint']=runner.setup_fingerprint(case_spec,case['quality_profile'],'no_load',False)
  identity=json.loads(row['standalone_owner_identity'])
  state={'schema':'ipmsm_standalone_solver_state_v1','case_id':case['case_id'],'producer_fingerprint':row['input_producer_fingerprint'],'owner_identity':identity,'owned_pid':identity[0],'stage':'analyze_returned_true','solver_state_uncertain':False,'Analyze_return_received':True,'Analyze_return_type':'builtins.bool','Analyze_return_repr':'True'}
  state_ref=write(project/'standalone_solver_state.json',state);row['artifact_standalone_solver_state']=state_ref['path']
  row={k:str(v)for k,v in row.items()};rows.append(row)
  cases.append({'input_case':case,'setup_audit':setup_ref,'saved_AEDT':rec(saved_path),'solver_state':state_ref})
  raw_cases[case['case_id']]={'reports':{k.removeprefix('artifact_report_'):rec(Path(v))for k,v in exported.items()},'native_profile':rec(profile),'own_acf':rec(acf)}
 combined=root/'results.csv';out=io.StringIO(newline='');writer=csv.DictWriter(out,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows);combined.write_bytes(out.getvalue().encode())
 def src(n):b=(source/n).read_bytes();return {'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}
 study={'schema':'ns40_research_zero_evidence_v1','source_root':str(source),'source_files':{n:src(n)for n in (*PHYSICAL,*HELPERS,'configs/material_m19_29g_reference_y2.json')},
  'library_root':str(library),'library_files':{p.relative_to(library).as_posix():{k:v for k,v in rec(p).items()if k!='path'}for p in library.rglob('*')if p.is_file()},
  'validation_code':{n:src(n)for n in ('calibrate_ipmsm_beta.py','research_ns40_zero.py')},'pure_parser':{k:v for k,v in rec(Path(parser.__file__)).items()if k!='path'},
  'material_reference':cases[0]['saved_AEDT'],'result_root':str(root/'results'),'result_csv':rec(combined),'geometry17':runner.extract_fixed_geometry(case_base),'cases':cases}
 study_ref=write(root/'study.json',study)
 plan={'schema':'ipmsm_production_zero_evidence_v1','evidence_mode':'inert_fixture','spec':spec_ref,'study':study_ref,'raw_cases':raw_cases,
       'synthetic_provenance':'INERT graph: genuine source172 low waveform/material bytes retained in separate PROVENANCE; NEW case/source/owner authority assertions are synthetic; 5000 time/voltage scaled analytically. No actual Source19 FEA or source equivalence claim.'}
 plan_ref=write(root/'production_origin.json',plan)
 return {'plan':root/'production_origin.json','sha256':plan_ref['sha256'],'spec':spec,'spec_raw':spec_raw,'rows':rows,'study':study,'plan_data':plan}
