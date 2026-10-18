"""Failure classification, the recovery ladder, and the commands it runs."""
import asyncio
import sys

import pytest

from solar_telemetry import ble, health, modbus
from solar_telemetry.health import Ladder, Outcome, Step

ADDRESS = "AA:BB:CC:DD:EE:FF"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    for name in ("RETRY_WAIT", "POWER_OFF_WAIT", "POWER_CYCLE_SETTLE", "BLUEZ_RESTART_SETTLE"):
        monkeypatch.setattr(health, name, 0)


class Commands(list):
    """The commands run. Each is answered with `answers[command]`, or an
    empty output."""

    def __init__(self):
        super().__init__()
        self.answers = {}

    async def run(self, *command):
        self.append(command)
        return self.answers.get(command, "")


@pytest.fixture
def commands(monkeypatch):
    ran = Commands()
    monkeypatch.setattr(health, "run", ran.run)
    return ran


@pytest.mark.parametrize(
    "error, outcome",
    [
        (ble.DeviceNotFound(ADDRESS, others=3), Outcome.NOT_FOUND),
        (ble.DeviceNotFound(ADDRESS, others=0), Outcome.ADAPTER_ERROR),
        (ble.BluezBusy("InProgress"), Outcome.BLUEZ_BUSY),
        (ble.AdapterError("no adapter"), Outcome.ADAPTER_ERROR),
        (ble.ConnectFailed("timeout"), Outcome.CONNECT_FAILED),
        (ble.ReadTimeout("0 bytes"), Outcome.READ_TIMEOUT),
        (modbus.FrameError("bad CRC"), Outcome.BAD_REPLY),
        (modbus.ExceptionReply(2), Outcome.BAD_REPLY),
    ],
)
def test_classify(error, outcome):
    assert isinstance(error, health.FAILURES)
    assert health.classify(error) is outcome


def test_classify_refuses_other_errors():
    with pytest.raises(TypeError):
        health.classify(ValueError("a bug"))


def test_outcomes_are_the_collector_values():
    assert [str(o) for o in Outcome] == [
        "ok", "not_found", "bluez_busy", "connect_failed", "read_timeout", "bad_reply", "adapter_error",
    ]


def test_the_ladder_climbs_one_rung_per_failure():
    ladder = Ladder()
    steps, rungs = [], []
    for _ in range(6):
        steps.append(ladder.on_failure(Outcome.CONNECT_FAILED))
        rungs.append(ladder.rung)
    assert steps == [Step.RETRY, Step.RETRY, Step.POWER_CYCLE_ADAPTER, Step.RESTART_BLUEZ, Step.EXIT, Step.EXIT]
    assert rungs == [1, 2, 3, 4, 5, 5]


def test_a_good_reading_resets_the_ladder():
    ladder = Ladder()
    ladder.on_failure(Outcome.READ_TIMEOUT)
    ladder.on_failure(Outcome.READ_TIMEOUT)
    ladder.on_success()
    assert ladder.step is Step.NONE and ladder.rung == 0
    assert ladder.on_failure(Outcome.READ_TIMEOUT) is Step.RETRY


def test_the_car_away_takes_no_step_and_starts_over():
    ladder = Ladder()
    assert ladder.on_failure(Outcome.NOT_FOUND) is Step.NONE
    ladder.on_failure(Outcome.CONNECT_FAILED)
    ladder.on_failure(Outcome.CONNECT_FAILED)
    assert ladder.on_failure(Outcome.NOT_FOUND) is Step.NONE
    assert ladder.on_failure(Outcome.CONNECT_FAILED) is Step.RETRY


def test_bluez_busy_goes_straight_to_restarting_bluez():
    ladder = Ladder()
    assert ladder.on_failure(Outcome.BLUEZ_BUSY) is Step.RESTART_BLUEZ
    assert ladder.rung == 4
    assert ladder.on_failure(Outcome.BLUEZ_BUSY) is Step.EXIT


def test_bluez_busy_after_a_retry():
    ladder = Ladder()
    ladder.on_failure(Outcome.CONNECT_FAILED)
    assert ladder.on_failure(Outcome.BLUEZ_BUSY) is Step.RESTART_BLUEZ


def test_an_adapter_error_climbs_like_any_failure():
    ladder = Ladder()
    assert ladder.on_failure(Outcome.ADAPTER_ERROR) is Step.RETRY


def test_retry_runs_nothing(commands):
    asyncio.run(health.recover(Step.RETRY))
    asyncio.run(health.recover(Step.NONE))
    assert commands == []


def test_power_cycle(commands):
    asyncio.run(health.recover(Step.POWER_CYCLE_ADAPTER))
    assert commands == [health.POWER_OFF, health.POWER_ON]


def test_power_cycle_powers_on_after_a_failed_power_off(commands):
    commands.answers[health.POWER_OFF] = None
    asyncio.run(health.recover(Step.POWER_CYCLE_ADAPTER))
    assert commands == [health.POWER_OFF, health.POWER_ON]


def test_power_cycle_tries_power_on_again(commands):
    commands.answers[health.POWER_ON] = None  # BlueZ's org.bluez.Error.Failed
    asyncio.run(health.recover(Step.POWER_CYCLE_ADAPTER))
    assert commands == [health.POWER_OFF] + [health.POWER_ON] * health.POWER_ON_TRIES


def test_restart_bluez(commands):
    asyncio.run(health.recover(Step.RESTART_BLUEZ))
    assert commands == [health.RESTART_BLUEZ]


def test_exit_raises(commands):
    with pytest.raises(health.RecoveryExhausted):
        asyncio.run(health.recover(Step.EXIT))
    assert commands == []


def test_root_commands_never_prompt():
    for command in (health.POWER_OFF, health.POWER_ON, health.RESTART_BLUEZ):
        assert command[:2] == ("sudo", "-n")


@pytest.mark.parametrize(
    "output, value",
    [
        ("throttled=0x0", 0),
        ("throttled=0x50005\n", 0x50005),
        ("", None),
        ("throttled=", None),
        ("VCHI initialization failed", None),
    ],
)
def test_parse_throttled(output, value):
    assert health.parse_throttled(output) == value


def test_throttled(commands):
    commands.answers[health.GET_THROTTLED] = "throttled=0x50000"
    assert asyncio.run(health.throttled()) == 0x50000


def test_throttled_without_vcgencmd(commands):
    commands.answers[health.GET_THROTTLED] = None
    assert asyncio.run(health.throttled()) is None


@pytest.mark.parametrize(
    "value, text",
    [
        (0, "none"),
        (0x1, "under-voltage now"),
        (0x50005, "under-voltage now, throttled now, under-voltage since boot, throttled since boot"),
        (0x100, "unknown bits 0x100"),
    ],
)
def test_describe_throttled(value, text):
    assert health.describe_throttled(value) == text


def run(*command):
    return asyncio.run(health.run(*command))


def test_run_returns_the_output():
    assert run(sys.executable, "-c", "print('throttled=0x0')") == "throttled=0x0"


def test_run_a_failing_command(caplog):
    assert run(sys.executable, "-c", "import sys; print('nope'); sys.exit(3)") is None
    assert "exit 3: nope" in caplog.text


def test_run_a_missing_command(caplog):
    assert run("/nonexistent/vcgencmd", "get_throttled") is None
    assert "/nonexistent/vcgencmd" in caplog.text


def test_run_times_out(monkeypatch, caplog):
    monkeypatch.setattr(health, "COMMAND_TIMEOUT", 0.2)
    assert run(sys.executable, "-c", "import time; time.sleep(10)") is None
    assert "no exit after 0.2 s" in caplog.text
