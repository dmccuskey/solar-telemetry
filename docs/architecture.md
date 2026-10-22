# Architecture

How the collector works inside: one long-running Python process, run by systemd, with its own Bluetooth client. The reasons behind it are in the [decisions](#decisions) below.

## Overview

```text
Renogy Wanderer ── BT-1 module ──BLE──▶ Raspberry Pi
                                         ├── solar-telemetry (systemd service)
                                         │     ble ▶ wanderer ▶ influx queue ▶ InfluxDB writer
                                         │       ╰─ health: classify, recover
                                         ├── solar-telemetry-weather (optional, its own service) ◀── Open-Meteo
                                         └── InfluxDB 1.x ◀── Grafana (here, or another machine)
```

The service runs on a Raspberry Pi within Bluetooth range of the charge controller. Every 5 minutes it connects, reads the controller's registers, disconnects, and writes a point to a local InfluxDB. Grafana, on the same machine or another, queries that InfluxDB ([ADR 004](decisions/004-one-machine.md)).

## Structure

The `solar_telemetry` package, one asyncio process. Its only dependency is [bleak](https://github.com/hbldh/bleak), for Bluetooth LE over BlueZ.

| module | does |
|---|---|
| `ble` | the Bluetooth client: find the controller by MAC address (then by a scan, which also counts the other devices in range), connect, read registers over Modbus, disconnect. Each step fails with its own error type: `DeviceNotFound`, `BluezBusy`, `AdapterError`, `ConnectFailed`, `ReadTimeout` ([ADR 001](decisions/001-own-ble-client.md)) |
| `modbus` | building read requests (function 03h only) and checking replies: address, function, length, CRC16, exception replies |
| `wanderer` | the controller's register map: the model, and the 35 words from 0x0100 parsed into the fields of a `charge_controller` point |
| `health` | classifying a failure into an outcome, the recovery ladder, the recovery commands, and `vcgencmd get_throttled` ([ADR 002](decisions/002-long-running-service.md), [ADR 006](decisions/006-reset-the-adapter.md)) |
| `influx` | line protocol, the bounded queue, and writing it to InfluxDB 1.x over HTTP, with no client library |
| `service` | the main loop, the schedule, the config (TOML), and the systemd notifications and watchdog |
| `weather` | the optional weather job, its own process: fetching Open-Meteo's sunlight and cloud cover for the config's location, and writing them through `influx` ([ADR 005](decisions/005-weather-from-open-meteo.md)) |

The tests (`tests/`, run with pytest) cover everything that needs no radio, against frames recorded from a real controller (`tests/fixtures/wanderer_frames.jsonl`) and fakes for bleak, the recovery commands and InfluxDB.

## Processing Flow

Each cycle:

1. **Read:** find the controller, connect, read the 35 registers from 0x0100 (and the model, once after a start), disconnect.
2. **On failure,** classify it ([outcomes](data.md#outcomes)). The car being away ends the cycle, and the next comes after the longer away interval. Any other failure takes the next step on the recovery ladder, then the service tries again at once: a failure costs seconds, not a cycle.
3. **Queue** a `collector` point for every attempt, and a `charge_controller` point for a good reading, each timestamped when the attempt began.
4. **Write** the queue to InfluxDB, oldest first. If the write fails, the points stay queued for the next cycle.
5. **Sleep** until the next cycle, pinging the systemd watchdog meanwhile. Cycles start 15 seconds after each mark of the interval on the clock (:00:15, :05:15, ..., or the away interval's marks while the car is away, when it's set longer), whatever time the service started, so a retry stays in its cycle's 5-minute block and Grafana sees one reading per block. The first cycle after a start runs at once.

Reading and writing never run at the same time: the queue is written only between readings. The steps of a cycle, and the sleep, each ping the watchdog, so a hang anywhere gets the service restarted.

## Safety Model

- **Read-only toward the controller.** The client only sends Modbus reads (function 03h). `modbus` builds no other request, so the service can't change charging settings or the load output.
- **Recovery is bounded.** Each rung of the ladder runs at most once per climb, with a wait after it. The last rung is exiting. The service never reboots the Pi itself: systemd does, when the service keeps exiting, at most once every 2 hours ([ADR 006](decisions/006-reset-the-adapter.md)).
- **The car being away is not a failure.** No resets happen when the controller simply isn't in range.
- **Least privilege.** The service runs as its own unprivileged user. As root it can run only five commands, allowed by name in a sudoers file ([Installation](installation.md#4-allow-the-recovery-commands)).

## Tools

Scripts in `tools/` for working on the client, run with the package installed:

| tool | does |
|---|---|
| `read_once.py` | one reading with the service's own client and parser, with each step's timing; `--write` also writes it to InfluxDB |
| `record_frames.py` | records request and reply frames from the controller, as test fixtures |
| `time_recovery.py` | times a recovery step (restarting BlueZ, or power-cycling the adapter) until the controller can be read again |
| `scan_ble.py` | a Bluetooth scan, sorted by signal strength |
| `gatt_dump.py` | lists a device's GATT services and characteristics |

## Design History

The collector started as a systemd timer running a script, which ran [renogy-bt](https://github.com/cyrils/renogy-bt)'s `example.py` as a subprocess and parsed its output. Running it that way for most of 2026 taught us which failures happen and which fixes work for each:

- **January 16:** the first readings, through `renogy-bt`, written to InfluxDB on the Pi and graphed by Grafana on another machine ([ADR 004](decisions/004-one-machine.md)). The script ran every 5 minutes from a timer.
- **January 16 to 17:** the first failures: reads that timed out until Bluetooth was reset. The script got retries, with a `bluetoothctl power off/on` between them.
- **January 18:** finding the controller by its MAC address first, instead of always scanning, cut a run from 27 s to 17 s. A local change to `renogy-bt`.
- **January 19:** `renogy-bt`'s disconnect raised `EOFError` during teardown, after readings in quick succession; another local change caught it. The run times were seen to alternate, about 20 s and 10 s, a pattern only explained in October ([ADR 003](decisions/003-no-gap-plugin.md)).
- **February 2:** `org.bluez.Error.InProgress`, a discovery BlueZ thought was still running. The power cycle didn't clear it; restarting `bluetooth.service` did.
- **April 25:** `reboot-the-world.sh`, the full manual recovery: stop the timer, kill leftover processes, restart `bluetooth` (and `dbus` with `--nuclear`), bounce `hci0`, poll once.
- **May 3:** less stable after the Pi was moved, so recovery should act sooner and harder. But the script couldn't restart the Bluetooth stack without risking its own run, and had one chance every 5 minutes.
- **May 8:** the controller being away and a broken Bluetooth stack gave the same errors. Telling them apart by whether a scan sees other devices, and a long-running service with its own recovery, were the ideas that became [ADR 002](decisions/002-long-running-service.md).
- **October 17:** the rewrite: the long-running service, with its own Bluetooth client ([ADR 001](decisions/001-own-ble-client.md)), replaced the timer. The earlier collector's files stay in this repository until it's retired.

## Decisions

- [ADR 001: Our Own BLE Client, Not renogy-bt](decisions/001-own-ble-client.md)
- [ADR 002: A Long-Running Service with In-Process Recovery](decisions/002-long-running-service.md)
- [ADR 003: bluetoothd Without Its GAP Plugin](decisions/003-no-gap-plugin.md)
- [ADR 004: InfluxDB Beside the Collector, Grafana Best on the Same Machine](decisions/004-one-machine.md)
- [ADR 005: Weather from Open-Meteo, in a Job of Its Own](decisions/005-weather-from-open-meteo.md)
