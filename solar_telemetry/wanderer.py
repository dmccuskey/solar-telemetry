"""The Wanderer's register map, and parsing register values into a reading.

From Renogy's Modbus document for the Rover, which the Wanderer answers too.
Only instantaneous values are stored: the controller's "today" aggregates
(0x010B-0x0114) reset at an unknown time and contradict each other, so daily
figures come from our own readings instead.

The fields and tags are the ones the timer-based poller wrote, so the Grafana
dashboard keeps working: temperatures in degrees F, values rounded as
renogy-bt rounds them, and the same derived powers. `faults` is new.
"""
from dataclasses import dataclass

DEVICE_ID = 255  # the BT module answers here, though register 0x001A reads 1

# (first register, words) for each read
MODEL = (0x000C, 8)
DYNAMIC = (0x0100, 35)  # 0x0100 to 0x0122, through the fault bits

CHARGING_STATES = {
    0: "deactivated",
    1: "activated",
    2: "mppt",
    3: "equalizing",
    4: "boost",
    5: "floating",
    6: "current limiting",
}


@dataclass(frozen=True)
class Reading:
    tags: dict[str, str]
    fields: dict[str, float | int]


def parse_model(words: tuple[int, ...]) -> str:
    """The model name from the 8 words at 0x000C: 16 ASCII bytes, space-padded."""
    raw = b"".join(word.to_bytes(2, "big") for word in words)
    return raw.decode("ascii", errors="replace").strip(" \x00") or "unknown"


def temperature_f(byte: int) -> float:
    """Degrees F from a temperature byte: b7 the sign, b0-b6 the degrees C."""
    celsius = -(byte & 0x7F) if byte & 0x80 else byte
    return celsius * 9 / 5 + 32


def parse_dynamic(words: tuple[int, ...]) -> tuple[str, dict[str, float | int]]:
    """The charging state and the fields from the 35 words at 0x0100."""
    if len(words) != DYNAMIC[1]:
        raise ValueError(f"{len(words)} words, {DYNAMIC[1]} expected")

    def word(register: int) -> int:
        return words[register - DYNAMIC[0]]

    def scaled(register: int, scale: float) -> float:
        return round(word(register) * scale, 2)

    fields: dict[str, float | int] = {
        "battery_percentage": word(0x0100),
        "battery_voltage": scaled(0x0101, 0.1),
        "battery_current": scaled(0x0102, 0.01),
        "controller_temperature": temperature_f(word(0x0103) >> 8),
        "battery_temperature": temperature_f(word(0x0103) & 0xFF),
        "load_voltage": scaled(0x0104, 0.1),
        "load_current": scaled(0x0105, 0.01),
        "load_power": word(0x0106),
        "pv_voltage": scaled(0x0107, 0.1),
        "pv_current": scaled(0x0108, 0.01),
        "pv_power": word(0x0109),
        "faults": word(0x0121) << 16 | word(0x0122),
    }
    fields["battery_power_w"] = fields["battery_voltage"] * fields["battery_current"]
    fields["battery_power_abs_w"] = abs(fields["battery_power_w"])
    fields["pv_power_calc_w"] = fields["pv_voltage"] * fields["pv_current"]
    fields["load_power_calc_w"] = fields["load_voltage"] * fields["load_current"]

    state = CHARGING_STATES.get(word(0x0120) & 0xFF, "unknown")
    return state, fields


def parse_reading(model_words: tuple[int, ...], dynamic_words: tuple[int, ...], device: str) -> Reading:
    """A reading from the two reads. `device` is the BT module's name, the
    `device` tag the poller wrote."""
    state, fields = parse_dynamic(dynamic_words)
    tags = {"device": device, "model": parse_model(model_words), "status": state}
    return Reading(tags=tags, fields=fields)
