"""Regenerate the two panels from the included 301 raw-report samples; no FEA."""
from pathlib import Path
import argparse,csv,hashlib,json,math,os

def record(p):
 b=Path(p).read_bytes();return {'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parent)
 p.add_argument('--output-dir',type=Path,default=Path('regenerated'))
 a=p.parse_args();e=json.loads((a.data_dir/'evidence.json').read_bytes())
 for name,want in e['package_input_records'].items():
  if record(a.data_dir/name)!=want:raise ValueError('Data/script hash differs: '+name)
 with(a.data_dir/'last_cycle.csv').open(encoding='utf-8',newline='')as f:rows=list(csv.DictReader(f))
 if len(rows)!=301:raise ValueError('Expected exactly301 existing report samples')
 time=[float(x['time_ms_raw'])for x in rows]
 if not all(x<y for x,y in zip(time,time[1:])):raise ValueError('Time is not strictly increasing')
 a.output_dir.mkdir(parents=True,exist_ok=True)
 os.environ.setdefault('MPLCONFIGDIR',str(a.output_dir/'.matplotlib_config'))
 import matplotlib
 matplotlib.use('Agg')
 import matplotlib.pyplot as plt
 plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'svg.hashsalt':'actual164-166-band-pair-v1','axes.spines.top':False,'axes.spines.right':False})
 fig,axes=plt.subplots(2,1,sharex=True,figsize=(9.2,6.1))
 fig.subplots_adjust(left=.10,right=.98,bottom=.10,top=.79,hspace=.13)
 styles=[('164','#0072B2','-','Band144; 9,828 triangles'),('166','#D55E00','--','Band0; 13,328 triangles')]
 for attempt,color,style,label in styles:
  torque=[float(x['signed_torque_'+attempt+'_Nm'])for x in rows]
  loss=[float(x['core_plus_solid_'+attempt+'_W'])for x in rows]
  if not all(math.isfinite(x)for x in torque+loss):raise ValueError('Nonfinite plotted value')
  axes[0].plot(time,torque,color=color,linestyle=style,linewidth=1.35,label=label)
  axes[1].plot(time,loss,color=color,linestyle=style,linewidth=1.35,label=label)
 axes[0].axhline(0,color='0.5',linewidth=.6,zorder=0)
 axes[0].set_ylabel('Signed torque [N m]')
 fig.text(.10,.915,'No-load at 1200 rpm: same 301 raw report times',fontsize=12)
 fig.legend(*axes[0].get_legend_handles_labels(),loc='upper center',bbox_to_anchor=(.54,.88),fontsize=9,ncol=2,frameon=False)
 axes[1].set_ylabel('Core + Solid loss [W]')
 axes[1].set_xlabel('Actual report time [ms]')
 axes[1].set_xlim(time[0],time[-1])
 for ax in axes:ax.grid(True,color='0.88',linewidth=.6);ax.set_axisbelow(True)
 fig.suptitle('Band CAD comparison; actual meshes differ',fontsize=13,y=.975)
 fig.savefig(a.output_dir/'band_geometry_pair.svg',metadata={'Date':None,'Creator':'regenerate.py'})
 fig.savefig(a.output_dir/'band_geometry_pair.png',dpi=200,metadata={'Software':'regenerate.py'})
 plt.close(fig)
 print(json.dumps({'rows':len(rows),'time_ms_first':time[0],'time_ms_last':time[-1],'matplotlib':matplotlib.__version__,'Native_FEAs':0}))

if __name__=='__main__':main()
