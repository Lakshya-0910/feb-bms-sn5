"""Everything the BMS commands in one tick.

Every field defaults to False by design: EV.7.1.3 makes the BMS contact normally
open, so a crash or missed tick fails the same way a cut wire does.
"""

from dataclasses import dataclass, field


@dataclass
class Outputs:
    """Commands to the relays, lights and motor controller."""

    # Isolation relays, EV.5.4. Both poles, normally open.
    air_positive_cmd: bool = False
    air_negative_cmd: bool = False
    precharge_relay_cmd: bool = False   # EV.5.6.5, mechanical relay

    # The BMS's contact in the shutdown circuit (EV.7.1.1 a). Closed is
    # permission for HV; after a fault it stays open until reset (EV.7.2.3).
    shutdown_circuit_closed: bool = False

    # The separate charging shutdown circuit, EV.8.3.
    charging_shutdown_closed: bool = False

    bms_indicator: bool = False       # EV.7.3.6, red, visible to the driver
    ts_status_indicator: bool = False # EV.5.11
    rtds_active: bool = False         # EV.9.7, ready-to-drive sound

    # EV.9.6.1: the motors respond to the accelerator only in READY_TO_DRIVE.
    motors_enabled: bool = False

    # Per-module bleed; EV.7.3.3 bans balancing while the SDC is open.
    balance_bleed: list[bool] = field(default_factory=list)

    @classmethod
    def safe(cls) -> "Outputs":
        """Everything de-energised - where the hardware falls on its own."""
        return cls()

    @property
    def tractive_system_live(self) -> bool:
        """True once both relays are closed, i.e. HV outside the container (EV.9.4.1)."""
        return self.air_positive_cmd and self.air_negative_cmd

    def describe(self) -> str:
        flags = [
            ("AIR+", self.air_positive_cmd),
            ("AIR-", self.air_negative_cmd),
            ("PRE", self.precharge_relay_cmd),
            ("SDC", self.shutdown_circuit_closed),
            ("CHG_SDC", self.charging_shutdown_closed),
            ("BMS_LED", self.bms_indicator),
            ("TSSI", self.ts_status_indicator),
            ("RTDS", self.rtds_active),
            ("MOTORS", self.motors_enabled),
        ]
        on = [name for name, value in flags if value]
        bleeding = sum(self.balance_bleed)
        if bleeding:
            on.append(f"BLEED({bleeding})")
        return " ".join(on) if on else "all off"
