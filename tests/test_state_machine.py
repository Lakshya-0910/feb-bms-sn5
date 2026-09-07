import unittest

from bms.state_machine import TRANSITIONS, to_mermaid
from bms.types import Event, Fault, State
from tests.helpers import Rig, make_config


class TestActivationSequence(unittest.TestCase):
    """EV.9.2: GLV, then tractive system active, then ready to drive."""

    def test_boots_through_self_test_to_idle(self):
        self.assertEqual(Rig().boot().state, State.IDLE)

    def test_self_test_fails_if_a_relay_is_already_closed(self):
        rig = Rig()
        rig.snapshot.air_positive_closed = True   # welded, or miswired
        rig.run(300)
        self.assertEqual(rig.state, State.FAULT)

    def test_self_test_fails_on_stale_sensors(self):
        rig = Rig()
        rig.snapshot.voltage_age_ms = 500         # EV.7.3.4 d
        rig.run(100)
        self.assertEqual(rig.state, State.FAULT)

    def test_tsms_starts_precharge(self):
        rig = Rig().boot()
        self.assertEqual(rig.run(20, tsms_closed=True), State.PRECHARGE)

    def test_ready_to_drive_needs_all_three_conditions(self):
        rig = Rig().boot()
        rig.run(20, tsms_closed=True)
        rig.precharge_to(0.95)
        self.assertEqual(rig.run(20), State.TS_ACTIVE)

        self.assertEqual(rig.run(20, brake_pressed=True), State.TS_ACTIVE)
        self.assertEqual(rig.run(20, brake_pressed=False, start_pressed=True),
                         State.TS_ACTIVE)
        self.assertEqual(rig.request_rtd(), State.READY_TO_DRIVE)


class TestPrecharge(unittest.TestCase):
    """EV.5.6: 90 % by voltage feedback, never by a timer."""

    def setUp(self):
        self.rig = Rig().boot()
        self.rig.run(20, tsms_closed=True)

    def test_time_alone_never_completes_a_precharge(self):
        self.rig.precharge_to(0.89)
        self.rig.run(4000)                        # well past any plausible ramp
        self.assertEqual(self.rig.state, State.PRECHARGE)

    def test_completes_at_ninety_percent(self):
        self.rig.precharge_to(0.90)
        self.assertEqual(self.rig.run(20), State.TS_ACTIVE)

    def test_stalled_precharge_faults_on_timeout(self):
        self.rig.run(5100)                        # never reaches 90 %
        self.assertEqual(self.rig.state, State.FAULT)
        self.assertIn(Fault.PRECHARGE_TIMEOUT, self.rig.bms.faults.active)

    def test_second_relay_stays_open_during_precharge(self):
        self.rig.precharge_to(0.50)
        self.rig.run(20)
        self.assertTrue(self.rig.outputs.air_negative_cmd)
        self.assertTrue(self.rig.outputs.precharge_relay_cmd)
        self.assertFalse(self.rig.outputs.air_positive_cmd)


class TestOutputsPerState(unittest.TestCase):
    """Outputs are a function of the state, so they are checked one state at a time."""

    def setUp(self):
        self.rig = Rig().boot()

    def _drive(self):
        self.rig.run(20, tsms_closed=True)
        self.rig.precharge_to(0.95)
        self.rig.run(20)
        return self.rig.request_rtd()

    def test_motors_are_inert_until_ready_to_drive(self):
        self.rig.run(20, tsms_closed=True)
        self.rig.precharge_to(0.95)
        self.rig.run(20)
        self.assertEqual(self.rig.state, State.TS_ACTIVE)   # EV.9.4
        self.assertFalse(self.rig.outputs.motors_enabled)

    def test_motors_respond_only_in_ready_to_drive(self):
        self._drive()
        self.assertTrue(self.rig.outputs.motors_enabled)    # EV.9.6.1

    def test_ready_to_drive_sound_is_bounded(self):
        self._drive()
        self.rig.run(1000)
        self.assertTrue(self.rig.outputs.rtds_active)        # EV.9.7.2, min 1 s
        self.rig.run(1500)
        self.assertFalse(self.rig.outputs.rtds_active)       # and max 3 s

    def test_idle_holds_the_shutdown_circuit_closed_with_relays_open(self):
        self.assertTrue(self.rig.outputs.shutdown_circuit_closed)
        self.assertFalse(self.rig.outputs.air_positive_cmd)


class TestShutdown(unittest.TestCase):
    def setUp(self):
        self.rig = Rig().boot()
        self.rig.run(20, tsms_closed=True)
        self.rig.precharge_to(0.95)
        self.rig.run(20)

    def test_opening_tsms_shuts_the_car_down(self):
        self.rig.run(20, tsms_closed=False)
        # Intermediate circuit is already drained, so it returns to idle at once.
        self.assertEqual(self.rig.state, State.IDLE)

    def test_glv_loss_shuts_the_car_down(self):
        self.rig.run(20, glv_on=False)            # EV.7.2.1 b
        self.assertEqual(self.rig.state, State.IDLE)

    def test_tractive_system_that_stays_live_faults(self):
        self.rig.snapshot.intermediate_volts = 300.0
        self.rig.run(20, tsms_closed=False)
        self.assertEqual(self.rig.state, State.SHUTDOWN)
        self.rig.run(5100)                        # EV.7.2.2 c allows five seconds
        self.assertEqual(self.rig.state, State.FAULT)
        self.assertIn(Fault.SHUTDOWN_TIMEOUT, self.rig.bms.faults.active)


