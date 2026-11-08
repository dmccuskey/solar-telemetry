#!/bin/bash
# Checks from outside that the Pi can still be found on the network. Run every
# 5 minutes by reachability-check.timer, as root, on another machine on the
# same network (a wired one is best). Installed as
# /usr/local/bin/reachability-check.sh; the hosts to check are in
# /etc/default/reachability-check (TARGETS, space-separated addresses).
#
# It asks for each host by broadcast ARP (arping), the way a machine that
# doesn't know it yet would. A Wi-Fi router can stop passing broadcasts to its
# clients while their own traffic still works: the Pi then goes on collecting,
# and its own network-watchdog sees nothing wrong, but no other machine can
# reach it ("No route to host") once their cached entries lapse. arping uses no
# cache, so it sees that at once.
#
# It only logs, to the journal (tag reachability-check) and to
# $STATE_DIR/changes.log: when a host stops answering (FAILS_TO_REPORT checks
# in a row, so a reboot of the host doesn't count), every REMIND_EVERY failed
# checks while it stays that way, and when it answers again.
set -u

TARGETS=${TARGETS:-}
FAILS_TO_REPORT=${FAILS_TO_REPORT:-2}
REMIND_EVERY=${REMIND_EVERY:-12}  # about an hour, at one check every 5 minutes
STATE_DIR=${STATE_DIR:-/var/lib/reachability-check}

CHANGES=$STATE_DIR/changes.log

log() { logger -t reachability-check "$*"; echo "$*"; }

record() {
    log "$*"
    echo "$(date '+%F %T %z') $*" >> "$CHANGES"
}

now() { date +%s; }

# The interface of the default route: the one the hosts are reached on.
interface() { ip route show default | awk '/default/ {for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}'; }

minutes_since() { echo $((($(now) - $1 + 30) / 60)); }

if [ -z "$TARGETS" ]; then
    log "no TARGETS set (in /etc/default/reachability-check): nothing to check"
    exit 1
fi
iface=$(interface)
if [ -z "$iface" ]; then
    log "no default route: can't check (this machine is off the network)"
    exit 0
fi

mkdir -p "$STATE_DIR"
status=0
for target in $TARGETS; do
    state=$STATE_DIR/$target   # "<failed checks in a row> <epoch of the first>"
    fails=0
    since=0
    [ -e "$state" ] && read -r fails since < "$state"

    arping -q -c 3 -w 5 -I "$iface" "$target" > /dev/null 2>&1
    result=$?
    if [ "$result" -eq 0 ]; then
        if [ "$fails" -ge "$FAILS_TO_REPORT" ]; then
            record "$target answers broadcast ARP again, after $(minutes_since "$since") minutes ($fails failed checks)"
        fi
        rm -f "$state"
        continue
    fi
    if [ "$result" -ne 1 ]; then
        log "arping failed for $target (exit $result): not counted"
        status=1
        continue
    fi

    [ "$fails" -eq 0 ] && since=$(now)
    fails=$((fails + 1))
    echo "$fails $since" > "$state"
    if [ "$fails" -eq "$FAILS_TO_REPORT" ]; then
        record "$target doesn't answer broadcast ARP ($fails checks in a row, since $(date -d "@$since" '+%F %T' 2>/dev/null || echo "$since")): down, or the router isn't passing broadcasts to it"
    elif [ "$fails" -gt "$FAILS_TO_REPORT" ] && [ $(((fails - FAILS_TO_REPORT) % REMIND_EVERY)) -eq 0 ]; then
        log "$target still doesn't answer broadcast ARP, for $(minutes_since "$since") minutes ($fails checks)"
    fi
done
exit "$status"
