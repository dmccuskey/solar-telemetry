#!/bin/bash

INTERFACE="wlan0"

# Automatically detect the default gateway/router IP via the interface
TARGET_IP=$(ip route show dev "$INTERFACE" | awk '/default via/ {print $3}')

# Fallback to an external DNS IP if no default route is assigned (e.g., if DHCP lost its lease)
if [ -z "$TARGET_IP" ]; then
    TARGET_IP="8.8.8.8"
fi

# Function to test connectivity
check_online() {
    ping -c 3 -W 5 "$TARGET_IP" > /dev/null 2>&1
}

# 1. First check
if check_online; then
    exit 0
fi

# 2. Network down - attempt interface reset
logger -t network-watchdog "Network down (Target: $TARGET_IP). Attempting to restart $INTERFACE..."
ip link set "$INTERFACE" down
sleep 5
ip link set "$INTERFACE" up
sleep 15

# Re-detect target in case DHCP issued a new gateway upon reconnect
TARGET_IP=$(ip route show dev "$INTERFACE" | awk '/default via/ {print $3}')
[ -z "$TARGET_IP" ] && TARGET_IP="8.8.8.8"

# 3. Second check after reset
if check_online; then
    logger -t network-watchdog "$INTERFACE restarted successfully. Network restored."
    exit 0
fi

# 4. Connection still dead - reboot
logger -t network-watchdog "Network recovery failed. Rebooting system..."
sync
/sbin/reboot

