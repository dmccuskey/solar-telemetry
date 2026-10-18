#!/usr/bin/env bash
set -euo pipefail

echo "🌍 Rebooting the Bluetooth universe..."

# --- Config ---
TIMER="renogy-poll.timer"
SERVICE="renogy-poll.service"
POLL_SCRIPT="/home/pi/solar-telemetry/renogy_poll_to_influx.py"

NUCLEAR=false

# --- Parse args ---
for arg in "$@"; do
    case "$arg" in
        --nuclear)
            NUCLEAR=true
            ;;
    esac
done

echo "⏹ Stopping timer (prevent overlap)..."
sudo systemctl stop "$TIMER" || true

echo "🧹 Killing leftover processes..."
pgrep -af "renogy_poll_to_influx|renogy-bt/example.py|python3.*renogy-bt" || true
sudo pkill -f "renogy_poll_to_influx.py" || true
sudo pkill -f "/home/pi/renogy-bt/example.py" || true
sleep 1

# --- 🔍 Auto-detect BlueZ stuck state (InProgress) ---
echo "🔍 Checking for BlueZ 'InProgress' state..."
if journalctl -u bluetooth --since "2 min ago" | grep -q "InProgress"; then
    echo "⚠️ Detected 'InProgress' in recent logs → escalating to nuclear"
    NUCLEAR=true
fi

echo "🔁 Restarting bluetooth.service (BlueZ)..."
sudo systemctl restart bluetooth
sleep 2

# --- 🧨 Nuclear option ---
if [ "$NUCLEAR" = true ]; then
    echo "💣 Nuclear mode: restarting DBus..."
    sudo systemctl restart dbus
    sleep 2
fi

echo "📡 Bouncing adapter (hci0)..."
if command -v hciconfig >/dev/null 2>&1; then
    sudo hciconfig hci0 down || true
    sleep 1
    sudo hciconfig hci0 up || true
    sleep 1
else
    echo "⚠️ hciconfig not found, skipping adapter bounce"
fi

echo "🧪 Running one manual poll (sanity check)..."
if [ -f "$POLL_SCRIPT" ]; then
    if "$POLL_SCRIPT"; then
        echo "✅ Manual poll succeeded"
    else
        echo "⚠️ Manual poll failed (continuing anyway)"
    fi
else
    echo "⚠️ Poll script not found at $POLL_SCRIPT"
fi

echo "▶️ Restarting timer..."
sudo systemctl start "$TIMER"

# --- 📊 Final status snapshot ---
echo "📊 Service status:"
systemctl status "$SERVICE" --no-pager -n 5 || true

echo "🎉 Done. The world has been rebooted."
