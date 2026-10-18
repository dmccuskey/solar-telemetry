# ADR 001: Our Own BLE Client, Not renogy-bt

**Status:** Accepted

## Context

The collector reads a Renogy Wanderer charge controller through its Bluetooth module (BT-1 type). Until now it has done that by running `example.py` from [renogy-bt](https://github.com/cyrils/renogy-bt) as a subprocess and parsing its printed output. The collector is moving to a long-running service ([ADR 002](002-long-running-service.md)), so it needs to talk to the controller from inside its own process. There were three ways to do that:

1. **Import `renogybt` as a library.**
2. **Write our own client** on [bleak](https://github.com/hbldh/bleak), from Renogy's published Modbus register map.
3. **Keep the subprocess** inside the service.

What we found in `renogy-bt`:

- **It's licensed GPL-3.0.** This project is MIT. Importing it would put the running collector under the GPL, and translating its code into ours would make ours GPL.
- **It's built to read once and exit.** `BaseClient.start()` owns the event loop and runs it until a future completes. Reads are driven by callbacks and timers, and the client `stop()`s itself when done. It's configured from an ini file. A long-running service needs the reverse: its own event loop, a reading it can `await`, and failures it can tell apart.
- **It needed local fixes that aren't upstream.** On our Pi, `disconnect()` raised `EOFError` during teardown and set an already-finished future. Discovery always ran a full scan, even when the MAC address was known. Neither is fixed upstream as of 2026-10, though the project is active.
- **A Wanderer needs very little of it.** The protocol is Modbus RTU over BLE: requests written to characteristic `ffd1` (service `ffd0`), replies as notifications on `fff1`, with a Modbus CRC16. Four reads cover everything we store: registers 12 (model), 26 (device address), 256 (the live readings, 34 words) and 57348 (battery type).

Keeping the subprocess (option 3) gives up what the service is for: each reading would still be a fresh process, and the service would still learn about failures by parsing text.

## Decision

The collector has its own BLE client, written on `bleak` from Renogy's Modbus register map and the BT module's GATT layout, not from `renogy-bt`'s code. It is MIT licensed, and the README credits `renogy-bt` as the project that showed how the module works.

- **API:** separate steps the service awaits: find the device (by MAC address first, with a scan only as a fallback), connect, read a register range, disconnect. Each read has a timeout.
- **Errors are typed,** so the service can choose a recovery step ([ADR 002](002-long-running-service.md)): device not found, connect failed, read timed out, BlueZ busy (`org.bluez.Error.InProgress`), bad CRC.
- **A connection per reading** by default: connect, read, disconnect. The controller accepts one BLE connection at a time, so this leaves room for the Renogy phone app between readings. Because connecting and disconnecting are separate steps, a persistent connection can be added later as a config option without changing the client.
- **Parsing** of the Wanderer's registers lives apart from the BLE transport, so other Renogy devices could be added without touching connection handling.

The two local fixes are offered upstream to `renogy-bt` as separate pull requests, independently of this project.

## Consequences

- We control connection handling and recovery, which is where nearly all the trouble has been.
- The service gets readings as Python values and errors as exceptions, not text to parse.
- No GPL dependency: the project stays MIT, and installation on the Pi needs only `bleak`.
- More code of our own to maintain and test, though for one device it's small.
- We support only the devices we write parsers for (the Wanderer, and probably the Rover and Adventurer, which share its register map). Other Renogy devices remain `renogy-bt`'s territory.
- Fixes and new device support in `renogy-bt` don't reach us automatically.
