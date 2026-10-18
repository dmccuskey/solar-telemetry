"""Record raw Modbus request and reply frames from the controller, as test fixtures.

Read-only: sends function 03 (read registers) and nothing else. Prints one JSON
object per request, with every notification chunk as it arrived.

Usage: record_frames.py <controller MAC address> [device id, default 255]
"""
import asyncio
import json
import sys
import time

from bleak import BleakClient

from solar_telemetry.modbus import FrameError, crc16, read_request, reply_length

ADDRESS = sys.argv[1]
DEVICE_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 255

NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"
REPLY_TIMEOUT = 5.0  # seconds to wait for a complete reply
CHUNK_GAP = 0.5  # seconds without a new chunk that end an incomplete reply

# (name, start register, words)
READS = [
    ("system_info", 0x000A, 16),  # ratings, model, versions, serial number
    ("device_address", 0x001A, 1),
    ("dynamic_34", 0x0100, 34),  # what renogy-bt reads today
    ("dynamic_35", 0x0100, 35),  # adds 0x0122, the low word of the fault bits
    ("faults", 0x0121, 2),
    ("battery_type", 0xE004, 1),
    ("too_many_words", 0x0100, 0x7E),  # expect exception 3
    ("reserved_address", 0x0000, 1),  # expect exception 2, or zeros
]


def request(device_id: int, register: int, words: int) -> bytes:
    """A read request, built by hand for the over-long read that read_request refuses."""
    if words <= 125:
        return read_request(device_id, register, words)
    pdu = bytes([device_id, 0x03]) + register.to_bytes(2, "big") + words.to_bytes(2, "big")
    return pdu + crc16(pdu)


async def main():
    chunks: list[tuple[float, bytes]] = []
    arrived = asyncio.Event()

    def on_notify(_char, data: bytearray):
        chunks.append((time.monotonic(), bytes(data)))
        arrived.set()

    async with BleakClient(ADDRESS, timeout=20.0) as client:
        await client.start_notify(NOTIFY_CHAR_UUID, on_notify)
        for name, register, words in READS:
            chunks.clear()
            frame = request(DEVICE_ID, register, words)
            sent = time.monotonic()
            await client.write_gatt_char(WRITE_CHAR_UUID, frame, response=False)

            reply = b""
            while time.monotonic() - sent < REPLY_TIMEOUT:
                arrived.clear()
                try:
                    await asyncio.wait_for(arrived.wait(), CHUNK_GAP)
                except asyncio.TimeoutError:
                    if reply:
                        break
                    continue
                reply = b"".join(c for _, c in chunks)
                try:
                    n = reply_length(reply, DEVICE_ID)
                except FrameError:
                    n = None  # not Modbus: record it all, until the chunk gap
                if n is not None and len(reply) >= n:
                    break

            print(json.dumps({
                "name": name,
                "request": frame.hex(),
                "reply": reply.hex(),
                "chunks": [{"ms": round((t - sent) * 1000), "hex": c.hex()} for t, c in chunks],
                "crc_ok": len(reply) >= 4 and crc16(reply[:-2]) == reply[-2:],
            }), flush=True)
            await asyncio.sleep(0.5)


asyncio.run(main())
