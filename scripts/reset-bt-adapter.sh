#!/bin/sh
# Reset the Bluetooth adapter (hci0) by unbinding and binding its driver, which
# reloads its firmware as a boot does, then restart bluetooth.service. For a
# hung adapter that restarting BlueZ doesn't reach: on a Pi 3 the on-board
# chip can stop answering ("hci0: command ... tx timeout").
#
# solar-telemetry's recovery runs it as root through sudo (ADR 006). Installed
# as /usr/local/sbin/solar-telemetry-reset-adapter. The device and its driver
# are read from sysfs, so it works for an on-board (UART) or a USB adapter.
set -eu

ADAPTER=hci0
DEVICE_LINK=/sys/class/bluetooth/$ADAPTER/device

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

if [ ! -e "$DEVICE_LINK/driver" ]; then
    log "no $ADAPTER, or it has no driver bound: nothing to reset"
    exit 1
fi
device=$(basename "$(readlink -f "$DEVICE_LINK")")
driver=$(readlink -f "$DEVICE_LINK/driver")

log "unbinding $device from $(basename "$driver")"
echo "$device" > "$driver/unbind"
wait_for "/sys/class/bluetooth/$ADAPTER" 5 absent || log "$ADAPTER still there after the unbind"
echo "$device" > "$driver/bind"
if ! wait_for "/sys/class/bluetooth/$ADAPTER" 15 present; then
    log "$ADAPTER didn't come back after the bind"
    exit 1
fi
systemctl restart bluetooth
log "$ADAPTER reset"
