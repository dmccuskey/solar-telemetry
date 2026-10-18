"""Line protocol, the queue, and writing it to InfluxDB."""
import asyncio
import http.server
import logging
import threading
import urllib.parse

import pytest

from solar_telemetry import influx
from solar_telemetry.influx import Point, Writer

T = 1_760_700_000


def point(n=0, **fields):
    return Point("charge_controller", {"controller": "car"}, fields or {"battery_v": 13.1}, T + n)


class Server:
    """A fake InfluxDB at `_post`: answers each request with the next of
    `replies` (status, text), and 204 when they run out."""

    def __init__(self):
        self.requests = []
        self.replies = []

    async def post(self, url, params, body):
        self.requests.append((url, params, body.decode()))
        return self.replies.pop(0) if self.replies else (204, "")

    def written(self):
        return [line for url, _, body in self.requests if url.endswith("/write") for line in body.split("\n")]


@pytest.fixture
def server(monkeypatch):
    fake = Server()
    monkeypatch.setattr(influx, "_post", fake.post)
    return fake


@pytest.fixture
def commands(monkeypatch):
    ran = []

    async def run(*command):
        ran.append(command)
        return ""

    monkeypatch.setattr(influx.health, "run", run)
    return ran


def flush(writer):
    return asyncio.run(writer.flush())


# Line protocol


def test_a_reading():
    fields = {"battery_v": 13.1, "battery_soc_pct": 95, "load_on": False, "charging_state": 0}
    assert influx.to_line(point(**fields)) == (
        f"charge_controller,controller=car battery_v=13.1,battery_soc_pct=95i,load_on=false,charging_state=0i {T}"
    )


def test_field_types():
    line = influx.to_line(point(f=13.0, i=-3, t=True, s="not_found", e=1e-05))
    assert line.endswith(f' f=13.0,i=-3i,t=true,s="not_found",e=1e-05 {T}')


def test_tags_are_sorted():
    line = influx.to_line(Point("m", {"b": "2", "a": "1"}, {"v": 1}, T))
    assert line == f"m,a=1,b=2 v=1i {T}"


def test_no_tags():
    assert influx.to_line(Point("m", {}, {"v": 1}, T)) == f"m v=1i {T}"


def test_escaping():
    line = influx.to_line(Point("a b,c", {"t k=": "v, w="}, {"f k": 'say "hi" \\'}, T))
    assert line == f'a\\ b\\,c,t\\ k\\==v\\,\\ w\\= f\\ k="say \\"hi\\" \\\\" {T}'


@pytest.mark.parametrize(
    "bad",
    [
        Point("m", {}, {}, T),
        Point("m", {"controller": ""}, {"v": 1}, T),
        Point("m", {}, {"v": float("nan")}, T),
        Point("m", {}, {"v": float("inf")}, T),
        Point("m", {}, {"v": None}, T),
    ],
)
def test_points_influxdb_refuses(bad):
    with pytest.raises(ValueError):
        influx.to_line(bad)


def test_add_refuses_a_bad_point():
    writer = Writer()
    with pytest.raises(ValueError):
        writer.add(Point("m", {}, {}, T))
    assert len(writer) == 0


# The queue


def test_a_full_queue_drops_the_oldest(caplog):
    writer = Writer(capacity=3)
    for n in range(5):
        writer.add(point(n))
    assert len(writer) == 3
    assert writer.dropped == 2
    assert [line.split()[-1] for line in writer.queue] == [str(T + n) for n in (2, 3, 4)]
    assert caplog.text.count("queue full") == 1


def test_a_good_write_reports_the_drop(server, caplog):
    writer = Writer(capacity=1)
    writer.add(point(0))
    writer.add(point(1))
    assert flush(writer)
    assert writer.dropped == 0
    assert "1 points were dropped" in caplog.text


# Writing


def test_flush_writes_the_queue_in_order(server):
    writer = Writer(url="http://influx:8086", database="solar")
    for n in range(3):
        writer.add(point(n))
    assert flush(writer)
    assert len(writer) == 0
    url, params, _ = server.requests[0]
    assert url == "http://influx:8086/write"
    assert params == {"db": "solar", "precision": "s"}
    assert server.written() == [influx.to_line(point(n)) for n in range(3)]


def test_flush_of_an_empty_queue_sends_nothing(server):
    assert flush(Writer())
    assert server.requests == []


def test_flush_in_batches(server, monkeypatch):
    monkeypatch.setattr(influx, "BATCH", 2)
    writer = Writer()
    for n in range(5):
        writer.add(point(n))
    assert flush(writer)
    assert [body.count("\n") + 1 for _, _, body in server.requests] == [2, 2, 1]
    assert len(server.written()) == 5


