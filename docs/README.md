# Documentation

New here? Start with the [Quick Start](../README.md#quick-start).

## Start

- [Installation](installation.md): preparing the Pi (Wi-Fi, watchdogs, power), then the full setup, under its own user, with its sudoers file, Bluetooth drop-in and systemd unit; the Grafana dashboard; the optional weather job; switching from the earlier timer, and updating

## Use

- [Operations](operations.md): running the service, its logs, how it recovers, InfluxDB outages, and what to check when it stops reading
- [Data](data.md): the `charge_controller` and `collector` measurements, the weather job's `weather` and `weather_daily`, every field, and example queries
- [Hardware](hardware.md): the controller's BT module and its GATT layout, checking the Bluetooth signal, and the Pi's power supply

## Internals

- [Architecture](architecture.md): how the service works inside: its modules, each cycle, the safety model, the tools, and the design history
- [ADR 001: Our Own BLE Client, Not renogy-bt](decisions/001-own-ble-client.md)
- [ADR 002: A Long-Running Service with In-Process Recovery](decisions/002-long-running-service.md)
- [ADR 003: bluetoothd Without Its GAP Plugin](decisions/003-no-gap-plugin.md)
- [ADR 004: InfluxDB Beside the Collector, Grafana Best on the Same Machine](decisions/004-one-machine.md)
- [ADR 005: Weather from Open-Meteo, in a Job of Its Own](decisions/005-weather-from-open-meteo.md)

## Project Structure

```text
README.md                   landing page and Quick Start
LICENSE
pyproject.toml              the package, its one dependency (bleak), and the solar-telemetry and solar-telemetry-weather commands
config.example.toml         the service's config to copy; config.toml is gitignored
solar_telemetry/            the service: ble, modbus, wanderer, health, influx, service; and the weather job, weather
tests/                      pytest, no radio needed
└── fixtures/               frames recorded from a real controller
systemd/
├── solar-telemetry.service the service's unit
├── solar-telemetry.sudoers the root commands its recovery may run, for /etc/sudoers.d
├── solar-telemetry-weather.service  optional: the weather job's unit
├── bluetooth-noplugin-gap.conf  a drop-in for bluetooth.service: bluetoothd without its GAP plugin, for fast connects
├── network-watchdog.service  optional: restarts Wi-Fi, reboots if the network stays down
├── network-watchdog.timer  every 5 minutes, from 5 minutes after boot
├── renogy-poll.service     the earlier collector, run once (oneshot, under flock)
└── renogy-poll.timer       every 5 minutes
grafana/
├── datasource.yaml         the solar data source, for Grafana's provisioning folder
└── solar.json              the dashboard, to import
scripts/
├── dev.sh                  development loop: test here, deploy to the Pi, run there, read the logs
├── network-watchdog.sh     run by network-watchdog.service
└── reboot-the-world.sh     the earlier collector's manual recovery for a wedged Bluetooth stack
tools/                      read_once, record_frames, time_recovery, scan_ble, gatt_dump: see Architecture
renogy_poll_to_influx.py    the earlier collector: one reading through renogy-bt, written to InfluxDB
my_wanderer.ini.example     the earlier collector's config; my_wanderer.ini is gitignored
docs/
├── README.md               this page
├── installation.md
├── operations.md
├── data.md
├── hardware.md
├── architecture.md
└── decisions/              architecture decision records
```

The earlier collector's files (`renogy_poll_to_influx.py`, `my_wanderer.ini.example`, the `renogy-poll` units and `reboot-the-world.sh`) go once the service has run for a while.
