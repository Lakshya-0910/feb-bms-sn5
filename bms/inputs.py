"""Everything the BMS can observe in one tick.

Split into what the pack reports (SensorSnapshot) and what a person does
(DriverInputs), because the rules treat them differently: sensor readings can be
interrupted and must be checked for staleness, while driver actions carry
authority and one of them is legally required to be physical.
"""

from dataclasses import dataclass, field


@dataclass
class SensorSnapshot:
    """One sampling of the pack. All readings are assumed simultaneous."""

    # One voltage per series module. EV.7.4.1 requires one measurement per group
    # of directly paralleled cells, so a 1s4p module is a single tap.
    cell_volts: list[float] = field(default_factory=list)
    cell_temps: list[float] = field(default_factory=list)

    pack_current_amps: float = 0.0    # positive = discharge, negative = charge
    intermediate_volts: float = 0.0   # motor-controller side of the relays

    # Age of the most recent successful read. EV.7.3.4 d makes a missing or
    # interrupted measurement a fault in its own right, rather than a stale
    # value to quietly reuse.
    voltage_age_ms: int = 0
    temp_age_ms: int = 0

    sense_fuses_ok: bool = True   # EV.7.3.4 b, fuses on the voltage-sense wires
    self_test_ok: bool = True     # EV.7.3.4 e, watchdog / RAM / config checksum
    imd_ok: bool = True           # EV.7.6, insulation monitoring device

    # What the contactors actually report, kept separate from the commands in
    # outputs.py so the two can be compared. A relay told to open that still
    # reads closed is welded.
    air_positive_closed: bool = False
    air_negative_closed: bool = False
    precharge_relay_closed: bool = False

    @property
    def pack_volts(self) -> float:
        return sum(self.cell_volts)

    def expect_shape(self, series_modules: int, temp_sensors: int) -> None:
        """Raise if the snapshot does not match the configured pack.

        A short list would otherwise read as 'those cells are at 0 V', turning a
        wiring error into an undervoltage fault and hiding the real cause.
        """
        if len(self.cell_volts) != series_modules:
            raise ValueError(
                f"expected {series_modules} cell voltages, got {len(self.cell_volts)}"
            )
        if len(self.cell_temps) != temp_sensors:
            raise ValueError(
                f"expected {temp_sensors} temperatures, got {len(self.cell_temps)}"
            )


@dataclass
class DriverInputs:
    """Driver and pit-crew actions."""

    glv_on: bool = False         # grounded low voltage system, EV.9.3
    tsms_closed: bool = False    # tractive system master switch, EV.7.9
    brake_pressed: bool = False  # required for ready-to-drive, EV.9.6.2
    start_pressed: bool = False  # the deliberate cockpit action, EV.9.6.2

    # EV.7.2.3 b and c: a latched fault clears only by manual action of a person
    # at the vehicle, and the driver must not be able to re-arm from the seat.
    # This flag has exactly one source, a physical button. can.py cannot reach
    # it, so the rule holds by structure rather than by a check someone could
    # delete.
    manual_reset: bool = False

    charger_connected: bool = False    # EV.8.3
    charger_shutdown_ok: bool = True   # charging shutdown circuit healthy
    shutdown_buttons_ok: bool = True   # EV.7.10, none pressed
