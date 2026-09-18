import unittest

import numpy as np

from laser_viewer.downward_detector import TriggerConfig, detect_frame


class DownwardDetectorTests(unittest.TestCase):
    def config(self, **changes):
        values = dict(
            threshold=60000,
            minimum_width=2,
            maximum_width=512,
            minimum_integrated_drop=1000,
            maximum_gap=3,
            edge_window=5,
            edge_minimum_active=3,
            maximum_center_disagreement=30.0,
        )
        values.update(changes)
        return TriggerConfig(**values)

    def test_noisy_broad_trough_geometric_center_is_stable(self):
        rng = np.random.default_rng(7)
        frame = np.full(2048, 65535, dtype=np.uint16)
        frame[1300:1601] = rng.integers(500, 59000, size=301, dtype=np.uint16)
        region = detect_frame(frame, self.config()).selected_region
        self.assertIsNotNone(region)
        self.assertAlmostEqual(region.boundary_center, 1450.0)
        self.assertAlmostEqual(region.sustained_edge_center, 1450.0, delta=2.0)
        self.assertAlmostEqual(region.selected_center, 1450.0, delta=2.0)

    def test_internal_short_gaps_are_joined(self):
        frame = np.full(2048, 65535, dtype=np.uint16)
        frame[620:931] = 12000
        frame[700:702] = 65535
        frame[810:813] = 65535
        region = detect_frame(frame, self.config(maximum_gap=3)).selected_region
        self.assertIsNotNone(region)
        self.assertEqual((region.start, region.end), (620, 930))
        self.assertEqual(region.filled_gap_count, 2)
        self.assertAlmostEqual(region.selected_center, 775.0, delta=2.0)

    def test_large_gap_remains_two_regions(self):
        frame = np.full(256, 65535, dtype=np.uint16)
        frame[40:80] = 1000
        frame[100:140] = 1000
        result = detect_frame(frame, self.config(maximum_gap=3, maximum_width=100))
        self.assertEqual(len(result.regions), 2)

    def test_clipped_bottom_recovers_center_and_core(self):
        frame = np.full(2048, 65535, dtype=np.uint16)
        frame[920:1211] = 0
        region = detect_frame(frame, self.config()).selected_region
        self.assertIsNotNone(region)
        self.assertAlmostEqual(region.boundary_center, 1065.0)
        self.assertAlmostEqual(region.sustained_edge_center, 1065.0, delta=2.0)
        self.assertAlmostEqual(region.deep_core_center, 1065.0, delta=1.0)
        self.assertAlmostEqual(region.quantile_center, 1065.0, delta=1.0)

    def test_amplitude_asymmetry_moves_energy_centroid_more_than_edges(self):
        frame = np.full(512, 65535, dtype=np.uint16)
        frame[100:301] = 50000
        frame[100:180] = 0
        region = detect_frame(frame, self.config()).selected_region
        self.assertIsNotNone(region)
        self.assertLess(region.centroid, 190.0)
        self.assertAlmostEqual(region.sustained_edge_center, 200.0, delta=2.0)

    def test_single_pixel_glitch_is_rejected(self):
        frame = np.full(256, 65535, dtype=np.uint16)
        frame[100] = 0
        self.assertIsNone(detect_frame(frame, self.config()).selected_region)

    def test_stronger_region_is_selected(self):
        frame = np.full(256, 65535, dtype=np.uint16)
        frame[10:20] = 59000
        frame[100:121] = 1000
        region = detect_frame(frame, self.config()).selected_region
        self.assertEqual(region.start, 100)

    def test_configuration_id_changes_with_centroid_parameters(self):
        first = self.config().configuration_id
        second = self.config(quantile_low=0.20).configuration_id
        self.assertNotEqual(first, second)

    def test_invalid_quantiles_are_rejected(self):
        with self.assertRaises(ValueError):
            self.config(quantile_low=0.90, quantile_high=0.10)


if __name__ == "__main__":
    unittest.main()
