from __future__ import annotations
import argparse,csv,hashlib,html,json,os
from dataclasses import asdict
from pathlib import Path
import numpy as np
from .downward_detector import QualityConfig,TriggerConfig,detect_frame
METHODS={'sustained_edge':'sustained_edge_center','boundary':'boundary_center','drop_weighted':'centroid'}
COLORS={'manual':'#ffffff','sustained_edge':'#20c997','boundary':'#3b82f6','drop_weighted':'#f59e0b'}

def atomic_json(path,value):
 t=path.with_suffix(path.suffix+'.tmp')
 with t.open('w',encoding='utf-8') as f: json.dump(value,f,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
 os.replace(t,path)
def approved(study,include_uncertain=False):
 allowed={'accepted','uncertain'} if include_uncertain else {'accepted'};out=[]
 records=[json.loads(x) for x in (study/'frames.jsonl').read_text().splitlines() if x]
 for r in records:
  a=study/'annotations'/f"{r['study_frame_id']}.json"
  if not a.exists():continue
  ann=json.loads(a.read_text())
  if ann.get('status') not in allowed:continue
  p=(study/r['frame_file']).resolve()
  if study.resolve() not in p.parents:raise ValueError('Frame path escapes study directory')
  frame=np.load(p,allow_pickle=False)
  if frame.ndim!=1:raise ValueError(f'Frame is not 1-D: {p}')
  digest=hashlib.sha256(np.asarray(frame,dtype=np.uint16).tobytes()).hexdigest()
  if r.get('frame_sha256') and digest!=r['frame_sha256']:raise IOError(f"Integrity failure: {r['study_frame_id']}")
  out.append((r,ann,np.asarray(frame,dtype=np.uint16)))
 if not out:raise ValueError('No approved frames found')
 return out
def stats(errors,total):
 a=np.asarray(errors,float);z=np.abs(a)
 if not len(a):return {'count':0,'yield':0.0,'invalid_frames':total}
 return {'count':len(a),'yield':len(a)/total,'invalid_frames':total-len(a),'bias':float(a.mean()),'median_absolute_error':float(np.median(z)),'rmse':float(np.sqrt(np.mean(a*a))),'p95_absolute_error':float(np.percentile(z,95)),'maximum_absolute_error':float(z.max()),'within_1_pixel':float(np.mean(z<=1)),'within_2_pixels':float(np.mean(z<=2))}
def evaluate(study,trigger,quality,include_uncertain=False):
 items=approved(study,include_uncertain);rows=[];errors={k:[] for k in METHODS}
 for rec,ann,frame in items:
  result=detect_frame(frame,trigger,quality);region=result.selected_region;manual=(float(ann['manual_left_edge'])+float(ann['manual_right_edge']))/2
  row={'study_frame_id':rec['study_frame_id'],'sequence':rec['sequence'],'manual_midpoint':manual,'frame_file':rec['frame_file'],'detector_valid':region is not None}
  for name,field in METHODS.items():
   value=None if region is None else getattr(region,field,None);row[name]=value;row[name+'_error']=None if value is None else float(value)-manual
   if value is not None:errors[name].append(float(value)-manual)
  rows.append(row)
 summary={k:stats(v,len(rows)) for k,v in errors.items()}
 ranking=sorted(METHODS,key=lambda k:(summary[k].get('median_absolute_error',1e99),summary[k].get('p95_absolute_error',1e99),summary[k].get('rmse',1e99),-summary[k]['yield']))
 return {'approved_frames':len(rows),'trigger':asdict(trigger),'quality':asdict(quality),'methods':summary,'ranking':ranking,'recommended':ranking[0],'rows':rows}
def svg(frame,row,title):
 W,H=1200,420;L,T,PW,PH=55,42,1120,320;v=frame.astype(float);lo,hi=float(v.min()),float(v.max());pad=max(100,(hi-lo)*.05);lo=max(0,lo-pad);hi=min(65535,hi+pad)
 x=lambda p:L+float(p)/max(1,len(v)-1)*PW;y=lambda q:T+(hi-float(q))/max(1,hi-lo)*PH
 pts=' '.join(f'{x(i):.1f},{y(v[i]):.1f}' for i in range(0,len(v),max(1,len(v)//1000)))
 lines=[]
 for n in ['manual','sustained_edge','boundary','drop_weighted']:
  q=row['manual_midpoint'] if n=='manual' else row.get(n)
  if q is not None:lines.append(f'<line x1="{x(q):.1f}" y1="{T}" x2="{x(q):.1f}" y2="{T+PH}" stroke="{COLORS[n]}" stroke-width="3"'+(' stroke-dasharray="8 6"' if n=='manual' else '')+'/>')
 labels={'manual':'Manual reference','sustained_edge':'Sustained edge','boundary':'Boundary center','drop_weighted':'Drop weighted'};leg=[]
 for i,n in enumerate(labels):leg.append(f'<text x="{65+i*275}" y="400" fill="{COLORS[n]}" font-size="16">● {labels[n]}</text>')
 return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"><rect width="100%" height="100%" rx="16" fill="#0b1220"/><text x="55" y="27" fill="white" font-size="20">{html.escape(title)}</text><rect x="{L}" y="{T}" width="{PW}" height="{PH}" fill="#07101c" stroke="#334155"/><polyline fill="none" stroke="#7dd3fc" stroke-width="2" points="{pts}"/>{"".join(lines)}{"".join(leg)}</svg>'
def report(study,out,res,examples):
 out.mkdir(parents=True,exist_ok=False);(out/'frames').mkdir();atomic_json(out/'results.json',res)
 fields=sorted({k for r in res['rows'] for k in r});f=(out/'frame_results.csv').open('w',newline='',encoding='utf-8');w=csv.DictWriter(f,fields);w.writeheader();w.writerows(res['rows']);f.close()
 data={r['study_frame_id']:frame for r,a,frame in approved(study)};leader=res['recommended'];ordered=sorted(res['rows'],key=lambda r:abs(r.get(leader+'_error') or 0),reverse=True);idx=np.linspace(0,len(ordered)-1,min(examples,len(ordered)),dtype=int);cards=[]
 for i in idx:
  r=ordered[int(i)];name=f"frame_{int(r['sequence']):06d}.svg";(out/'frames'/name).write_text(svg(data[r['study_frame_id']],r,f"Approved frame {r['sequence']}"))
  cards.append(f'<section class="card"><img src="frames/{name}"><p>Sustained edge: {r["sustained_edge_error"]:+.2f} px | Boundary: {r["boundary_error"]:+.2f} px | Drop weighted: {r["drop_weighted_error"]:+.2f} px</p></section>')
 names={'sustained_edge':'Sustained edge','boundary':'Boundary center','drop_weighted':'Drop weighted'};trs=''.join(f'<tr><td>{i+1}</td><td>{names[n]}</td><td>{res["methods"][n]["yield"]:.1%}</td><td>{res["methods"][n].get("median_absolute_error",0):.3f}</td><td>{res["methods"][n].get("p95_absolute_error",0):.3f}</td><td>{res["methods"][n].get("rmse",0):.3f}</td></tr>' for i,n in enumerate(res['ranking']))
 doc=f'''<!doctype html><meta charset="utf-8"><title>Final LISM Centroid Comparison</title><style>body{{margin:0;background:#07111f;color:#eef6ff;font:16px/1.5 system-ui}}main{{max-width:1250px;margin:auto;padding:28px}}.card,.hero{{background:#111c2e;border:1px solid #27405e;border-radius:18px;padding:22px;margin:16px 0}}.winner{{color:#9ff4d4;font-weight:700}}table{{width:100%;border-collapse:collapse}}th,td{{padding:12px;border-bottom:1px solid #27405e;text-align:right}}th:nth-child(2),td:nth-child(2){{text-align:left}}img{{width:100%}}</style><main><section class="hero"><div class="winner">Recommended: {names[leader]}</div><h1>Final LISM Centroid Comparison</h1><p>{res['approved_frames']} manually approved frames. Lower error is better.</p></section><section class="card"><h2>Results</h2><table><tr><th>Rank</th><th>Method</th><th>Yield</th><th>Typical error</th><th>95% error</th><th>RMSE</th></tr>{trs}</table><p>Typical error means median absolute error. The white dashed line in each graphic is the manual reference.</p></section><h2>Approved frame examples</h2>{''.join(cards)}<section class="card"><h2>Scope</h2><p>This evaluates centroid choice only. It does not validate steering timing, calibration, or motion safety.</p></section></main>'''
 (out/'report.html').write_text(doc,encoding='utf-8')
def main():
 p=argparse.ArgumentParser();p.add_argument('--study',required=True);p.add_argument('--output',required=True);p.add_argument('--examples',type=int,default=12);p.add_argument('--include-uncertain',action='store_true');p.add_argument('--threshold',type=int,default=60000);p.add_argument('--minimum-width',type=int,default=2);p.add_argument('--maximum-width',type=int,default=512);p.add_argument('--minimum-integrated-drop',type=float,default=10000);p.add_argument('--maximum-gap',type=int,default=3);p.add_argument('--edge-window',type=int,default=5);p.add_argument('--edge-minimum-active',type=int,default=3);p.add_argument('--quantile-low',type=float,default=.1);p.add_argument('--quantile-high',type=float,default=.9);p.add_argument('--core-fraction',type=float,default=.8);p.add_argument('--core-minimum-width',type=int,default=2);p.add_argument('--maximum-center-disagreement',type=float,default=12);p.add_argument('--minimum-active-pixels',type=int,default=12);p.add_argument('--minimum-active-coverage',type=float,default=.35);p.add_argument('--maximum-internal-gap',type=int,default=8);p.add_argument('--maximum-internal-gap-fraction',type=float,default=.25);p.add_argument('--minimum-sustained-span',type=int,default=12);a=p.parse_args();study=Path(a.study).resolve();out=Path(a.output).resolve();res=evaluate(study,TriggerConfig.from_mapping(vars(a)),QualityConfig(),a.include_uncertain);report(study,out,res,a.examples);print(json.dumps({'recommended':res['recommended'],'ranking':res['ranking'],'report':str(out/'report.html')},indent=2))
if __name__=='__main__':main()
