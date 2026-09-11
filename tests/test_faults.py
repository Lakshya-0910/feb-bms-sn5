import unittest

from bms.faults import FaultManager
from bms.outputs import Outputs
from bms.types import Fault, State
from tests.helpers import healthy, make_config, run_for


class TestDebounce(unittest.TestCase):
    """A condition must persist before it latches. Windows differ by physics."""

    def setUp(self):
        self.config = make_config()
        self.manager = FaultManager(self.config)

    def test_overvoltage_does_not_latch_before_the_window(self):
        snap = healthy(self.config)
        snap.cell_volts[2] = 4.30
        # 40 ms against a 50 ms voltage window.
        self.assertFalse(bool(run_for(self.manager, snap, self.config, ms=40)))

    def test_overvoltage_latches_once_the_window_elapses(self):
        snap = healthy(self.config)
        snap.cell_volts[2] = 4.30
        active = run_for(self.manager, snap, self.config, ms=50)
        self.assertIn(Fault.CELL_OVERVOLT, active)

    def test_a_transient_never_latches(self):
        spike = healthy(self.config)
        spike.cell_volts[2] = 4.30
        run_for(self.manager, spike, self.config, ms=30)
        run_for(self.manager, healthy(self.config), self.config, ms=30)
        run_for(self.manager, spike, self.config, ms=30)
        # The counter restarted when the condition cleared, so 30 + 30 ms of
        # noise never reaches the 50 ms window.
        self.assertFalse(self.manager.faulted)

    def test_missing_measurements_latch_immediately(self):
        # EV.7.3.4 d. A sensor that cannot be read is not noise to average out.
        snap = healthy(self.config)
        snap.voltage_age_ms = 500
        self.assertIn(Fault.SENSE_TIMEOUT,
                      run_for(self.manager, snap, self.config, ms=self.config.tick_ms))

    def test_temperature_uses_a_longer_window_than_voltage(self):
        snap = healthy(self.config, temp=70.0)
        self.assertFalse(bool(run_for(self.manager, snap, self.config, ms=100)))
        self.assertIn(Fault.CELL_OVERTEMP,
                      run_for(self.manager, snap, self.config, ms=200))


class TestThresholds(unittest.TestCase):
    """Trip points are the datasheet limit pulled in by measurement accuracy."""

    def setUp(self):
        self.config = make_config()
        self.manager = FaultManager(self.config)

    def test_accuracy_margin_is_applied_to_overvoltage(self):
        # Datasheet says 4.20 V, accuracy is 25 mV, so we trip at 4.175 V.
        self.assertAlmostEqual(self.config.effective_overvolt, 4.175)

        just_under = healthy(self.config)
        just_under.cell_volts[0] = 4.17
        self.assertFalse(bool(run_for(self.manager, just_under, self.config, ms=100)))

        over = healthy(self.config)
        over.cell_volts[0] = 4.18
        self.assertIn(Fault.CELL_OVERVOLT, run_for(self.manager, over, self.config, ms=100))

    def test_undervoltage(self):
        snap = healthy(self.config)
        snap.cell_volts[4] = 2.40
        self.assertIn(Fault.CELL_UNDERVOLT, run_for(self.manager, snap, self.config, ms=100))

    def test_temperature_ceiling_is_the_lower_of_datasheet_and_rule(self):
        # EV.7.5.2 caps at 60 C regardless of what the cell allows.
        hot = make_config(datasheet_max_temp=80.0)
        self.assertAlmostEqual(hot.effective_max_temp, 58.0)

    def test_overcurrent_discharge_and_charge_are_separate_limits(self):
        discharging = healthy(self.config, current=100.0)
        self.assertIn(Fault.OVERCURRENT_DISCHARGE,
                      run_for(self.manager, discharging, self.config, ms=100))

        manager = FaultManager(self.config)
        charging = healthy(self.config, current=-20.0)
        self.assertIn(Fault.OVERCURRENT_CHARGE,
                      run_for(manager, charging, self.config, ms=100, state=State.CHARGING))


