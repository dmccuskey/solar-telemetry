"""The service: the main loop, the schedule, the systemd watchdog, the config (ADR 002).

Each cycle takes one reading. A failed reading is classified, and anything but
the car being away climbs the recovery ladder and tries again at once, until
a reading succeeds or the ladder runs out. Then the queue is written to
InfluxDB, and the service sleeps until the next cycle: the next mark of the
interval on the clock, or of the longer away interval while the car is away.

Every attempt queues a `collector` point (its outcome, timings, signal
strength, the recovery step it led to, `get_throttled`, the queue's length); a
good one also queues its `charge_controller` point. The queue is written once per cycle, never
alongside a read.

Run by systemd as a `Type=notify` service with `WatchdogSec`: it signals ready
at startup, pings the watchdog between the steps of a cycle and while it
sleeps, and exits with an error when the ladder runs out, for systemd to
restart it.

Usage: python -m solar_telemetry.service <config.toml>
"""
import argparse
import asyncio
import dataclasses
import logging
import math
import os
import re
import signal
import socket
import sys
import time
import tomllib

from solar_telemetry import ble, health, influx, wanderer
from solar_telemetry.health import Outcome, Step

log = logging.getLogger(__name__)

INTERVAL = 300.0  # seconds between readings
AWAY_INTERVAL = 900.0  # while the car is away
# Cycles start this long after each mark of the interval on the clock (:00:15,
# :05:15, ...), whatever time the service started: a retry then lands in the same
# 5-minute block as its cycle, and the dashboard shows one reading per block.
MARK_OFFSET = 15.0
STOP_FLUSH_TIMEOUT = 15.0  # for the last write, on the way out

MAC_ADDRESS = re.compile(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", re.IGNORECASE)


# Config


class ConfigError(Exception):
    """The config file is missing, isn't TOML, or has a bad or unknown setting."""


@dataclasses.dataclass(frozen=True)
class Config:
    controller: str  # the `controller` tag, e.g. "car"
    address: str  # the BT module's MAC address
    interval: float = INTERVAL
    away_interval: float = AWAY_INTERVAL
    influx_url: str = influx.URL
    database: str = influx.DATABASE


# Each setting: (section, key) -> (Config field, type). Anything else in the
# file is an error, so a misspelt key isn't silently ignored.
SETTINGS = {
    ("controller", "name"): ("controller", str),
    ("controller", "address"): ("address", str),
    ("schedule", "interval_s"): ("interval", float),
    ("schedule", "away_interval_s"): ("away_interval", float),
    ("influxdb", "url"): ("influx_url", str),
    ("influxdb", "database"): ("database", str),
}
REQUIRED = {("controller", "name"), ("controller", "address")}


def load_config(path: str) -> Config:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse_config(data)


def parse_config(data: dict) -> Config:
    values = {}
    for section, table in data.items():
        if not isinstance(table, dict):
            raise ConfigError(f"{section}: not a section")
        for key, value in table.items():
            if (section, key) not in SETTINGS:
                raise ConfigError(f"{section}.{key}: unknown setting")
            field, kind = SETTINGS[section, key]
            values[field] = _check(f"{section}.{key}", value, kind)
    missing = [f"{section}.{key}" for section, key in sorted(REQUIRED) if SETTINGS[section, key][0] not in values]
    if missing:
        raise ConfigError(f"missing: {', '.join(missing)}")
    if not MAC_ADDRESS.fullmatch(values["address"]):
        raise ConfigError(f"controller.address: not a MAC address: {values['address']}")
    return Config(**values)


def _check(name: str, value, kind: type):
    if kind is str:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{name}: must be a non-empty string")
        return value.strip()
    # A number of seconds. A bool is an int to Python, but not here.
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name}: must be a number of seconds above 0")
    return float(value)


# systemd


