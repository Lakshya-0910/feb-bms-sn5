"""Core vocabulary for the SN5 BMS: states, events and fault codes.

Rule references are to the Formula SAE Rules 2026 v1.0 (10 Sept 2025). Note the
2026 rules renamed the AMS to the BMS; older FSAE material still says AMS.

Units used across the project:
    voltage      volts       (float)
    temperature  degrees C   (float)
    current      amps        (float, positive = discharge, negative = charge)
    time         milliseconds (int, so timing is exact and reproducible)
"""

from dataclasses import dataclass
from enum import Enum, Flag, auto


class State(Enum):
    """The ten states of the machine, in roughly the order they are reached."""

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
    """Things that happen to the machine.

    Events are derived from the sensor snapshot by monitor.py and faults.py. The
    transition table matches on events only and never reads a raw sensor value,
    so adding a sensor means adding a derivation rather than editing state logic.
    """

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
    """Fault codes, as a bit flag so several can be active at once.

    Grouped by the five things EV.7.3.4 requires the BMS to monitor. Duties (b),
    (d) and (e) are the ones most implementations skip, and they are mandatory.
    """

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
    AIR_WELD = auto()
    SHUTDOWN_TIMEOUT = auto()


# Kept next to the codes rather than only in a document, so the mapping cannot
# drift out of date. Printed by the simulator and used to build the write-up's
# traceability table.
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
    Fault.AIR_WELD: "EV.5.4.2",
    Fault.SHUTDOWN_TIMEOUT: "EV.7.2.2 c",
}

CHANNEL_NONE = -1  # fault is not tied to a specific sensor channel


@dataclass
class FirstFault:
    """The first fault to latch in a latch cycle, kept until a manual reset.

    One failure cascades into others within milliseconds: a hot cell sags under
    load and trips undervoltage too, so by the time anyone reads the fault mask
    the causal code is indistinguishable from its consequences. This records
    only the first, and is broadcast on CAN so it survives the pack going dark.
    """

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