class TestContextDependentLimits(unittest.TestCase):
    """The charge window is tighter than the discharge window at both ends."""

    def setUp(self):
        self.config = make_config()

    def test_forty_four_degrees_is_fine_while_driving(self):
        manager = FaultManager(self.config)
        warm = healthy(self.config, temp=44.0)
        self.assertFalse(bool(run_for(manager, warm, self.config, ms=300)))

    def test_forty_four_degrees_is_a_fault_while_charging(self):
        manager = FaultManager(self.config)
        warm = healthy(self.config, temp=44.0, current=-5.0)
        active = run_for(manager, warm, self.config, ms=300, state=State.CHARGING)
        self.assertIn(Fault.CELL_OVERTEMP, active)

    def test_charging_a_cold_pack_faults(self):
        manager = FaultManager(self.config)
        cold = healthy(self.config, temp=1.0, current=-5.0)
        active = run_for(manager, cold, self.config, ms=300, state=State.CHARGING)
        self.assertIn(Fault.CELL_UNDERTEMP, active)

    def test_the_same_cold_pack_may_discharge(self):
        manager = FaultManager(self.config)
        cold = healthy(self.config, temp=1.0, current=5.0)
        self.assertFalse(bool(run_for(manager, cold, self.config, ms=300)))

    def test_regen_braking_is_not_charging(self):
        """Current flows into the pack under regen, but the car is still driving.

        47 C is legal on track and a fault on a charger. Reading the current sign
        instead of the state turned every lift-off into a shutdown.
        """
        manager = FaultManager(self.config)
        regen = healthy(self.config, temp=47.0, current=-8.0)
        active = run_for(manager, regen, self.config, ms=400,
                         state=State.READY_TO_DRIVE)
        self.assertFalse(bool(active), f"regen tripped {manager.describe()}")

    def test_the_same_temperature_still_faults_on_a_charger(self):
        manager = FaultManager(self.config)
        charging = healthy(self.config, temp=47.0, current=-8.0)
        active = run_for(manager, charging, self.config, ms=400, state=State.CHARGING)
        self.assertIn(Fault.CELL_OVERTEMP, active)

    def test_current_noise_around_zero_does_not_move_the_limit(self):
        # A hall sensor with 1 A of error reading a stationary car crosses zero
        # constantly; the temperature window must not follow it.
        manager = FaultManager(self.config)
        warm = healthy(self.config, temp=47.0)
        for i in range(40):
            warm.pack_current_amps = 0.8 if i % 2 else -0.8
            run_for(manager, warm, self.config, ms=10, state=State.READY_TO_DRIVE)
        self.assertFalse(manager.faulted)


