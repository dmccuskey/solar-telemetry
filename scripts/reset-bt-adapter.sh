#!/bin/sh
# Reset the Bluetooth adapter (hci0) by unbinding and binding its driver, which
# reloads its firmware as a boot does, then restart bluetooth.service. For a
# hung adapter that restarting BlueZ doesn't reach: on a Pi 3 the on-board
# chip can stop answering ("hci0: command ... tx timeout").
#
# solar-telemetry's recovery runs it as root through sudo (ADR 006). Installed
# as /usr/local/sbin/solar-telemetry-reset-adapter. The device and its driver
# are read from sysfs, so it works for an on-board (UART) or a USB adapter.
#
# With no hci0 (its driver unbound: a reset that failed between unbind and
# bind, or by hand), there's nothing in sysfs to read them from. The script
# then binds the device it last reset, remembered until the next boot, or
# else a Pi's on-board chip, if either is there with no driver bound.
set -eu

ADAPTER=hci0
DEVICE_LINK=/sys/class/bluetooth/$ADAPTER/device
REMEMBERED=/run/solar-telemetry-reset-adapter  # "<device path> <driver path>"
ON_BOARD="/sys/bus/serial/devices/serial0-0 /sys/bus/serial/drivers/hci_uart_bcm"

log() { logger -t solar-telemetry-reset-adapter "$*"; echo "$*"; }

# Waits up to $2 seconds for $1 to exist ("present") or not ("absent").
wait_for() {
    i=0
    while [ "$i" -lt "$2" ]; do
        if [ "$3" = present ] && [ -e "$1" ]; then return 0; fi
        if [ "$3" = absent ] && [ ! -e "$1" ]; then return 0; fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# Binds device $1 (its sysfs path) to driver $2, and waits for hci0.
bind() {
    log "binding $(basename "$1") to $(basename "$2")"
    basename "$1" > "$2/bind"
    if ! wait_for "/sys/class/bluetooth/$ADAPTER" 15 present; then
        log "$ADAPTER didn't come back after the bind"
        exit 1
    fi
}

if [ -e "$DEVICE_LINK/driver" ]; then
    device=$(readlink -f "$DEVICE_LINK")
    driver=$(readlink -f "$DEVICE_LINK/driver")
    echo "$device $driver" > "$REMEMBERED"
    log "unbinding $(basename "$device") from $(basename "$driver")"
    basename "$device" > "$driver/unbind"
    wait_for "/sys/class/bluetooth/$ADAPTER" 5 absent || log "$ADAPTER still there after the unbind"
    bind "$device" "$driver"
else
    found=
    for pair in "$(cat "$REMEMBERED" 2>/dev/null || true)" "$ON_BOARD"; do
        # shellcheck disable=SC2086  # the pair splits into its two paths
        set -- $pair
        if [ $# -eq 2 ] && [ -e "$1" ] && [ ! -e "$1/driver" ] && [ -d "$2" ]; then
            found=1
            break
        fi
    done
    if [ -z "$found" ]; then
        log "no $ADAPTER, and no unbound Bluetooth device known: nothing to reset"
        exit 1
    fi
    log "no $ADAPTER: its device $(basename "$1") has no driver bound"
    bind "$1" "$2"
fi
systemctl restart bluetooth
log "$ADAPTER reset"