class Systemd:
    """sd_notify without libsystemd: a datagram to $NOTIFY_SOCKET. Does
    nothing when not run by systemd."""

    def __init__(self, environ=os.environ):
        self.socket = environ.get("NOTIFY_SOCKET")
        usec = environ.get("WATCHDOG_USEC")
        pid = environ.get("WATCHDOG_PID")
        if usec and (not pid or int(pid) == os.getpid()):
            self.watchdog = int(usec) / 1e6  # seconds
        else:
            self.watchdog = None

    @property
    def ping_every(self) -> float:
        """How often to ping: twice per watchdog interval, as systemd advises."""
        return self.watchdog / 2 if self.watchdog else math.inf

    def notify(self, state: str) -> None:
        if not self.socket:
            return
        address = "\0" + self.socket[1:] if self.socket.startswith("@") else self.socket
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(address)
                s.sendall(state.encode())
        except OSError as e:
            log.warning("sd_notify %s: %s", state, e)

    def ping(self) -> None:
        if self.watchdog:
            self.notify("WATCHDOG=1")


class JournalFormatter(logging.Formatter):
    """Prefixes each line with its syslog priority (`<4>` for a warning), which
    journald strips and stores, so `journalctl -p warning` works. Every line of
    a traceback gets the prefix: journald takes each line as its own entry."""

    PRIORITIES = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4, logging.INFO: 6, logging.DEBUG: 7}

    def format(self, record: logging.LogRecord) -> str:
        priority = self.PRIORITIES.get(record.levelno, 6)
        return "\n".join(f"<{priority}>{line}" for line in super().format(record).split("\n"))


def to_journal(stream=sys.stderr, environ=os.environ) -> bool:
    """Whether the stream is the journal: $JOURNAL_STREAM names its device and inode."""
    try:
        st = os.fstat(stream.fileno())
    except (OSError, ValueError, AttributeError):
        return False
    return environ.get("JOURNAL_STREAM") == f"{st.st_dev}:{st.st_ino}"


# The service


def until_next_mark(now: float, interval: float) -> float:
    """Seconds from `now` (Unix time) to the next mark of `interval` on the
    clock, plus MARK_OFFSET."""
    mark = (now - MARK_OFFSET) // interval * interval + interval
    return mark + MARK_OFFSET - now