class TestDiscreteFaults(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.manager = FaultManager(self.config)

    def test_blown_sense_fuse(self):
        snap = healthy(self.config)
        snap.sense_fuses_ok = False        # EV.7.3.4 b
        self.assertIn(Fault.SENSE_FUSE_BLOWN,
                      run_for(self.manager, snap, self.config, ms=100))

    def test_internal_bms_fault(self):
        snap = healthy(self.config)
        snap.self_test_ok = False          # EV.7.3.4 e
        self.assertIn(Fault.BMS_INTERNAL,
                      run_for(self.manager, snap, self.config, ms=100))

    def test_imd_fault(self):
        snap = healthy(self.config)
        snap.imd_ok = False                # EV.7.6
        self.assertIn(Fault.IMD, run_for(self.manager, snap, self.config, ms=100))


class TestWeldedRelay(unittest.TestCase):
    """A relay commanded open that still reads closed is welded."""

    def setUp(self):
        self.config = make_config()
        self.manager = FaultManager(self.config)

    def test_closed_feedback_against_an_open_command_is_a_weld(self):
        snap = healthy(self.config)
        snap.air_positive_closed = True
        active = run_for(self.manager, snap, self.config, ms=200,
                         last_outputs=Outputs.safe())
        self.assertIn(Fault.AIR_POSITIVE_WELD, active)

    def test_both_relays_welded_are_both_reported(self):
        """A shared code let the second weld overwrite the first in the live map,
        so one welded relay could hide behind the other."""
        snap = healthy(self.config)
        snap.air_positive_closed = True
        snap.air_negative_closed = True
        active = run_for(self.manager, snap, self.config, ms=200,
                         last_outputs=Outputs.safe())
        self.assertIn(Fault.AIR_POSITIVE_WELD, active)
        self.assertIn(Fault.AIR_NEGATIVE_WELD, active)

    def test_each_relay_is_reported_on_its_own(self):
        snap = healthy(self.config)
        snap.air_negative_closed = True
        active = run_for(self.manager, snap, self.config, ms=200,
                         last_outputs=Outputs.safe())
        self.assertIn(Fault.AIR_NEGATIVE_WELD, active)
        self.assertNotIn(Fault.AIR_POSITIVE_WELD, active)

    def test_closed_feedback_against_a_close_command_is_normal(self):
        snap = healthy(self.config)
        snap.air_positive_closed = True
        snap.air_negative_closed = True
        commanded = Outputs(air_positive_cmd=True, air_negative_cmd=True)
        active = run_for(self.manager, snap, self.config, ms=200, last_outputs=commanded)
        self.assertNotIn(Fault.AIR_POSITIVE_WELD, active)
        self.assertNotIn(Fault.AIR_NEGATIVE_WELD, active)


class TestLatchingAndReset(unittest.TestCase):
    """EV.7.3.5 and EV.7.2.3: latch, and clear only by manual action."""

    def setUp(self):
        self.config = make_config()
        self.manager = FaultManager(self.config)
        hot = healthy(self.config, temp=70.0)
        run_for(self.manager, hot, self.config, ms=300)

    def test_fault_is_latched(self):
        self.assertIn(Fault.CELL_OVERTEMP, self.manager.active)

    def test_fault_survives_the_condition_going_away(self):
        run_for(self.manager, healthy(self.config), self.config, ms=500)
        self.assertIn(Fault.CELL_OVERTEMP, self.manager.active)

    def test_reset_refuses_while_the_condition_is_live(self):
        self.assertFalse(self.manager.resettable)
        self.assertFalse(self.manager.reset())
        self.assertTrue(self.manager.faulted)

    def test_reset_succeeds_once_the_pack_has_cooled(self):
        run_for(self.manager, healthy(self.config), self.config, ms=100)
        self.assertTrue(self.manager.reset())
        self.assertFalse(self.manager.faulted)
        self.assertIsNone(self.manager.first)


class TestFirstFault(unittest.TestCase):
    """Only the earliest fault of a latch cycle is kept."""

    def setUp(self):
        self.config = make_config()
        self.manager = FaultManager(self.config)

    def test_records_the_causal_fault_not_the_consequence(self):
        hot = healthy(self.config, temp=70.0)
        hot.cell_temps[5] = 75.0
        run_for(self.manager, hot, self.config, ms=300)

        # The cell now sags under load, tripping undervoltage as a consequence.
        cascade = healthy(self.config, temp=75.0)
        cascade.cell_volts[1] = 2.00
        run_for(self.manager, cascade, self.config, ms=300, start_ms=300)

        self.assertIn(Fault.CELL_UNDERVOLT, self.manager.active)
        self.assertEqual(self.manager.first.code, Fault.CELL_OVERTEMP)
        self.assertEqual(self.manager.first.channel, 5)

    def test_snapshot_carries_the_reading_and_the_limit(self):
        hot = healthy(self.config, temp=70.0)
        run_for(self.manager, hot, self.config, ms=300, start_ms=1000)
        first = self.manager.first

        self.assertAlmostEqual(first.measured, 70.0)
        self.assertAlmostEqual(first.threshold, 58.0)
        self.assertEqual(first.state, State.TS_ACTIVE)
        self.assertIn("EV.7.5.2", first.describe())


if __name__ == "__main__":
    unittest.main()
