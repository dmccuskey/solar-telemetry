# Operations

Running the service day to day: controlling it, reading its logs, how it recovers, and what to check when it stops reading. Setting it up is in [Installation](installation.md); what it writes is in [Data](data.md).

## Running It

```bash
systemctl status solar-telemetry
sudo systemctl restart solar-telemetry    # after changing the config, or updating
sudo systemctl stop solar-telemetry
sudo systemctl start solar-telemetry
```

A stop cancels a reading in progress (it disconnects from the controller first), writes whatever is queued for InfluxDB, and logs `stopped`. The config is read only at start.

The controller accepts one Bluetooth connection at a time. The service holds it for a few seconds per reading, so the Renogy phone app usually connects fine in between; if a reading comes while the app is connected, it fails and the service retries. To use the app or another tool for longer, stop the service first.

## The Logs

The service logs to the systemd journal:

```bash
journalctl -u solar-telemetry -f              # follow
journalctl -u solar-telemetry --since today
journalctl -u solar-telemetry -p warning      # failures and recovery only
```

A good reading is one line. Typical lines:

```text
INFO solar_telemetry.service: controller garage (AA:BB:CC:DD:EE:FF): every 300 s, every 300 s while away; InfluxDB http://127.0.0.1:8086, database solar; watchdog 180 s
INFO solar_telemetry.service: controller model: RNG-CTRL-WND10
INFO solar_telemetry.service: battery 13.6 V, 100 %, floating; pv 12 W; connect 1.27 s
WARNING solar_telemetry.service: connect_failed: disconnected during a read, after 0 bytes; power: ok; next: retry
INFO solar_telemetry.health: recovery: retry in 5 s
INFO solar_telemetry.health: recovered after rung 1 (retry)
INFO solar_telemetry.service: controller not found, other devices in range: the car is probably away; trying every 300 s
INFO solar_telemetry.service: controller is back
```

