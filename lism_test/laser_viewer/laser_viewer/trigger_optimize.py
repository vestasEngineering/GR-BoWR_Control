from __future__ import annotations
import argparse, json, time
from pathlib import Path
import numpy as np
from .trigger_analysis import compare_off_on_fast

def parse_values(text, cast): return [cast(v.strip()) for v in text.split(',') if v.strip()]
def duration(seconds):
    seconds=max(0,int(seconds)); return f'{seconds//3600:02d}:{seconds%3600//60:02d}:{seconds%60:02d}'
def progress(dataset,pair,pairs,frame,frames,started):
    elapsed=time.monotonic()-started; done=(pair-1)*frames+frame; total=pairs*frames; rate=done/elapsed if elapsed else 0; remaining=(total-done)/rate if rate else 0
    print(f'\r{dataset} {pair}/{pairs} frames {frame:,}/{frames:,} elapsed {duration(elapsed)} remaining {duration(remaining)}',end='',flush=True)
    if pair==pairs and frame==frames: print()
def main(argv=None):
    p=argparse.ArgumentParser(description='Memory-bounded exact LISM trigger optimizer');p.add_argument('--off',required=True);p.add_argument('--on',required=True);p.add_argument('--output',default='trigger_optimizer_results.json');p.add_argument('--thresholds',default='50000,55000,60000');p.add_argument('--minimum-widths',default='2,4,8');p.add_argument('--integrated-drops',default='10000,100000,500000');p.add_argument('--maximum-gaps',default='0,1,2');p.add_argument('--maximum-width',type=int,default=512);p.add_argument('--max-false-fraction',type=float,default=.001);a=p.parse_args(argv)
    off=np.load(a.off,allow_pickle=False,mmap_mode='r');on=np.load(a.on,allow_pickle=False,mmap_mode='r');grid={'threshold':parse_values(a.thresholds,int),'minimum_width':parse_values(a.minimum_widths,int),'minimum_integrated_drop':parse_values(a.integrated_drops,float),'maximum_gap':parse_values(a.maximum_gaps,int)}
    print(f'Loaded OFF: {len(off):,} frames\nLoaded ON:  {len(on):,} frames\nCandidates: {int(np.prod([len(v) for v in grid.values()]))}\nPeak region cache: disabled (streaming mode)')
    started=time.monotonic();rows=compare_off_on_fast(off,on,{'maximum_width':a.maximum_width},grid,max_false_fraction=a.max_false_fraction,progress=progress);Path(a.output).write_text(json.dumps(rows,indent=2,allow_nan=False),encoding='utf-8')
    print(f'Completed in {duration(time.monotonic()-started)}\nAccepted candidates: {sum(r["accepted"] for r in rows)}/{len(rows)}\nResults: {Path(a.output).resolve()}');print(json.dumps(rows[:10],indent=2))
if __name__=='__main__':main()
