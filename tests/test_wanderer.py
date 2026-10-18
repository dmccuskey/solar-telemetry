"""Parsing the Wanderer's registers, against frames recorded from the controller."""
import pytest

from solar_telemetry.modbus import parse_read_reply
from solar_telemetry.wanderer import (
    DEVICE_ID,
    DYNAMIC,
    MODEL,
    parse_dynamic,
    parse_model,
    parse_reading,
    temperature_f,
)
from test_fixtures import load_frames

# What renogy-bt's RoverClient parsed from the same dynamic_35 frame (2026-10-17),
# for the fields the poller stored.
RENOGY_BT_FIELDS = {
    "battery_percentage": 93,
    "battery_voltage": 13.0,
    "battery_current": 0.0,
    "controller_temperature": 68.0,
    "battery_temperature": 32.0,
    "load_voltage": 13.0,
    "load_current": 0.09,
    "load_power": 1,
    "pv_voltage": 0.0,
    "pv_current": 0.0,
    "pv_power": 0,
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


def dynamic_words() -> tuple[int, ...]:
    return recorded_words("dynamic_35", *DYNAMIC)


def test_model():
    assert parse_model(model_words()) == "RNG-CTRL-WND10"


def test_blank_model_is_unknown():
    assert parse_model((0x2020,) * 8) == "unknown"


def test_fields_match_renogy_bt():
    _, fields = parse_dynamic(dynamic_words())
    for name, value in RENOGY_BT_FIELDS.items():
        assert fields[name] == value, name
        assert type(fields[name]) is type(value), name


def test_derived_fields():
    _, fields = parse_dynamic(dynamic_words())
    assert fields["battery_power_w"] == 13.0 * 0.0
    assert fields["battery_power_abs_w"] == 0.0
    assert fields["pv_power_calc_w"] == 0.0
    assert fields["load_power_calc_w"] == 13.0 * 0.09


def test_faults_read_zero():
    _, fields = parse_dynamic(dynamic_words())
    assert fields["faults"] == 0


def test_faults_join_the_two_words_high_first():
    words = list(dynamic_words())
    words[0x0121 - DYNAMIC[0]] = 0x1000  # B28, PV reversed
    words[0x0122 - DYNAMIC[0]] = 0x0001
    _, fields = parse_dynamic(tuple(words))
    assert fields["faults"] == 0x1000_0001


def test_charging_state():
    state, _ = parse_dynamic(dynamic_words())
    assert state == "deactivated"


@pytest.mark.parametrize("raw, state", [(0x0002, "mppt"), (0x6405, "floating"), (0x8004, "boost"), (0x0007, "unknown")])
def test_charging_state_is_the_low_byte(raw, state):
    words = list(dynamic_words())
    words[0x0120 - DYNAMIC[0]] = raw
    assert parse_dynamic(tuple(words))[0] == state


@pytest.mark.parametrize("byte, fahrenheit", [(0, 32.0), (20, 68.0), (0x80 | 10, 14.0), (0x7F, 260.6)])
def test_temperature_is_sign_and_magnitude(byte, fahrenheit):
    assert temperature_f(byte) == pytest.approx(fahrenheit)


def test_scaling_rounds_like_renogy_bt():
    words = list(dynamic_words())
    words[0x0101 - DYNAMIC[0]] = 137  # 137 * 0.1 is 13.700000000000001
    _, fields = parse_dynamic(tuple(words))
    assert fields["battery_voltage"] == 13.7


def test_wrong_number_of_words():
    with pytest.raises(ValueError):
        parse_dynamic(dynamic_words()[:34])


def test_reading_has_the_poller_tags():
    reading = parse_reading(model_words(), dynamic_words(), "BT-TH-TEST")
    assert reading.tags == {"device": "BT-TH-TEST", "model": "RNG-CTRL-WND10", "status": "deactivated"}
    assert set(reading.fields) == set(RENOGY_BT_FIELDS) | {
        "battery_power_w", "battery_power_abs_w", "pv_power_calc_w", "load_power_calc_w", "faults",
    }
