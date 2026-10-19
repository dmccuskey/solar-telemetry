"""Writing points to InfluxDB 1.x, through a bounded queue (ADR 002).

Every point is queued, as a line of line protocol, and the queue is written
oldest first. When InfluxDB is down the points wait, with their original
timestamps, and go out once it's back. When the queue is full the oldest are
dropped, and the drop is logged. If writes keep failing, InfluxDB is
restarted, once per outage.

InfluxDB runs on the same Pi, so writes use plain HTTP to localhost, with no
authentication, and second precision.
"""
import asyncio
import collections
import dataclasses
import logging
import math
import urllib.error
import urllib.parse
import urllib.request

from solar_telemetry import health

log = logging.getLogger(__name__)

URL = "http://127.0.0.1:8086"
DATABASE = "solar"
CAPACITY = 2 * 7 * 24 * 12  # a week, at two points per 5-minute cycle
BATCH = 1000  # points per request
TIMEOUT = 10.0
RESTART_AFTER = 3  # failed flushes in a row before InfluxDB is restarted

RESTART_INFLUXDB = ("sudo", "-n", "systemctl", "restart", "influxdb")

Value = float | int | bool | str


@dataclasses.dataclass(frozen=True)
class Point:
    measurement: str
    tags: dict[str, str]
    fields: dict[str, Value]
    time: int  # seconds since the epoch


def to_line(point: Point) -> str:
    """The point in line protocol. Tags are sorted by key, as InfluxDB prefers.
    Raises ValueError for a point InfluxDB would refuse: no fields, an empty
    tag value, or a float that isn't finite."""
    if not point.fields:
        raise ValueError(f"{point.measurement}: no fields")
    key = _escape(point.measurement, ", ")
    for name, value in sorted(point.tags.items()):
        if not value:
            raise ValueError(f"{point.measurement}: tag {name} is empty")
        key += f",{_escape(name, ',= ')}={_escape(value, ',= ')}"
    fields = ",".join(f"{_escape(name, ',= ')}={_field(value)}" for name, value in point.fields.items())
    return f"{key} {fields} {point.time}"


def _escape(text: str, special: str) -> str:
    text = text.replace("\\", "\\\\")
    for char in special:
        text = text.replace(char, "\\" + char)
    return text


def _field(value: Value) -> str:
    match value:
        case bool():  # before int: a bool is an int
            return "true" if value else "false"
        case int():
            return f"{value}i"
        case float() if math.isfinite(value):
            return repr(value)
        case float():
            raise ValueError(f"not a finite float: {value}")
        case str():
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise ValueError(f"not a field value: {value!r}")


class Writer:
    """The queue, and writing it to one database."""

    def __init__(
        self, url: str = URL, database: str = DATABASE, capacity: int = CAPACITY, restart_after: int | None = RESTART_AFTER
    ):
        self.url = url
        self.database = database
        self.queue = collections.deque(maxlen=capacity)
        self.restart_after = restart_after  # failed flushes before restarting InfluxDB; None never
        self.failures = 0  # failed flushes in a row
        self.dropped = 0  # points dropped from a full queue since the last good write
        self.restarted = False  # InfluxDB was restarted in this outage

    def __len__(self) -> int:
        return len(self.queue)

    def add(self, point: Point) -> None:
        """Queue a point. Raises ValueError for one InfluxDB would refuse."""
        line = to_line(point)
        if len(self.queue) == self.queue.maxlen:
            if not self.dropped:
                log.warning("queue full (%d points): dropping the oldest", self.queue.maxlen)
            self.dropped += 1
        self.queue.append(line)

    async def flush(self) -> bool:
        """Write the queue, oldest first, in batches. Returns whether it's empty.

        Points InfluxDB refuses (400) are dropped and logged: retrying them
        would block the queue. A missing database is created. Any other
        failure keeps the points for the next flush. Not to run alongside
        add(): a drop from a full queue would shift the batch being written.
        """
        while self.queue:
            batch = [self.queue[i] for i in range(min(BATCH, len(self.queue)))]
            status, text = await self._write(batch)
            if status == 404 and "database not found" in text:
                log.info("creating database %s", self.database)
                status, text = await self._query(f'CREATE DATABASE "{self.database}"')
                if status == 200:
                    status, text = await self._write(batch)
            if status not in (204, 400):
                await self._failed(status, text)
                return False
            for _ in batch:
                self.queue.popleft()
            if status == 400:  # a partial write: the valid points were written
                log.error("InfluxDB refused points in a batch of %d: %s", len(batch), text)
            else:
                self._succeeded(len(batch))
        return True

    def _succeeded(self, written: int) -> None:
        if self.failures:
            log.info("InfluxDB writes work again after %d failed: %d points written", self.failures, written)
        if self.dropped:
            log.warning("%d points were dropped from the full queue", self.dropped)
        self.failures = 0
        self.dropped = 0
        self.restarted = False

    async def _failed(self, status: int | None, text: str) -> None:
        self.failures += 1
        message = f"InfluxDB write failed ({status or 'no reply'}: {text}), {len(self.queue)} points queued"
        if self.failures == 1:
            log.warning(message)
        else:
            log.debug(message)
        if self.restart_after is not None and self.failures >= self.restart_after and not self.restarted:
            log.warning("restarting InfluxDB after %d failed writes", self.failures)
            await health.run(*RESTART_INFLUXDB)
            self.restarted = True

    async def _write(self, lines: list[str]) -> tuple[int | None, str]:
        params = {"db": self.database, "precision": "s"}
        return await _post(f"{self.url}/write", params, "\n".join(lines).encode())

    async def _query(self, query: str) -> tuple[int | None, str]:
        return await _post(f"{self.url}/query", {"q": query}, b"")


async def _post(url: str, params: dict[str, str], body: bytes) -> tuple[int | None, str]:
    """POST and return the status and the reply's text, or None and the error
    when there was no reply."""
    return await asyncio.to_thread(_post_blocking, f"{url}?{urllib.parse.urlencode(params)}", body)


def _post_blocking(url: str, body: bytes) -> tuple[int | None, str]:
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            return reply.status, reply.read().decode(errors="replace").strip()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace").strip()
    except OSError as e:  # refused, timed out, or no route
        return None, str(getattr(e, "reason", e))
