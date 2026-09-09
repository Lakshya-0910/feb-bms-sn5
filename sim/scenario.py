"""Parser for .scn scenario files.

The format is deliberately small: timestamped commands, one per line, plus
ramps for anything that changes gradually.

    # comment
    @0      glv on
    @100    tsms on
    @2000   ramp cell_temp[0:4] 45 -> 62 over 5000
    @9000   brake on
    @9100   start press
    end     12000
"""

import re
from dataclasses import dataclass, field
from typing import Callable

Setter = Callable[["World", float], None]  # noqa: F821 - World lives in run.py


@dataclass
class Command:
    time_ms: int
    text: str
    apply: Callable[["World"], None]  # noqa: F821


@dataclass
class Ramp:
    start_ms: int
    end_ms: int
    text: str
    setter: Callable[["World", float], None]  # noqa: F821
    start_value: float
    end_value: float

    def value_at(self, t_ms: int) -> float:
        if t_ms <= self.start_ms:
            return self.start_value
        if t_ms >= self.end_ms:
            return self.end_value
        span = self.end_ms - self.start_ms
        return self.start_value + (self.end_value - self.start_value) * (
            (t_ms - self.start_ms) / span
        )


@dataclass
class Scenario:
    name: str
    description: str = ""
    commands: list[Command] = field(default_factory=list)
    ramps: list[Ramp] = field(default_factory=list)
    duration_ms: int = 10_000


_SWITCHES = {
    "glv": "glv_on",
    "tsms": "tsms_closed",
    "brake": "brake_pressed",
    "charger": "charger_connected",
    "shutdown_button": "shutdown_buttons_ok",
}

_ARRAYS = {"cell_v": "cell_volts", "cell_temp": "cell_temps"}

_SCALARS = {
    "current": "pack_current_amps",
    "intermediate": "intermediate_volts",
    "voltage_age": "voltage_age_ms",
    "temp_age": "temp_age_ms",
}

_FLAGS = {
    "sense_fuse": ("sense_fuses_ok", {"ok": True, "blown": False}),
    "imd": ("imd_ok", {"ok": True, "fault": False}),
    "self_test": ("self_test_ok", {"ok": True, "fail": False}),
    "air+": ("air_positive_closed", {"closed": True, "open": False}),
    "air-": ("air_negative_closed", {"closed": True, "open": False}),
}

_ON_OFF = {"on": True, "off": False, "closed": True, "open": False,
           "pressed": True, "released": False}


def _indices(target: str, size: int) -> tuple[str, range]:
    """Split cell_v[2] or cell_temp[0:4] into a field name and the rows it hits."""
    match = re.match(r"^(\w+)(?:\[(.*)\])?$", target)
    if not match:
        raise ValueError(f"cannot read target {target!r}")
    name, index = match.group(1), match.group(2)
    if index in (None, "all", ""):
        return name, range(size)
    if ":" in index:
        lo, hi = index.split(":")
        return name, range(int(lo or 0), int(hi or size))
    return name, range(int(index), int(index) + 1)


def _array_setter(target: str):
    def setter(world, value: float) -> None:
        field_name = _ARRAYS[_indices(target, 1)[0]]
        rows = getattr(world.snapshot, field_name)
        _, span = _indices(target, len(rows))
        for i in span:
            rows[i] = value
    return setter


def _parse_line(line: str, time_ms: int, scenario: Scenario) -> None:
    parts = line.split()
    head = parts[0]

    if head == "ramp":
        # ramp <target> <from> -> <to> over <ms>
        target, start, _, end, _, duration = parts[1:7]
        scenario.ramps.append(Ramp(
            time_ms, time_ms + int(duration), line,
            _array_setter(target), float(start), float(end),
        ))
        return

    if head in _SWITCHES:
        attr, value = _SWITCHES[head], _ON_OFF[parts[1]]
        scenario.commands.append(Command(
            time_ms, line, lambda w, a=attr, v=value: setattr(w.driver, a, v)))
        return

    if head == "start":
        value = parts[1] == "press"
        scenario.commands.append(Command(
            time_ms, line, lambda w, v=value: setattr(w.driver, "start_pressed", v)))
        return

    if head == "reset":
        value = parts[1] == "press"
        scenario.commands.append(Command(
            time_ms, line, lambda w, v=value: setattr(w.driver, "manual_reset", v)))
        return

    if head in _FLAGS:
        attr, options = _FLAGS[head]
        value = options[parts[1]]
        scenario.commands.append(Command(
            time_ms, line, lambda w, a=attr, v=value: setattr(w.snapshot, a, v)))
        return

    if head in _SCALARS:
        attr, value = _SCALARS[head], float(parts[1])
        scenario.commands.append(Command(
            time_ms, line,
            lambda w, a=attr, v=value: setattr(w.snapshot, a, type(getattr(w.snapshot, a))(v))))
        return

    if head == "plant":
        value = _ON_OFF[parts[1]]
        scenario.commands.append(Command(
            time_ms, line, lambda w, v=value: setattr(w, "plant_enabled", v)))
        return

    if head.split("[")[0] in _ARRAYS:
        setter, value = _array_setter(head), float(parts[1])
        scenario.commands.append(Command(
            time_ms, line, lambda w, s=setter, v=value: s(w, v)))
        return

    raise ValueError(f"unknown command {line!r}")


def parse(path: str) -> Scenario:
    scenario = Scenario(name=path.split("/")[-1].removesuffix(".scn"))

    with open(path) as handle:
        for raw in handle:
            line = raw.split("#")[0].strip()
            if not line:
                if raw.startswith("#") and not scenario.description:
                    scenario.description = raw.lstrip("# ").strip()
                continue

            if line.startswith("end"):
                scenario.duration_ms = int(line.split()[1])
                continue

            if not line.startswith("@"):
                raise ValueError(f"expected a timestamp: {line!r}")

            stamp, _, rest = line.partition(" ")
            _parse_line(rest.strip(), int(stamp[1:]), scenario)

    if scenario.commands or scenario.ramps:
        latest = max([c.time_ms for c in scenario.commands]
                     + [r.end_ms for r in scenario.ramps])
        scenario.duration_ms = max(scenario.duration_ms, latest + 1000)
    return scenario
