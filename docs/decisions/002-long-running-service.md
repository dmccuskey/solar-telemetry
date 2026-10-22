# ADR 002: A Long-Running Service with In-Process Recovery

**Status:** Accepted; amended 2026-10-17 (see [Amendment: The Ladder in Detail](#amendment-2026-10-17-the-ladder-in-detail)); the ladder and reboots amended by [ADR 006](006-reset-the-adapter.md) (2026-10-22)

## Context

The collector has run as a systemd timer: every 5 minutes, `renogy_poll_to_influx.py` starts, takes one reading, writes it to InfluxDB and exits. A `flock` keeps runs from overlapping, and a 90-second `TimeoutStartSec` kills a hung run. Recovery is spread across the poller (one `bluetoothctl power off/on` on failure) and a manual script, `reboot-the-world.sh`, which stops the timer, kills leftover processes, restarts `bluetooth` (and `dbus` with `--nuclear`), bounces `hci0` and polls once.

What running it that way taught us:

- **One recovery attempt per 5 minutes.** A wedged BLE stack stays wedged for several cycles, and between runs nothing is watching.
- **Different failures need different fixes.** A stuck discovery shows up as `org.bluez.Error.InProgress`, which a controller power cycle doesn't clear: only restarting `bluetooth.service` does. A connect timeout is often fixed by a power cycle. "Device not found" usually means the car is away, and then any reset is pointless noise.
- **The car goes away.** When it does, the controller is out of range. That is normal, not a failure, and it must not trigger recovery.
- **Undervoltage looks like a BLE problem.** On a Pi 3, a weak power supply shows up as dropped connections and failed reads. `vcgencmd get_throttled` other than `0x0` means power, not Bluetooth.
- **InfluxDB is local.** It runs on the same Pi, so if it's down, the collector can see that and act on it. Grafana on another machine reads from it.

## Decision

The collector becomes one long-running Python process (asyncio), run by systemd as a service rather than a timer.

- **systemd:** `Restart=always`, and `Type=notify` with `WatchdogSec`, so the service is restarted after a crash and after a hang. The service pings the watchdog from its main loop.
- **Readings** every 5 minutes (configurable), through the client of [ADR 001](001-own-ble-client.md), one connection per reading.
- **Failures are classified** before anything is reset:
  - **Car away:** the device isn't found, but a scan sees other devices. No recovery. The service keeps trying at a slower pace and logs the state change once, not every cycle.
  - **BlueZ busy** (`InProgress`): restart `bluetooth.service`.
  - **Connect or read failure:** retry, then power-cycle the controller.
  - **Adapter dead:** a scan sees nothing at all, or errors. Escalate.
  - **Undervoltage:** `get_throttled` isn't `0x0`. Log it with every failure, since resets won't fix power.
- **The recovery ladder** climbs one step at a time, with a wait after each and a reset back to the bottom after a good reading: retry; power-cycle the controller (`bluetoothctl power off/on`); restart `bluetooth.service`; exit with an error and let systemd restart the service. Restarting `dbus` and rebooting the Pi stay manual.
- **When InfluxDB is down,** readings wait in a bounded queue in memory (about a week of readings at the default interval) and are written, with their original timestamps, once it's back. If writes keep failing, the service restarts InfluxDB (`influxdb.service`) once. When the queue is full, the oldest readings are dropped and the drop is logged.
- **Privileges:** the service runs as an unprivileged user. The few commands it needs as root (`bluetoothctl`, `systemctl restart bluetooth` and `systemctl restart influxdb`) are allowed by name in a sudoers file.

## Consequences

- Recovery takes seconds, not 5-minute cycles, and each failure gets the fix that matches it.
- An absent car no longer produces resets or failed-unit noise.
- The watchdog covers a hung process, which the old `TimeoutStartSec` did for a single run.
- `flock`, the timer and most of `reboot-the-world.sh` are no longer needed. The script stays as a manual tool.
- Readings survive an InfluxDB outage, but not a restart of the service itself: the queue is in memory only.
- The service holds state, so it needs care about leaks and runaway loops, and its logs matter more: each state change and recovery step is logged.
- The sudoers rule is part of installation and has to be documented.

## Amendment 2026-10-17: The Ladder in Detail

Settled while building the `health` module.

- **"The controller" in the ladder means the Pi's Bluetooth adapter** (`hci0`, which BlueZ calls the controller), not the charge controller. `bluetoothctl power off/on` power-cycles the adapter; nothing in recovery touches the charge controller. The code calls the step "power-cycle the adapter".
- **The ladder retries twice:** retry, retry, power-cycle the adapter, restart `bluetooth.service`, exit. A failed retry costs up to a connect timeout, so the bottom of the ladder can take about a minute.
- **The car being away resets the ladder,** besides taking no step: a scan that sees other devices shows the adapter works, so the controller coming back starts a fresh climb.
- **BlueZ busy skips the rungs below restarting `bluetooth.service`,** since that's the only fix for a stuck `InProgress`. An adapter error climbs one rung at a time, like the other failures.
- **Undervoltage changes nothing in the ladder.** It's logged with each failure, as decided above.
- **The adapter power cycle stays as a precaution.** It was the old poller's fix, at a time when the disconnect left BlueZ in a bad state (an `EOFError` on teardown); the new client disconnects cleanly, so the step may never be needed. The `collector` measurement's `recovery_step` shows how often each rung runs and which one fixed a failure.
- **Power on can fail right after power off** (`org.bluez.Error.Failed`, seen on the Pi), which would leave the adapter off. The step waits a second before powering on, and tries again if it fails.
