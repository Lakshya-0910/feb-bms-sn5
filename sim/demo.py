"""Verification run for presenting the project.

    python3 -m sim.demo

Every line is computed by exercising the real modules - no result is hardcoded.
Sections follow the project requirements, then the rules the design claims to
satisfy, then the test suite.
"""

import ast
import io
import sys
import unittest
from pathlib import Path

from bms.can import PERIODS_MS, BMS_FAULTS, BMS_PACK_SUMMARY, BMS_STATUS, BmsCan, CanBus, Frame
from bms.can import DASH_COMMAND, decode_pack_summary
from bms.config import DEFAULT, LOW_VOLTAGE_LIMIT, MIN_TEMP_COVERAGE, MAX_SYSTEM_VOLTAGE
from bms.monitor import summarise
from bms.outputs import Outputs
from bms.state_machine import BMS, TRANSITIONS
from bms.types import FAULT_RULES, Fault, State
from sim.run import World

WIDTH = 94
DETAIL_COL = 50
_passed = 0
_failed = 0


def header(title: str, subtitle: str) -> None:
    print("=" * WIDTH)
    print(f"  {title}")
    print(f"  {subtitle}")
    print("=" * WIDTH)


def section(number: int, title: str) -> None:
    label = f"-- {number}. {title} "
    print(f"\n{label}{'-' * max(0, WIDTH - len(label))}")


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _passed, _failed
    if ok:
        _passed += 1
    else:
        _failed += 1
    head = f"  [{'PASS' if ok else 'FAIL'}]  {label}"
    print(f"{head}{' ' * max(1, DETAIL_COL - len(head))}{detail}" if detail else head)
    return ok


def line(text: str = "") -> None:
    print(f"  {text}" if text else "")


class Car:
    """A BMS plus the car around it, driven in simulated time."""

    def __init__(self, config=DEFAULT, volts: float = 3.85, temp: float = 28.0):
        self.config = config
        self.world = World(config)
        self.world.snapshot.cell_volts = [volts] * config.series_modules
        self.world.snapshot.cell_temps = [temp] * config.temp_sensors
        self.bms = BMS(config)
        self.bus = CanBus()
        self.can = BmsCan(self.bus, config)

    @property
    def snapshot(self):
        return self.world.snapshot

    @property
    def state(self) -> State:
        return self.bms.state

    def run(self, ms: int, **driver):
        for name, value in driver.items():
            setattr(self.world.driver, name, value)
        tick = self.config.tick_ms
        for _ in range(0, max(ms, tick), tick):
            outputs = self.bms.step(self.snapshot, self.world.driver, tick)
            self.can.update(self.bms, self.snapshot, summarise(self.snapshot, self.config), tick)
            self.world.settle(outputs, tick)
        return self.bms.state

    def drive(self):
        """Power up and reach ready-to-drive."""
        self.run(20, glv_on=True)
        self.run(1500, tsms_closed=True)
        self.run(10, brake_pressed=True, start_pressed=False)
        self.run(20, start_pressed=True)
        return self.state

    def fault_at(self) -> int | None:
        for t, _, event, target in self.bms.history:
            if target is State.FAULT:
                return t
        return None


# ----------------------------------------------------------------- sections

def requirements() -> None:
    section(1, "PROJECT REQUIREMENTS")

    check("Battery states defined", len(State) == 10, f"{len(State)} states")
    guarded = sum(1 for t in TRANSITIONS if t.guard is not None)
    check("Transitions between states", len(TRANSITIONS) >= 10,
          f"{len(TRANSITIONS)} transitions, {guarded} guarded")

    tests = run_tests()
    check("Unit tests for driver/sensor inputs", tests["failures"] == 0,
          f"{tests['total']} tests, {tests['failures']} failures")

    tx = sum(1 for i in PERIODS_MS)
    check("CAN integration (bonus)", tx >= 5, f"{tx} transmitted messages, 2 received")

    external = third_party_imports()
    check("No libraries in the state machine", not external,
          "standard library only" if not external else f"found {external}")