| line | means |
|---|---|
| `controller <name> (...): every ...` | the service started, with its settings; `watchdog off` when run by hand |
| `controller model: ...` | the first good reading after a start |
| `battery ... V, ... %, <state>; pv ... W; connect ... s` | a good reading |
| `<outcome>: <error>; power: <flags>; next: <step>` | a failed attempt (a warning): the [outcome](data.md#outcomes), the error, `get_throttled` (`ok`, or the flags set), and the recovery step it leads to |
| `recovery: ...` | a recovery step being taken |
| `recovered after rung <n> (<step>)` | a good reading after recovery: that step fixed it |
| `controller not found, ... the car is probably away` | logged once when the controller goes out of range; nothing more until it's back |
| `controller is back` | the first good reading after it was away |
| `power: <flags>` | `get_throttled` changed on a good reading: undervoltage or heat |
| `InfluxDB write failed (...), <n> points queued` | the first failed write of an outage; the points wait. See [When InfluxDB Is Down](#when-influxdb-is-down) |
| `InfluxDB writes work again after <n> failed: ...` | the outage is over, and the queue was written |
| `every recovery step failed: exiting, for systemd to restart the service` | the ladder ran out (an error); systemd restarts the service |

**Keep the journal across reboots:** on Raspberry Pi OS the journal may be kept in memory only, so a reboot loses the logs from before it. To keep them, set `Storage=persistent` in `/etc/systemd/journald.conf` and run `sudo systemctl restart systemd-journald`.

The `collector` measurement in InfluxDB has the same story as numbers, one point per attempt, kept as long as the readings: see [Data: collector](data.md#collector).

## Recovery

Each cycle takes one reading. When it fails, the service classifies the failure, takes the next step on the recovery ladder, and tries again at once. The cycle ends when a reading succeeds, the car turns out to be away, or the ladder runs out. The reasons behind each step are in [ADR 002](decisions/002-long-running-service.md).

| rung | step | then |
|---|---|---|
| 1 | retry | waits 5 s |
| 2 | retry | waits 5 s |
| 3 | power-cycle the Pi's Bluetooth adapter: `bluetoothctl power off`, then `power on` (tried up to 3 times, 1 s apart) | waits 3 s |
| 4 | restart `bluetooth.service` | waits 3 s |
| 5 | exit with an error | systemd restarts the service |

- **One rung per failed attempt,** from the bottom. A good reading resets the ladder.
- **BlueZ busy** ("in progress") goes straight to rung 4: restarting BlueZ is the only thing that clears it.
- **The car being away** is not a failure: no step, and it resets the ladder. The service tries again after the away interval: by default 5 minutes, the same as the usual interval.
- **Undervoltage** changes nothing in the ladder, since no Bluetooth reset fixes power. It's logged with every failure, and recorded in `throttled`.
- **After an exit,** systemd restarts the service after 30 seconds. If it keeps exiting, the delay grows to 30 minutes, and it never stops trying for good.
- **The watchdog:** if the service stops pinging systemd for 3 minutes (a hang), systemd kills and restarts it.

Recovery never restarts D-Bus or reboots the Pi. Those stay manual: `sudo systemctl restart dbus` (which disrupts other services), or `sudo reboot`.

## When InfluxDB Is Down

InfluxDB runs on the same Pi. When a write fails, the points stay in a queue in memory and are written, oldest first and with their original timestamps, once InfluxDB answers again.

- After 3 failed writes in a row, the service restarts InfluxDB (`systemctl restart influxdb`), once per outage.
- The queue holds 4,032 points, a week of readings at the default interval. When it's full, the oldest are dropped, and the drop is logged.
- The queue is lost if the service itself restarts.
- A missing database is created. Points InfluxDB refuses as invalid are dropped and logged, so they can't block the queue.

## The Weather Job

The optional weather job, `solar-telemetry-weather`, is a service of its own ([Installation](installation.md#9-add-the-weather)):

```bash
systemctl status solar-telemetry-weather
journalctl -u solar-telemetry-weather -p warning --since today
```

It logs one line per fetch, at each mark of `refresh_s` on the clock plus a minute (:01, :16, :31, :46 by default). A failed fetch (no internet, Open-Meteo down or refusing) is a warning, and the next fetch fills the gap, up to a day back; for a longer outage, run it once by hand with `--days N`. It shares InfluxDB's queue logic with the service, but never restarts InfluxDB. A bad or missing `[weather]` location makes it exit with status 2, and systemd leaves it stopped until the config is fixed.

## When It Stops Reading

Check, in this order:

1. **Power:** `vcgencmd get_throttled`. Anything but `throttled=0x0` is undervoltage or heat. On a Pi 3, a weak USB supply shows up as dropped connections and failed reads, and no Bluetooth reset fixes it: change the supply ([Hardware: Power](hardware.md#power)). A quiet `journalctl -k | grep -i voltage` doesn't prove the voltage is fine.
2. **The service:** `systemctl status solar-telemetry`. If it's restarting over and over, the journal says why.
3. **The journal:** `journalctl -u solar-telemetry -p warning --since today` lists each failed attempt with its outcome and the step it led to.
4. **Range:** `not_found` with other devices in range means the controller isn't advertising where the Pi can hear it: the car is away, the controller has no power, or the phone app holds the connection. Failures that come and go, with `rssi_dbm` near -90 or below, point at a weak signal: see [Hardware: The Signal](hardware.md#the-signal). `adapter_error` (no devices at all) points at the Pi's adapter: `bluetoothctl show` should say `Powered: yes`.
5. **Slow connects:** `connect_s` of about 11 s on every other cycle (and about 1.3 s between) means bluetoothd runs with its GAP plugin: see [Installation](installation.md#5-keep-connects-fast).
6. **InfluxDB:** `systemctl status influxdb`, and `queued` in the latest `collector` points: anything above 0 means writes are failing.

To take a reading by hand, stop the service (one connection at a time), then run `tools/read_once.py` with the controller's MAC address: it prints each step's timing, or the error the service would act on.

```bash
sudo systemctl stop solar-telemetry
/opt/solar-telemetry/.venv/bin/python /opt/solar-telemetry/tools/read_once.py AA:BB:CC:DD:EE:FF
sudo systemctl start solar-telemetry
```
