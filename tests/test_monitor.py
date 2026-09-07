import unittest

from bms.monitor import balance_needed, balance_settled, precharge_complete, summarise
from tests.helpers import healthy, make_config


class TestSummary(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_locates_extremes_with_indices(self):
        snap = healthy(self.config)
        snap.cell_volts[3] = 4.10
        snap.cell_volts[6] = 3.20
        snap.cell_temps[5] = 47.0

        s = summarise(snap, self.config)

        self.assertEqual(s.max_cell_index, 3)
        self.assertEqual(s.min_cell_index, 6)
        self.assertEqual(s.max_temp_index, 5)
        self.assertAlmostEqual(s.imbalance_volts, 0.90)

    def test_pack_voltage_is_the_sum_of_cells(self):
        s = summarise(healthy(self.config, volts=3.70), self.config)
        self.assertAlmostEqual(s.pack_volts, 3.70 * 8)

    def test_charging_is_negative_current(self):
        self.assertTrue(summarise(healthy(self.config, current=-5.0), self.config).charging)
        self.assertFalse(summarise(healthy(self.config, current=5.0), self.config).charging)

    def test_shape_mismatch_is_rejected(self):
        snap = healthy(self.config)
        snap.cell_volts.pop()
        # A short list would otherwise read as a cell at 0 V, hiding a wiring
        # fault behind an undervoltage fault.
        with self.assertRaises(ValueError):
            summarise(snap, self.config)


class TestPrecharge(unittest.TestCase):
    """EV.5.6.1 a: 90 % of pack voltage, decided by feedback (EV.5.6.2 a)."""

    def setUp(self):
        self.config = make_config()
        self.pack = 3.70 * 8

    def test_completes_at_exactly_ninety_percent(self):
        snap = healthy(self.config, intermediate=self.pack * 0.90)
        self.assertTrue(precharge_complete(summarise(snap, self.config), self.config))

    def test_does_not_complete_just_below(self):
        snap = healthy(self.config, intermediate=self.pack * 0.899)
        self.assertFalse(precharge_complete(summarise(snap, self.config), self.config))

    def test_empty_pack_reports_no_progress_instead_of_crashing(self):
        snap = healthy(self.config, volts=0.0, intermediate=0.0)
        self.assertEqual(summarise(snap, self.config).precharge_ratio, 0.0)


class TestBalancing(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def _with_imbalance(self, delta):
        snap = healthy(self.config)
        snap.cell_volts[0] += delta
        return summarise(snap, self.config)

    def test_balancing_starts_above_threshold(self):
        self.assertTrue(balance_needed(self._with_imbalance(0.06), self.config))
        self.assertFalse(balance_needed(self._with_imbalance(0.04), self.config))

    def test_hysteresis_stops_lower_than_it_starts(self):
        # 0.045 V is under the 0.05 V start bar but over the 0.04 V stop bar,
        # so a pack drifting here keeps bleeding instead of chattering.
        mid = self._with_imbalance(0.045)
        self.assertFalse(balance_needed(mid, self.config))
        self.assertFalse(balance_settled(mid, self.config))


if __name__ == "__main__":
    unittest.main()
