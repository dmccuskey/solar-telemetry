"""scripts/reachability-check.sh, run against stubbed ip, arping and logger."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "reachability-check.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

PI = "192.0.2.16"
OTHER = "192.0.2.17"

# Each stub logs its arguments to calls, and answers from files in a folder of
# their own: arping exits with the code in arping_<target> (0 replies, 1 none).
STUBS = {
    "ip": 'cat "$ANSWERS/route"',
    "arping": 'for last; do :; done; exit "$(cat "$ANSWERS/arping_$last")"',
    "logger": "",
}


class Check:
    def __init__(self, tmp_path):
        self.stub = tmp_path / "stub"
        self.stub.mkdir()
        self.answers = tmp_path / "answers"
        self.answers.mkdir()
        for name, body in STUBS.items():
            path = self.stub / name
            path.write_text(f'#!/bin/sh\necho "{name} $*" >> "$ANSWERS/calls"\n{body}\n')
            path.chmod(0o755)
        self.state_dir = tmp_path / "state"
        self.targets = PI
        self.set(route="default via 192.0.2.1 dev eth0 proto dhcp src 192.0.2.20 metric 100\n")
        self.answer(PI, 0)
        self.answer(OTHER, 0)

    def set(self, **files):
        for name, text in files.items():
            (self.answers / name).write_text(text)

    def answer(self, target, code):
        self.set(**{f"arping_{target}": str(code)})

    def run(self, times=1, expect=0):
        env = dict(
            os.environ,
            PATH=f"{self.stub}:{os.environ['PATH']}",
            ANSWERS=str(self.answers),
            STATE_DIR=str(self.state_dir),
            TARGETS=self.targets,
        )
        out = ""
        for _ in range(times):
            result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
            assert result.returncode == expect, result.stdout + result.stderr
            out += result.stdout
        return out

    @property
    def calls(self):
        path = self.answers / "calls"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def changes(self):
        path = self.state_dir / "changes.log"
        return path.read_text().splitlines() if path.exists() else []

    def fails(self, target=PI):
        path = self.state_dir / target
        return int(path.read_text().split()[0]) if path.exists() else 0


@pytest.fixture
def check(tmp_path):
    return Check(tmp_path)


def test_a_host_that_answers_logs_nothing(check):
    assert check.run(3) == ""
    assert check.changes == []
    assert f"arping -q -c 3 -w 5 -I eth0 {PI}" in check.calls


def test_one_miss_is_not_reported(check):
    """A reboot of the host can miss one check."""
    check.answer(PI, 1)
    assert check.run() == ""
    assert check.fails() == 1
    check.answer(PI, 0)
    assert check.run() == ""
    assert check.fails() == 0
    assert check.changes == []


def test_two_misses_are_reported_once(check):
    """2026-11-07: the router stopped passing broadcasts to its Wi-Fi clients."""
    check.answer(PI, 1)
    out = check.run(2)
    assert f"{PI} doesn't answer broadcast ARP (2 checks in a row" in out
    assert "the router isn't passing broadcasts" in out
    assert len(check.changes) == 1
    assert check.run(3) == ""
    assert len(check.changes) == 1


def test_a_reminder_every_hour(check):
    check.answer(PI, 1)
    check.run(2)
    assert check.run(11) == ""
    assert f"{PI} still doesn't answer broadcast ARP" in check.run()
    assert check.fails() == 14
    assert len(check.changes) == 1


def test_answering_again_is_reported(check):
    check.answer(PI, 1)
    check.run(4)
    check.answer(PI, 0)
    out = check.run()
    assert f"{PI} answers broadcast ARP again, after 0 minutes (4 failed checks)" in out
    assert len(check.changes) == 2
    assert check.fails() == 0


def test_hosts_are_counted_apart(check):
    check.targets = f"{PI} {OTHER}"
    check.answer(OTHER, 1)
    out = check.run(2)
    assert OTHER in out and PI not in out
    assert check.fails(PI) == 0
    assert check.fails(OTHER) == 2


def test_an_arping_error_is_not_a_miss(check):
    """Exit 2: arping itself failed (e.g. not run as root)."""
    check.answer(PI, 2)
    out = check.run(3, expect=1)
    assert "arping failed" in out
    assert check.fails() == 0
    assert check.changes == []


def test_no_default_route_checks_nothing(check):
    check.answer(PI, 1)
    check.set(route="")
    assert "no default route" in check.run(3)
    assert not any(c.startswith("arping") for c in check.calls)
    assert check.fails() == 0


def test_no_targets_is_an_error(check):
    check.targets = ""
    assert "no TARGETS set" in check.run(expect=1)
