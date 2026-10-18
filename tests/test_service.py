"""The config, sd_notify, and the service's cycles, with a fake controller and InfluxDB."""
import asyncio
import io
import logging
import os
import socket
import sys
import tempfile

import pytest

from solar_telemetry import ble, health, influx, service, wanderer
from solar_telemetry.modbus import parse_read_reply
from solar_telemetry.service import Config, ConfigError, Service, Systemd, parse_config
from test_fixtures import load_frames

ADDRESS = "AA:BB:CC:DD:EE:FF"
RSSI = -86
MINIMAL = {"controller": {"name": "car", "address": ADDRESS}}


def recorded_words(name: str, words: int) -> tuple[int, ...]:
    return parse_read_reply(bytes.fromhex(load_frames()[name]["reply"]), wanderer.DEVICE_ID, words)


DYNAMIC_WORDS = recorded_words("dynamic_35", 35)
MODEL_WORDS = recorded_words("system_info", 16)[2:10]  # system_info starts at 0x000A, the model at 0x000C


# Config


def test_minimal_config_takes_the_defaults():
    assert parse_config(MINIMAL) == Config("car", ADDRESS, 300.0, 900.0, "http://127.0.0.1:8086", "solar")


def test_full_config():
    config = parse_config({
        **MINIMAL,
        "schedule": {"interval_s": 60, "away_interval_s": 600.5},
        "influxdb": {"url": "http://other:8086", "database": "test"},
    })
    assert config == Config("car", ADDRESS, 60.0, 600.5, "http://other:8086", "test")


def test_the_example_config_loads():
    config = service.load_config(os.path.join(os.path.dirname(__file__), "..", "config.example.toml"))
    assert config == parse_config(MINIMAL)


@pytest.mark.parametrize(
    "data, message",
    [
        ({}, "missing: controller.address, controller.name"),
        ({"controller": {"name": "car"}}, "missing: controller.address"),
        ({**MINIMAL, "schedule": {"intervall_s": 60}}, "schedule.intervall_s: unknown setting"),
        ({**MINIMAL, "extra": {"name": "x"}}, "extra.name: unknown setting"),
        ({**MINIMAL, "schedule": 5}, "schedule: not a section"),
        ({"controller": {"name": "", "address": ADDRESS}}, "controller.name: must be a non-empty string"),
        ({"controller": {"name": "car", "address": "BT-TH-1234"}}, "not a MAC address"),
        ({**MINIMAL, "schedule": {"interval_s": "300"}}, "schedule.interval_s: must be a number"),
        ({**MINIMAL, "schedule": {"interval_s": True}}, "schedule.interval_s: must be a number"),
        ({**MINIMAL, "schedule": {"interval_s": 0}}, "schedule.interval_s: must be a number"),
        ({**MINIMAL, "schedule": {"away_interval_s": float("inf")}}, "schedule.away_interval_s: must be a number"),
    ],
)
def test_bad_configs(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_a_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="config.toml"):
        service.load_config(str(tmp_path / "config.toml"))


