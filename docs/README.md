# Documentation

New here? Start with the [Quick Start](../README.md#quick-start).

## Start

- [Installation](installation.md): the long-running service on a Raspberry Pi, under its own user, with its sudoers file and systemd unit

## Internals

- [Architecture](architecture.md): how the new long-running service works, its planned modules, its safety model, and the design history
- [ADR 001: Our Own BLE Client, Not renogy-bt](decisions/001-own-ble-client.md)
- [ADR 002: A Long-Running Service with In-Process Recovery](decisions/002-long-running-service.md)

## Project Structure

```text
README.md                   landing page and Quick Start
LICENSE
renogy_poll_to_influx.py    the current collector: one reading, written to InfluxDB
my_wanderer.ini.example     controller config to copy; my_wanderer.ini is gitignored
config.example.toml         the service's config to copy; config.toml is gitignored
solar_telemetry/            the service: ble, modbus, wanderer, health, influx, service
scripts/
├── reboot-the-world.sh     manual recovery for a wedged Bluetooth stack
└── network-watchdog.sh     optional: restarts Wi-Fi, reboots if the network stays down
systemd/
├── solar-telemetry.service the long-running service
├── solar-telemetry.sudoers the root commands its recovery may run, for /etc/sudoers.d
├── renogy-poll.service     runs the collector once (oneshot, under flock)
├── renogy-poll.timer       every 5 minutes
├── network-watchdog.service
└── network-watchdog.timer  every 5 minutes, from 5 minutes after boot
tools/
├── scan_ble.py             BLE scan, sorted by signal strength
└── gatt_dump.py            lists a device's GATT services: gatt_dump.py <MAC address>
docs/
├── README.md               this page
├── installation.md
├── architecture.md
└── decisions/              architecture decision records
```