class Service:
    def __init__(self, config: Config, writer: influx.Writer | None = None, systemd: Systemd | None = None):
        self.config = config
        self.writer = writer or influx.Writer(config.influx_url, config.database)
        self.systemd = systemd or Systemd()
        self.ladder = health.Ladder()
        self.tags = {"controller": config.controller}
        self.model = None  # read once, for the log
        self.away = False
        self.throttled = 0  # the last get_throttled value logged
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """End run(): at once while sleeping, or by cancelling the cycle."""
        self._stop.set()

    async def run(self) -> None:
        """Cycle until stop(). Raises RecoveryExhausted when every step of the
        ladder failed. Either way, the queue is written one last time."""
        log.info(
            "controller %s (%s): every %g s, every %g s while away; InfluxDB %s, database %s; watchdog %s",
            self.config.controller, self.config.address, self.config.interval, self.config.away_interval,
            self.config.influx_url, self.config.database,
            f"{self.systemd.watchdog:g} s" if self.systemd.watchdog else "off",
        )
        self.systemd.notify("READY=1")
        try:
            while not self._stop.is_set():
                cycle = asyncio.ensure_future(self.cycle())
                stopped = asyncio.ensure_future(self._stop.wait())
                await asyncio.wait({cycle, stopped}, return_when=asyncio.FIRST_COMPLETED)
                stopped.cancel()
                if not cycle.done():
                    cycle.cancel()
                    await asyncio.gather(cycle, return_exceptions=True)
                    break
                await self._sleep(until_next_mark(time.time(), cycle.result()))
        finally:
            self.systemd.notify("STOPPING=1")
            await self._last_flush()

    async def cycle(self) -> float:
        """One reading, recovering until it succeeds, the car is away, or the
        ladder runs out (RecoveryExhausted). Then the queue is written.
        Returns the interval to the next cycle."""
        backlog = len(self.writer)  # what earlier cycles couldn't write
        while True:
            read_at = int(time.time())
            measured = {}
            fields = error = None
            try:
                fields = await self.read(measured)
            except health.FAILURES as e:
                error = e
                outcome = health.classify(e)
            else:
                outcome = Outcome.OK
            throttled = await health.throttled()

            if outcome is Outcome.OK:
                self.ladder.on_success()
                step = Step.NONE
                self.writer.add(influx.Point("charge_controller", self.tags, fields, read_at))
            else:
                step = self.ladder.on_failure(outcome)
            self._log(outcome, error, step, fields, measured, throttled)
            self.writer.add(self._collector(read_at, outcome, measured, throttled, backlog))
            self.systemd.ping()

            if step is Step.NONE:
                break
            await health.recover(step)  # raises RecoveryExhausted for Step.EXIT
            self.systemd.ping()

        await self.writer.flush()
        self.systemd.ping()
        return self.config.away_interval if outcome is Outcome.NOT_FOUND else self.config.interval

    async def read(self, measured: dict[str, float | int]) -> wanderer.Fields:
        """Find, connect, read, disconnect. Puts each step's seconds in
        `measured` as it completes, and the signal strength once the
        controller is found, under the `collector` field names."""
        t = time.monotonic()

        def took(name: str) -> None:
            nonlocal t
            measured[name] = round(time.monotonic() - t, 2)
            t = time.monotonic()

        device, measured["rssi_dbm"] = await ble.find(self.config.address)
        took("find_s")
        async with await ble.Connection.connect(device) as connection:
            took("connect_s")
            if self.model is None:
                self.model = wanderer.parse_model(await connection.read(wanderer.DEVICE_ID, *wanderer.MODEL))
                log.info("controller model: %s", self.model)
            words = await connection.read(wanderer.DEVICE_ID, *wanderer.DYNAMIC)
            took("read_s")
        return wanderer.parse_dynamic(words)

    def _collector(
        self, read_at: int, outcome: Outcome, measured: dict[str, float | int], throttled: int | None, backlog: int
    ):
        fields = {"outcome": str(outcome), **measured, "recovery_step": self.ladder.rung, "queued": backlog}
        if throttled is not None:
            fields["throttled"] = throttled
        return influx.Point("collector", self.tags, fields, read_at)

    def _log(self, outcome, error, step, fields, measured, throttled) -> None:
        """One line per attempt, except while the car stays away: that's
        logged when it goes and when it's back."""
        power = health.describe_throttled(throttled) if throttled else None
        if outcome is Outcome.OK:
            if self.away:
                log.info("controller is back")
                self.away = False
            state = wanderer.CHARGING_STATES.get(fields["charging_state"], "unknown")
            log.info(
                "battery %.1f V, %d %%, %s; pv %d W; signal %d dBm, connect %.2f s",
                fields["battery_v"], fields["battery_soc_pct"], state, fields["pv_w"], measured["rssi_dbm"],
                measured["connect_s"],
            )
            if (throttled or 0) != self.throttled:
                log.warning("power: %s", power or "none")
        elif outcome is Outcome.NOT_FOUND:
            if not self.away:
                log.info(
                    "controller not found, other devices in range: the car is probably away; trying every %g s",
                    self.config.away_interval,
                )
                self.away = True
        else:
            log.warning("%s: %s; power: %s; next: %s", outcome, error, power or "ok", step)
        if throttled is not None:
            self.throttled = throttled

    async def _sleep(self, seconds: float) -> None:
        """Sleep, pinging the watchdog, until the time is up or stop()."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0 and not self._stop.is_set():
            self.systemd.ping()
            try:
                await asyncio.wait_for(self._stop.wait(), min(left, self.systemd.ping_every))
            except TimeoutError:
                pass

    async def _last_flush(self) -> None:
        if not len(self.writer):
            return
        try:
            async with asyncio.timeout(STOP_FLUSH_TIMEOUT):
                if await self.writer.flush():
                    return
        except TimeoutError:
            pass
        log.error("exiting with %d points not written", len(self.writer))


async def serve(config: Config) -> int:
    service = Service(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, service.stop)
    try:
        await service.run()
    except health.RecoveryExhausted as e:
        log.error("%s: exiting, for systemd to restart the service", e)
        return 1
    log.info("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solar-telemetry", description=__doc__.split("\n")[0])
    parser.add_argument("config", help="the config file (TOML)")
    args = parser.parse_args(argv)
    # No timestamps: the journal adds its own.
    handler = logging.StreamHandler()
    formatter = JournalFormatter if to_journal(handler.stream) else logging.Formatter
    handler.setFormatter(formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        config = load_config(args.config)
    except ConfigError as e:
        log.error("config: %s", e)
        return 2
    return asyncio.run(serve(config))


if __name__ == "__main__":
    sys.exit(main())
