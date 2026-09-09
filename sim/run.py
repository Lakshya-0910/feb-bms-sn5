"""Replay a scenario against the BMS and print what happened.

    python -m sim.run sim/scenarios/normal_drive.scn
    python -m sim.run sim/scenarios/overtemp_during_drive.scn --can

The plant model is small but real: the intermediate circuit charges toward pack
voltage while the precharge relay is closed and drains once the relays open,
which is what the discharge circuit of EV.5.6.3 does. Without it every scenario
would have to hand-script voltages that the hardware produces on its own.
"""

import argparse
import math
import sys

from bms.can import BmsCan, CanBus
from bms.config import DEFAULT, LOW_VOLTAGE_LIMIT, PackConfig
from bms.inputs import DriverInputs, SensorSnapshot
from bms.monitor import summarise
from bms.state_machine import BMS
from sim.scenario import Scenario, parse

PRECHARGE_TAU_MS = 300.0   # resistor and DC-link capacitance
DISCHARGE_TAU_MS = 800.0   # bleed resistor across the DC link


class World:
    """The car around the BMS: sensors, driver, and a little physics."""

    def __init__(self, config: PackConfig):
        self.config = config
        self.snapshot = SensorSnapshot(
            cell_volts=[3.85] * config.series_modules,
            cell_temps=[28.0] * config.temp_sensors,
        )
        self.driver = DriverInputs()
        self.plant_enabled = True

    def settle(self, outputs, dt_ms: int) -> None:
        if not self.plant_enabled:
            return

        target = (self.snapshot.pack_volts
                  if (outputs.precharge_relay_cmd or outputs.air_positive_cmd)
                  else 0.0)
        tau = PRECHARGE_TAU_MS if target else DISCHARGE_TAU_MS
        alpha = 1.0 - math.exp(-dt_ms / tau)
        self.snapshot.intermediate_volts += (
            target - self.snapshot.intermediate_volts) * alpha

        # Relay feedback follows the commands unless a scenario has pinned it.
        self.snapshot.air_positive_closed = outputs.air_positive_cmd
        self.snapshot.air_negative_closed = outputs.air_negative_cmd
        self.snapshot.precharge_relay_closed = outputs.precharge_relay_cmd


def run(scenario: Scenario, config: PackConfig = DEFAULT,
        show_can: bool = False, stream=sys.stdout) -> BMS:
    world = World(config)
    bms = BMS(config)
    bus = CanBus()
    can = BmsCan(bus, config)

    tick = config.tick_ms
    pending = sorted(scenario.commands, key=lambda c: c.time_ms)
    next_command = 0
    last_state, last_outputs, last_faults = bms.state, None, bms.faults.active

    def log(t_ms: int, text: str) -> None:
        print(f"  {t_ms / 1000:7.3f}s  {text}", file=stream)

    print(f"\n{scenario.name}", file=stream)
    if scenario.description:
        print(f"  {scenario.description}", file=stream)
    print(f"  {config.summary()}\n", file=stream)

    for t_ms in range(0, scenario.duration_ms + tick, tick):
        while next_command < len(pending) and pending[next_command].time_ms <= t_ms:
            command = pending[next_command]
            command.apply(world)
            log(t_ms, f". {command.text}")
            next_command += 1

        for ramp in scenario.ramps:
            if ramp.start_ms <= t_ms <= ramp.end_ms:
                ramp.setter(world, ramp.value_at(t_ms))

        outputs = bms.step(world.snapshot, world.driver, tick)
        frames = can.update(bms, world.snapshot, summarise(world.snapshot, config), tick)
        world.settle(outputs, tick)

        if bms.state is not last_state:
            _, source, event, target = bms.history[-1]
            log(t_ms, f"{source} -> {target}   ({event})")
            last_state = bms.state

        if bms.faults.active != last_faults:
            log(t_ms, f"! {bms.faults.describe()}")
            last_faults = bms.faults.active

        if last_outputs is None or outputs.describe() != last_outputs:
            log(t_ms, f"  outputs: {outputs.describe()}")
            last_outputs = outputs.describe()

        if show_can:
            for frame in frames:
                log(t_ms, f"  can {frame}")

    print(file=stream)
    if bms.faults.first:
        print(f"  first fault: {bms.faults.first.describe()}", file=stream)
    print(f"  final state: {bms.state}", file=stream)
    print(f"  intermediate circuit: {world.snapshot.intermediate_volts:.1f} V "
          f"({'low voltage' if world.snapshot.intermediate_volts <= LOW_VOLTAGE_LIMIT else 'HV'})",
          file=stream)
    print(f"  can frames sent: {len(bus.sent)}", file=stream)
    if can.rejected_resets:
        print(f"  resets refused over can: {can.rejected_resets}", file=stream)
    print(file=stream)
    return bms


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a BMS scenario.")
    parser.add_argument("scenario")
    parser.add_argument("--can", action="store_true", help="also print CAN frames")
    args = parser.parse_args()
    run(parse(args.scenario), show_can=args.can)


if __name__ == "__main__":
    main()
