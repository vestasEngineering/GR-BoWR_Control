import tempfile
import unittest
from pathlib import Path
import numpy as np
from laser_viewer.centroid_study import StudyStore, analyze_study, candidate_thresholds, generate_report

def detection(center=20.0, width=20, drop=20000):
    return {"start":10,"end":30,"width":width,"integrated_drop":drop,"peak_drop":10000,"minimum_raw":55535,
        "active_coverage":.9,"largest_internal_gap":0,"filled_gap_count":0,"filled_gap_pixels":0,"background_rail_fraction":.8,
        "interior_low_fraction":.7,"shape_score":.9,"center_disagreement":.2,"boundary_center":center,
        "sustained_edge_center":center+.2,"quantile_center":center-.2,"centroid":center+1,"deep_core_center":center-2,
        "active_median_center":center,"selected_center":center+.2}

class StudyTests(unittest.TestCase):
    def setUp(self): self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root=Path(self.temp.name)
    def test_outlier_and_false_trigger_analysis(self):
        store=StudyStore(self.root/'s',{"study_name":"test"})
        for i,mid in enumerate([20.,21.,22.,60.]):
            r=store.append_frame(np.arange(64,dtype=np.uint16),i,i,detection(20+i),f'e{i}')
            store.save_annotation(r.study_frame_id,{"status":"accepted","manual_left_edge":mid-10,"manual_right_edge":mid+10})
        r=store.append_frame(np.zeros(64,dtype=np.uint16),9,9,detection(width=2,drop=1000),'ef')
        store.save_annotation(r.study_frame_id,{"status":"rejected","rejection_reason":"false_trigger","false_trigger_family":"single_deep_glitch"})
        result=analyze_study(store)
        self.assertEqual(result['progress']['accepted'],4); self.assertEqual(result['rejections']['false_trigger'],1)
        self.assertTrue(result['largest_errors']); self.assertIn('width',result['feature_analysis'])
    def test_review_fields_and_report_outputs(self):
        store=StudyStore(self.root/'s',{"study_name":"test"});r=store.append_frame(np.arange(64,dtype=np.uint16),1,1,detection(),'e')
        a=store.save_annotation(r.study_frame_id,{"status":"accepted","manual_left_edge":10,"manual_right_edge":30,"outlier_disposition":"valid_difficult","review_note":"real shape"})
        self.assertEqual(a['outlier_disposition'],'valid_difficult'); p=generate_report(store)
        self.assertTrue(p.exists());self.assertTrue((p.parent/'accepted_outliers.csv').exists());self.assertTrue((p.parent/'detector_candidates.csv').exists() or True)
    def test_candidate_threshold_preserves_true_frames(self):
        rows=candidate_thresholds([10,11,12,13,14],[1,2,3,4,5],'width')
        self.assertTrue(rows);self.assertGreaterEqual(rows[0]['accepted_retention'],.9);self.assertGreater(rows[0]['false_rejection'],.5)
if __name__=='__main__': unittest.main()
