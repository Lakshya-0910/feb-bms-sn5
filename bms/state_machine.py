"""Ten states, a table of transitions, and one step() per tick.

Moore machine: outputs depend on the current state, never on the path taken,
which matches the hardware and lets outputs be tested one state at a time.

Transitions are data, not code, so the table renders directly as the state
diagram in the docs - a diagram generated this way cannot drift from the code.
"""

from dataclasses import dataclass, field
from typing import Callable

from .config import DEFAULT, LOW_VOLTAGE_LIMIT, PackConfig
from .faults import FaultManager
from .inputs import DriverInputs, SensorSnapshot
from .monitor import (
    PackSummary,
    balance_needed,
    balance_settled,
    precharge_complete,
    summarise,
)
from .outputs import Outputs
from .types import Event, Fault, State


@dataclass
class Context:
    """Everything a guard is allowed to look at."""

    config: PackConfig
    summary: PackSummary
    snapshot: SensorSnapshot
    driver: DriverInputs
    faults: FaultManager
    uptime_ms: int
    state_elapsed_ms: int
    start_edge: bool = False   # start button pressed this tick, not merely held


Guard = Callable[[Context], bool]


@dataclass(frozen=True)
class Transition:
    source: State
    event: Event
    target: State
    guard: Guard | None = None
    rule: str = ""


def _ts_permitted(ctx: Context) -> bool:
    """Driver-side conditions for HV to be allowed up. EV.7.2.1 b drops the
    circuit with the GLV system, and the shutdown buttons sit in series."""
    d = ctx.driver
    return d.glv_on and d.tsms_closed and d.shutdown_buttons_ok


def _self_test_ok(ctx: Context) -> bool:
    """Sense channels answering, BMS healthy, and both relays confirmed open."""
    s = ctx.snapshot
    return (
        not ctx.faults.faulted
        and not ctx.summary.voltage_stale
        and not ctx.summary.temp_stale
        and s.self_test_ok
        and s.sense_fuses_ok
        and not s.air_positive_closed
        and not s.air_negative_closed
    )


def _may_start_precharge(ctx: Context) -> bool:
    return _ts_permitted(ctx) and not ctx.faults.faulted


def _rtd_conditions(ctx: Context) -> bool:
    """EV.9.6.2: brake held plus a deliberate action; TS_ACTIVE as the source
    state supplies the third condition. A button already held is not an action -
    without the edge, clearing a fault would re-arm the car under the driver's
    resting foot and thumb."""
    return ctx.driver.brake_pressed and ctx.start_edge


def _charge_needed(ctx: Context) -> bool:
    """Do not start charging a pack that is already full, or IDLE and CHARGING
    ping-pong for as long as the charger stays plugged in."""
    return ctx.summary.min_cell_volts < ctx.config.charge_target_volts


def _balance_wanted(ctx: Context) -> bool:
    return _balancing_allowed(ctx) and balance_needed(ctx.summary, ctx.config)


def _balancing_allowed(ctx: Context) -> bool:
    """EV.7.3.3 forbids balancing while the shutdown circuit is open."""
    return _ts_permitted(ctx) or ctx.driver.charger_connected


def _reset_accepted(ctx: Context) -> bool:
    """Only clears if nothing is still wrong. EV.7.2.3 a."""
    return ctx.faults.reset()


# First matching row wins; no two rows share a (source, event) with overlapping guards.
TRANSITIONS: tuple[Transition, ...] = (
    Transition(State.INIT, Event.TICK, State.SELF_TEST),

    Transition(State.SELF_TEST, Event.SELF_TEST_PASS, State.IDLE, _self_test_ok,
               "EV.7.3.4 d,e"),

    Transition(State.IDLE, Event.TSMS_CLOSED, State.PRECHARGE, _may_start_precharge,
               "EV.9.2"),
    Transition(State.IDLE, Event.CHARGER_CONNECTED, State.CHARGING, _charge_needed,
               "EV.8.3"),
    Transition(State.IDLE, Event.BALANCE_REQUEST, State.BALANCING, _balance_wanted,
               "EV.7.3.3"),

    Transition(State.PRECHARGE, Event.PRECHARGE_DONE, State.TS_ACTIVE,
               rule="EV.5.6.1 a"),
    Transition(State.PRECHARGE, Event.TSMS_OPENED, State.SHUTDOWN, rule="EV.7.2.1"),

    Transition(State.TS_ACTIVE, Event.RTD_REQUEST, State.READY_TO_DRIVE,
               _rtd_conditions, "EV.9.6.2"),
    Transition(State.TS_ACTIVE, Event.TSMS_OPENED, State.SHUTDOWN, rule="EV.7.2.1"),

    Transition(State.READY_TO_DRIVE, Event.RTD_EXIT, State.SHUTDOWN, rule="EV.7.2.1"),
    Transition(State.READY_TO_DRIVE, Event.TSMS_OPENED, State.SHUTDOWN, rule="EV.7.2.1"),

    # A finished charge top-balances first when the pack needs it.
    Transition(State.CHARGING, Event.CHARGE_COMPLETE, State.BALANCING, _balance_wanted,
               "EV.7.3.3"),
    Transition(State.CHARGING, Event.CHARGE_COMPLETE, State.IDLE),
    Transition(State.CHARGING, Event.CHARGER_REMOVED, State.IDLE),
    Transition(State.CHARGING, Event.BALANCE_REQUEST, State.BALANCING,
               _balance_wanted, "EV.7.3.3"),

    Transition(State.BALANCING, Event.BALANCE_DONE, State.IDLE),
    Transition(State.BALANCING, Event.CHARGER_REMOVED, State.IDLE),
    Transition(State.BALANCING, Event.TSMS_CLOSED, State.IDLE),

    Transition(State.SHUTDOWN, Event.TS_DISCHARGED, State.IDLE, rule="EV.7.2.2 c"),

    # EV.7.2.3: the only way out of a latched fault is a person at the vehicle.
    Transition(State.FAULT, Event.MANUAL_RESET, State.SELF_TEST, _reset_accepted,
               "EV.7.2.3"),
)


