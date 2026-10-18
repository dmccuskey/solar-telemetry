# Installation

Installing the long-running service on a Raspberry Pi, as a systemd service under its own user. The design is in [ADR 002](decisions/002-long-running-service.md).

The [Quick Start](../README.md#quick-start) is the short version of this page.

## What You Need

- A Raspberry Pi with Bluetooth, running Raspberry Pi OS (Debian 12 or later), within range of the controller's BT module: `bluetoothctl show`
- Python 3.11 or later: `python3 --version`
- InfluxDB 1.x on the same Pi, as `influxdb.service`, with authentication off: `systemctl status influxdb`

The service creates its database (`solar` by default) on its first write.

## 1. Create the Service User

The service runs as `solar-telemetry`, a system user with no login and no home. The `video` group lets it read `vcgencmd get_throttled`:

```bash
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin \
  --groups video solar-telemetry
```

## 2. Get the Code

The code goes in `/opt/solar-telemetry`, owned by your own user and readable by the service user. A home folder won't do: on Raspberry Pi OS it's closed to other users.

```bash
sudo install -d -o "$USER" -g "$USER" /opt/solar-telemetry
git clone https://github.com/dmccuskey/solar-telemetry.git /opt/solar-telemetry
cd /opt/solar-telemetry
python3 -m venv .venv
.venv/bin/pip install -e .
```

`pip` can take a few minutes on a Pi 3. Check that it worked:

```bash
.venv/bin/solar-telemetry --help
```

## 3. Configure It

Find the controller's BT module, whose name starts with `BT-TH`, and note its MAC address:

```bash
.venv/bin/python tools/scan_ble.py
```

```text
AA:BB:CC:DD:EE:FF  rssi=-71  name=BT-TH-XXXXXXXX
```

The config lives in `/etc/solar-telemetry/`, readable by root and the service user only:

```bash
sudo install -d -m 750 -g solar-telemetry /etc/solar-telemetry
sudo install -m 640 -g solar-telemetry config.example.toml /etc/solar-telemetry/config.toml
sudoedit /etc/solar-telemetry/config.toml
```

Set `name` (the `controller` tag on every point, e.g. `garage`) and `address` (the BT module's MAC address). The other settings are optional; [`config.example.toml`](../config.example.toml) shows their defaults.

## 4. Allow the Recovery Commands

Recovery power-cycles the Bluetooth adapter and restarts `bluetooth.service`, and restarts InfluxDB when writes keep failing. The sudoers file allows exactly those four command lines, and nothing else:

```bash
sudo install -m 440 systemd/solar-telemetry.sudoers /etc/sudoers.d/solar-telemetry
sudo visudo -cf /etc/sudoers.d/solar-telemetry
sudo -l -U solar-telemetry
```

`visudo` prints `parsed OK`, and `sudo -l` lists the four commands.

## 5. Try It

The controller takes one Bluetooth connection at a time, so stop anything else that reads it first (the earlier collector: `sudo systemctl stop renogy-poll.timer`). Then run the service by hand, as its user:

```bash
sudo -u solar-telemetry /opt/solar-telemetry/.venv/bin/solar-telemetry /etc/solar-telemetry/config.toml
```

Within about 30 seconds it logs a reading:

```text
INFO solar_telemetry.service: controller garage (AA:BB:CC:DD:EE:FF): every 300 s, every 900 s while away; InfluxDB http://127.0.0.1:8086, database solar; watchdog off
INFO solar_telemetry.service: controller model: RNG-CTRL-WND10
INFO solar_telemetry.service: battery 13.2 V, 96 %, floating; pv 14 W; connect 1.27 s
```

Stop it with Ctrl-C: it logs `stopped`. If it logs `controller not found, other devices in range`, the controller is out of range or another program holds its connection.

## 6. Install the Service

```bash
sudo install -m 644 systemd/solar-telemetry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now solar-telemetry
journalctl -u solar-telemetry -f
```

`systemctl status solar-telemetry` shows it `active (running)`. Each reading is one line in the journal, and each failed attempt a warning with the recovery step it led to.

The unit restarts the service if it crashes, if it stops pinging systemd's watchdog for 3 minutes, and when it exits after every recovery step has failed. Restarts back off from 30 seconds to 30 minutes and never stop for good.

## Switching from the Timer

The service replaces `renogy-poll.timer`. Only one of them can run, since they share the controller's one connection:

```bash
sudo systemctl disable --now renogy-poll.timer
sudo systemctl enable --now solar-telemetry
```

Going back is the reverse:

```bash
sudo systemctl disable --now solar-telemetry
sudo systemctl enable --now renogy-poll.timer
```

## Updating

```bash
cd /opt/solar-telemetry
git pull
.venv/bin/pip install -e .    # only needed when pyproject.toml changed
sudo systemctl restart solar-telemetry
```

If `systemd/solar-telemetry.service` changed, install it again as in step 6 and run `sudo systemctl daemon-reload` before the restart.
