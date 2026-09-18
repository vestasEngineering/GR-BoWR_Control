from __future__ import annotations
import csv, json, queue, threading, time
from pathlib import Path
import numpy as np

class TriggerSessionRecorder:
    def __init__(self, root, label, manifest, queue_size=4096):
        safe=lambda s: ''.join(c if c.isalnum() or c in '-_' else '_' for c in str(s))
        self.directory=Path(root)/safe(manifest['camera_id'])/(time.strftime('%Y%m%d_%H%M%S')+'_'+safe(label))
        self.directory.mkdir(parents=True, exist_ok=False)
        self.manifest=manifest; self.q=queue.Queue(queue_size); self.frames=[]; self.rows=[]; self.dropped=0; self.error=None
        self._stop=object(); self.worker=threading.Thread(target=self._run,name=f"LISM-recorder-{manifest['camera_id']}",daemon=True); self.worker.start()
    def submit(self, number, timestamp, frame, result):
        if result.selected_region is None: return
        try:self.q.put_nowait((number,timestamp,np.asarray(frame,dtype=np.uint16).copy(),result.selected_region))
        except queue.Full:self.dropped+=1
    def _run(self):
        try:
            while True:
                item=self.q.get()
                if item is self._stop:return
                number,timestamp,frame,r=item
                row={'camera_id':self.manifest['camera_id'],'camera_serial':self.manifest['camera_serial'],'session_id':self.manifest['session_id'],'frame_number':number,'timestamp_monotonic':timestamp}
                for name in r.__dataclass_fields__: row[name]=getattr(r,name)
                row['peak_drop']=65535-int(r.minimum_raw); self.frames.append(frame); self.rows.append(row)
        except Exception as exc:self.error=str(exc)
    def stop(self):
        self.q.put(self._stop); self.worker.join(30)
        if self.worker.is_alive():raise RuntimeError('Recorder did not stop')
        if self.error:raise RuntimeError(self.error)
        np.save(self.directory/'frames.npy',np.stack(self.frames) if self.frames else np.empty((0,0),dtype=np.uint16),allow_pickle=False)
        fields=list(self.rows[0]) if self.rows else ['camera_id','frame_number']
        with (self.directory/'trigger_events.csv').open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(self.rows)
        (self.directory/'session_manifest.json').write_text(json.dumps(self.manifest,indent=2),encoding='utf-8')
        (self.directory/'summary.json').write_text(json.dumps({'trigger_frames':len(self.frames),'dropped':self.dropped,'complete':True},indent=2),encoding='utf-8')
        return self.directory
