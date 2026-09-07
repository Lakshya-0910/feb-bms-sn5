import unittest

from bms.can import (
    BMS_CELL_TEMPS, BMS_CELL_VOLTS, BMS_FAULTS, BMS_PACK_SUMMARY, BMS_STATUS,
    CHARGER_CONTROL, DASH_COMMAND, BmsCan, CanBus, Frame, decode_pack_summary,
)
from bms.monitor import summarise
from bms.types import Fault, State
from tests.helpers import Rig


class CanRig:
    """A Rig with a bus attached, stepping both together."""

    def __init__(self, **kwargs):
        self.rig = Rig(**kwargs)
        self.bus = CanBus()
        self.can = BmsCan(self.bus, self.rig.config)

    def run(self, ms: int, **driver_changes):
        tick = self.rig.config.tick_ms
        for _ in range(0, max(ms, tick), tick):
            self.rig.run(tick, **driver_changes)
            driver_changes = {}
            self.can.update(self.rig.bms, self.rig.snapshot,
                            summarise(self.rig.snapshot, self.rig.config), tick)
        return self.rig.state

    def boot(self):
        self.run(20)
        return self


class TestPeriodicRates(unittest.TestCase):
    def test_status_is_ten_times_the_rate_of_pack_summary(self):
        rig = CanRig().boot()
        rig.run(1000)
        status = len(rig.bus.frames(BMS_STATUS))
        summary = len(rig.bus.frames(BMS_PACK_SUMMARY))
        self.assertAlmostEqual(status / summary, 10, delta=1)


class TestEncoding(unittest.TestCase):
    def test_pack_summary_survives_a_round_trip(self):
        rig = CanRig(volts=3.85).boot()
        rig.rig.snapshot.pack_current_amps = -12.5
        rig.run(200)

        decoded = decode_pack_summary(rig.bus.frames(BMS_PACK_SUMMARY)[-1])
        self.assertAlmostEqual(decoded["pack_volts"], 3.85 * 8, places=1)
        self.assertAlmostEqual(decoded["current_amps"], -12.5, places=1)
        self.assertAlmostEqual(decoded["min_cell_volts"], 3.85, places=3)

    def test_every_frame_is_eight_bytes(self):
        rig = CanRig().boot()
        rig.run(1000)
        self.assertTrue(all(len(f.data) == 8 for f in rig.bus.sent))

    def test_cell_voltages_are_multiplexed_across_every_cell(self):
        rig = CanRig().boot()
        rig.run(1000)
        blocks = {f.data[0] for f in rig.bus.frames(BMS_CELL_VOLTS)}
        # 8 cells at 3 per frame needs 3 blocks to cover the pack.
        self.assertEqual(blocks, {0, 1, 2})

    def test_temperatures_are_multiplexed_too(self):
        rig = CanRig().boot()
        rig.run(1000)
        blocks = {f.data[0] for f in rig.bus.frames(BMS_CELL_TEMPS)}
        self.assertEqual(blocks, {0, 1})


class TestFaultReporting(unittest.TestCase):
    def setUp(self):
        self.rig = CanRig().boot()
        self.rig.rig.snapshot.cell_temps[4] = 70.0

    def test_a_fault_is_reported_without_waiting_for_the_next_heartbeat(self):
        before = len(self.rig.bus.frames(BMS_FAULTS))
        self.rig.run(300)          # far short of the 1000 ms heartbeat
        self.assertGreater(len(self.rig.bus.frames(BMS_FAULTS)), before)

    def test_fault_frame_carries_the_first_fault_and_its_channel(self):
        self.rig.run(300)
        data = self.rig.bus.frames(BMS_FAULTS)[-1].data

        mask = int.from_bytes(data[0:2], "little")
        first_code = int.from_bytes(data[2:4], "little")
        channel = data[4]
        measured = int.from_bytes(data[5:7], "little", signed=True) / 10

        self.assertTrue(mask & Fault.CELL_OVERTEMP.value)
        self.assertEqual(first_code, Fault.CELL_OVERTEMP.value)
        self.assertEqual(channel, 4)
        self.assertAlmostEqual(measured, 70.0, places=1)


class TestChargerControl(unittest.TestCase):
    def test_current_is_requested_only_while_charging(self):
        rig = CanRig(volts=3.60).boot()
        rig.run(100)
        self.assertEqual(rig.bus.frames(CHARGER_CONTROL)[-1].data[0], 0)

        rig.run(100, charger_connected=True)
        self.assertEqual(rig.rig.state, State.CHARGING)
        self.assertEqual(rig.bus.frames(CHARGER_CONTROL)[-1].data[0], 1)


class TestResetOverCanIsRefused(unittest.TestCase):
    """EV.7.2.3 b and c: a reset is a physical action at the vehicle."""

    def setUp(self):
        self.rig = CanRig().boot()
        self.rig.rig.snapshot.cell_temps[2] = 70.0
        self.rig.run(300)
        self.assertEqual(self.rig.rig.state, State.FAULT)
        self.rig.rig.snapshot.cell_temps[2] = 25.0   # the pack has cooled
        self.rig.run(100)

    def _send_dash(self, start=False, reset=False):
        flags = (0b01 if start else 0) | (0b10 if reset else 0)
        self.rig.bus.inject(Frame(DASH_COMMAND, bytes([flags]) + bytes(7)))
        return self.rig.can.receive()

    def test_a_reset_over_can_does_not_clear_the_fault(self):
        dash, _ = self._send_dash(reset=True)
        self.assertTrue(dash.reset_requested)
        self.rig.run(200)
        self.assertEqual(self.rig.rig.state, State.FAULT)

    def test_refused_resets_are_counted_rather_than_ignored(self):
        for _ in range(3):
            self._send_dash(reset=True)
        self.assertEqual(self.rig.can.rejected_resets, 3)

    def test_the_physical_button_still_works(self):
        self.rig.run(100, manual_reset=True)
        self.assertNotEqual(self.rig.rig.state, State.FAULT)

    def test_a_start_request_over_can_is_accepted(self):
        # A dash button is still a cockpit action; only reset must be physical.
        dash, _ = self._send_dash(start=True)
        self.assertTrue(dash.start_requested)
        self.assertEqual(self.rig.can.rejected_resets, 0)


if __name__ == "__main__":
    unittest.main()
