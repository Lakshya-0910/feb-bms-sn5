"""A stand-in CAN layer: real message layout, no real hardware.

Bytes are packed by hand rather than with struct so the layout, byte order and
scaling of each signal stay visible. Payloads are 8 bytes, as classical CAN.

Ninety-six cells do not fit in one frame, so cell data is multiplexed: each
frame carries a block index and the readings for that block.
"""

from dataclasses import dataclass, field

from .config import PackConfig
from .monitor import PackSummary
from .types import Fault, State

# --- message ids ---------------------------------------------------------
BMS_STATUS = 0x180
BMS_PACK_SUMMARY = 0x181
BMS_CELL_VOLTS = 0x182
BMS_CELL_TEMPS = 0x183
BMS_FAULTS = 0x184
CHARGER_CONTROL = 0x300

CHARGER_STATUS = 0x200   # received
DASH_COMMAND = 0x201     # received

PERIODS_MS = {
    BMS_STATUS: 10,          # 100 Hz, the heartbeat other nodes watch
    BMS_PACK_SUMMARY: 100,
    BMS_CELL_VOLTS: 100,
    BMS_CELL_TEMPS: 100,
    BMS_FAULTS: 1000,        # also sent immediately whenever the mask changes
    CHARGER_CONTROL: 100,
}

VOLTS_PER_BIT = 0.1
AMPS_PER_BIT = 0.1
CELLS_PER_FRAME = 3      # 1 mux byte + 3 x uint16
TEMPS_PER_FRAME = 6      # 1 mux byte + 6 x int8


@dataclass(frozen=True)
class Frame:
    can_id: int
    data: bytes

    def __str__(self) -> str:
        return f"{self.can_id:03X}#{self.data.hex().upper()}"


class CanBus:
    """Records what was sent and hands back what was injected."""

    def __init__(self) -> None:
        self.sent: list[Frame] = []
        self._inbox: list[Frame] = []

    def send(self, frame: Frame) -> None:
        self.sent.append(frame)

    def inject(self, frame: Frame) -> None:
        self._inbox.append(frame)

    def drain(self) -> list[Frame]:
        received, self._inbox = self._inbox, []
        return received

    def frames(self, can_id: int) -> list[Frame]:
        return [f for f in self.sent if f.can_id == can_id]


@dataclass
class DashCommand:
    start_requested: bool = False
    reset_requested: bool = False


@dataclass
class ChargerStatus:
    present: bool = False
    volts: float = 0.0
    amps: float = 0.0


def _u16(value: int) -> bytes:
    return max(0, min(0xFFFF, value)).to_bytes(2, "little")


def _i16(value: int) -> bytes:
    return max(-32768, min(32767, value)).to_bytes(2, "little", signed=True)


def _i8(value: int) -> bytes:
    return max(-128, min(127, value)).to_bytes(1, "little", signed=True)


def _bits(*flags: bool) -> bytes:
    byte = 0
    for i, flag in enumerate(flags):
        if flag:
            byte |= 1 << i
    return bytes([byte])


