"""Take one reading with the service's own client and parser, and print it.

Read-only: finds the controller, connects, reads the model and the live block,
disconnects. Prints how long each step took, then the tags and fields. A
failure prints the client's error type, which is what the service will act on.

Usage: read_once.py <controller MAC address> [device tag, default "test"]
"""
import asyncio
import logging
import sys
import time

from solar_telemetry import ble, wanderer

ADDRESS = sys.argv[1]
DEVICE = sys.argv[2] if len(sys.argv) > 2 else "test"


async def main() -> int:
    t = time.monotonic()

    def step(name: str) -> None:
        nonlocal t
        print(f"{name}: {time.monotonic() - t:.2f} s", flush=True)
        t = time.monotonic()

    try:
        device = await ble.find(ADDRESS)
        step("find")
        async with await ble.Connection.connect(device) as connection:
            step("connect")
            model = await connection.read(wanderer.DEVICE_ID, *wanderer.MODEL)
            dynamic = await connection.read(wanderer.DEVICE_ID, *wanderer.DYNAMIC)
            step("read")
        step("disconnect")
    except Exception as e:
        print(f"failed: {type(e).__name__}: {e}")
        return 1

    reading = wanderer.parse_reading(model, dynamic, DEVICE)
    print(reading.tags)
    for name, value in reading.fields.items():
        print(f"  {name} = {value}")
    return 0


logging.basicConfig(level=logging.INFO)
sys.exit(asyncio.run(main()))
