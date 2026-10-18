# Architecture

How the collector works inside. This describes the design being built: the scripts in this repository today are the earlier, timer-based collector it replaces (see [Design History](#design-history)).

## Overview

```text
Renogy Wanderer ── BT-1 module ──BLE──▶ Raspberry Pi
                                         ├── solar-telemetry (systemd service)
                                         │     BLE client ▶ parser ▶ queue ▶ writer
                                         └── InfluxDB 1.x ◀── Grafana (another machine)
```

The collector runs on a Raspberry Pi within Bluetooth range of the charge controller. Every few minutes it connects, reads the controller's registers, disconnects, and writes one point to a local InfluxDB. Grafana, on another machine, queries that InfluxDB.

## Structure

Planned modules:

| module | does |
|---|---|
| `ble` | the BLE client: find the device, connect, read a register range over Modbus, disconnect. Typed errors. ([ADR 001](decisions/001-own-ble-client.md)) |
| `modbus` | building read requests and checking replies (CRC16) |
| `wanderer` | the Wanderer's register map, and parsing the replies into a reading |
| `health` | classifying a failure (car away, BlueZ busy, connect failure, adapter dead, undervoltage) and the recovery ladder ([ADR 002](decisions/002-long-running-service.md)) |
| `influx` | the bounded queue, and writing points in line protocol |
| `service` | the main loop, the schedule, the systemd watchdog, configuration |

## Processing Flow

Each cycle:

1. **Read:** find the controller by MAC address, connect, read registers 12, 26, 256 and 57348, disconnect.
2. **On failure,** classify it and take the next step on the recovery ladder, or slow down if the car is away. Then wait for the next cycle.
3. **Parse** the registers into a reading, add the derived fields (battery, PV and load power), and put it in the queue with its timestamp.
4. **Write** everything in the queue to InfluxDB. If the write fails, the readings stay queued for the next cycle.
5. **Ping** the systemd watchdog.

## Safety Model

- **Read-only toward the controller.** The client only sends Modbus reads (function 3). It never writes registers, so it can't change charging settings or the load output.
- **Recovery is bounded.** Each step of the ladder runs at most once per climb, with a wait after it. The last step is exiting, not rebooting the Pi.
- **The car being away is not a failure.** No resets happen when the controller simply isn't in range.

## Design History

The collector started as a systemd timer running a script, which ran [renogy-bt](https://github.com/cyrils/renogy-bt)'s `example.py` as a subprocess and parsed its output. Recovery was a Bluetooth power cycle inside the script, plus a manual `reboot-the-world.sh`. Running it that way for most of 2026 taught us which failures happen and which fixes work for each, and that the car being away has to be told apart from a broken BLE stack. Those lessons are recorded in the two ADRs below.

## Decisions

- [ADR 001: Our Own BLE Client, Not renogy-bt](decisions/001-own-ble-client.md)
- [ADR 002: A Long-Running Service with In-Process Recovery](decisions/002-long-running-service.md)
