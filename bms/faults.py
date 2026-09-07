"""Judge whether the pack is behaving, and remember when it was not.

Three mechanisms: debounce (a condition must persist, with windows set by the
physics), latching (EV.7.3.5 and EV.7.2.3 - faults never self-clear and reset
refuses while a condition is live), and context (charge limits are tighter than
discharge limits, so the same reading can be legal one way and a fault the other).
"""

from dataclasses import dataclass

from .config import PackConfig
from .inputs import SensorSnapshot
from .monitor import PackSummary
from .outputs import Outputs
from .types import CHANNEL_NONE, FAULT_RULES, Fault, FirstFault, State


@dataclass(frozen=True)
class Condition:
    """A fault condition seen this tick, before debounce has had its say."""

    code: Fault
    measured: float
    threshold: float
    channel: int = CHANNEL_NONE


class FaultManager:
    """Evaluates conditions each tick and holds the latched fault set."""

    def __init__(self, config: PackConfig) -> None:
        self.config = config
        self.active: Fault = Fault.NONE          # latched, cleared only by reset()
        self.first: FirstFault | None = None
        self._elapsed: dict[Fault, int] = {}     # ms each condition has persisted
        self._live: dict[Fault, Condition] = {}  # conditions true right now

    # --- per-tick evaluation ---------------------------------------------

    def update(
        self,
        summary: PackSummary,
        snapshot: SensorSnapshot,
        state: State,
        dt_ms: int,
        uptime_ms: int,
        last_outputs: Outputs | None = None,
    ) -> Fault:
        """Fold this tick into the fault state. Returns the latched set."""
        self._live = {c.code: c for c in self._evaluate(summary, snapshot, state, last_outputs)}

        for code, condition in self._live.items():
            self._elapsed[code] = self._elapsed.get(code, 0) + dt_ms
            if self._elapsed[code] >= self._debounce_ms(code):
                self._latch(condition, state, uptime_ms)

        # A condition that clears starts over if it returns; only the latch survives.
        for code in list(self._elapsed):
            if code not in self._live:
                del self._elapsed[code]

        return self.active

    def _evaluate(
        self,
        summary: PackSummary,
        snapshot: SensorSnapshot,
        state: State,
        last_outputs: Outputs | None,
    ) -> list[Condition]:
        c = self.config
        found: list[Condition] = []

        # EV.7.3.4 a - cell voltage, EV.7.4.2
        if summary.max_cell_volts > c.effective_overvolt:
            found.append(Condition(Fault.CELL_OVERVOLT, summary.max_cell_volts,
                                   c.effective_overvolt, summary.max_cell_index))
        if summary.min_cell_volts < c.effective_undervolt:
            found.append(Condition(Fault.CELL_UNDERVOLT, summary.min_cell_volts,
                                   c.effective_undervolt, summary.min_cell_index))

        # EV.7.3.4 c / EV.7.5.2. The charge window is tighter at both ends.
        charging = state is State.CHARGING or summary.charging
        max_temp = c.effective_charge_max_temp if charging else c.effective_max_temp
        min_temp = c.effective_charge_min_temp if charging else c.effective_discharge_min_temp

        if summary.max_temp > max_temp:
            found.append(Condition(Fault.CELL_OVERTEMP, summary.max_temp,
                                   max_temp, summary.max_temp_index))
        if summary.min_temp < min_temp:
            found.append(Condition(Fault.CELL_UNDERTEMP, summary.min_temp,
                                   min_temp, summary.min_temp_index))

        # Current limits, from the cell datasheet.
        if summary.current_amps > c.effective_max_discharge_amps:
            found.append(Condition(Fault.OVERCURRENT_DISCHARGE, summary.current_amps,
                                   c.effective_max_discharge_amps))
        if -summary.current_amps > c.effective_max_charge_amps:
            found.append(Condition(Fault.OVERCURRENT_CHARGE, -summary.current_amps,
                                   c.effective_max_charge_amps))

        # EV.7.3.4 d - missing or interrupted measurements.
        if summary.voltage_stale or summary.temp_stale:
            age = max(snapshot.voltage_age_ms, snapshot.temp_age_ms)
            found.append(Condition(Fault.SENSE_TIMEOUT, age, c.sense_timeout_ms))

        # EV.7.3.4 b - the fuses protecting the sense wires.
        if not snapshot.sense_fuses_ok:
            found.append(Condition(Fault.SENSE_FUSE_BLOWN, 0.0, 0.0))

        # EV.7.3.4 e - the BMS's own health.
        if not snapshot.self_test_ok:
            found.append(Condition(Fault.BMS_INTERNAL, 0.0, 0.0))

        if not snapshot.imd_ok:
            found.append(Condition(Fault.IMD, 0.0, 0.0))

        # Commanded open but reading closed means welded; debounced long enough
        # for a healthy contactor to actually move.
        if last_outputs is not None:
            if not last_outputs.air_positive_cmd and snapshot.air_positive_closed:
                found.append(Condition(Fault.AIR_WELD, 1.0, 0.0, channel=0))
            if not last_outputs.air_negative_cmd and snapshot.air_negative_closed:
                found.append(Condition(Fault.AIR_WELD, 1.0, 0.0, channel=1))

        return found

    def _debounce_ms(self, code: Fault) -> int:
        c = self.config
        if code in (Fault.CELL_OVERVOLT, Fault.CELL_UNDERVOLT, Fault.SENSE_FUSE_BLOWN):
            return c.voltage_debounce_ms
        if code in (Fault.CELL_OVERTEMP, Fault.CELL_UNDERTEMP):
            return c.temp_debounce_ms
        if code in (Fault.OVERCURRENT_DISCHARGE, Fault.OVERCURRENT_CHARGE, Fault.AIR_WELD):
            return c.current_debounce_ms
        if code is Fault.IMD:
            return c.imd_debounce_ms
        # Missing data and internal faults are not noise, so no debounce.
        return 0

    def _latch(self, condition: Condition, state: State, uptime_ms: int) -> None:
        self.active |= condition.code
        if self.first is None:
            self.first = FirstFault(
                code=condition.code,
                measured=condition.measured,
                threshold=condition.threshold,
                timestamp_ms=uptime_ms,
                state=state,
                channel=condition.channel,
            )

    # --- faults raised by the state machine rather than by a sensor -------

    def latch_external(
        self,
        code: Fault,
        state: State,
        uptime_ms: int,
        measured: float = 0.0,
        threshold: float = 0.0,
    ) -> None:
        """For faults only the state machine can see, e.g. a precharge that never
        completed or a tractive system that stayed live too long."""
        self._latch(Condition(code, measured, threshold), state, uptime_ms)

    # --- reset ------------------------------------------------------------

    @property
    def resettable(self) -> bool:
        """True when nothing is still wrong, so a reset would actually take."""
        return not self._live

    def reset(self) -> bool:
        """Clear the latch; refuses while a condition is live. Callers must already
        have a physical reset (EV.7.2.3 b), whose only source is DriverInputs."""
        if not self.resettable:
            return False
        self.active = Fault.NONE
        self.first = None
        self._elapsed.clear()
        return True

    # --- reporting --------------------------------------------------------

    @property
    def faulted(self) -> bool:
        return self.active is not Fault.NONE

    def active_codes(self) -> list[Fault]:
        return [code for code in Fault if code in self.active]

    def describe(self) -> str:
        if not self.faulted:
            return "no faults"
        return ", ".join(
            f"{code.name} [{FAULT_RULES.get(code, '')}]" for code in self.active_codes()
        )
