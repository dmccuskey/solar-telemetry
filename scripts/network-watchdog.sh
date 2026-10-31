#!/bin/bash
# A network watchdog that rarely reboots. Run every 5 minutes by
# network-watchdog.timer, as root. Installed as /usr/local/bin/network-watchdog.sh.
#
# The collector doesn't need the network (InfluxDB is on the same Pi), and a
# reboot can't fix a router that's off, so the Pi reboots only when its own
# Wi-Fi looks stuck (driver or firmware), and only when all of these hold:
#
#   1. the outage has lasted: FAILS_BEFORE_REBOOT failed checks in a row
#      (6, about 30 minutes);
#   2. the cheaper fix failed: `nmcli device connect`, after every failed check;
#   3. a forced Wi-Fi scan points at the Pi: it fails, sees no network at all,
#      or the kernel logged brcmfmac errors during the outage. A scan that sees
#      our network (the router's problem) or only the neighbours' (the router is
#      off) means wait;
#   4. no reboot by this script in the last REBOOT_WINDOW_S (24 h).
#
# The count of failed checks is kept in /run (a reboot resets it); the time of
# the last reboot and a log of reboots in /var/lib/network-watchdog, since the
# journal doesn't survive a reboot.
set -u

INTERFACE=${INTERFACE:-wlan0}
FAILS_BEFORE_REBOOT=${FAILS_BEFORE_REBOOT:-6}
REBOOT_WINDOW_S=${REBOOT_WINDOW_S:-86400}
RUN_DIR=${RUN_DIR:-/run/network-watchdog}
STATE_DIR=${STATE_DIR:-/var/lib/network-watchdog}
RECONNECT_WAIT_S=${RECONNECT_WAIT_S:-10}  # after the reconnect, for DHCP and the route
SCAN_WAIT_S=${SCAN_WAIT_S:-10}

OUTAGE=$RUN_DIR/outage           # "<failed checks> <epoch of the first>"
LAST_REBOOT=$STATE_DIR/last-reboot  # epoch
REBOOT_LOG=$STATE_DIR/reboots.log

log() { logger -t network-watchdog "$*"; echo "$*"; }

now() { date +%s; }

# The default route's gateway, on any interface (Wi-Fi, or Ethernet when it's
# plugged in); empty with no default route (no DHCP lease).
gateway() { ip route show default | awk '/default via/ {print $3; exit}'; }

online() {
    local target
    target=$(gateway)
    [ -n "$target" ] && ping -c 3 -W 5 "$target" > /dev/null 2>&1
}

# nmcli's terse output escapes ':' in a value as '\:'.
unescape() { sed 's/\\:/:/g'; }

# The SSIDs of the saved Wi-Fi profiles, one per line.
saved_ssids() {
    nmcli -t -f NAME,TYPE connection show | unescape |
        awk -F: '$NF == "802-11-wireless" {sub(/:[^:]*$/, ""); print}' |
        while IFS= read -r name; do
            nmcli -g 802-11-wireless.ssid connection show "$name" | unescape
        done | grep .
}

# Decides whether the Pi's own Wi-Fi is at fault; prints the reason. Returns 0
# for the Pi (reboot), 1 for the network (wait).
pi_at_fault() {
    local since=$1 seen count ours
    if journalctl -q -k -p err --since "@$since" --no-pager 2>/dev/null | grep -qi brcmf; then
        echo "the kernel logged brcmfmac errors"
        return 0
    fi
    if ! nmcli device wifi rescan ifname "$INTERFACE" > /dev/null 2>&1; then
        # Refused while a scan is already running: wait for it once.
        sleep "$SCAN_WAIT_S"
        if ! nmcli device wifi rescan ifname "$INTERFACE" > /dev/null 2>&1; then
            echo "the Wi-Fi scan failed"
            return 0
        fi
    fi
    sleep "$SCAN_WAIT_S"
    # One line per access point, "<BSSID>:<SSID>": a hidden network's SSID is
    # empty, but it still counts as a network seen.
    if ! seen=$(nmcli -t -f BSSID,SSID device wifi list ifname "$INTERFACE" --rescan no 2>/dev/null); then
        echo "the scan's results couldn't be read"
        return 0
    fi
    if [ -z "$seen" ]; then
        echo "the scan saw no networks"
        return 0
    fi
    count=$(printf '%s\n' "$seen" | wc -l | tr -d ' ')
    seen=$(printf '%s\n' "$seen" | sed 's/^\([0-9A-Fa-f][0-9A-Fa-f]\\:\)\{5\}[0-9A-Fa-f][0-9A-Fa-f]://' | unescape)
    ours=$(saved_ssids | grep -Fxf - <(printf '%s\n' "$seen") | head -n 1)
    if [ -n "$ours" ]; then
        echo "the scan sees our network, so the router or the gateway is at fault"
    else
        echo "the scan sees $count access points but not our network, so the router is off"
    fi
    return 1
}

mkdir -p "$RUN_DIR" "$STATE_DIR"

if online; then
    if [ -e "$OUTAGE" ]; then
        read -r fails _ < "$OUTAGE"
        log "network back, after $fails failed checks"
        rm -f "$OUTAGE"
    fi
    exit 0
fi

fails=0
since=$(now)
[ -e "$OUTAGE" ] && read -r fails since < "$OUTAGE"
fails=$((fails + 1))
echo "$fails $since" > "$OUTAGE"
log "check $fails failed (gateway: $(gateway | grep . || echo none)); reconnecting $INTERFACE"

nmcli --wait 30 device connect "$INTERFACE" > /dev/null 2>&1
sleep "$RECONNECT_WAIT_S"
if online; then
    log "network back after reconnecting $INTERFACE, after $fails failed checks"
    rm -f "$OUTAGE"
    exit 0
fi

if [ "$fails" -lt "$FAILS_BEFORE_REBOOT" ]; then
    exit 0
fi

if ! reason=$(pi_at_fault "$since"); then
    log "down for $fails checks; not rebooting: $reason"
    exit 0
fi

last=0
[ -e "$LAST_REBOOT" ] && read -r last < "$LAST_REBOOT"
if [ $(($(now) - last)) -lt "$REBOOT_WINDOW_S" ]; then
    log "down for $fails checks and $reason, but the last reboot was at $(date -d "@$last" '+%F %T' 2>/dev/null || echo "$last"): not rebooting"
    exit 0
fi

now > "$LAST_REBOOT"
echo "$(date '+%F %T %z') reboot: down for $fails checks, $reason" >> "$REBOOT_LOG"
log "down for $fails checks and $reason: rebooting"
sync
reboot