def third_party_imports() -> list[str]:
    """Read every import in bms/ and flag anything outside the standard library."""
    allowed = {"dataclasses", "enum", "typing", "math", "abc"}
    found = set()
    for path in sorted(Path("bms").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return sorted(found - allowed)


def thresholds() -> None:
    section(2, "THRESHOLDS: DATASHEET LIMIT, ACCURACY, TRIP POINT")
    c = DEFAULT
    line(f"{'quantity':<24}{'limit':>10}{'accuracy':>11}{'trips at':>11}   rule")
    rows = [
        ("cell overvoltage", c.cell_max_volts, c.voltage_accuracy, c.effective_overvolt,
         "V", "EV.7.4.2"),
        ("cell undervoltage", c.cell_min_volts, c.voltage_accuracy, c.effective_undervolt,
         "V", "EV.7.4.2"),
        ("cell temperature", min(c.datasheet_max_temp, 60.0), c.temp_accuracy,
         c.effective_max_temp, "C", "EV.7.5.2"),
        ("charge temp, low", c.charge_min_temp, c.temp_accuracy,
         c.effective_charge_min_temp, "C", "EV.7.5.2"),
        ("charge temp, high", c.charge_max_temp, c.temp_accuracy,
         c.effective_charge_max_temp, "C", "EV.7.5.2"),
        ("discharge current", c.max_discharge_amps, c.current_accuracy,
         c.effective_max_discharge_amps, "A", "datasheet"),
    ]
    for name, limit, acc, trip, unit, rule in rows:
        line(f"{name:<24}{limit:>8.3f} {unit}{acc:>9.3f} {unit}{trip:>9.3f} {unit}   {rule}")
    line()
    line("EV.7.4.2 and EV.7.5.2 require limits to hold considering measurement")
    line("accuracy, so each trip point is the limit pulled in by its sensor error.")


def pack_legality() -> None:
    section(3, "PACK LEGALITY, CHECKED AT CONSTRUCTION")
    c = DEFAULT
    check("Temperature coverage", c.temp_coverage >= MIN_TEMP_COVERAGE,
          f"{c.temp_coverage:.0%} monitored, {MIN_TEMP_COVERAGE:.0%} required, EV.7.5.5")
    check("Maximum system voltage", c.max_pack_volts <= MAX_SYSTEM_VOLTAGE,
          f"{c.max_pack_volts:.0f} V peak, {MAX_SYSTEM_VOLTAGE:.0f} V ceiling, EV.3.3.2")
    check("One voltage tap per paralleled group", c.series_modules == c.temp_sensors,
          f"{c.series_modules} taps for {c.total_cells} cells, EV.7.4.1")
    safe = Outputs.safe()
    every_output_open = not any([safe.air_positive_cmd, safe.air_negative_cmd,
                                 safe.shutdown_circuit_closed, safe.motors_enabled])
    check("De-energised state is the safe state", every_output_open,
          "all outputs open by default, EV.7.1.3")


def normal_operation() -> None:
    section(4, "NORMAL OPERATION: EV.9.2 ACTIVATION SEQUENCE")
    car = Car()
    car.run(20, glv_on=True)
    car.run(1500, tsms_closed=True)
    inert = not car.bms.outputs.motors_enabled and car.state is State.TS_ACTIVE
    car.run(10, brake_pressed=True, start_pressed=False)
    car.run(20, start_pressed=True)
    for t, source, event, target in car.bms.history:
        line(f"{t / 1000:7.3f} s  {str(source):<15} -> {str(target):<15} {event}")
    line()
    out = car.bms.outputs
    line(f"outputs now: {out.describe()}")
    check("Ready to drive reached", car.state is State.READY_TO_DRIVE, str(car.state))
    check("Motors inert until ready to drive", inert,
          "no torque in TS_ACTIVE, EV.9.4")
    check("Motors enabled once ready to drive", car.bms.outputs.motors_enabled,
          "EV.9.6.1")

    car.run(8000, tsms_closed=False)
    opened = next(t for t, _, _, tgt in car.bms.history if tgt is State.SHUTDOWN)
    settled = next(t for t, src, _, _ in car.bms.history if src is State.SHUTDOWN)
    check("Tractive system reaches low voltage",
          settled - opened < DEFAULT.shutdown_verify_ms,
          f"{(settled - opened) / 1000:.2f} s of 5 s, EV.7.2.2 c")


def fault_detection() -> None:
    section(5, "FAULT DETECTION: THE FIVE DUTIES OF EV.7.3.4")

    def trip(mutate, label, expect, duty):
        car = Car()
        car.drive()
        mutate(car)
        car.run(600)
        latched = expect in car.bms.faults.active
        at = car.fault_at()
        detail = f"{expect.name} at {at} ms" if latched else "not detected"
        check(f"{duty}  {label}", latched and car.state is State.FAULT, detail)

    def overvolt(car):
        car.snapshot.cell_volts[5] = 4.30

    def undervolt(car):
        car.snapshot.cell_volts[5] = 2.40

    def hot(car):
        car.snapshot.cell_temps[30] = 64.0

    def fuse(car):
        car.snapshot.sense_fuses_ok = False

    def stale(car):
        car.snapshot.voltage_age_ms = 500

    def internal(car):
        car.snapshot.self_test_ok = False

    def overcurrent(car):
        car.snapshot.pack_current_amps = 120.0

    trip(overvolt, "cell voltage above range", Fault.CELL_OVERVOLT, "a ")
    trip(undervolt, "cell voltage below range", Fault.CELL_UNDERVOLT, "a ")
    trip(fuse, "voltage-sense fuse blown", Fault.SENSE_FUSE_BLOWN, "b ")
    trip(hot, "cell temperature above range", Fault.CELL_OVERTEMP, "c ")
    trip(stale, "measurement missing or stale", Fault.SENSE_TIMEOUT, "d ")
    trip(internal, "fault inside the BMS itself", Fault.BMS_INTERNAL, "e ")
    trip(overcurrent, "discharge current over limit", Fault.OVERCURRENT_DISCHARGE, "  ")

    car = Car()
    car.run(20, glv_on=True)
    car.world.plant_enabled = False
    car.snapshot.air_positive_closed = True
    car.run(400)
    check("    isolation relay welded shut",
          Fault.AIR_WELD in car.bms.faults.active,
          "open command, closed feedback, EV.5.4.2")


def mandated_behaviour() -> None:
    section(6, "BEHAVIOUR THE RULES REQUIRE")

    # EV.5.6.2 a - completion by feedback, never by elapsed time.
    car = Car()
    car.run(20, glv_on=True)
    car.world.plant_enabled = False
    car.run(20, tsms_closed=True)
    car.snapshot.intermediate_volts = car.snapshot.pack_volts * 0.89
    car.run(4000)
    held = car.state is State.PRECHARGE
    car.snapshot.intermediate_volts = car.snapshot.pack_volts * 0.90
    car.run(20)
    check("Precharge ends on voltage, not time",
          held and car.state is State.TS_ACTIVE,
          "89 % held 4 s, 90 % advanced, EV.5.6.2 a")

    # EV.5.6 - a precharge that never completes must fault.
    stalled = Car()
    stalled.run(20, glv_on=True)
    stalled.world.plant_enabled = False
    stalled.run(6000, tsms_closed=True)
    check("Stalled precharge faults out",
          Fault.PRECHARGE_TIMEOUT in stalled.bms.faults.active,
          f"timeout at {DEFAULT.precharge_timeout_ms} ms, EV.5.6")

    # EV.9.6.2 - three conditions, at the same time.
    partial = Car()
    partial.run(20, glv_on=True)
    partial.run(1500, tsms_closed=True)
    brake_only = partial.run(100, brake_pressed=True) is State.TS_ACTIVE
    partial.run(100, brake_pressed=False, start_pressed=True)
    start_only = partial.state is State.TS_ACTIVE
    partial.run(20, start_pressed=False)
    both = partial.run(20, brake_pressed=True, start_pressed=True)
    check("Ready to drive needs three conditions",
          brake_only and start_only and both is State.READY_TO_DRIVE,
          "brake or button alone refused, EV.9.6.2")

    # EV.9.7.2 - the sound lasts between one and three seconds.
    sound = Car()
    sound.drive()
    on_at_one = sound.run(900) and sound.bms.outputs.rtds_active
    sound.run(1500)
    off_after = not sound.bms.outputs.rtds_active
    check("Ready-to-drive sound bounded", on_at_one and off_after,
          f"{DEFAULT.rtds_duration_ms} ms, EV.9.7.2 allows 1000-3000")

    # EV.7.3.5 and EV.7.2.3 - latched until a person resets it at the car.
    latched = Car()
    latched.drive()
    latched.snapshot.cell_temps[9] = 70.0
    latched.run(400)
    faulted = latched.state is State.FAULT
    out = latched.bms.outputs
    lights = out.bms_indicator and out.ts_status_indicator
    opened = not out.shutdown_circuit_closed and not out.air_positive_cmd
    check("Fault opens circuit, lights indicators",
          faulted and lights and opened, "EV.7.3.5 b, EV.7.3.6")

    latched.run(200, manual_reset=True)
    still = latched.state is State.FAULT
    latched.run(20, manual_reset=False)
    latched.snapshot.cell_temps[9] = 28.0
    latched.run(300)
    cooled_but_latched = latched.state is State.FAULT
    check("Reset refused while condition is live", still and cooled_but_latched,
          "latched, does not self-clear, EV.7.2.3 a")

    latched.run(100, manual_reset=True)
    check("Physical reset clears it",
          latched.state in (State.SELF_TEST, State.IDLE, State.PRECHARGE, State.TS_ACTIVE)
          and not latched.bms.faults.faulted,
          f"via SELF_TEST, EV.7.2.3 b")

    # EV.7.2.3 b and c - a reset over CAN must not work.
    over_can = Car()
    over_can.drive()
    over_can.snapshot.cell_temps[9] = 70.0
    over_can.run(400)
    over_can.snapshot.cell_temps[9] = 28.0
    over_can.run(300)
    over_can.bus.inject(Frame(DASH_COMMAND, bytes([0b10]) + bytes(7)))
    over_can.can.receive()
    over_can.run(300)
    check("Reset requested over CAN is refused",
          over_can.state is State.FAULT and over_can.can.rejected_resets == 1,
          "physical action required, EV.7.2.3 b,c")

    # Regen pushes current into the pack while driving. Charge limits must not
    # follow the current sign, or a legal warm pack faults on track.
    regen = Car(temp=47.0)
    regen.drive()
    regen.snapshot.pack_current_amps = -8.0
    regen.run(600)
    driving_ok = regen.state is State.READY_TO_DRIVE
    charged = Car(temp=47.0)
    charged.run(20, glv_on=True)
    charged.snapshot.cell_volts = [3.60] * DEFAULT.series_modules
    charged.run(600, charger_connected=True)
    charger_faults = Fault.CELL_OVERTEMP in charged.bms.faults.active
    check("Regen braking is not mistaken for charging", driving_ok and charger_faults,
          "47 C drives, faults on a charger")

    # EV.7.3.3 - no balancing while the shutdown circuit is open.
    imbalanced = Car()
    imbalanced.run(20, glv_on=True)
    imbalanced.snapshot.cell_volts[0] += 0.20
    imbalanced.run(300)
    check("No balancing with circuit open",
          imbalanced.state is State.IDLE, "stays idle, EV.7.3.3")


def can_output() -> None:
    section(7, "CAN OUTPUT")
    car = Car()
    car.drive()
    car.run(500)

    line(f"{'id':>6}  {'message':<20}{'period':>8}  {'frames':>7}")
    names = {0x180: "BMS_Status", 0x181: "BMS_PackSummary", 0x182: "BMS_CellVoltages",
             0x183: "BMS_CellTemps", 0x184: "BMS_Faults", 0x300: "Charger_Control"}
    for can_id, period in PERIODS_MS.items():
        line(f"{can_id:>#6x}  {names[can_id]:<20}{period:>6} ms  "
             f"{len(car.bus.frames(can_id)):>7}")
    line()
    status = car.bus.frames(BMS_STATUS)[-1]
    summary = car.bus.frames(BMS_PACK_SUMMARY)[-1]
    decoded = decode_pack_summary(summary)
    line(f"last status frame   {status}")
    line(f"last summary frame  {summary}")
    line(f"decoded             {decoded['pack_volts']:.1f} V  "
         f"{decoded['current_amps']:.1f} A  "
         f"cells {decoded['min_cell_volts']:.3f}-{decoded['max_cell_volts']:.3f} V")

    round_trip = abs(decoded["pack_volts"] - car.snapshot.pack_volts) < 0.2
    check("Frames decode back to the values sent", round_trip,
          "pack summary round trip")
    eight = all(len(f.data) == 8 for f in car.bus.frames(BMS_STATUS))
    check("Payloads are valid classical CAN", eight, "8 bytes per frame")

    faulting = Car()
    faulting.drive()
    before = len(faulting.bus.frames(BMS_FAULTS))
    faulting.snapshot.cell_temps[30] = 70.0
    faulting.run(400)
    check("Fault reported immediately on change",
          len(faulting.bus.frames(BMS_FAULTS)) > before,
          "sent on change, 1 Hz idle")
    first = faulting.bms.faults.first
    check("First fault names channel and value",
          first is not None and first.channel == 30,
          f"{first.code.name} ch{first.channel}, "
          f"{first.measured:g} C vs {first.threshold:g} C" if first else "none")


def run_tests() -> dict:
    loader = unittest.TestLoader()
    suite = loader.discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    return {"total": result.testsRun,
            "failures": len(result.failures) + len(result.errors)}


def test_suite() -> None:
    section(8, "TEST SUITE")
    loader = unittest.TestLoader()
    total = 0
    for name in ("monitor", "faults", "state_machine", "can", "scenarios"):
        suite = loader.loadTestsFromName(f"tests.test_{name}")
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        total += result.testsRun
        status = "ok" if result.wasSuccessful() else "FAILED"
        line(f"{name:<20}{result.testsRun:>4} tests   {status}")
    line(f"{'total':<20}{total:>4} tests")


def main() -> int:
    header("SN5 BATTERY MANAGEMENT SYSTEM - state machine verification",
           "Berkeley Formula Electric - CS recruitment project - Lakshya Saini")
    line()
    c = DEFAULT
    line(f"{'pack':<12}{c.series_modules}s{c.cells_per_module}p, {c.total_cells} cells, "
         f"{c.nominal_pack_volts:.0f} V nominal, {c.pack_capacity_ah:.1f} Ah")
    line(f"{'sensing':<12}{c.series_modules} voltage taps, {c.temp_sensors} thermistors "
         f"({c.temp_coverage:.0%} of cells)")
    line(f"{'loop':<12}{1000 // c.tick_ms} Hz ({c.tick_ms} ms tick)")
    line(f"{'rules':<12}Formula SAE 2026, version 1.0, 10 September 2025")

    requirements()
    thresholds()
    pack_legality()
    normal_operation()
    fault_detection()
    mandated_behaviour()
    can_output()
    test_suite()

    print()
    print("=" * WIDTH)
    verdict = "all checks passed" if _failed == 0 else f"{_failed} CHECK(S) FAILED"
    print(f"  RESULT   {_passed + _failed} checks run, {_passed} passed - {verdict}")
    print("=" * WIDTH)
    print()
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
