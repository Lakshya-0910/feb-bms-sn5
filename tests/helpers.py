"""Shared fixtures. Tests use a small pack so failures are readable."""

from bms.config import PackConfig
from bms.faults import FaultManager
from bms.inputs import DriverInputs, SensorSnapshot
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


class Rig:
    """Drives a BMS through simulated time. Intermediate circuit starts at 0 V,
    since the discharge circuit (EV.5.6.3) drains it while the relays are open."""

    def __init__(self, config: PackConfig | None = None, **snapshot_kwargs):
        from bms.outputs import Outputs
        from bms.state_machine import BMS

        self.config = config or make_config()
        snapshot_kwargs.setdefault("intermediate", 0.0)
        self.snapshot = healthy(self.config, **snapshot_kwargs)
        self.driver = DriverInputs(glv_on=True)
        self.bms = BMS(self.config)
        self.outputs = Outputs.safe()

    @property
    def state(self) -> State:
        return self.bms.state

    def run(self, ms: int, **driver_changes):
        for name, value in driver_changes.items():
            setattr(self.driver, name, value)
        for _ in range(0, max(ms, self.config.tick_ms), self.config.tick_ms):
            self.outputs = self.bms.step(self.snapshot, self.driver)
        return self.bms.state

    def boot(self) -> "Rig":
        """Reach IDLE: one tick out of INIT, one through SELF_TEST."""
        self.run(self.config.tick_ms * 2)
        return self

    def request_rtd(self):
        """Hold the brake, then press start. The press must be fresh: a button
        held through precharge is not the manual action EV.9.6.2 asks for."""
        self.run(self.config.tick_ms, brake_pressed=True, start_pressed=False)
        return self.run(self.config.tick_ms, start_pressed=True)

    def precharge_to(self, fraction: float) -> None:
        self.snapshot.intermediate_volts = self.snapshot.pack_volts * fraction
