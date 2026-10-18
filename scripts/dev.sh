#!/bin/bash
# Development loop from the laptop: test here, deploy the working tree to the Pi, run it there, read the logs.
#
#   scripts/dev.sh test [pytest args]     run the tests on the laptop (needs uv)
#   scripts/dev.sh deploy                 rsync the working tree to ~/solar-telemetry-dev on the Pi, update its venv
#   scripts/dev.sh run <command...>       run a command in ~/solar-telemetry-dev, with the service stopped
#   scripts/dev.sh logs [journalctl args] the service's journal (default: the last 50 lines)
#
# The live checkout (/opt/solar-telemetry) is never touched: it changes only by git pull.

set -eo pipefail

HOST="${SOLAR_HOST:-solarpi}"
DEST="solar-telemetry-dev"   # relative to the Pi user's home
SERVICE="solar-telemetry.service"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
    sed -n '3,8s/^# \{0,1\}//p' "$0"
    exit 2
}

cmd_test() {
    cd "$ROOT"
    uv run --python 3.13 pytest "$@"
}

cmd_deploy() {
    # Excluded paths are also kept on the Pi: its venv and local config survive --delete.
    rsync -az --delete \
        --exclude .git/ --exclude .venv/ --exclude __pycache__/ --exclude .pytest_cache/ \
        --exclude '*.egg-info/' --exclude uv.lock --exclude '*.ini' --exclude config.toml \
        "$ROOT/" "$HOST:$DEST/"

    # Reinstall only when pyproject.toml is newer than the last install.
    ssh "$HOST" "cd $DEST &&
        if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi &&
        if [ ! -e .venv/.installed ] || [ pyproject.toml -nt .venv/.installed ]; then
            .venv/bin/pip install --quiet -e . && touch .venv/.installed && echo 'venv updated'
        fi"
}

cmd_run() {
    [ $# -gt 0 ] || usage
    # The controller takes one BLE connection at a time: stop the service (it disconnects on the way
    # out), and start it again however the command ends.
    ssh -t "$HOST" "cd $DEST &&
        sudo systemctl stop $SERVICE &&
        trap 'sudo systemctl start $SERVICE' EXIT HUP INT TERM &&
        PATH=\$PWD/.venv/bin:\$PATH $(printf '%q ' "$@")"
}

cmd_logs() {
    if [ $# -eq 0 ]; then set -- -n 50; fi
    ssh "$HOST" journalctl -u "$SERVICE" --no-pager $(printf '%q ' "$@")
}

case "$1" in
    test)   shift; cmd_test "$@" ;;
    deploy) shift; cmd_deploy ;;
    run)    shift; cmd_run "$@" ;;
    logs)   shift; cmd_logs "$@" ;;
    *)      usage ;;
esac
