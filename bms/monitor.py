"""Reduce a raw snapshot to the few facts the rest of the BMS reasons about.

Nothing here decides anything - faults.py judges. Splitting them keeps the fault
rules readable and locates each extreme once per tick rather than once per check.
"""

from dataclasses import dataclass

from .config import PackConfig
from .inputs import SensorSnapshot


@dataclass(frozen=True)
class PackSummary:
    """What one tick of sensor data amounts to. Extremes carry their index, so a
    report can say "module 37 hit 61 C" rather than just "61 C"."""

    pack_volts: float
    current_amps: float

    min_cell_volts: float
    min_cell_index: int
    max_cell_volts: float
    max_cell_index: int
    mean_cell_volts: float
    imbalance_volts: float

    min_temp: float
    min_temp_index: int
    max_temp: float
    max_temp_index: int

    intermediate_volts: float
    precharge_ratio: float   # intermediate / pack, the EV.5.6.1 a measurement

    voltage_stale: bool      # EV.7.3.4 d
    temp_stale: bool

    @property
    def charging(self) -> bool:
        return self.current_amps < 0.0


def summarise(snapshot: SensorSnapshot, config: PackConfig) -> PackSummary:
    """Condense a snapshot. Raises if it does not match the configured pack."""
    snapshot.expect_shape(config.series_modules, config.temp_sensors)

    volts = snapshot.cell_volts
    temps = snapshot.cell_temps

    min_v_index = min(range(len(volts)), key=volts.__getitem__)
    max_v_index = max(range(len(volts)), key=volts.__getitem__)
    min_t_index = min(range(len(temps)), key=temps.__getitem__)
    max_t_index = max(range(len(temps)), key=temps.__getitem__)

    pack_volts = snapshot.pack_volts

    return PackSummary(
        pack_volts=pack_volts,
        current_amps=snapshot.pack_current_amps,
        min_cell_volts=volts[min_v_index],
        min_cell_index=min_v_index,
        max_cell_volts=volts[max_v_index],
        max_cell_index=max_v_index,
        mean_cell_volts=sum(volts) / len(volts),
        imbalance_volts=volts[max_v_index] - volts[min_v_index],
        min_temp=temps[min_t_index],
        min_temp_index=min_t_index,
        max_temp=temps[max_t_index],
        max_temp_index=max_t_index,
        intermediate_volts=snapshot.intermediate_volts,
        # Empty pack: report no progress rather than crash; undervoltage is the
        # real story there.
        precharge_ratio=(snapshot.intermediate_volts / pack_volts) if pack_volts > 0 else 0.0,
        voltage_stale=snapshot.voltage_age_ms > config.sense_timeout_ms,
        temp_stale=snapshot.temp_age_ms > config.sense_timeout_ms,
    )


def precharge_complete(summary: PackSummary, config: PackConfig) -> bool:
    """EV.5.6.1 a: 90 % of pack before the second IR closes. EV.5.6.2 a makes this
    voltage feedback, so there is deliberately no time term here."""
    return summary.precharge_ratio >= config.precharge_target_fraction


def balance_needed(summary: PackSummary, config: PackConfig) -> bool:
    return summary.imbalance_volts > config.balance_threshold_volts


def balance_settled(summary: PackSummary, config: PackConfig) -> bool:
    # Hysteresis, or the pack chatters in and out of BALANCING at the threshold.
    return summary.imbalance_volts <= (
        config.balance_threshold_volts - config.balance_hysteresis_volts
    )
