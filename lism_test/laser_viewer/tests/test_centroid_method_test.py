import unittest
import numpy as np
from laser_viewer.centroid_method_test import TestConfig, calculate_methods, method_ranking, select_region

class CentroidMethodTests(unittest.TestCase):
    def test_symmetric_square_wave_centers_match(self):
        frame=np.full(128,65535,dtype=np.uint16);frame[40:60]=0
        config=TestConfig(maximum_width=100,minimum_integrated_drop=1)
        region=select_region(frame,config);methods=calculate_methods(frame,region,config)
        for value in methods.values(): self.assertAlmostEqual(value,49.5)

    def test_boundary_center_resists_asymmetric_amplitude(self):
        frame=np.full(128,65535,dtype=np.uint16);frame[40:60]=0;frame[40:45]=30000
        config=TestConfig(maximum_width=100,minimum_integrated_drop=1,threshold=60000)
        region=select_region(frame,config);methods=calculate_methods(frame,region,config)
        self.assertAlmostEqual(methods['boundary_center'],49.5)
        self.assertGreater(methods['rail_weighted'],49.5)

    def test_ranking_rewards_repeatability_and_return(self):
        summaries=[]
        for label,a,b in [('LEFT',100,100),('CENTER',200,200),('RIGHT',300,300),('RETURN_CENTER',201,205)]:
            summaries.append({'label':label,'methods':{'stable':{'count':20,'mean':a,'robust_sigma_mad':1,'standard_deviation':1},'noisy':{'count':20,'mean':b,'robust_sigma_mad':10,'standard_deviation':10}}})
        ranking=method_ranking(summaries,['LEFT','CENTER','RIGHT'],('CENTER','RETURN_CENTER'),10)
        self.assertEqual(ranking[0]['method'],'stable')

if __name__=='__main__':unittest.main()
