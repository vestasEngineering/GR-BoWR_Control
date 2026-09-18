from __future__ import annotations
import argparse,csv,json,os,time
from dataclasses import asdict
from pathlib import Path
import numpy as np
from .downward_detector import QualityConfig,TriggerConfig,detect_frame

def json_value(value):
    if isinstance(value, np.ndarray): return value.tolist()
    if isinstance(value, np.generic): return value.item()
    if isinstance(value, dict): return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [json_value(v) for v in value]
    if isinstance(value, Path): return str(value)
    return value

def atomic_json(path,value):
    temp=path.with_suffix(path.suffix+'.tmp')
    with temp.open('w',encoding='utf-8') as f: json.dump(json_value(value),f,indent=2,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
def score(frames,trigger,quality):
    selected=[]
    for frame in frames:
        region=detect_frame(frame,trigger,quality).selected_region
        if region:selected.append(region)
    good=[r for r in selected if r.measurement_quality];n=len(frames)
    return {'total_frames':n,'basic_trigger_frames':len(selected),'good_frames':len(good),'basic_trigger_fraction':len(selected)/n if n else 0,'good_frame_yield':len(good)/n if n else 0,'good_trigger_precision':len(good)/len(selected) if selected else 0,'median_shape_score':float(np.median([r.shape_score for r in good])) if good else None,'centroid_std':float(np.std([r.centroid for r in good])) if good else None,'boundary_center_std':float(np.std([r.boundary_center for r in good])) if good else None,'median_background_rail_fraction':float(np.median([r.background_rail_fraction for r in selected])) if selected else None}
def main(argv=None):
    from .camera import LismCamera
    from .config import Config
    from .optimizer import Candidate
    p=argparse.ArgumentParser(description='Operator-guided camera setting optimizer for clean negative-going square waves');p.add_argument('--config',default='config/default.json');p.add_argument('--output',required=True);p.add_argument('--biases',default='254,270,286,302,318');p.add_argument('--gains',default='15');p.add_argument('--st-highs',default='8000,8500,9000,9500,10000');p.add_argument('--st-low',type=int,default=100);p.add_argument('--edge-delay',type=int,default=88);p.add_argument('--capture-frames',type=int,default=3000);p.add_argument('--settle-frames',type=int,default=32);p.add_argument('--threshold',type=int,default=60000);p.add_argument('--minimum-width',type=int,default=2);p.add_argument('--maximum-width',type=int,default=512);p.add_argument('--minimum-integrated-drop',type=float,default=10000);p.add_argument('--maximum-gap',type=int,default=1);p.add_argument('--max-false-fraction',type=float,default=.001);a=p.parse_args(argv)
    values=lambda s:[int(x) for x in s.split(',')]; candidates=[Candidate(b,g,h,a.st_low,a.edge_delay) for b in values(a.biases) for g in values(a.gains) for h in values(a.st_highs)];out=Path(a.output).expanduser().resolve();out.mkdir(parents=True,exist_ok=False);captures=out/'captures';captures.mkdir();cfg,base=Config.load(a.config);trigger=TriggerConfig(a.threshold,a.minimum_width,a.maximum_width,a.minimum_integrated_drop,a.maximum_gap);quality=QualityConfig();rows=[]
    with LismCamera(cfg,base) as camera:
        original=camera.configuration();primary=None
        try:
            for i,candidate in enumerate(candidates,1):
                print(f'\n[{i}/{len(candidates)}] {asdict(candidate)}');observed=camera.apply_temporary_configuration(**candidate.as_apply_args())
                input('Set scan and laser OFF, then press Enter: ');camera.start();off=camera.capture_frames(a.capture_frames,a.settle_frames);camera.stop()
                input('Set normal scan ON at fixed CENTER, then press Enter: ');camera.start();on=camera.capture_frames(a.capture_frames,a.settle_frames);camera.stop()
                stem=f'{i-1:03d}_b{candidate.bias}_g{candidate.gain}_h{candidate.st_high}';np.save(captures/f'{stem}_off.npy',off.astype(np.uint16));np.save(captures/f'{stem}_on.npy',on.astype(np.uint16));off_metrics=score(off,trigger,quality);on_metrics=score(on,trigger,quality);accepted=off_metrics['basic_trigger_fraction']<=a.max_false_fraction
                row={'candidate':asdict(candidate),'observed':observed,'accepted':accepted,'off':off_metrics,'on':on_metrics};rows.append(row);atomic_json(out/'camera_candidates.json',rows)
        except BaseException as exc: primary=exc;raise
        finally:
            try:camera.stop()
            except Exception:pass
            try:restored=camera.restore_configuration(original);atomic_json(out/'restored_configuration.json',restored)
            except Exception as exc:
                atomic_json(out/'restore_error.json',{'error':str(exc)})
                if primary is None:raise
    ranked=sorted(rows,key=lambda r:(r['accepted'],r['on']['good_frame_yield'],r['on']['good_trigger_precision'],r['on']['median_shape_score'] or 0),reverse=True);atomic_json(out/'camera_ranked.json',ranked);atomic_json(out/'recommended_configuration.json',ranked[0] if ranked and ranked[0]['accepted'] else None);print(json.dumps(ranked[:5],indent=2))
if __name__=='__main__':main()
