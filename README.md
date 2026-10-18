# solar-telemetry

Logs a Renogy solar charge controller's readings to InfluxDB from a Raspberry Pi, over Bluetooth LE.

A small Python service on a Raspberry Pi, within Bluetooth range of the controller's BT module, reads it every 5 minutes and writes the readings to InfluxDB 1.x, for graphing in Grafana. It recovers from Bluetooth failures by itself, and knows the controller being out of range from Bluetooth being broken. It was built to watch the solar charger that keeps a rarely driven car's 12 V battery healthy.

```text
charge_controller,controller=garage battery_v=13.6,battery_charge_a=0.85,battery_soc_pct=100i,pv_v=19.2,pv_a=0.62,pv_w=12i,load_v=13.6,load_a=0.09,load_w=1i,load_on=true,controller_temp_c=18i,charging_state=5i,faults=0i,pv_total_wh=2781i,... 1760731200
```

Not affiliated with, or endorsed by, Renogy.

## Features

- Reads a Renogy Wanderer (and controllers that share its Modbus registers, such as the Rover and Adventurer) through a BT-1 Bluetooth module
- Writes battery, solar and load voltage, current and power, the charging state, faults and the controller's lifetime totals to InfluxDB 1.x, with the unit in each field's name
- Read-only toward the controller: it only reads registers, so it can't change charging settings
- Recovers from Bluetooth failures in seconds, with a fix for each kind: retry, power-cycle the adapter, restart BlueZ, and last, exit for systemd to restart it
- Tells the controller being out of range (the car is away) from a broken Bluetooth stack, and resets nothing when it's only away
- Logs undervoltage with each failure: on a Pi 3, a weak power supply looks like a Bluetooth problem
- Keeps a week of readings in memory while InfluxDB is down, and writes them with their original timestamps once it's back
- Records every attempt in a second measurement, `collector`: outcome, timings, recovery step, power, queue length
- Runs under systemd as its own unprivileged user, with a watchdog; the few root commands it needs are allowed by name
- Its own Bluetooth client on [bleak](https://github.com/hbldh/bleak), and no other dependency

## Quick Start

This takes about 20 minutes, on a Raspberry Pi running Raspberry Pi OS. You will have the controller's readings arriving in InfluxDB every 5 minutes.

You need a Pi with Bluetooth within range of the controller (`bluetoothctl show`), Python 3.11 or later (`python3 --version`), git, and InfluxDB 1.x on the same Pi with authentication off (`systemctl status influxdb`).

### 1. Get the code

The service runs as its own user, `solar-telemetry`, from `/opt/solar-telemetry`:

```bash
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin \
  --groups video solar-telemetry
sudo install -d -o "$USER" -g "$USER" /opt/solar-telemetry
git clone https://github.com/dmccuskey/solar-telemetry.git /opt/solar-telemetry
cd /opt/solar-telemetry
python3 -m venv .venv
.venv/bin/pip install -e .
```

**Going further:** why the user, the folder and the `video` group ([Installation](docs/installation.md)).

### 2. Configure the controller

Find the controller's BT module. Its name starts with `BT-TH`:

```bash
.venv/bin/python tools/scan_ble.py
```

```text
AA:BB:CC:DD:EE:FF  rssi=-71  name=BT-TH-XXXXXXXX
```

Copy the example config, and set `address` to that MAC address and `name` to a name for the controller (it tags every point):

```bash
sudo install -d -m 750 -g solar-telemetry /etc/solar-telemetry
sudo install -m 640 -g solar-telemetry config.example.toml /etc/solar-telemetry/config.toml
sudoedit /etc/solar-telemetry/config.toml
```

### 3. Check that it reads

Run it by hand, as its user:

```bash
sudo -u solar-telemetry .venv/bin/solar-telemetry /etc/solar-telemetry/config.toml
```

Within about 30 seconds it logs a reading. Stop it with Ctrl-C:

```text
INFO solar_telemetry.service: controller model: RNG-CTRL-WND10
INFO solar_telemetry.service: battery 13.6 V, 100 %, floating; pv 12 W; connect 1.27 s
```

`controller not found, other devices in range` means the controller is out of range, or another program holds its one connection: the Renogy phone app, for one.

### 4. Run it as a service

Allow the recovery commands, start bluetoothd without its GAP plugin (it makes every other connect 10 times slower), then install and start the unit:

```bash
sudo install -m 440 systemd/solar-telemetry.sudoers /etc/sudoers.d/solar-telemetry
sudo install -D -m 644 systemd/bluetooth-noplugin-gap.conf /etc/systemd/system/bluetooth.service.d/noplugin-gap.conf
sudo systemctl daemon-reload && sudo systemctl restart bluetooth
sudo install -m 644 systemd/solar-telemetry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now solar-telemetry
journalctl -u solar-telemetry -f
```

A reading every 5 minutes, one line each. The readings are in InfluxDB's `solar` database, created on the first write: `influx -database solar -execute 'SELECT * FROM charge_controller ORDER BY time DESC LIMIT 1'`.

**Going further:** the logs, recovery and what to watch ([Operations](docs/operations.md)); the fields, with example queries ([Data](docs/data.md)).

To update later, run `git pull` in `/opt/solar-telemetry`, then `sudo systemctl restart solar-telemetry`.

## Documentation

- [Installation](docs/installation.md): the full setup, step by step, and switching from the earlier timer-based collector
- [Operations](docs/operations.md): running it day to day, the logs, recovery, and what to do when it stops reading
- [Data](docs/data.md): the measurements and fields written to InfluxDB, with example queries
- [Architecture](docs/architecture.md): how it works inside, and the decisions behind it

Everything is listed on the [documentation home](docs/README.md).

## How It Compares

- **[renogy-bt](https://github.com/cyrils/renogy-bt)** reads many Renogy devices (controllers, batteries, inverters, DC chargers) and can log to MQTT, PVOutput or a URL. It's the place to start for a device other than a charge controller, or for a one-off reading. This project reads only charge controllers, and is built to run unattended for months: its own recovery, InfluxDB logging, and a record of how each reading went.

## Safety

The service only reads from the controller, never writes. As root it runs only the four commands in [its sudoers file](systemd/solar-telemetry.sudoers): powering the Bluetooth adapter off and on, and restarting `bluetooth` and `influxdb`.

## Acknowledgements

[renogy-bt](https://github.com/cyrils/renogy-bt) by Cyril Sebastian showed how Renogy's BT modules work, and this project's first collector ran it. The service has its own client, written from Renogy's register map ([ADR 001](docs/decisions/001-own-ble-client.md)).

## License

solar-telemetry is released under the [MIT License](LICENSE). The earlier collector, still in this repository until it's retired, runs renogy-bt (GPL-3.0) as a separate program.
