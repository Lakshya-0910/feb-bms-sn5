"""Shared fixtures. Tests use a small pack so failures are readable."""

from bms.config import PackConfig
from bms.faults import FaultManager
from bms.inputs import SensorSnapshot
from bms.monitor import summarise
from bms.types import State

# 8s4p keeps the same 25 % temperature coverage as the real 96s4p pack while
# printing a list of 8 numbers instead of 96 when an assertion fails.
def make_config(**overrides) -> PackConfig:
    base = dict(series_modules=8, cells_per_module=4, temp_sensors=8)
    base.update(overrides)
    return PackConfig(**base)


def healthy(config: PackConfig, volts: float = 3.70, temp: float = 30.0,
            current: float = 0.0, intermediate: float | None = None) -> SensorSnapshot:
    pack = volts * config.series_modules
    return SensorSnapshot(
        cell_volts=[volts] * config.series_modules,
        cell_temps=[temp] * config.temp_sensors,
        pack_current_amps=current,
        intermediate_volts=pack if intermediate is None else intermediate,
    )


def run_for(manager: FaultManager, snapshot: SensorSnapshot, config: PackConfig,
            ms: int, state: State = State.TS_ACTIVE, last_outputs=None,
            start_ms: int = 0):
    """Feed the same snapshot for `ms` of simulated time. Returns the latched set."""
    active = manager.active
    for elapsed in range(0, ms, config.tick_ms):
        active = manager.update(
            summarise(snapshot, config), snapshot, state,
            config.tick_ms, start_ms + elapsed, last_outputs,
        )
    return active
