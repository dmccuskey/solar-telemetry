"""The Modbus framing, against frames recorded from the controller and the worked
examples in Renogy's protocol document (controller address 01h)."""
import pytest

from solar_telemetry.modbus import (
    ExceptionReply,
    FrameError,
    crc16,
    parse_read_reply,
    read_request,
    reply_length,
)
from test_fixtures import load_frames

# (fixture name, start register, words), as tools/record_frames.py read them
RECORDED_READS = [
    ("system_info", 0x000A, 16),
    ("device_address", 0x001A, 1),
    ("dynamic_34", 0x0100, 34),
    ("dynamic_35", 0x0100, 35),
    ("faults", 0x0121, 2),
    ("battery_type", 0xE004, 1),
    ("reserved_address", 0x0000, 1),
]

# (section, register, words, request, reply) from the document, where request,
# reply and CRCs agree. Left out: 3.4 and 3.13, whose request CRCs are wrong;
# 3.5, 3.7 and 3.10, whose replies have fewer words than requested; 3.9, whose
# reply has no header.
DOCUMENT_READS = [
    ("3.1", 0x000A, 1, "01 03 000A 0001 A408", "01 03 02 181E 324C"),
    ("3.2", 0x000C, 8, "01 03 000C 0008 840F", "01 03 10 2020 2020 4D54 3438 3330 2020 2020 2020 EE98"),
    ("3.3", 0x0014, 4, "01 03 0014 0004 040D", "01 03 08 0003 0201 0001 0203 8A54"),
    ("3.6", 0x0101, 1, "01 03 0101 0001 D436", "01 03 02 007B F867"),
    ("3.8", 0x0104, 3, "01 03 0104 0003 45F6", "01 03 06 0078 00C8 00F0 00C5"),
    ("3.11", 0x0115, 3, "01 03 0115 0003 15F3", "01 03 06 0008 0001 0006 1176"),
    ("3.12", 0x0118, 4, "01 03 0118 0004 C5F2", "01 03 08 0001 0203 0000 0108 C0A3"),
]


def frame(hex_with_spaces: str) -> bytes:
    return bytes.fromhex(hex_with_spaces.replace(" ", ""))


def with_crc(body: bytes) -> bytes:
    return body + crc16(body)


@pytest.fixture(scope="module")
def recorded():
    return load_frames()


@pytest.mark.parametrize("name, register, words", RECORDED_READS)
def test_requests_match_the_recorded_ones(recorded, name, register, words):
    assert read_request(255, register, words).hex() == recorded[name]["request"]


@pytest.mark.parametrize("section, register, words, request_hex, reply_hex", DOCUMENT_READS)
def test_requests_match_the_document(section, register, words, request_hex, reply_hex):
    assert read_request(1, register, words) == frame(request_hex)


@pytest.mark.parametrize("name, register, words", RECORDED_READS)
def test_recorded_replies_parse(recorded, name, register, words):
    values = parse_read_reply(bytes.fromhex(recorded[name]["reply"]), 255, words)
    assert len(values) == words


@pytest.mark.parametrize("section, register, words, request_hex, reply_hex", DOCUMENT_READS)
def test_document_replies_parse(section, register, words, request_hex, reply_hex):
    values = parse_read_reply(frame(reply_hex), 1, words)
    assert values == tuple(int.from_bytes(frame(reply_hex)[3 + 2 * i:5 + 2 * i], "big") for i in range(words))


def test_reply_values():
    reply = load_frames()["dynamic_35"]["reply"]
    values = parse_read_reply(bytes.fromhex(reply), 255, 35)
    assert values[0] == 93  # SOC, %
    assert values[1] == 130  # battery voltage ×0.1 V
    assert values[0x21:] == (0, 0)  # the fault bits
    assert parse_read_reply(frame("01 03 02 181E 324C"), 1, 1) == (0x181E,)


@pytest.mark.parametrize("words", [0, 126])
def test_request_word_count_is_1_to_125(words):
    with pytest.raises(ValueError):
        read_request(255, 0x0100, words)


@pytest.mark.parametrize("device_id", [0, 256])
def test_request_device_id_is_1_to_255(device_id):
    with pytest.raises(ValueError):
        read_request(device_id, 0x0100, 1)


@pytest.mark.parametrize("register, words", [(-1, 1), (0xFFFF, 2), (0x10000, 1)])
def test_request_registers_stay_in_range(register, words):
    with pytest.raises(ValueError):
        read_request(255, register, words)


def test_last_register_can_be_read():
    assert read_request(255, 0xFFFF, 1)[2:4] == b"\xff\xff"


def test_module_console_is_not_modbus(recorded):
    reply = bytes.fromhex(recorded["too_many_words"]["reply"])
    with pytest.raises(FrameError, match="address"):
        reply_length(reply, 255)
    with pytest.raises(FrameError, match="address"):
        parse_read_reply(reply, 255, 125)


@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_exception_replies(code):
    with pytest.raises(ExceptionReply) as raised:
        parse_read_reply(with_crc(bytes([255, 0x83, code])), 255, 35)
    assert raised.value.code == code


def test_exception_reply_with_bad_crc_is_a_frame_error():
    with pytest.raises(FrameError, match="CRC"):
        parse_read_reply(bytes([255, 0x83, 2, 0, 0]), 255, 1)


def test_bad_crc(recorded):
    reply = bytearray.fromhex(recorded["dynamic_35"]["reply"])
    reply[5] ^= 0x01
    with pytest.raises(FrameError, match="CRC"):
        parse_read_reply(bytes(reply), 255, 35)


def test_wrong_device_id():
    with pytest.raises(FrameError, match="address"):
        parse_read_reply(frame("01 03 02 181E 324C"), 255, 1)


def test_wrong_function_code():
    with pytest.raises(FrameError, match="function"):
        parse_read_reply(with_crc(bytes([255, 0x06, 0x01, 0x0A, 0x00, 0x01])), 255, 1)


def test_fewer_words_than_requested():
    # The document's 3.5: two words requested, one in the reply.
    with pytest.raises(FrameError, match="data bytes"):
        parse_read_reply(frame("01 03 02 0064 B9AF"), 1, 2)


def test_truncated_reply(recorded):
    reply = bytes.fromhex(recorded["dynamic_35"]["reply"])
    for n in (0, 1, 2, 3, len(reply) - 1):
        with pytest.raises(FrameError, match="short"):
            parse_read_reply(reply[:n], 255, 35)


def test_trailing_bytes(recorded):
    reply = bytes.fromhex(recorded["dynamic_35"]["reply"])
    with pytest.raises(FrameError, match="expected"):
        parse_read_reply(reply + b"\x00", 255, 35)


def test_reply_length_while_chunks_arrive(recorded):
    reply = bytes.fromhex(recorded["dynamic_35"]["reply"])
    assert reply_length(b"", 255) is None
    assert reply_length(reply[:2], 255) is None
    assert reply_length(reply[:3], 255) == len(reply) == 75
    assert reply_length(bytes([255, 0x83]), 255) == 5


def test_reply_length_rejects_a_wrong_header_early():
    with pytest.raises(FrameError, match="address"):
        reply_length(b"p", 255)
    with pytest.raises(FrameError, match="function"):
        reply_length(bytes([255, 0x10]), 255)
