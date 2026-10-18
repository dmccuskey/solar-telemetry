# Installation

Installing the long-running service on a Raspberry Pi, as a systemd service under its own user. The design is in [ADR 002](decisions/002-long-running-service.md).

The [Quick Start](../README.md#quick-start) is the short version of this page.

## What You Need

- A Raspberry Pi with Bluetooth, running Raspberry Pi OS (Debian 12 or later), within range of the controller's BT module: `bluetoothctl show`
- Python 3.11 or later: `python3 --version`
- InfluxDB 1.x on the same Pi, as `influxdb.service`, with authentication off: `systemctl status influxdb`

The service creates its database (`solar` by default) on its first write.

One machine can run all of it, Grafana too: a Pi 4 or bigger. On a Pi 3, keep Grafana on another machine. The reasons are in [ADR 004](decisions/004-one-machine.md).

## Prepare the Pi

A Pi that collects on its own, often out of sight, needs a reliable network, a working watchdog and a good power supply. Check these once, before installing.

### Power

On a Pi 3, a weak power supply shows up as Bluetooth failures. `vcgencmd get_throttled` should say `throttled=0x0`, with the Pi under load: see [Hardware: Power](hardware.md#power) for which supplies did and didn't.

### Wi-Fi That Stays On

On a fresh Raspberry Pi OS, Wi-Fi can come up blocked after every reboot (`rfkill list` shows `Wireless LAN` as `Soft blocked: yes`), even after `rfkill unblock wifi`. NetworkManager keeps its own radio state, `WirelessEnabled=false` in `/var/lib/NetworkManager/NetworkManager.state`, and blocks the radio again at start. Turn it on through NetworkManager, which saves the state:

```bash
sudo nmcli radio wifi on
```

After a reboot, `nmcli radio all` shows `WIFI` as `enabled`, and `rfkill list` shows no soft block. A service that runs `rfkill unblock` at boot isn't needed.

### Wi-Fi Settings

- **Power saving off:** with it on, a Pi on Wi-Fi can stay up but stop answering. Set it on the Pi's Wi-Fi connection (`nmcli connection show` lists them) and bring the connection up again:

  ```bash
  sudo nmcli connection modify "My Network" 802-11-wireless.powersave disable
  sudo nmcli connection up "My Network"
  nmcli -g 802-11-wireless.powersave connection show "My Network"    # disable
  ```

- **A network that passes broadcasts:** some IoT and guest networks drop ARP broadcasts between devices. The Pi then runs and reaches the router, but other machines, such as one running Grafana, get "No route to host". Put it on the main network.
- **2.4 GHz only on a Pi 3:** it has no 5 GHz radio, so it needs a network with a 2.4 GHz band.
- **A way back:** with one saved Wi-Fi network and no other, changing the router's password strands the Pi. Change the Pi's saved password first (`sudo nmcli connection modify "My Network" wifi-sec.psk "new password"`), or keep an Ethernet cable at hand: the wired connection comes up by itself.

### The Hardware Watchdog

The Pi's hardware watchdog reboots it if the system hangs. Raspberry Pi OS turns it on (`/usr/lib/systemd/system.conf.d/40-rpi-enable-watchdog.conf`). Check it:

```bash
systemctl show -p RuntimeWatchdogUSec    # RuntimeWatchdogUSec=1min
```

If it says `0` or `infinity`, turn it on with a drop-in, then reboot:

```bash
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=1min\n' | sudo tee /etc/systemd/system.conf.d/watchdog.conf
sudo reboot
```

### The Network Watchdog

Optional. Every 5 minutes, `network-watchdog` pings the router; if that fails, it restarts `wlan0`, and if the network is still down, it reboots the Pi. It can't see a Pi that reaches the router but isn't reachable from other machines (the broadcasts above). Install it once the code is in `/opt/solar-telemetry` ([step 2](#2-get-the-code)):

```bash
cd /opt/solar-telemetry
sudo install -m 755 scripts/network-watchdog.sh /usr/local/bin/
sudo install -m 644 systemd/network-watchdog.service systemd/network-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now network-watchdog.timer
```

Its log lines are in `journalctl -t network-watchdog`.

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

## 5. Keep Connects Fast

bluetoothd's GAP plugin has the BT module's connections run 10 times slower on every other connect: it applies the module's stored preference of 100 to 200 ms between packets, while the module asks for 10 to 20 ms once it's connected, and the kernel keeps whichever came last for the next connect. Without the plugin, every connect takes about 1.3 s instead of 11 s on every other cycle. Nothing else the plugin reads is needed: the service finds the controller by address. The reasons, and the trace behind them, are in [ADR 003](decisions/003-no-gap-plugin.md).

```bash
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
sudo install -m 644 systemd/bluetooth-noplugin-gap.conf /etc/systemd/system/bluetooth.service.d/noplugin-gap.conf
sudo systemctl daemon-reload
sudo systemctl restart bluetooth
journalctl -u bluetooth -b | grep Excluding
```

The journal shows `Excluding (cli) gap`. The drop-in replaces the unit's `ExecStart`: if your distribution starts bluetoothd from another path (`systemctl cat bluetooth`), change it to match. It affects every Bluetooth device on the Pi, so leave it out if another program there needs the plugin.

## 6. Try It

The controller takes one Bluetooth connection at a time, so stop anything else that reads it first (the earlier collector: `sudo systemctl stop renogy-poll.timer`). Then run the service by hand, as its user:

```bash
sudo -u solar-telemetry /opt/solar-telemetry/.venv/bin/solar-telemetry /etc/solar-telemetry/config.toml
```

Within about 30 seconds it logs a reading:

```text
INFO solar_telemetry.service: controller garage (AA:BB:CC:DD:EE:FF): every 300 s, every 300 s while away; InfluxDB http://127.0.0.1:8086, database solar; watchdog off
INFO solar_telemetry.service: controller model: RNG-CTRL-WND10
INFO solar_telemetry.service: battery 13.2 V, 96 %, floating; pv 14 W; connect 1.27 s
```

Stop it with Ctrl-C: it logs `stopped`. If it logs `controller not found, other devices in range`, the controller is out of range or another program holds its connection.

## 7. Install the Service

```bash
sudo install -m 644 systemd/solar-telemetry.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now solar-telemetry
journalctl -u solar-telemetry -f
```

`systemctl status solar-telemetry` shows it `active (running)`. Each reading is one line in the journal, and each failed attempt a warning with the recovery step it led to.

The unit restarts the service if it crashes, if it stops pinging systemd's watchdog for 3 minutes, and when it exits after every recovery step has failed. Restarts back off from 30 seconds to 30 minutes and never stop for good.

## 8. Graph It in Grafana

Optional, on the machine that runs Grafana (9 or later, with its InfluxDB data source). The files are in `grafana/`.

The data source, `solar`, is a provisioning file: Grafana loads it at startup. Put in the Pi's IP address or plain hostname, not a `.local` name: Grafana's own resolver skips mDNS, so a `.local` name fails with "no such host".

```bash
sed 's/SOLARPI_ADDRESS/192.0.2.10/' grafana/datasource.yaml |
  sudo install -o root -g grafana -m 640 /dev/stdin /etc/grafana/provisioning/datasources/solar.yaml
sudo systemctl restart grafana-server
```

Its **Min time interval** of 5 minutes, the read interval, means every panel shows one value per 5 minutes at most: the mean of a voltage, the last charging state, the worst connect time.

The dashboard, `grafana/solar.json`, is imported by hand, so it stays editable in Grafana: **Dashboards › New › Import**, upload the file. Then set its **Time zone** variable (under **Edit › Settings › Variables**) to yours, such as `Europe/Berlin`, so the daily panels' days start at your midnight, and save. The dashboard shows the last 6 hours; the daily panels keep to the last 14 days whatever the range.

After changing the dashboard in Grafana, export it again (**Export › Export as JSON**, without "Export for sharing externally") over `grafana/solar.json`.

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

If `systemd/solar-telemetry.service` changed, install it again as in step 7 and run `sudo systemctl daemon-reload` before the restart.
