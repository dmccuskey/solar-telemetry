"""Take one reading with the service's own client and parser, and print it.

Read-only: finds the controller, connects, reads the model and the live block,
disconnects. Prints how long each step took, the model, then the fields. A
failure prints the client's error type, which is what the service will act on.

Usage: read_once.py <controller MAC address>
"""
import asyncio
import logging
import sys
import time

from solar_telemetry import ble, wanderer

ADDRESS = sys.argv[1]


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

    fields = wanderer.parse_dynamic(dynamic)
    state = wanderer.CHARGING_STATES.get(fields["charging_state"], "unknown")
    print(f"{wanderer.parse_model(model)}, {state}")
    for name, value in fields.items():
        print(f"  {name} = {value}")
    return 0


logging.basicConfig(level=logging.INFO)
sys.exit(asyncio.run(main()))