def test_a_failed_write_keeps_the_points(server, caplog):
    server.replies = [(None, "Connection refused")]
    writer = Writer()
    writer.add(point())
    assert not flush(writer)
    assert len(writer) == 1
    assert writer.failures == 1
    assert "Connection refused" in caplog.text


def test_a_failed_batch_keeps_it_and_the_rest(server, monkeypatch):
    monkeypatch.setattr(influx, "BATCH", 2)
    server.replies = [(204, ""), (500, "timeout")]
    writer = Writer()
    for n in range(5):
        writer.add(point(n))
    assert not flush(writer)
    assert [line.split()[-1] for line in writer.queue] == [str(T + n) for n in (2, 3, 4)]


def test_the_points_go_out_once_influxdb_is_back(server, caplog):
    caplog.set_level(logging.INFO)
    server.replies = [(None, "refused"), (None, "refused")]
    writer = Writer()
    writer.add(point(0))
    flush(writer)
    writer.add(point(1))
    flush(writer)
    writer.add(point(2))
    assert flush(writer)
    assert writer.failures == 0
    assert server.written()[-3:] == [influx.to_line(point(n)) for n in range(3)]
    assert "work again after 2 failed" in caplog.text


def test_refused_points_are_dropped(server, caplog):
    server.replies = [(400, '{"error":"partial write: field type conflict"}')]
    writer = Writer()
    writer.add(point())
    assert flush(writer)
    assert len(writer) == 0
    assert "field type conflict" in caplog.text


def test_a_missing_database_is_created(server):
    server.replies = [(404, '{"error":"database not found: \\"solar\\""}'), (200, ""), (204, "")]
    writer = Writer(database="solar")
    writer.add(point())
    assert flush(writer)
    assert [url.rsplit("/", 1)[1] for url, _, _ in server.requests] == ["write", "query", "write"]
    assert server.requests[1][1] == {"q": 'CREATE DATABASE "solar"'}


def test_a_failed_create_keeps_the_points(server):
    server.replies = [(404, '{"error":"database not found: \\"solar\\""}'), (None, "refused")]
    writer = Writer()
    writer.add(point())
    assert not flush(writer)
    assert len(writer) == 1


# Restarting InfluxDB


def test_influxdb_is_restarted_once_per_outage(server, commands):
    server.replies = [(None, "refused")] * 5
    writer = Writer()
    writer.add(point())
    for _ in range(influx.RESTART_AFTER - 1):
        flush(writer)
    assert commands == []
    flush(writer)
    assert commands == [influx.RESTART_INFLUXDB]
    flush(writer)
    flush(writer)
    assert commands == [influx.RESTART_INFLUXDB]


def test_a_good_write_rearms_the_restart(server, commands):
    server.replies = [(None, "refused")] * influx.RESTART_AFTER + [(204, "")] + [(None, "refused")] * influx.RESTART_AFTER
    writer = Writer()
    for _ in range(2 * influx.RESTART_AFTER + 1):
        writer.add(point())
        flush(writer)
    assert commands == [influx.RESTART_INFLUXDB] * 2


def test_refused_points_dont_count_toward_a_restart(server, commands):
    server.replies = [(400, "bad")] * (influx.RESTART_AFTER + 1)
    writer = Writer()
    for _ in range(influx.RESTART_AFTER + 1):
        writer.add(point())
        flush(writer)
    assert commands == []


# HTTP, against a real server on localhost


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.received.append((self.path, self.rfile.read(length).decode()))
        status, text = self.server.reply
        self.send_response(status)
        self.end_headers()
        self.wfile.write(text.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.received = []
    server.reply = (204, "")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_post(http_server):
    url = f"http://127.0.0.1:{http_server.server_port}"
    writer = Writer(url=url, database="solar")
    writer.add(point())
    assert flush(writer)
    path, body = http_server.received[0]
    assert urllib.parse.urlsplit(path).path == "/write"
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(path).query) == {"db": ["solar"], "precision": ["s"]}
    assert body == influx.to_line(point())


def test_post_an_error_reply(http_server):
    http_server.reply = (400, '{"error":"unable to parse"}')
    url = f"http://127.0.0.1:{http_server.server_port}/write"
    assert asyncio.run(influx._post(url, {}, b"x")) == (400, '{"error":"unable to parse"}')


def test_post_with_nothing_listening():
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    server.server_close()
    status, text = asyncio.run(influx._post(f"http://127.0.0.1:{port}/write", {}, b"x"))
    assert status is None
    assert "refused" in text.lower()
