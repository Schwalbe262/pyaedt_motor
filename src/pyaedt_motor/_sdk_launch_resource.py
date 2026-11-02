"""One Ansys-only resource input, bounded by this process's actual Slurm job/step.

No SDK/Native imports, scheduler mutations, proxy calls or SLURM environment writes.
Only capture_current performs two own-job read-only scontrol commands.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib, os, re, shutil, socket, subprocess, time

NAME='ANSYSEM_TASKS_PER_NODE'
VALUE='4'
POLICY={'name':NAME,'value':VALUE,'requested_cores':4,'minimum_job_CPUs':64,'minimum_step_CPUs':4,
        'UID':1641,'account':'dw16','node':'n116','maximum_capture_age_seconds':30,
        'command_timeout_seconds':10,'command_output_cap_bytes':65536,'read_only':True}
SOURCE_PINS={
 '/opt/ohpc/pub/Electronics/v252/AnsysEM/schedulers/scripts/SLURM_GetRunInfo_linux.py':
 {'bytes':7386,'sha256':'7b2a581d4915c4d716a8e43d0614b4facc615e745af8b73a1da509b514ebfb39'},
 '/opt/ohpc/pub/Electronics/v252/AnsysEM/schedulers/scripts/SLURM_Util.py':
 {'bytes':8975,'sha256':'96d08faf9a6f86906588854f463a31810565ab5212d4dc7242b4e000fc4efbd0'}}
SLURM_KEYS=('SLURM_JOB_ID','SLURM_JOBID','SLURM_STEP_ID','SLURM_STEPID','SLURM_JOB_NODELIST',
 'SLURM_NODELIST','SLURM_STEP_NODELIST','SLURM_CPUS_PER_TASK','SLURM_CPUS_ON_NODE',
 'SLURM_JOB_CPUS_PER_NODE','SLURM_NTASKS','SLURM_TASKS_PER_NODE','SLURM_STEP_NUM_TASKS',
 'SLURM_STEP_TASKS_PER_NODE','SLURM_SCHEDULER_STEP_CPUS','SLURM_SCHEDULER_TASK_CPUS')

def need(v,m):
 if not v:raise ValueError(m)

def rec(raw):return {'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def check_installed_sources(h):
 for path,want in SOURCE_PINS.items():need(h.record(path)==want,'installed proxy source changed: '+path)
 return SOURCE_PINS

def snapshot(env):
 result={key:env.get(key)for key in SLURM_KEYS}
 need(all(v is None or type(v)is str and len(v)<=256 and '\n'not in v and '\r'not in v for v in result.values()),'bounded Slurm whitelist required')
 return result

def ids(env):
 job=env.get('SLURM_JOB_ID');step=env.get('SLURM_STEP_ID')
 need(type(job)is str and re.fullmatch(r'[1-9][0-9]{0,10}',job),'strict numeric current job required')
 need(type(step)is str and re.fullmatch(r'0|[1-9][0-9]{0,8}',step),'strict numeric current step required')
 for key,value in [('SLURM_JOBID',job),('SLURM_STEPID',step)]:need(key not in env or env[key]==value,'Slurm identity alias conflict')
 need(env.get('SLURM_CPUS_PER_TASK')=='4'and env.get('SLURM_SCHEDULER_TASK_CPUS')=='4','actual task request must be4')
 return job,step

def fields(raw,keys):
 need(type(raw)is bytes and 0<len(raw)<=65536,'bounded nonempty scontrol bytes required')
 text=raw.decode('utf-8',errors='strict').strip();need(len(text.splitlines())==1,'one exact current scontrol record required')
 result={}
 for key in keys:
  values=re.findall(r'(?:^|\s)'+re.escape(key)+r'=([^\s]+)',text)
  need(len(values)==1,'missing/duplicate scontrol key: '+key);result[key]=values[0]
 return result

def evaluate(job_raw,step_raw,env,*,uid,node,policy=None):
 selected=POLICY if policy is None else policy
 need(type(uid)is int and uid==1641 and node==selected['node']and selected['UID']==1641 and selected['account']=='dw16','own UID1641/explicit node required')
 jobid,stepid=ids(env);job=fields(job_raw,('JobId','UserId','JobState','NodeList','NumNodes','NumCPUs'))
 step=fields(step_raw,('StepId','UserId','State','NodeList','Nodes','CPUs','Tasks'))
 need(job['JobId']==jobid and step['StepId']==jobid+'.'+stepid,'scontrol current job/step mismatch')
 need(job['UserId']=='dw16(1641)'and step['UserId']=='1641','actual own job/step UID required')
 need(job['JobState']==step['State']=='RUNNING'and job['NodeList']==step['NodeList']==node,'current running single explicit allocation required')
 need(job['NumNodes']==step['Nodes']=='1','one node required')
 for v in (job['NumCPUs'],step['CPUs'],step['Tasks']):need(re.fullmatch(r'[1-9][0-9]{0,8}',v),'typed positive Slurm resource required')
 allocated,step_cpus=int(job['NumCPUs']),int(step['CPUs'])
 minimum=selected['minimum_job_CPUs'];need(type(minimum)is int and minimum>=4,'typed job minimum>=4 required')
 need(minimum<=allocated and 4<=step_cpus<=allocated and step['Tasks']=='1','resource4 must fit actual job minimum and own step>=4/one task')
 return {'job':job,'step':step,'requested_Ansys_resource_slots':4,'actual_job_allocated_CPUs':allocated,
         'actual_step_CPUs':step_cpus,'resource4_within_observed_allocation':True,'actual_Native_solve_cores_proven':False}

def capture_current(h,env,*,policy=None):
 selected=POLICY if policy is None else policy
 need(os.name=='posix'and os.getuid()==os.geteuid()==1641 and socket.gethostname().split('.')[0]==selected['node'],'exact current worker principal required')
 prior=snapshot(env);job,step=ids(env);check_installed_sources(h)
 executable=shutil.which('scontrol');need(executable is not None,'scontrol required; no fallback')
 executable=str(Path(executable).resolve(strict=True));need(Path(executable).name=='scontrol','exact scontrol executable required')
 executable_record=h.record(executable);responses=[];commands=[];start=time.monotonic()
 for kind,identifier in [('job',job),('step',job+'.'+step)]:
  command=[executable,'--oneliner','show',kind,identifier]
  with __import__('tempfile').TemporaryFile()as stdout,__import__('tempfile').TemporaryFile()as stderr:
   completed=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,check=False,timeout=10)
   need(stdout.tell()<=65536 and stderr.tell()<=65536,'scontrol output exceeds cap');stdout.seek(0);stderr.seek(0);raw=stdout.read(65537);err=stderr.read(65537)
  need(completed.returncode==0 and not err,'readonly current job/step query failed; no fallback')
  responses.append(raw);commands.append({'argv':command,'returncode':0,'stdout_record':rec(raw),'stderr_record':rec(err)})
 need(snapshot(env)==prior and h.record(executable)==executable_record,'Slurm environment or query executable drift')
 selected=evaluate(*responses,env,uid=os.getuid(),node=socket.gethostname().split('.')[0],policy=selected);check_installed_sources(h)
 need(time.monotonic()-start<=30,'stale resource observation')
 return {'schema':'owned_current_Slurm_Ansys_resource4_gate_v1','observed_UTC':datetime.now(timezone.utc).isoformat(),
         'commands':commands,'executable':{'path':executable,**executable_record},'selected':selected,
         'Slurm_whitelist':prior,'proxy_sources':SOURCE_PINS,'proxy_executed':False,
         **({'raw_job':responses[0].decode('utf-8'),'raw_step':responses[1].decode('utf-8')}if policy is not None else {})},start

class Scope:
 def __init__(self,env,evidence,captured_at):
  need(evidence['selected']['resource4_within_observed_allocation']is True,'positive current resource gate required')
  need(snapshot(env)==evidence['Slurm_whitelist'],'Slurm drift before resource binding')
  need(NAME not in env or env[NAME]==VALUE,'conflicting Ansys resource input')
  need(0<=time.monotonic()-captured_at<=30,'stale resource evidence')
  self.env=env;self.slurm=snapshot(env);self.old=(NAME in env,env.get(NAME));self.captured_at=captured_at
  before=dict(env);env[NAME]=VALUE
  need({k:v for k,v in env.items()if k!=NAME}=={k:v for k,v in before.items()if k!=NAME},'only Ansys resource input may change')
 def check(self):
  need(snapshot(self.env)==self.slurm and self.env.get(NAME)==VALUE,'pre-factory Ansys/Slurm drift')
  need(0<=time.monotonic()-self.captured_at<=30,'stale pre-factory resource observation')
 def observe_native(self,native_env):
  need(native_env.get(NAME)==VALUE,'owned Native Ansys resource4 readback differs')
  return {'name':NAME,'requested':VALUE,'owned_Native_observed':VALUE,'Native_solver4cores_proven':False,'proxy_call_observed':False}
 def restore(self):
  try:after=snapshot(self.env);unchanged=after==self.slurm
  except ValueError:after=None;unchanged=False
  finally:
   if self.old[0]:self.env[NAME]=self.old[1]
   else:self.env.pop(NAME,None)
  return {'Slurm_whitelist_unchanged':unchanged,'Slurm_whitelist_after':after,
          'Ansys_resource_input_restored':(NAME in self.env,self.env.get(NAME))==self.old}
