"""Parsing the Wanderer's registers, against frames recorded from the controller."""
import pytest

from solar_telemetry.modbus import parse_read_reply
from solar_telemetry.wanderer import (
    CHARGING_STATES,
    DEVICE_ID,
    DYNAMIC,
    MODEL,
    parse_dynamic,
    parse_model,
    temperature_c,
)
from test_fixtures import load_frames

# The dynamic_35 frame (2026-10-17 20:52, at night), decoded by hand from the
# register map. The scaled values agree with what renogy-bt parsed from it.
RECORDED_FIELDS = {
    "battery_v": 13.0,
    "battery_charge_a": 0.0,
    "battery_soc_pct": 93,
    "pv_v": 0.0,
    "pv_a": 0.0,
    "pv_w": 0,
    "load_v": 13.0,
    "load_a": 0.09,
    "load_w": 1,
    "load_on": True,
    "controller_temp_c": 20,
    "charging_state": 0,
    "faults": 0,
    "pv_total_wh": 2781,
    "load_total_wh": 8169,
    "charge_total_ah": 207,
    "discharge_total_ah": 645,
    "operating_days": 273,
    "over_discharges": 0,
    "full_charges": 0,
}


def recorded_words(name: str, register: int, words: int) -> tuple[int, ...]:
    reply = bytes.fromhex(load_frames()[name]["reply"])
    values = parse_read_reply(reply, DEVICE_ID, words)
    assert len(values) == words
    return values


def model_words() -> tuple[int, ...]:
    # system_info read 16 words from 0x000A; the model is 8 of them from 0x000C
    words = recorded_words("system_info", 0x000A, 16)
    return words[MODEL[0] - 0x000A:][:MODEL[1]]


def dynamic_words(**changes: int) -> tuple[int, ...]:
    """The recorded words, with registers changed: `r0x0120=0x8004`."""
    words = list(recorded_words("dynamic_35", *DYNAMIC))
    for name, value in changes.items():
        words[int(name[1:], 16) - DYNAMIC[0]] = value
    return tuple(words)


def test_model():
    assert parse_model(model_words()) == "RNG-CTRL-WND10"


def test_blank_model_is_unknown():
    assert parse_model((0x2020,) * 8) == "unknown"


def test_recorded_fields():
    fields = parse_dynamic(dynamic_words())
    assert fields == RECORDED_FIELDS
    for name, value in RECORDED_FIELDS.items():
        assert type(fields[name]) is type(value), name


def test_scaling_gives_the_nearest_decimal():
    fields = parse_dynamic(dynamic_words(r0x0101=137, r0x0105=7))  # 137 * 0.1 is 13.700000000000001
    assert fields["battery_v"] == 13.7
    assert fields["load_a"] == 0.07


def test_32_bit_registers_are_high_word_first():
    fields = parse_dynamic(dynamic_words(r0x0121=0x1000, r0x0122=0x0001, r0x011C=0x0002, r0x011D=0x0003))
    assert fields["faults"] == 0x1000_0001  # B28, PV reversed, and B0
    assert fields["pv_total_wh"] == 0x0002_0003


@pytest.mark.parametrize("raw, state, load_on", [
    (0x8000, 0, True),
    (0x6405, 5, False),  # load off at 100 % brightness, floating
    (0xE404, 4, True),
    (0x0007, 7, False),  # not in the document: stored as it is
])
def test_register_0x0120_splits_into_load_and_state(raw, state, load_on):
    fields = parse_dynamic(dynamic_words(r0x0120=raw))
    assert fields["charging_state"] == state
    assert fields["load_on"] is load_on


def test_charging_state_names():
    assert CHARGING_STATES[parse_dynamic(dynamic_words())["charging_state"]] == "deactivated"


@pytest.mark.parametrize("byte, celsius", [(0, 0), (20, 20), (0x80 | 10, -10), (0x7F, 127), (0x80, 0)])
def test_temperature_is_sign_and_magnitude(byte, celsius):
    assert temperature_c(byte) == celsius


def test_controller_temperature_is_the_high_byte():
    assert parse_dynamic(dynamic_words(r0x0103=0x8A14))["controller_temp_c"] == -10


def test_wrong_number_of_words():
    with pytest.raises(ValueError):
        parse_dynamic(dynamic_words()[:34])
