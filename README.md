# solar-telemetry

Logs a Renogy solar charge controller's readings to InfluxDB from a Raspberry Pi, over Bluetooth LE.

A Raspberry Pi within Bluetooth range of the controller's BT module reads it every 5 minutes and writes the readings to InfluxDB 1.x, for graphing in Grafana. It was built to watch the solar charger that keeps a rarely driven car's 12 V battery healthy.

```text
wanderer,device=BT-TH-XXXXXXXX,model=RNG-CTRL-WND10,status=floating battery_voltage=13.6,battery_percentage=100,pv_voltage=19.2,pv_power=12,controller_temperature=18 1760731200000000000
```

> **Status:** this is the first, timer-based collector, as it has run since early 2026. It's being replaced by a long-running service with its own Bluetooth client and in-process recovery: see [Architecture](docs/architecture.md).

Not affiliated with, or endorsed by, Renogy.

## Features

- Reads a Renogy Wanderer (and controllers that share its registers, such as the Rover and Adventurer) through a BT-1 Bluetooth module
- Writes battery, solar and load voltage, current and power, temperatures and charging state to InfluxDB 1.x
- Adds derived fields: battery power (signed and absolute), and solar and load power calculated from voltage and current
- Runs every 5 minutes from a systemd timer, with a lock so runs never overlap and a hard timeout for a hung run
- Retries a failed read, and power-cycles the Bluetooth controller once when a read fails
- A recovery script for a wedged Bluetooth stack: [`reboot-the-world.sh`](scripts/reboot-the-world.sh)
- Diagnostic tools: a BLE scan with signal strength, and a GATT service dump
- An optional network watchdog that restarts Wi-Fi, and reboots the Pi if that doesn't bring the network back

## Quick Start

The following steps take about 30 minutes, on a Raspberry Pi running Raspberry Pi OS. You will have the controller's readings arriving in InfluxDB every 5 minutes.

You need a Pi with Bluetooth, within range of the controller (`bluetoothctl show`), Python 3.11 or later (`python3 --version`), git, and InfluxDB 1.x running on the Pi (`influx -version`). The paths below assume the user `pi`.

### 1. Get the code

```bash
cd ~
git clone https://github.com/dmccuskey/solar-telemetry.git
git clone https://github.com/cyrils/renogy-bt.git
cd renogy-bt
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The collector runs [renogy-bt](https://github.com/cyrils/renogy-bt)'s `example.py` to read the controller, from `~/renogy-bt`.

### 2. Configure the controller

Find the controller's BT module. Its name starts with `BT-TH`:

```bash
~/renogy-bt/.venv/bin/python3 ~/solar-telemetry/tools/scan_ble.py
```

```text
AA:BB:CC:DD:EE:FF  rssi=-71  name=BT-TH-XXXXXXXX
```

Copy the example config and set `mac_addr` and `alias` from that line:

```bash
cp ~/solar-telemetry/my_wanderer.ini.example ~/renogy-bt/my_wanderer.ini
nano ~/renogy-bt/my_wanderer.ini
```

### 3. Check that it reads

Create the database, then run one poll:

```bash
influx -execute 'CREATE DATABASE renogy'
python3 ~/solar-telemetry/renogy_poll_to_influx.py
```

It prints the readings and ends with `=== END RENOGY SCRIPT ===`. `Device not found` means the controller is out of range or the MAC address is wrong; the Renogy phone app being connected also blocks it, since the controller accepts one connection at a time.

The script power-cycles Bluetooth after a failed read, which needs `sudo` without a password for `bluetoothctl`. On Raspberry Pi OS the `pi` user has that already. Otherwise, add a sudoers rule: `pi ALL=(root) NOPASSWD: /usr/bin/bluetoothctl`.

### 4. Run it every 5 minutes

```bash
sudo cp ~/solar-telemetry/systemd/renogy-poll.service ~/solar-telemetry/systemd/renogy-poll.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now renogy-poll.timer
journalctl -u renogy-poll -f
```

**Going further:** when Bluetooth gets stuck, `scripts/reboot-the-world.sh` stops the timer, restarts the Bluetooth stack, runs one poll and starts the timer again.

To update later, run `git pull` in `~/solar-telemetry`.

## Documentation

- [Architecture](docs/architecture.md): how the new service works, and the decisions behind it
- [Decisions](docs/decisions/): why the collector gets its own Bluetooth client, and why it becomes a long-running service

Everything is listed on the [documentation home](docs/README.md).

## How It Compares

- **[renogy-bt](https://github.com/cyrils/renogy-bt)** reads many Renogy devices (controllers, batteries, inverters, DC chargers) and can log to MQTT, PVOutput or a URL. It's the place to start for a device other than a charge controller. This project uses it today, and adds InfluxDB logging and recovery for a Pi that runs unattended.

## Acknowledgements

[renogy-bt](https://github.com/cyrils/renogy-bt) by Cyril Sebastian showed how Renogy's BT modules work. This project's collector runs it today; the new service will have its own client ([ADR 001](docs/decisions/001-own-ble-client.md)).

## License

solar-telemetry is released under the [MIT License](LICENSE). renogy-bt, which the current collector runs as a separate program, is under the GPL-3.0.