class BmsCan:
    """Builds the outgoing frames and interprets the incoming ones."""

    def __init__(self, bus: CanBus, config: PackConfig) -> None:
        self.bus = bus
        self.config = config
        self._timers = {can_id: 0 for can_id in PERIODS_MS}
        self._volt_block = 0
        self._temp_block = 0
        self._last_mask = Fault.NONE
        # Counted rather than silently dropped: a dashboard repeatedly asking to
        # clear a latched fault is worth seeing in a log.
        self.rejected_resets = 0

    # --- transmit ---------------------------------------------------------

    def update(self, bms, snapshot, summary: PackSummary, dt_ms: int) -> list[Frame]:
        sent: list[Frame] = []

        fault_changed = bms.faults.active != self._last_mask
        self._last_mask = bms.faults.active

        for can_id in PERIODS_MS:
            self._timers[can_id] += dt_ms
            due = self._timers[can_id] >= PERIODS_MS[can_id]
            if can_id == BMS_FAULTS and fault_changed:
                due = True                      # never wait a second to report a fault
            if not due:
                continue
            self._timers[can_id] = 0
            frame = self._build(can_id, bms, snapshot, summary)
            if frame is not None:
                self.bus.send(frame)
                sent.append(frame)
        return sent

    def _build(self, can_id: int, bms, snapshot, summary: PackSummary) -> Frame | None:
        if can_id == BMS_STATUS:
            out = bms.outputs
            return Frame(can_id, bytes([list(State).index(bms.state)])
                         + _bits(out.shutdown_circuit_closed, out.air_positive_cmd,
                                 out.air_negative_cmd, out.precharge_relay_cmd,
                                 out.motors_enabled, out.rtds_active,
                                 out.bms_indicator, out.ts_status_indicator)
                         + bms.uptime_ms.to_bytes(4, "little", signed=False)[:4]
                         + bytes([1 if bms.faults.faulted else 0])
                         + bytes(1))

        if can_id == BMS_PACK_SUMMARY:
            return Frame(can_id,
                         _u16(round(summary.pack_volts / VOLTS_PER_BIT))
                         + _i16(round(summary.current_amps / AMPS_PER_BIT))
                         + _u16(round(summary.min_cell_volts * 1000))
                         + _u16(round(summary.max_cell_volts * 1000)))

        if can_id == BMS_CELL_VOLTS:
            block = self._volt_block
            start = block * CELLS_PER_FRAME
            cells = snapshot.cell_volts[start:start + CELLS_PER_FRAME]
            self._volt_block = (block + 1) % self._blocks(len(snapshot.cell_volts),
                                                          CELLS_PER_FRAME)
            payload = bytes([block]) + b"".join(_u16(round(v * 1000)) for v in cells)
            return Frame(can_id, payload.ljust(8, b"\x00"))

        if can_id == BMS_CELL_TEMPS:
            block = self._temp_block
            start = block * TEMPS_PER_FRAME
            temps = snapshot.cell_temps[start:start + TEMPS_PER_FRAME]
            self._temp_block = (block + 1) % self._blocks(len(snapshot.cell_temps),
                                                          TEMPS_PER_FRAME)
            payload = bytes([block]) + b"".join(_i8(round(t)) for t in temps)
            return Frame(can_id, payload.ljust(8, b"\x00"))

        if can_id == BMS_FAULTS:
            first = bms.faults.first
            payload = _u16(bms.faults.active.value)
            payload += _u16(first.code.value if first else 0)
            payload += bytes([first.channel & 0xFF if first and first.channel >= 0 else 0xFF])
            payload += _i16(round(first.measured * 10) if first else 0)
            return Frame(can_id, payload.ljust(8, b"\x00"))

        if can_id == CHARGER_CONTROL:
            # Only ask for current while actually charging; anything else is a
            # request to stop.
            charging = bms.state is State.CHARGING
            return Frame(can_id,
                         bytes([1 if charging else 0])
                         + _u16(round(self.config.charge_target_volts
                                      * self.config.series_modules / VOLTS_PER_BIT))
                         + _u16(round(self.config.max_charge_amps / AMPS_PER_BIT))
                         + bytes(3))
        return None

    @staticmethod
    def _blocks(count: int, per_frame: int) -> int:
        return max(1, (count + per_frame - 1) // per_frame)

    # --- receive ----------------------------------------------------------

    def receive(self) -> tuple[DashCommand, ChargerStatus]:
        """Parse inbound frames. A reset asked for over CAN is refused here.

        EV.7.2.3 b and c require a physical reset at the vehicle and forbid the
        driver re-arming from the cockpit, so this never reaches DriverInputs.
        """
        dash, charger = DashCommand(), ChargerStatus()
        for frame in self.bus.drain():
            if frame.can_id == DASH_COMMAND:
                flags = frame.data[0]
                dash.start_requested = bool(flags & 0b01)
                dash.reset_requested = bool(flags & 0b10)
                if dash.reset_requested:
                    self.rejected_resets += 1
            elif frame.can_id == CHARGER_STATUS:
                charger.present = bool(frame.data[0])
                charger.volts = int.from_bytes(frame.data[1:3], "little") * VOLTS_PER_BIT
                charger.amps = int.from_bytes(frame.data[3:5], "little") * AMPS_PER_BIT
        return dash, charger


def decode_pack_summary(frame: Frame) -> dict:
    """Inverse of the pack summary encoder, for tests and the trace viewer."""
    return {
        "pack_volts": int.from_bytes(frame.data[0:2], "little") * VOLTS_PER_BIT,
        "current_amps": int.from_bytes(frame.data[2:4], "little", signed=True) * AMPS_PER_BIT,
        "min_cell_volts": int.from_bytes(frame.data[4:6], "little") / 1000,
        "max_cell_volts": int.from_bytes(frame.data[6:8], "little") / 1000,
    }
