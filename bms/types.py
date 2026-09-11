"""States, events and fault codes.

Rules cited are Formula SAE 2026 v1.0; the 2026 rules renamed the AMS to the BMS.
Units: volts, degrees C, amps (+ discharge / - charge), milliseconds as ints.
"""

from dataclasses import dataclass
from enum import Enum, Flag, auto


class State(Enum):
    """The ten states, in roughly the order they are reached."""

    INIT = auto()
    SELF_TEST = auto()       # verify sense channels and relay positions, EV.7.3.4 d,e
    IDLE = auto()            # GLV up, isolation relays open, tractive system inactive
    PRECHARGE = auto()       # intermediate circuit charging, EV.5.6
    TS_ACTIVE = auto()       # HV live outside the container, motors inert, EV.9.4
    READY_TO_DRIVE = auto()  # motors respond to the accelerator, EV.9.6
    CHARGING = auto()        # charger attached, charge-window limits apply, EV.8
    BALANCING = auto()       # passive bleed; forbidden while SDC is open, EV.7.3.3
    SHUTDOWN = auto()        # de-energise and confirm < 60 V within 5 s, EV.7.2.2 c
    FAULT = auto()           # latched; only a physical reset leaves it, EV.7.2.3

    def __str__(self) -> str:
        return self.name


class Event(Enum):
    """Derived from sensor data by monitor.py and faults.py, never read raw by the
    transition table - so a new sensor means a new derivation, not new state logic."""

    NONE = auto()
    TICK = auto()               # time advanced; drives the timed states
    SELF_TEST_PASS = auto()
    TSMS_CLOSED = auto()        # driver closed the tractive system master switch
    TSMS_OPENED = auto()
    PRECHARGE_DONE = auto()     # intermediate circuit reached 90 % of pack, EV.5.6.1 a
    RTD_REQUEST = auto()        # brake held and start pressed together, EV.9.6.2
    RTD_EXIT = auto()
    CHARGER_CONNECTED = auto()
    CHARGER_REMOVED = auto()
    CHARGE_COMPLETE = auto()
    BALANCE_REQUEST = auto()
    BALANCE_DONE = auto()
    TS_DISCHARGED = auto()      # tractive system confirmed below 60 V, EV.7.2.2 c
    FAULT_DETECTED = auto()     # any-state edge into FAULT, EV.7.3.5
    MANUAL_RESET = auto()       # physical reset at the vehicle, EV.7.2.3

    def __str__(self) -> str:
        return self.name


class Fault(Flag):
    """Bit flags, so several can be active at once and the set fits one CAN word.
    Grouped by the five things EV.7.3.4 requires the BMS to monitor."""

    NONE = 0

    # (a) voltage outside the permitted range, EV.7.4.2
    CELL_OVERVOLT = auto()
    CELL_UNDERVOLT = auto()

    # (b) voltage-sense overcurrent protection blown or tripped
    SENSE_FUSE_BLOWN = auto()

    # (c) temperature outside the permitted range, EV.7.5.2
    CELL_OVERTEMP = auto()
    CELL_UNDERTEMP = auto()

    # (d) missing or interrupted voltage/temperature measurement
    SENSE_TIMEOUT = auto()

    # (e) a fault in the BMS itself
    BMS_INTERNAL = auto()

    # Current limits come from the cell datasheet rather than a rule
    OVERCURRENT_DISCHARGE = auto()
    OVERCURRENT_CHARGE = auto()

    # Tractive-system integrity
    PRECHARGE_TIMEOUT = auto()
    IMD = auto()
    AIR_POSITIVE_WELD = auto()
    AIR_NEGATIVE_WELD = auto()
    SHUTDOWN_TIMEOUT = auto()


# Kept beside the codes so the mapping cannot drift out of date.
FAULT_RULES: dict[Fault, str] = {
    Fault.CELL_OVERVOLT: "EV.7.4.2",
    Fault.CELL_UNDERVOLT: "EV.7.4.2",
    Fault.SENSE_FUSE_BLOWN: "EV.7.3.4 b",
    Fault.CELL_OVERTEMP: "EV.7.5.2",
    Fault.CELL_UNDERTEMP: "EV.7.5.2",
    Fault.SENSE_TIMEOUT: "EV.7.3.4 d",
    Fault.BMS_INTERNAL: "EV.7.3.4 e",
    Fault.OVERCURRENT_DISCHARGE: "cell datasheet",
    Fault.OVERCURRENT_CHARGE: "cell datasheet",
    Fault.PRECHARGE_TIMEOUT: "EV.5.6",
    Fault.IMD: "EV.7.6",
    Fault.AIR_POSITIVE_WELD: "EV.5.4.2",
    Fault.AIR_NEGATIVE_WELD: "EV.5.4.2",
    Fault.SHUTDOWN_TIMEOUT: "EV.7.2.2 c",
}

CHANNEL_NONE = -1  # fault is not tied to a specific sensor channel


@dataclass
class FirstFault:
    """The first fault of a latch cycle. One failure cascades into others within
    milliseconds, so without this the causal code is lost among its consequences."""

    code: Fault
    measured: float          # the offending reading, in that code's unit
    threshold: float         # the effective limit it violated
    timestamp_ms: int
    state: State             # state the machine was in when it tripped
    channel: int = CHANNEL_NONE

    def describe(self) -> str:
        where = "" if self.channel == CHANNEL_NONE else f" ch{self.channel}"
        rule = FAULT_RULES.get(self.code, "")
        return (
            f"{self.code.name}{where} measured={self.measured:g} "
            f"limit={self.threshold:g} in {self.state} at {self.timestamp_ms} ms [{rule}]"
        )
