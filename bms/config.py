"""Pack geometry and every threshold the BMS decides with.

Sources are marked per value: either the cell datasheet or a 2026 FSAE rule.

Cell data is PROVISIONAL. The packet specifies the SN4 accumulator used an
off-the-shelf Energus 1s4p module; the exact datasheet has not been read yet, so
these are the published figures for the Samsung INR18650-25R cells that module
is built from. Confirm against the datasheet before submission.
"""

from dataclasses import dataclass

# Rule constants, quoted rather than inlined so their origin is obvious.
MAX_SYSTEM_VOLTAGE = 600.0    # EV.3.3.2, between any two points
LOW_VOLTAGE_LIMIT = 60.0      # T.9.1.2, "Low Voltage" is <= 60 V DC
RULE_MAX_CELL_TEMP = 60.0     # EV.7.5.2 ceiling, whatever the datasheet says
MIN_TEMP_COVERAGE = 0.20      # EV.7.5.5, at least 20 % of cells monitored


@dataclass(frozen=True)
class PackConfig:
    """Immutable pack definition. Tests build variants by overriding fields."""

    # --- Geometry -------------------------------------------------------
    # SN5 accumulator: 96 modules in series, each an off-the-shelf 1s4p group.
    series_modules: int = 96
    cells_per_module: int = 4      # in parallel, so one voltage tap covers all
    temp_sensors: int = 96         # one per module = 25 % of cells, EV.7.5.5

    # --- Cell limits, from the datasheet --------------------------------
    cell_max_volts: float = 4.20
    cell_min_volts: float = 2.50
    cell_nominal_volts: float = 3.60
    cell_capacity_ah: float = 2.50

    datasheet_max_temp: float = 60.0     # discharge ceiling
    charge_min_temp: float = 0.0         # charging below this damages cells
    charge_max_temp: float = 45.0
    discharge_min_temp: float = -20.0

    max_discharge_amps: float = 80.0     # 20 A/cell continuous x 4 parallel
    max_charge_amps: float = 16.0        # 4 A/cell x 4 parallel

    # --- Measurement accuracy -------------------------------------------
    # EV.7.4.2 and EV.7.5.2 require limits to be met "considering measurement
    # accuracy", so every threshold is pulled in by the error of the sensor
    # that checks it. Trip early by exactly our uncertainty, never late.
    voltage_accuracy: float = 0.025      # 25 mV, AFE plus sense-wire drop
    temp_accuracy: float = 2.0           # 2 C, thermistor plus ADC
    current_accuracy: float = 1.0        # 1 A, hall sensor

    # --- Timing ---------------------------------------------------------
    tick_ms: int = 10                    # 100 Hz control loop
    precharge_target_fraction: float = 0.90   # EV.5.6.1 a
    precharge_timeout_ms: int = 5000
    shutdown_verify_ms: int = 5000       # EV.7.2.2 c, must reach LV in 5 s
    rtds_duration_ms: int = 2000         # EV.9.7.2 allows 1000-3000 ms
    sense_timeout_ms: int = 100          # EV.7.3.4 d, stale reading is a fault

    # Debounce windows. A single noisy sample must not open the shutdown
    # circuit mid-corner, but a real excursion still has to latch quickly.
    voltage_debounce_ms: int = 50
    temp_debounce_ms: int = 200          # thermal mass makes spikes implausible
    current_debounce_ms: int = 100
    imd_debounce_ms: int = 20            # external device, already conditioned

    # --- Balancing ------------------------------------------------------
    balance_threshold_volts: float = 0.05
    balance_hysteresis_volts: float = 0.01

    def __post_init__(self) -> None:
        """Reject an illegal pack at construction rather than on track."""
        if self.temp_coverage < MIN_TEMP_COVERAGE:
            raise ValueError(
                f"temperature coverage {self.temp_coverage:.1%} is below the "
                f"{MIN_TEMP_COVERAGE:.0%} required by EV.7.5.5"
            )
        if self.max_pack_volts > MAX_SYSTEM_VOLTAGE:
            raise ValueError(
                f"pack reaches {self.max_pack_volts:.1f} V, over the "
                f"{MAX_SYSTEM_VOLTAGE:.0f} V ceiling in EV.3.3.2"
            )
        if min(self.voltage_accuracy, self.temp_accuracy, self.current_accuracy) <= 0:
            raise ValueError("measurement accuracy must be positive, EV.7.4.2")
        if self.effective_overvolt <= self.effective_undervolt:
            raise ValueError("accuracy margins have collapsed the voltage window")

    # --- Derived geometry -----------------------------------------------
    @property
    def total_cells(self) -> int:
        return self.series_modules * self.cells_per_module

    @property
    def temp_coverage(self) -> float:
        return self.temp_sensors / self.total_cells

    @property
    def max_pack_volts(self) -> float:
        return self.series_modules * self.cell_max_volts

    @property
    def nominal_pack_volts(self) -> float:
        return self.series_modules * self.cell_nominal_volts

    @property
    def pack_capacity_ah(self) -> float:
        return self.cells_per_module * self.cell_capacity_ah

    # --- Effective limits, accuracy already applied ----------------------
    # These are what the fault checks compare against. The raw datasheet
    # values above are kept only so the margin is visible and auditable.
    @property
    def effective_overvolt(self) -> float:
        return self.cell_max_volts - self.voltage_accuracy

    @property
    def effective_undervolt(self) -> float:
        return self.cell_min_volts + self.voltage_accuracy

    @property
    def effective_max_temp(self) -> float:
        # EV.7.5.2: the lower of the datasheet limit and the 60 C rule ceiling.
        return min(self.datasheet_max_temp, RULE_MAX_CELL_TEMP) - self.temp_accuracy

    @property
    def effective_charge_min_temp(self) -> float:
        return self.charge_min_temp + self.temp_accuracy

    @property
    def effective_charge_max_temp(self) -> float:
        return self.charge_max_temp - self.temp_accuracy

    @property
    def effective_discharge_min_temp(self) -> float:
        return self.discharge_min_temp + self.temp_accuracy

    @property
    def effective_max_discharge_amps(self) -> float:
        return self.max_discharge_amps - self.current_accuracy

    @property
    def effective_max_charge_amps(self) -> float:
        return self.max_charge_amps - self.current_accuracy

    def precharge_target_volts(self, pack_volts: float) -> float:
        """Voltage the intermediate circuit must reach before the second IR closes."""
        return pack_volts * self.precharge_target_fraction

    def summary(self) -> str:
        return (
            f"{self.series_modules}s{self.cells_per_module}p, "
            f"{self.total_cells} cells, {self.nominal_pack_volts:.0f} V nominal / "
            f"{self.max_pack_volts:.0f} V max, {self.pack_capacity_ah:.1f} Ah, "
            f"{self.temp_coverage:.0%} temperature coverage"
        )


DEFAULT = PackConfig()
