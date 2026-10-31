"""scripts/network-watchdog.sh, run against stubbed ip, ping, nmcli, journalctl,
logger, sleep and reboot."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "network-watchdog.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

# Each stub logs its arguments to calls, and answers from files in a folder of
# their own (a file named for a command would replace its stub).
STUBS = {
    "ip": 'cat "$ANSWERS/route"',
    "ping": 'exit "$(cat "$ANSWERS/ping")"',
    "journalctl": 'cat "$ANSWERS/kernel_errors"',
    "logger": "",
    "sleep": "",
    "sync": "",
    "reboot": 'touch "$ANSWERS/rebooted"',
    "nmcli": r'''
case "$*" in
    "--wait 30 device connect "*) [ -e "$ANSWERS/connect_fixes" ] && echo 0 > "$ANSWERS/ping"; exit 0 ;;
    "device wifi rescan "*) exit "$(cat "$ANSWERS/rescan")" ;;
    "-t -f BSSID,SSID device wifi list "*) cat "$ANSWERS/scan" ;;
    "-t -f NAME,TYPE connection show") cat "$ANSWERS/connections" ;;
    "-g 802-11-wireless.ssid connection show "*) cat "$ANSWERS/ssid_$5" ;;
    *) echo "unexpected: $*" >&2; exit 2 ;;
esac''',
}

OUR_NETWORK = r"11\:22\:33\:44\:55\:66:Home\:Net"
NEIGHBOURS = "AA\\:BB\\:CC\\:DD\\:EE\\:01:Next Door\nAA\\:BB\\:CC\\:DD\\:EE\\:02:\n"


class Watchdog:
    def __init__(self, tmp_path):
        self.stub = tmp_path / "stub"
        self.stub.mkdir()
        self.answers = tmp_path / "answers"
        self.answers.mkdir()
        for name, body in STUBS.items():
            path = self.stub / name
            path.write_text(f'#!/bin/sh\necho "{name} $*" >> "$ANSWERS/calls"\n{body}\n')
            path.chmod(0o755)
        self.run_dir = tmp_path / "run"
        self.state_dir = tmp_path / "state"
        self.set(
            route="default via 192.0.2.1 dev wlan0 proto dhcp\n",
            ping="0",
            kernel_errors="",
            rescan="0",
            scan=NEIGHBOURS + OUR_NETWORK + "\n",
            connections="Home Net:802-11-wireless\nlo:loopback\nWired connection 1:802-3-ethernet\n",
            **{"ssid_Home Net": "Home\\:Net\n"},
        )

    def set(self, **files):
        for name, text in files.items():
            (self.answers / name).write_text(text)

    def down(self):
        self.set(ping="1")

    def run(self, times=1):
        env = dict(
            os.environ,
            PATH=f"{self.stub}:{os.environ['PATH']}",
            ANSWERS=str(self.answers),
            RUN_DIR=str(self.run_dir),
            STATE_DIR=str(self.state_dir),
        )
        for _ in range(times):
            result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            assert "unexpected" not in result.stderr, result.stderr
        return result.stdout

    @property
    def calls(self):
        path = self.answers / "calls"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def rebooted(self):
        return (self.answers / "rebooted").exists()

    @property
    def fails(self):
        path = self.run_dir / "outage"
        return int(path.read_text().split()[0]) if path.exists() else 0


@pytest.fixture
def watchdog(tmp_path):
    return Watchdog(tmp_path)


def test_online_does_nothing(watchdog):
    watchdog.run()
    assert not any(c.startswith("nmcli") for c in watchdog.calls)
    assert watchdog.fails == 0


def test_a_failed_check_reconnects_but_does_not_scan(watchdog):
    watchdog.down()
    watchdog.run()
    assert "nmcli --wait 30 device connect wlan0" in watchdog.calls
    assert not any("wifi" in c for c in watchdog.calls)
    assert watchdog.fails == 1
    assert not watchdog.rebooted


def test_a_reconnect_that_works_ends_the_outage(watchdog):
    watchdog.down()
    watchdog.run(3)
    watchdog.set(connect_fixes="")
    out = watchdog.run()
    assert "network back after reconnecting wlan0, after 4 failed checks" in out
    assert watchdog.fails == 0


def test_the_network_coming_back_resets_the_count(watchdog):
    watchdog.down()
    watchdog.run(5)
    watchdog.set(ping="0")
    assert "after 5 failed checks" in watchdog.run()
    watchdog.down()
    watchdog.run()
    assert watchdog.fails == 1


def test_no_reboot_before_six_checks(watchdog):
    watchdog.down()
    watchdog.set(scan="")
    watchdog.run(5)
    assert not watchdog.rebooted
    assert not any("wifi" in c for c in watchdog.calls)


def test_router_off_waits(watchdog):
    """2026-10-30: the router unplugged, the neighbours' networks still there."""
    watchdog.down()
    watchdog.set(scan=NEIGHBOURS)
    out = watchdog.run(8)
    assert "sees 2 access points but not our network, so the router is off" in out
    assert not watchdog.rebooted


def test_our_network_visible_waits(watchdog):
    watchdog.down()
    out = watchdog.run(6)
    assert "the scan sees our network" in out
    assert not watchdog.rebooted


def test_no_gateway_counts_as_a_failed_check(watchdog):
    watchdog.set(route="")
    watchdog.run()
    assert watchdog.fails == 1
    assert not any(c.startswith("ping") for c in watchdog.calls)


@pytest.mark.parametrize(
    "files, reason",
    [
        ({"scan": ""}, "the scan saw no networks"),
        ({"rescan": "1"}, "the Wi-Fi scan failed"),
        ({"kernel_errors": "brcmfmac: brcmf_sdio_bus_rxctl: resumed on timeout\n"}, "brcmfmac errors"),
    ],
)
def test_reboots_when_the_pi_is_at_fault(watchdog, files, reason):
    watchdog.down()
    watchdog.set(**files)
    out = watchdog.run(6)
    assert reason in out
    assert watchdog.rebooted
    log = (watchdog.state_dir / "reboots.log").read_text()
    assert "down for 6 checks" in log and reason in log
    assert abs(int((watchdog.state_dir / "last-reboot").read_text()) - time.time()) < 60


def test_at_most_one_reboot_a_day(watchdog):
    watchdog.state_dir.mkdir()
    (watchdog.state_dir / "last-reboot").write_text(f"{int(time.time()) - 23 * 3600}\n")
    watchdog.down()
    watchdog.set(scan="")
    out = watchdog.run(7)
    assert "but the last reboot was at" in out
    assert not watchdog.rebooted


def test_a_reboot_more_than_a_day_ago_allows_another(watchdog):
    watchdog.state_dir.mkdir()
    (watchdog.state_dir / "last-reboot").write_text(f"{int(time.time()) - 25 * 3600}\n")
    watchdog.down()
    watchdog.set(scan="")
    watchdog.run(6)
    assert watchdog.rebooted


def test_a_hidden_network_does_not_match_our_network(watchdog):
    """A blank SSID, a hidden network's, never counts as ours."""
    watchdog.down()
    watchdog.set(scan="AA\\:BB\\:CC\\:DD\\:EE\\:02:\n", **{"ssid_Home Net": "\n"})
    out = watchdog.run(6)
    assert "sees 1 access points but not our network" in out
