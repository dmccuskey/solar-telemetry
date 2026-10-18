"""Time a recovery step: how long until the controller can be read again.

Runs the step's commands with no settle wait, then tries `ble.find` every half
second until it works, then connects and reads once. Prints the time of each
part from the start of the step. Run it with the poller paused (dev.sh run):
it restarts or power-cycles Bluetooth on the Pi.

Usage: time_recovery.py <restart_bluez | power_cycle_adapter> <controller MAC address>
"""
import asyncio
import sys
import time

from solar_telemetry import ble, health, modbus, wanderer

STEPS = {
    "restart_bluez": [health.RESTART_BLUEZ],
    "power_cycle_adapter": [health.POWER_OFF, health.POWER_ON],
}
FIND_GIVE_UP = 60.0


async def main(step: str, address: str) -> int:
    start = time.monotonic()

    def mark(name: str) -> None:
        print(f"{name}: {time.monotonic() - start:.2f} s", flush=True)

    for command in STEPS[step]:
        if await health.run(*command) is None:
            print(f"failed: {' '.join(command)}")
            return 1
        mark(" ".join(command[2:]))

    while True:
        try:
            device = await ble.find(address)
            break
        except ble.BleError as e:
            if time.monotonic() - start > FIND_GIVE_UP:
                print(f"failed: no find after {FIND_GIVE_UP} s: {type(e).__name__}: {e}")
                return 1
            mark(f"find failed ({type(e).__name__})")
            await asyncio.sleep(0.5)
    mark("found")

    try:
        async with await ble.Connection.connect(device) as connection:
            mark("connected")
            words = await connection.read(wanderer.DEVICE_ID, *wanderer.DYNAMIC)
            mark("read")
    except (ble.BleError, modbus.ModbusError) as e:
        mark(f"failed: {type(e).__name__}: {e}")
        return 1
    mark("disconnected")
    print(f"battery_v: {wanderer.parse_dynamic(words)['battery_v']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in STEPS:
        sys.exit(__doc__.split("Usage: ")[1])
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
