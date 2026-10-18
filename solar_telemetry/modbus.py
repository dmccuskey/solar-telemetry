"""Modbus RTU framing for reading registers: build read requests, check replies.

Only function 03h (read holding registers). The service never writes to the
controller. The checks are strict because the BT module can answer with things
that aren't Modbus at all: an over-long read gets its ASCII debug console back.
"""

READ_REGISTERS = 0x03
EXCEPTION_FLAG = 0x80
MAX_READ_WORDS = 125

EXCEPTION_NAMES = {
    1: "function not supported",
    2: "bad register address or address + length",
    3: "bad value (too many words)",
    4: "device failure",
}


class ModbusError(Exception):
    """A read that didn't give register values."""


class FrameError(ModbusError):
    """The reply isn't a valid Modbus reply to the request: wrong address,
    function code or length, bad CRC, or not Modbus at all."""


class ExceptionReply(ModbusError):
    """The controller understood the request and refused it."""

    def __init__(self, code: int):
        self.code = code
        super().__init__(f"exception {code}: {EXCEPTION_NAMES.get(code, 'unknown')}")


def crc16(data: bytes) -> bytes:
    """Modbus CRC16, low byte first, as it goes on the wire."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")


def read_request(device_id: int, register: int, words: int) -> bytes:
    """The frame that reads `words` registers from `register` on: function 03h."""
    if not 1 <= device_id <= 255:
        raise ValueError(f"device id {device_id} is not 1 to 255")
    if not 1 <= words <= MAX_READ_WORDS:
        raise ValueError(f"{words} words is not 1 to {MAX_READ_WORDS}")
    if not (0 <= register and register + words <= 0x10000):
        raise ValueError(f"registers {register:#x} + {words} are outside 0x0000-0xFFFF")
    pdu = bytes([device_id, READ_REGISTERS]) + register.to_bytes(2, "big") + words.to_bytes(2, "big")
    return pdu + crc16(pdu)


def reply_length(buf: bytes, device_id: int) -> int | None:
    """The full length of the reply that `buf` starts, or None while the header
    is incomplete. For joining notification chunks.

    Raises FrameError as soon as the header shows the reply isn't Modbus from
    `device_id`, so the caller needn't wait out a timeout.
    """
    if len(buf) >= 1 and buf[0] != device_id:
        raise FrameError(f"reply from address {buf[0]}, not {device_id}")
    if len(buf) < 2:
        return None
    if buf[1] == READ_REGISTERS | EXCEPTION_FLAG:
        return 5  # address, error code, exception code, CRC
    if buf[1] != READ_REGISTERS:
        raise FrameError(f"function code {buf[1]:#04x} in the reply, not {READ_REGISTERS:#04x}")
    if len(buf) < 3:
        return None
    return 3 + buf[2] + 2


def parse_read_reply(reply: bytes, device_id: int, words: int) -> tuple[int, ...]:
    """The register values from the reply to a read of `words` registers from
    `device_id`. Raises ExceptionReply if the controller refused the read, and
    FrameError if the reply isn't a valid answer to it."""
    expected = reply_length(reply, device_id)
    if expected is None or len(reply) < expected:
        raise FrameError(f"reply is {len(reply)} bytes, too short")
    if len(reply) > expected:
        raise FrameError(f"reply is {len(reply)} bytes, {expected} expected")
    if crc16(reply[:-2]) != reply[-2:]:
        raise FrameError(f"bad CRC {reply[-2:].hex()}, {crc16(reply[:-2]).hex()} expected")
    if reply[1] & EXCEPTION_FLAG:
        raise ExceptionReply(reply[2])
    if reply[2] != words * 2:
        raise FrameError(f"reply has {reply[2]} data bytes, {words * 2} expected")
    data = reply[3:-2]
    return tuple(int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2))
