# Architecture

How the collector works inside: one long-running Python process, run by systemd, with its own Bluetooth client. The reasons behind it are in the two [decisions](#decisions) below.

## Overview

```text
Renogy Wanderer ── BT-1 module ──BLE──▶ Raspberry Pi
                                         ├── solar-telemetry (systemd service)
                                         │     ble ▶ wanderer ▶ influx queue ▶ InfluxDB writer
                                         │       ╰─ health: classify, recover
                                         └── InfluxDB 1.x ◀── Grafana (another machine)
```

The service runs on a Raspberry Pi within Bluetooth range of the charge controller. Every 5 minutes it connects, reads the controller's registers, disconnects, and writes a point to a local InfluxDB. Grafana, on another machine, queries that InfluxDB.

## Structure

The `solar_telemetry` package, one asyncio process. Its only dependency is [bleak](https://github.com/hbldh/bleak), for Bluetooth LE over BlueZ.

| module | does |
|---|---|
| `ble` | the Bluetooth client: find the controller by MAC address (then by a scan, which also counts the other devices in range), connect, read registers over Modbus, disconnect. Each step fails with its own error type: `DeviceNotFound`, `BluezBusy`, `AdapterError`, `ConnectFailed`, `ReadTimeout` ([ADR 001](decisions/001-own-ble-client.md)) |
| `modbus` | building read requests (function 03h only) and checking replies: address, function, length, CRC16, exception replies |
| `wanderer` | the controller's register map: the model, and the 35 words from 0x0100 parsed into the fields of a `charge_controller` point |
| `health` | classifying a failure into an outcome, the recovery ladder, the recovery commands, and `vcgencmd get_throttled` ([ADR 002](decisions/002-long-running-service.md)) |
| `influx` | line protocol, the bounded queue, and writing it to InfluxDB 1.x over HTTP, with no client library |
| `service` | the main loop, the schedule, the config (TOML), and the systemd notifications and watchdog |

The tests (`tests/`, run with pytest) cover everything that needs no radio, against frames recorded from a real controller (`tests/fixtures/wanderer_frames.jsonl`) and fakes for bleak, the recovery commands and InfluxDB.

## Processing Flow

Each cycle:

1. **Read:** find the controller, connect, read the 35 registers from 0x0100 (and the model, once after a start), disconnect.
2. **On failure,** classify it ([outcomes](data.md#outcomes)). The car being away ends the cycle, and the next comes after the longer away interval. Any other failure takes the next step on the recovery ladder, then the service tries again at once: a failure costs seconds, not a cycle.
3. **Queue** a `collector` point for every attempt, and a `charge_controller` point for a good reading, each timestamped when the attempt began.
4. **Write** the queue to InfluxDB, oldest first. If the write fails, the points stay queued for the next cycle.
5. **Sleep** until the next cycle, pinging the systemd watchdog meanwhile. Cycles start 15 seconds after each mark of the interval on the clock (:00:15, :05:15, ..., or the quarter hours while the car is away), whatever time the service started, so a retry stays in its cycle's 5-minute block and Grafana sees one reading per block. The first cycle after a start runs at once.

Reading and writing never run at the same time: the queue is written only between readings. The steps of a cycle, and the sleep, each ping the watchdog, so a hang anywhere gets the service restarted.

## Safety Model

- **Read-only toward the controller.** The client only sends Modbus reads (function 03h). `modbus` builds no other request, so the service can't change charging settings or the load output.
- **Recovery is bounded.** Each rung of the ladder runs at most once per climb, with a wait after it. The last rung is exiting, not rebooting the Pi.
- **The car being away is not a failure.** No resets happen when the controller simply isn't in range.
- **Least privilege.** The service runs as its own unprivileged user. As root it can run only four commands, allowed by name in a sudoers file ([Installation](installation.md#4-allow-the-recovery-commands)).

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

The collector started as a systemd timer running a script, which ran [renogy-bt](https://github.com/cyrils/renogy-bt)'s `example.py` as a subprocess and parsed its output. Recovery was a Bluetooth power cycle inside the script, plus a manual `reboot-the-world.sh`. Running it that way for most of 2026 taught us which failures happen and which fixes work for each, and that the car being away has to be told apart from a broken Bluetooth stack. The service replaced it in October 2026; the earlier collector's files stay in this repository until it's retired.

## Decisions

- [ADR 001: Our Own BLE Client, Not renogy-bt](decisions/001-own-ble-client.md)
- [ADR 002: A Long-Running Service with In-Process Recovery](decisions/002-long-running-service.md)
