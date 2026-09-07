"""Everything the BMS can observe in one tick.

Pack readings and driver actions are separate because the rules treat them so:
readings can be interrupted, while one driver action must legally be physical.
"""

from dataclasses import dataclass, field


@dataclass
class SensorSnapshot:
    """One sampling of the pack. All readings are assumed simultaneous."""

    # EV.7.4.1: one measurement per paralleled group, so a 1s4p module is one tap.
    cell_volts: list[float] = field(default_factory=list)
    cell_temps: list[float] = field(default_factory=list)

    pack_current_amps: float = 0.0    # positive = discharge, negative = charge
    intermediate_volts: float = 0.0   # motor-controller side of the relays

    # Age of the last good read; EV.7.3.4 d makes a missing measurement a fault.
    voltage_age_ms: int = 0
    temp_age_ms: int = 0

    sense_fuses_ok: bool = True   # EV.7.3.4 b, fuses on the voltage-sense wires
    self_test_ok: bool = True     # EV.7.3.4 e, watchdog / RAM / config checksum
    imd_ok: bool = True           # EV.7.6, insulation monitoring device

    # Contactor feedback, kept apart from the commands so the two can be
    # compared: a relay told to open that still reads closed is welded.
    air_positive_closed: bool = False
    air_negative_closed: bool = False
    precharge_relay_closed: bool = False

    @property
    def pack_volts(self) -> float:
        return sum(self.cell_volts)

    def expect_shape(self, series_modules: int, temp_sensors: int) -> None:
        """Raise on a mismatched pack; a short list would read as cells at 0 V,
        hiding a wiring error behind an undervoltage fault."""
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

    # EV.7.2.3 b,c: reset is a physical action at the vehicle, never from the
    # cockpit. This flag's only source is that button - can.py cannot reach it.
    manual_reset: bool = False

    charger_connected: bool = False    # EV.8.3
    charger_shutdown_ok: bool = True   # charging shutdown circuit healthy
    shutdown_buttons_ok: bool = True   # EV.7.10, none pressed
