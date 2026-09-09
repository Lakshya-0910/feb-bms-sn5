import io
import unittest
from pathlib import Path

from bms.config import LOW_VOLTAGE_LIMIT
from bms.types import Fault, State
from sim.run import run
from sim.scenario import parse

SCENARIOS = Path("sim/scenarios")

# Every scenario is a claim about behaviour, so each one is asserted rather
# than merely executed.
EXPECTED = {
    "normal_drive": (State.IDLE, None),
    "precharge_fail": (State.FAULT, Fault.PRECHARGE_TIMEOUT),
    "overtemp_during_drive": (State.FAULT, Fault.CELL_OVERTEMP),
    "cell_undervolt": (State.FAULT, Fault.CELL_UNDERVOLT),
    "sense_wire_loss": (State.FAULT, Fault.SENSE_TIMEOUT),
    "air_weld": (State.FAULT, Fault.AIR_WELD),
    "charge_cycle": (State.IDLE, None),
    "fault_reset_sequence": (State.TS_ACTIVE, None),
}


def replay(name):
    return run(parse(str(SCENARIOS / f"{name}.scn")), stream=io.StringIO())


class TestScenarios(unittest.TestCase):
    def test_every_scenario_file_is_covered(self):
        on_disk = {p.stem for p in SCENARIOS.glob("*.scn")}
        self.assertEqual(on_disk, set(EXPECTED))

    def test_scenarios_end_where_expected(self):
        for name, (state, first_fault) in EXPECTED.items():
            with self.subTest(scenario=name):
                bms = replay(name)
                self.assertEqual(bms.state, state)
                if first_fault is None:
                    self.assertIsNone(bms.faults.first)
                else:
                    self.assertEqual(bms.faults.first.code, first_fault)

    def test_a_clean_run_never_faults(self):
        bms = replay("normal_drive")
        self.assertFalse(bms.faults.faulted)
        states = {target for _, _, _, target in bms.history}
        self.assertIn(State.READY_TO_DRIVE, states)

    def test_shutdown_reaches_low_voltage_inside_five_seconds(self):
        """EV.7.2.2 c, measured rather than asserted."""
        bms = replay("normal_drive")
        opened = next(t for t, _, _, target in bms.history if target is State.SHUTDOWN)
        settled = next(t for t, source, _, _ in bms.history if source is State.SHUTDOWN)
        self.assertLess(settled - opened, 5000)

    def test_a_reset_recovers_the_car(self):
        bms = replay("fault_reset_sequence")
        events = [event for _, _, event, _ in bms.history]
        self.assertIn(State.FAULT, [t for _, _, _, t in bms.history])
        self.assertFalse(bms.faults.faulted)


class TestScenarioParser(unittest.TestCase):
    def test_ranges_and_single_cells_are_addressed_separately(self):
        scenario = parse(str(SCENARIOS / "overtemp_during_drive.scn"))
        self.assertTrue(scenario.ramps)
        ramp = scenario.ramps[0]
        self.assertEqual(ramp.value_at(ramp.start_ms), 40.0)
        self.assertEqual(ramp.value_at(ramp.end_ms), 64.0)
        self.assertAlmostEqual(ramp.value_at((ramp.start_ms + ramp.end_ms) // 2), 52.0)

    def test_unknown_commands_are_rejected(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".scn", delete=False) as f:
            f.write("@0 wobble the pack\n")
        with self.assertRaises(ValueError):
            parse(f.name)


if __name__ == "__main__":
    unittest.main()
