# ADR 006: Reset a Hung Adapter, and Let systemd Reboot

**Status:** Accepted (2026-10-22). Amends [ADR 002](002-long-running-service.md): the ladder gets a rung, and a reboot is no longer manual only.

## Context

On 2026-10-22, after three days with every reading `ok`, the Pi 3's on-board Bluetooth chip (a BCM43438 on the UART, driver `hci_uart_bcm`) stopped answering at 00:40. The kernel logged `hci0: command 0x200c tx timeout` and `Frame reassembly failed`, then `Unable to disable scanning` on every attempt after. To the service it was `bluez_busy` ("Operation already in progress"), so the ladder went to restarting `bluetooth.service`, which didn't help: bluetoothd was fine, the chip under it wasn't. The next failure exited, and systemd restarted the service on its backoff, every 30 minutes in the end, with no start limit. No readings for 12.5 hours, until a reboot by hand. Power was fine throughout (`get_throttled` 0).

What resets the chip short of a reboot is reloading its driver: unbinding the device from the driver and binding it again runs the driver's setup, which loads the chip's firmware as at boot. Tried on the Pi with a healthy chip: `hci0` came back in about a second, with the same firmware, and the next reading was `ok`. Whether it cures a real hang can't be tried on demand; a hang can't be made.

`bluetoothctl power off/on` (rung 3) doesn't reach this: it asks the chip, through BlueZ, to power down, and a hung chip doesn't answer.

The Raspberry Pi OS of old had `hciuart.service` to restart; trixie has none: the kernel's serdev driver attaches the chip.

## Decision

- **A new rung, "reset the adapter",** after restarting BlueZ and before exiting: retry, retry, power-cycle the adapter, restart BlueZ, reset the adapter, exit. `recovery_step` 5 is now the reset, 6 the exit.
- **The reset is a script,** `scripts/reset-bt-adapter.sh`, installed as `/usr/local/sbin/solar-telemetry-reset-adapter` and allowed with no arguments in the sudoers file. It reads `hci0`'s device and driver from sysfs (so it works for a USB adapter too), unbinds, binds, waits for `hci0` to come back, and restarts `bluetooth.service`, which loses the adapter when it goes. A script rather than `tee` into sysfs from the service: sudo then allows one fixed command, not writes to a path.
- **systemd reboots, not the service.** When even the reset fails, the service exits, as before. The unit now has a start limit, `StartLimitBurst=8` in `StartLimitIntervalSec=4h`, with `StartLimitAction=reboot`: the 9th start within 4 hours reboots the Pi instead. With the restart backoff (30 s growing to 30 minutes over 5 restarts), the 9th start comes 115 to 130 minutes after the first exit, however long each failed run takes. The window is wider than that on purpose: at the 30-minute delay, 2 hours hold only 4 starts, so a 2-hour window might never fill.
- **The car being away can't reboot the Pi:** `not_found` takes no step and never exits the service.

## Alternatives

- **The service reboots, as a last rung** (`sudo systemctl reboot`). Simpler to follow in the logs, but a bug in the ladder or the classification could reboot the Pi every cycle. systemd's start limit counts only real exits.
- **Reboot at once when the reset fails.** Faster back, but a reboot is the heaviest step, and a dead chip would reboot the Pi every few minutes.
- **Leave reboots manual,** as ADR 002 had it: the 12.5 hours of 2026-10-22.

## Consequences

- A hang like 2026-10-22's should cost one cycle, if the rebind cures it: the reset comes about 20 s after the first failure. If it doesn't, the Pi reboots about 2 hours later, and then at most every 2 hours while the adapter stays dead. A reboot loses the journal, which isn't persistent on the Pi: to keep a trace of a reboot loop, make it persistent (`Storage=persistent`).
- Installing and updating now include the script (installation step 4), and the sudoers file has five commands.
- `recovery_step` 5 changed meaning: before 2026-10-22, it was the exit.