class TestFaultBehaviour(unittest.TestCase):
    """EV.7.3.5 and EV.7.2.3."""

    def setUp(self):
        self.rig = Rig().boot()
        self.rig.run(20, tsms_closed=True)
        self.rig.precharge_to(0.95)
        self.rig.run(20)
        self.rig.request_rtd()
        self.assertEqual(self.rig.state, State.READY_TO_DRIVE)
        self.rig.snapshot.cell_temps[3] = 70.0
        self.rig.run(300)

    def test_a_fault_from_any_state_reaches_fault(self):
        self.assertEqual(self.rig.state, State.FAULT)

    def test_fault_opens_everything_and_lights_both_indicators(self):
        out = self.rig.outputs
        self.assertFalse(out.shutdown_circuit_closed)
        self.assertFalse(out.air_positive_cmd)
        self.assertFalse(out.air_negative_cmd)
        self.assertFalse(out.motors_enabled)
        self.assertTrue(out.bms_indicator)          # EV.7.3.6
        self.assertTrue(out.ts_status_indicator)    # EV.5.11.5

    def test_reset_is_refused_while_the_pack_is_still_hot(self):
        self.rig.run(100, manual_reset=True)
        self.assertEqual(self.rig.state, State.FAULT)

    def test_reset_works_once_the_condition_clears(self):
        self.rig.snapshot.cell_temps[3] = 30.0
        self.rig.run(100)
        self.assertEqual(self.rig.state, State.FAULT)   # latched, not self-clearing
        self.rig.run(50, manual_reset=True)
        self.assertNotEqual(self.rig.state, State.FAULT)

    def test_a_held_start_button_does_not_re_arm_the_car(self):
        """EV.9.6.2 wants a manual action; the driver's thumb was already down."""
        self.rig.snapshot.cell_temps[3] = 30.0
        self.rig.run(100)
        self.rig.run(200, manual_reset=True)
        self.assertNotEqual(self.rig.state, State.READY_TO_DRIVE)

    def test_reset_returns_through_self_test_not_straight_to_driving(self):
        self.rig.snapshot.cell_temps[3] = 30.0
        self.rig.run(100)
        self.rig.run(10, manual_reset=True)
        self.assertEqual(self.rig.state, State.SELF_TEST)


class TestChargingAndBalancing(unittest.TestCase):
    def test_charger_starts_a_charge_when_one_is_needed(self):
        rig = Rig(volts=3.60).boot()
        self.assertEqual(rig.run(20, charger_connected=True), State.CHARGING)

    def test_a_full_pack_does_not_start_charging(self):
        rig = Rig(volts=4.15).boot()
        self.assertEqual(rig.run(50, charger_connected=True), State.IDLE)

    def test_finished_charge_top_balances_an_uneven_pack(self):
        rig = Rig(volts=3.60).boot()
        rig.run(20, charger_connected=True)
        rig.snapshot.cell_volts = [4.15] * rig.config.series_modules
        rig.snapshot.cell_volts[0] = 4.05          # 0.10 V out of balance
        self.assertEqual(rig.run(20), State.BALANCING)

    def test_balancing_bleeds_the_high_cells_only(self):
        rig = Rig(volts=3.60).boot()
        rig.run(20, charger_connected=True)
        rig.snapshot.cell_volts = [4.15] * rig.config.series_modules
        rig.snapshot.cell_volts[0] = 4.05
        rig.run(20)
        bleed = rig.outputs.balance_bleed
        self.assertFalse(bleed[0])                 # the lowest cell is left alone
        self.assertTrue(all(bleed[1:]))

    def test_balancing_needs_a_closed_shutdown_circuit(self):
        # EV.7.3.3: no charger and no TSMS means the circuit is open.
        rig = Rig().boot()
        rig.snapshot.cell_volts[0] += 0.20
        rig.run(100)
        self.assertEqual(rig.state, State.IDLE)


class TestTableIntegrity(unittest.TestCase):
    def test_no_transition_skips_precharge(self):
        illegal = [t for t in TRANSITIONS
                   if t.source is State.IDLE and t.target is State.TS_ACTIVE]
        self.assertEqual(illegal, [])              # EV.5.6.2 forbids it

    def test_only_fault_leaves_by_manual_reset(self):
        resets = [t for t in TRANSITIONS if t.event is Event.MANUAL_RESET]
        self.assertEqual([t.source for t in resets], [State.FAULT])

    def test_no_duplicate_unguarded_rows(self):
        seen = set()
        for t in TRANSITIONS:
            if t.guard is None:
                self.assertNotIn((t.source, t.event), seen)
                seen.add((t.source, t.event))

    def test_diagram_is_generated_from_the_table(self):
        diagram = to_mermaid()
        for t in TRANSITIONS:
            self.assertIn(f"{t.source.name} --> {t.target.name}", diagram)


if __name__ == "__main__":
    unittest.main()
