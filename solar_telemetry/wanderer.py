"""The Wanderer's register map, and parsing register values into a reading.

From Renogy's Modbus document for the Rover, which the Wanderer answers too.
Every field comes from one read of the 35 words at 0x0100. The controller's
"today" aggregates (0x010B-0x0114) are left out: they reset at an unknown time
and contradict each other, so daily figures come from our own readings and the
lifetime totals instead.

Field names carry their unit. The battery temperature is left out: no sensor
is fitted, and it reads 0.
"""

DEVICE_ID = 255  # the BT module answers here, though register 0x001A reads 1

# (first register, words) for each read
MODEL = (0x000C, 8)  # read once, for the log
DYNAMIC = (0x0100, 35)  # 0x0100 to 0x0122, through the fault bits

# The `charging_state` codes, for the log and the dashboard's value mapping
CHARGING_STATES = {
    0: "deactivated",
    1: "activated",
    2: "mppt",
    3: "equalizing",
    4: "boost",
    5: "floating",
    6: "current limiting",
}

Fields = dict[str, float | int | bool]


def parse_model(words: tuple[int, ...]) -> str:
    """The model name from the 8 words at 0x000C: 16 ASCII bytes, space-padded."""
    raw = b"".join(word.to_bytes(2, "big") for word in words)
    return raw.decode("ascii", errors="replace").strip(" \x00") or "unknown"


def temperature_c(byte: int) -> int:
    """Degrees C from a temperature byte: b7 the sign, b0-b6 the magnitude."""
    return -(byte & 0x7F) if byte & 0x80 else byte


def parse_dynamic(words: tuple[int, ...]) -> Fields:
    """The fields of a `charge_controller` point, from the 35 words at 0x0100."""
    if len(words) != DYNAMIC[1]:
        raise ValueError(f"{len(words)} words, {DYNAMIC[1]} expected")

    def word(register: int) -> int:
        return words[register - DYNAMIC[0]]

    def double(register: int) -> int:
        return word(register) << 16 | word(register + 1)

    return {
        "battery_v": word(0x0101) / 10,
        "battery_charge_a": word(0x0102) / 100,
        "battery_soc_pct": word(0x0100),
        "pv_v": word(0x0107) / 10,
        "pv_a": word(0x0108) / 100,
        "pv_w": word(0x0109),
        "load_v": word(0x0104) / 10,
        "load_a": word(0x0105) / 100,
        "load_w": word(0x0106),
        "load_on": bool(word(0x0120) & 0x8000),
        "controller_temp_c": temperature_c(word(0x0103) >> 8),
        "charging_state": word(0x0120) & 0xFF,
        "faults": double(0x0121),
        "pv_total_wh": double(0x011C),
        "load_total_wh": double(0x011E),
        "charge_total_ah": double(0x0118),
        "discharge_total_ah": double(0x011A),
        "operating_days": word(0x0115),
        "over_discharges": word(0x0116),
        "full_charges": word(0x0117),
    }
