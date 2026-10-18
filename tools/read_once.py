"""Take one reading with the service's own client and parser, and print it.

Read-only: finds the controller, connects, reads the model and the live block,
disconnects. Prints how long each step took, the model, then the fields. A
failure prints the client's error type, which is what the service will act on.

With --write, also writes the reading and its `collector` point to the local
InfluxDB's `solar` database (created if missing), tagged with the controller's
name.

Usage: read_once.py <controller MAC address> [--write <controller name>]
"""
import asyncio
import logging
import sys
import time

from solar_telemetry import ble, health, influx, wanderer

ADDRESS = sys.argv[1]
CONTROLLER = sys.argv[3] if sys.argv[2:3] == ["--write"] else None


async def main() -> int:
    t = time.monotonic()
    timings = {}

    def step(name: str) -> None:
        nonlocal t
        timings[name] = time.monotonic() - t
        print(f"{name}: {timings[name]:.2f} s", flush=True)
        t = time.monotonic()

    try:
        read_at = int(time.time())
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
    if CONTROLLER:
        return await write(fields, timings, read_at)
    return 0


async def write(fields: wanderer.Fields, timings: dict[str, float], read_at: int) -> int:
    tags = {"controller": CONTROLLER}
    writer = influx.Writer()
    writer.add(influx.Point("charge_controller", tags, fields, read_at))
    collector = {
        "outcome": str(health.Outcome.OK),
        "find_s": round(timings["find"], 2),
        "connect_s": round(timings["connect"], 2),
        "read_s": round(timings["read"], 2),
        "recovery_step": 0,
        "queued": 0,
    }
    throttled = await health.throttled()
    if throttled is not None:
        collector["throttled"] = throttled
    writer.add(influx.Point("collector", tags, collector, read_at))
    for line in writer.queue:
        print(line)
    if not await writer.flush():
        print(f"write failed: {len(writer)} points still queued")
        return 1
    print(f"written to {writer.database}")
    return 0


logging.basicConfig(level=logging.INFO)
sys.exit(asyncio.run(main()))