class BMS:
    """The machine. Feed it a snapshot and driver inputs; it returns commands."""

    def __init__(self, config: PackConfig = DEFAULT) -> None:
        self.config = config
        self.state = State.INIT
        self.faults = FaultManager(config)
        self.uptime_ms = 0
        self.state_elapsed_ms = 0
        self.history: list[tuple[int, State, Event, State]] = []
        self._outputs = Outputs.safe()
        self._start_was_pressed = False

    # --- the tick ---------------------------------------------------------

    def step(self, snapshot: SensorSnapshot, driver: DriverInputs,
             dt_ms: int | None = None) -> Outputs:
        """Advance one tick. Elapsed time is passed in - nothing here reads a clock."""
        dt = self.config.tick_ms if dt_ms is None else dt_ms
        self.uptime_ms += dt
        self.state_elapsed_ms += dt

        summary = summarise(snapshot, self.config)
        self.faults.update(summary, snapshot, self.state, dt, self.uptime_ms, self._outputs)

        ctx = Context(self.config, summary, snapshot, driver, self.faults,
                      self.uptime_ms, self.state_elapsed_ms,
                      start_edge=driver.start_pressed and not self._start_was_pressed)
        self._start_was_pressed = driver.start_pressed

        # Faults only this machine can see: they are about elapsed time, not readings.
        self._check_timeouts(ctx)

        for event in self._derive_events(ctx):
            if self._apply(event, ctx):
                break

        self._outputs = self._outputs_for(ctx)
        return self._outputs

    def _apply(self, event: Event, ctx: Context) -> bool:
        """Take the first transition that matches. Returns True if the state moved."""
        for t in TRANSITIONS:
            if t.source is not self.state or t.event is not event:
                continue
            if t.guard is not None and not t.guard(ctx):
                continue
            self.history.append((self.uptime_ms, self.state, event, t.target))
            self.state = t.target
            self.state_elapsed_ms = 0
            return True
        return False

    # --- event derivation -------------------------------------------------

    def _derive_events(self, ctx: Context) -> list[Event]:
        """Turn the world into events. Faults outrank everything else."""
        if self.faults.faulted and self.state is not State.FAULT:
            # The any-state edge into FAULT, kept here rather than as thirty table
            # rows - which is all a hierarchical superstate would be doing anyway.
            self.history.append((self.uptime_ms, self.state, Event.FAULT_DETECTED,
                                 State.FAULT))
            self.state = State.FAULT
            self.state_elapsed_ms = 0
            return []

        if self.state is State.FAULT:
            return [Event.MANUAL_RESET] if ctx.driver.manual_reset else []

        events: list[Event] = []

        if self.state is State.INIT:
            events.append(Event.TICK)

        elif self.state is State.SELF_TEST:
            events.append(Event.SELF_TEST_PASS)

        elif self.state is State.IDLE:
            if ctx.driver.charger_connected:
                events.append(Event.CHARGER_CONNECTED)
            if ctx.driver.tsms_closed:
                events.append(Event.TSMS_CLOSED)
            if balance_needed(ctx.summary, ctx.config):
                events.append(Event.BALANCE_REQUEST)

        elif self.state is State.PRECHARGE:
            if not _ts_permitted(ctx):
                events.append(Event.TSMS_OPENED)
            elif precharge_complete(ctx.summary, ctx.config):
                events.append(Event.PRECHARGE_DONE)

        elif self.state is State.TS_ACTIVE:
            if not _ts_permitted(ctx):
                events.append(Event.TSMS_OPENED)
            elif _rtd_conditions(ctx):
                events.append(Event.RTD_REQUEST)

        elif self.state is State.READY_TO_DRIVE:
            if not ctx.driver.shutdown_buttons_ok:
                events.append(Event.RTD_EXIT)
            elif not _ts_permitted(ctx):
                events.append(Event.TSMS_OPENED)

        elif self.state is State.CHARGING:
            if not ctx.driver.charger_connected:
                events.append(Event.CHARGER_REMOVED)
            elif ctx.summary.min_cell_volts >= ctx.config.charge_target_volts:
                events.append(Event.CHARGE_COMPLETE)
            elif balance_needed(ctx.summary, ctx.config):
                events.append(Event.BALANCE_REQUEST)

        elif self.state is State.BALANCING:
            if ctx.driver.tsms_closed:
                events.append(Event.TSMS_CLOSED)
            elif balance_settled(ctx.summary, ctx.config):
                events.append(Event.BALANCE_DONE)

        elif self.state is State.SHUTDOWN:
            if ctx.snapshot.intermediate_volts <= LOW_VOLTAGE_LIMIT:
                events.append(Event.TS_DISCHARGED)

        events.append(Event.TICK)
        return events

    def _check_timeouts(self, ctx: Context) -> None:
        if (self.state is State.PRECHARGE
                and self.state_elapsed_ms > self.config.precharge_timeout_ms):
            # EV.5.6.2 a decides completion by feedback, so this can only fault.
            self.faults.latch_external(
                Fault.PRECHARGE_TIMEOUT, self.state, self.uptime_ms,
                measured=ctx.summary.precharge_ratio,
                threshold=self.config.precharge_target_fraction,
            )

        if (self.state is State.SHUTDOWN
                and self.state_elapsed_ms > self.config.shutdown_verify_ms
                and ctx.snapshot.intermediate_volts > LOW_VOLTAGE_LIMIT):
            # EV.7.2.2 c: the tractive system must be below 60 V within 5 s.
            self.faults.latch_external(
                Fault.SHUTDOWN_TIMEOUT, self.state, self.uptime_ms,
                measured=ctx.snapshot.intermediate_volts,
                threshold=LOW_VOLTAGE_LIMIT,
            )

    # --- outputs, a pure function of the state ----------------------------

    def _outputs_for(self, ctx: Context) -> Outputs:
        state = self.state

        if state is State.FAULT:
            # EV.7.3.5 b: both lights on until a manual reset.
            return Outputs(bms_indicator=True, ts_status_indicator=True)

        if state in (State.INIT, State.SELF_TEST, State.SHUTDOWN):
            return Outputs.safe()

        if state is State.IDLE:
            return Outputs(shutdown_circuit_closed=True)

        if state is State.PRECHARGE:
            # One relay plus precharge; the second IR waits for the intermediate
            # circuit to catch up (EV.5.6.2).
            return Outputs(
                shutdown_circuit_closed=True,
                air_negative_cmd=True,
                precharge_relay_cmd=True,
            )

        if state is State.TS_ACTIVE:
            return Outputs(
                shutdown_circuit_closed=True,
                air_positive_cmd=True,
                air_negative_cmd=True,
                ts_status_indicator=True,
            )

        if state is State.READY_TO_DRIVE:
            return Outputs(
                shutdown_circuit_closed=True,
                air_positive_cmd=True,
                air_negative_cmd=True,
                ts_status_indicator=True,
                motors_enabled=True,
                # EV.9.7.2: sounded for between 1 and 3 seconds on entry.
                rtds_active=self.state_elapsed_ms <= self.config.rtds_duration_ms,
            )

        if state is State.CHARGING:
            return Outputs(
                shutdown_circuit_closed=True,
                charging_shutdown_closed=True,
                air_positive_cmd=True,
                air_negative_cmd=True,
            )

        if state is State.BALANCING:
            return Outputs(
                shutdown_circuit_closed=True,
                charging_shutdown_closed=ctx.driver.charger_connected,
                balance_bleed=self._bleed_mask(ctx),
            )

        raise AssertionError(f"no outputs defined for {state}")

    def _bleed_mask(self, ctx: Context) -> list[bool]:
        """Bleed anything above the lowest cell by more than the hysteresis band,
        so the pack converges on its weakest module."""
        floor = ctx.summary.min_cell_volts + ctx.config.balance_hysteresis_volts
        return [v > floor for v in ctx.snapshot.cell_volts]

    # --- reporting --------------------------------------------------------

    def trace(self) -> list[str]:
        return [
            f"{t:>7} ms  {src} --{evt}--> {dst}"
            for t, src, evt, dst in self.history
        ]


def to_mermaid() -> str:
    """Render TRANSITIONS as a Mermaid diagram, so the picture in the write-up
    cannot disagree with the code it documents."""
    lines = ["stateDiagram-v2", "    [*] --> INIT"]
    for t in TRANSITIONS:
        label = f"{t.event.name} ({t.rule})" if t.rule else t.event.name
        lines.append(f"    {t.source.name} --> {t.target.name}: {label}")
    lines.append("    note right of FAULT")
    lines.append("        Any state enters FAULT when the fault manager latches.")
    lines.append("        EV.7.3.5, and it stays until a manual reset (EV.7.2.3).")
    lines.append("    end note")
    return "\n".join(lines)