def test_a_config_file_that_isnt_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[controller\n")
    with pytest.raises(ConfigError, match="config.toml"):
        service.load_config(str(path))


def test_main_exits_2_for_a_bad_config(tmp_path):
    assert service.main([str(tmp_path / "missing.toml")]) == 2


# sd_notify


@pytest.fixture
def notify_socket():
    """A datagram socket standing in for systemd's. A short path: AF_UNIX
    paths are limited to about 100 bytes, and pytest's tmp_path is longer
    on macOS."""
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = os.path.join(directory, "notify")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.bind(path)
            s.settimeout(1)
            yield path, s


def test_notify_sends_to_the_socket(notify_socket):
    path, s = notify_socket
    systemd = Systemd({"NOTIFY_SOCKET": path, "WATCHDOG_USEC": "60000000"})
    systemd.notify("READY=1")
    systemd.ping()
    assert [s.recv(100), s.recv(100)] == [b"READY=1", b"WATCHDOG=1"]
    assert systemd.watchdog == 60
    assert systemd.ping_every == 30


def test_outside_systemd_notify_does_nothing():
    systemd = Systemd({})
    systemd.notify("READY=1")
    systemd.ping()
    assert systemd.watchdog is None


def test_the_watchdog_is_for_another_process():
    assert Systemd({"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": str(os.getpid() + 1)}).watchdog is None
    assert Systemd({"WATCHDOG_USEC": "60000000", "WATCHDOG_PID": str(os.getpid())}).watchdog == 60


def test_a_missing_socket_is_logged_not_raised(caplog):
    Systemd({"NOTIFY_SOCKET": "/tmp/no-such-socket-here"}).notify("READY=1")
    assert "sd_notify READY=1" in caplog.text


# The journal


def record(level: int, message: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord("solar_telemetry.service", level, __file__, 1, message, None, exc_info)


@pytest.mark.parametrize("level, prefix", [
    (logging.CRITICAL, "<2>"), (logging.ERROR, "<3>"), (logging.WARNING, "<4>"),
    (logging.INFO, "<6>"), (logging.DEBUG, "<7>"),
])
def test_each_line_carries_its_priority(level, prefix):
    line = service.JournalFormatter("%(levelname)s %(name)s: %(message)s").format(record(level, "hello"))
    assert line == f"{prefix}{logging.getLevelName(level)} solar_telemetry.service: hello"


def test_every_line_of_a_traceback_carries_the_priority():
    try:
        raise ValueError("boom")
    except ValueError:
        text = service.JournalFormatter("%(message)s").format(record(logging.ERROR, "failed", sys.exc_info()))
    lines = text.split("\n")
    assert len(lines) > 2
    assert all(line.startswith("<3>") for line in lines)


def test_to_journal_matches_the_streams_device_and_inode(tmp_path):
    with open(tmp_path / "log", "w") as f:
        st = os.fstat(f.fileno())
        assert service.to_journal(f, {"JOURNAL_STREAM": f"{st.st_dev}:{st.st_ino}"})
        assert not service.to_journal(f, {"JOURNAL_STREAM": f"{st.st_dev}:{st.st_ino + 1}"})
        assert not service.to_journal(f, {})


def test_to_journal_without_a_file_descriptor():
    assert not service.to_journal(io.StringIO(), {"JOURNAL_STREAM": "1:2"})


# The service


class FakeSystemd:
    def __init__(self, watchdog=None):
        self.states = []
        self.watchdog = watchdog

    @property
    def ping_every(self):
        return self.watchdog / 2 if self.watchdog else float("inf")

    def notify(self, state):
        self.states.append(state)

    def ping(self):
        self.states.append("WATCHDOG=1")


class Controller:
    """A fake controller behind ble.find and ble.Connection. Each attempt
    takes the next of `failures` (an exception, raised by find or connect),
    and succeeds once they run out."""

    def __init__(self):
        self.failures = []
        self.reads = []
        self.connected = False
        self.hang = False  # connect never returns

    async def find(self, address):
        assert address == ADDRESS
        error = self.failures.pop(0) if self.failures else None
        if error and not isinstance(error, ble.ConnectFailed):
            raise error
        return error or "device", RSSI

    async def connect(self, device):
        if self.hang:
            await asyncio.Event().wait()
        if isinstance(device, Exception):
            raise device
        self.connected = True
        return self

    async def read(self, device_id, register, words):
        assert device_id == wanderer.DEVICE_ID
        self.reads.append(register)
        return {wanderer.MODEL: MODEL_WORDS, wanderer.DYNAMIC: DYNAMIC_WORDS}[register, words]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.connected = False


class FakeInflux:
    """A fake InfluxDB at influx._post: 204, or `status` when set."""

    def __init__(self):
        self.lines = []
        self.status = 204

    async def post(self, url, params, body):
        if self.status == 204:
            self.lines += body.decode().split("\n")
        return self.status, ""

    def points(self, measurement):
        return [line for line in self.lines if line.startswith(measurement + ",")]


@pytest.fixture
def controller(monkeypatch):
    fake = Controller()
    monkeypatch.setattr(ble, "find", fake.find)
    monkeypatch.setattr(ble.Connection, "connect", fake.connect)
    return fake


@pytest.fixture
def db(monkeypatch):
    fake = FakeInflux()
    monkeypatch.setattr(influx, "_post", fake.post)
    return fake


@pytest.fixture
def commands(monkeypatch):
    """The commands health runs. get_throttled answers 0."""
    for name in ("RETRY_WAIT", "POWER_OFF_WAIT", "POWER_CYCLE_SETTLE", "BLUEZ_RESTART_SETTLE"):
        monkeypatch.setattr(health, name, 0)
    ran = []

    async def run(*command):
        if command == health.GET_THROTTLED:
            return "throttled=0x0"
        ran.append(command)
        return ""

    monkeypatch.setattr(health, "run", run)
    return ran


@pytest.fixture
def svc(controller, db, commands):
    return Service(parse_config(MINIMAL), systemd=FakeSystemd())


def collector(line: str) -> dict[str, str]:
    """A collector line's fields, as raw strings."""
    fields = line.split(" ")[1]
    return dict(field.split("=", 1) for field in fields.split(","))


def outcomes(db) -> list[tuple[str, str]]:
    return [(c["outcome"], c["recovery_step"]) for c in map(collector, db.points("collector"))]


def test_a_good_cycle_writes_the_reading_and_its_collector_point(svc, controller, db, commands):
    assert asyncio.run(svc.cycle()) == 300
    [reading] = db.points("charge_controller")
    assert reading.startswith("charge_controller,controller=car battery_v=13.0,")
    assert "battery_soc_pct=93i" in reading and "load_on=true" in reading
    [point] = db.points("collector")
    fields = collector(point)
    assert fields["outcome"] == '"ok"'
    assert fields["recovery_step"] == "0i" and fields["throttled"] == "0i" and fields["queued"] == "0i"
    assert {"find_s", "connect_s", "read_s"} <= fields.keys()
    assert fields["rssi_dbm"] == f"{RSSI}i"
    assert commands == []
    assert not controller.connected
    assert "WATCHDOG=1" in svc.systemd.states


def test_the_model_is_read_once(svc, controller, caplog):
    caplog.set_level(logging.INFO)
    asyncio.run(svc.cycle())
    asyncio.run(svc.cycle())
    assert controller.reads == [wanderer.MODEL[0], wanderer.DYNAMIC[0], wanderer.DYNAMIC[0]]
    assert "controller model: RNG-CTRL-WND10" in caplog.text


def test_the_car_away_slows_down_without_recovery(svc, controller, db, commands, caplog):
    caplog.set_level(logging.INFO)
    controller.failures = [ble.DeviceNotFound(ADDRESS, others=4), ble.DeviceNotFound(ADDRESS, others=2)]
    assert asyncio.run(svc.cycle()) == 900
    assert asyncio.run(svc.cycle()) == 900
    assert asyncio.run(svc.cycle()) == 300
    assert outcomes(db) == [('"not_found"', "0i"), ('"not_found"', "0i"), ('"ok"', "0i")]
    assert ["rssi_dbm" in collector(point) for point in db.points("collector")] == [False, False, True]
    assert commands == []
    assert caplog.text.count("the car is probably away") == 1
    assert caplog.text.count("controller is back") == 1


def test_failures_climb_the_ladder_within_the_cycle(svc, controller, db, commands):
    controller.failures = [ble.ConnectFailed("timeout"), ble.ReadTimeout("0 bytes"), ble.ConnectFailed("timeout")]
    assert asyncio.run(svc.cycle()) == 300
    assert outcomes(db) == [
        ('"connect_failed"', "1i"), ('"read_timeout"', "2i"), ('"connect_failed"', "3i"), ('"ok"', "0i"),
    ]
    connect_failed = [c for c in map(collector, db.points("collector")) if c["outcome"] == '"connect_failed"']
    assert [c["rssi_dbm"] for c in connect_failed] == [f"{RSSI}i"] * 2  # found, so measured
    assert commands == [health.POWER_OFF, health.POWER_ON]
    assert svc.ladder.rung == 0
    assert len(db.points("charge_controller")) == 1


def test_bluez_busy_restarts_bluetooth_at_once(svc, controller, db, commands):
    controller.failures = [ble.BluezBusy("InProgress")]
    asyncio.run(svc.cycle())
    assert outcomes(db) == [('"bluez_busy"', "4i"), ('"ok"', "0i")]
    assert commands == [health.RESTART_BLUEZ]


def test_the_car_away_mid_climb_ends_the_cycle(svc, controller, db, commands):
    controller.failures = [ble.ConnectFailed("timeout"), ble.DeviceNotFound(ADDRESS, others=3)]
    assert asyncio.run(svc.cycle()) == 900
    assert outcomes(db) == [('"connect_failed"', "1i"), ('"not_found"', "0i")]


def test_the_ladder_running_out_writes_and_raises(svc, controller, db, commands):
    controller.failures = [ble.AdapterError("no adapter")] * 5
    with pytest.raises(health.RecoveryExhausted):
        asyncio.run(svc.run())
    assert outcomes(db) == [('"adapter_error"', f"{rung}i") for rung in range(1, 6)]
    assert commands == [health.POWER_OFF, health.POWER_ON, health.RESTART_BLUEZ]
    assert svc.systemd.states[0] == "READY=1" and svc.systemd.states[-1] == "STOPPING=1"


def test_a_bug_is_not_recovered_from(svc, controller, commands):
    controller.failures = [ValueError("a bug")]
    with pytest.raises(ValueError):
        asyncio.run(svc.cycle())
    assert commands == []


def test_influxdb_down_keeps_the_points(svc, db):
    db.status = None
    asyncio.run(svc.cycle())
    asyncio.run(svc.cycle())
    assert len(svc.writer) == 4
    db.status = 204
    asyncio.run(svc.cycle())
    assert len(svc.writer) == 0
    # Each cycle counts what the ones before it left: 0, then 2, then 4.
    assert [collector(line)["queued"] for line in db.points("collector")] == ["0i", "2i", "4i"]


def test_failures_and_power_are_logged(svc, controller, commands, monkeypatch, caplog):
    async def run(*command):
        return "throttled=0x50005" if command == health.GET_THROTTLED else ""

    monkeypatch.setattr(health, "run", run)
    controller.failures = [ble.ConnectFailed("timeout")]
    asyncio.run(svc.cycle())
    assert "connect_failed: timeout; power: under-voltage now, throttled now" in caplog.text
    assert "next: retry" in caplog.text


def test_stop_while_sleeping_ends_run_at_once(svc, db):
    async def go():
        task = asyncio.create_task(svc.run())
        while not db.points("collector"):
            await asyncio.sleep(0.01)
        svc.stop()
        await asyncio.wait_for(task, 1)

    asyncio.run(go())
    assert svc.systemd.states[0] == "READY=1" and svc.systemd.states[-1] == "STOPPING=1"


def test_stop_during_a_cycle_cancels_it_and_writes_the_queue(svc, controller, db):
    async def go():
        task = asyncio.create_task(svc.run())
        await asyncio.sleep(0.05)
        svc.writer.add(influx.Point("collector", svc.tags, {"outcome": "ok"}, 1))
        svc.stop()
        await asyncio.wait_for(task, 1)

    controller.hang = True
    asyncio.run(go())
    assert len(db.points("collector")) == 1
    assert len(svc.writer) == 0


def test_the_sleep_pings_the_watchdog(svc):
    svc.systemd.watchdog = 0.04  # a ping every 0.02 s
    asyncio.run(svc._sleep(0.1))
    assert svc.systemd.states.count("WATCHDOG=1") >= 4


# the schedule

MARK = 1_760_000_400.0  # a multiple of 300 and of 900 seconds: a mark on the clock


@pytest.mark.parametrize(
    "now, wait",
    [
        (MARK + 15, 300),  # a cycle that took no time
        (MARK + 15 + 297, 3),  # one that took most of the interval
        (MARK + 5, 10),  # after a start just past a mark: the offset is still to come
        (MARK + 15 + 299.5, 0.5),
    ],
)
def test_cycles_start_at_the_next_mark_on_the_clock(now, wait):
    assert service.until_next_mark(now, 300) == pytest.approx(wait)


def test_the_away_interval_keeps_to_its_own_marks():
    assert service.until_next_mark(MARK + 300 + 20, 900) == pytest.approx(900 - 300 - 5)
