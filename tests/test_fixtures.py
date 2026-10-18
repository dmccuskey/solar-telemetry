"""Checks that the recorded frames are what the other tests assume they are."""
import json
from pathlib import Path

FRAMES = Path(__file__).parent / "fixtures" / "wanderer_frames.jsonl"


def load_frames() -> dict[str, dict]:
    with FRAMES.open() as f:
        return {frame["name"]: frame for frame in map(json.loads, f)}


def test_read_replies_have_the_requested_length():
    for name, frame in load_frames().items():
        if name == "too_many_words":
            continue
        request = bytes.fromhex(frame["request"])
        reply = bytes.fromhex(frame["reply"])
        words = int.from_bytes(request[4:6], "big")
        assert reply[:3] == bytes([0xFF, 0x03, words * 2]), name
        assert len(reply) == 3 + words * 2 + 2, name


def test_too_many_words_got_the_module_console_not_modbus():
    reply = bytes.fromhex(load_frames()["too_many_words"]["reply"])
    assert reply.startswith(b"pylon_debug>")
