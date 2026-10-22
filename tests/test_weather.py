"""The weather job: Open-Meteo's reply into points, and a fetch, with a fake Open-Meteo and InfluxDB."""
import asyncio
import io
import json
import logging
import urllib.error

import pytest

from solar_telemetry import influx, weather
from solar_telemetry.service import Config, ConfigError
from solar_telemetry.weather import Job, WeatherError

TAGS = {"controller": "car"}
Q = 900
T = 1_792_425_600  # a quarter's end
MIDNIGHT = 1_792_389_600  # local midnight, the day T is in
DAY = 86_400

# Shaped like Open-Meteo's reply, cut short: three quarters, the last still in progress at NOW.
REPLY = {
    "latitude": 0.0, "longitude": 0.0, "timezone": "America/Denver", "utc_offset_seconds": -21600,
    "minutely_15": {
        "time": [T - Q, T, T + Q],
        "shortwave_radiation": [332.0, 336.0, 337.0],
        "cloud_cover": [66, 74, 70],
        "sunshine_duration": [900.0, 412.5, 900.0],
        "precipitation": [0.0, 0.3, 0.1],
        "snowfall": [0.0, 0.0, 0.0],
        "relative_humidity_2m": [89, 97, 98],
    },
    "daily": {
        "time": [MIDNIGHT - DAY, MIDNIGHT, MIDNIGHT + DAY],
        "sunshine_duration": [28366.09, 33257.02, 17625.64],
        "shortwave_radiation_sum": [14.25, 13.6, 4.53],
        "cloud_cover_mean": [44, 52, 97],
        "precipitation_sum": [0.0, 11.2, 45.5],
        "precipitation_hours": [0.0, 13.0, 17.0],
        "snowfall_sum": [0.0, 0.0, 0.7],
    },
}
NOW = T + 300


def config(**settings):
    return Config("car", "AA:BB:CC:DD:EE:FF", **({"latitude": 1.5, "longitude": -2.5} | settings))


# The request


def test_the_query_asks_for_a_day_of_quarters_and_six_days():
    q = weather.query(1.5, -2.5)
    assert q["latitude"] == "1.5" and q["longitude"] == "-2.5"
    assert q["minutely_15"] == (
        "shortwave_radiation,cloud_cover,sunshine_duration,precipitation,snowfall,relative_humidity_2m"
    )
    assert q["daily"] == (
        "sunshine_duration,shortwave_radiation_sum,cloud_cover_mean,precipitation_sum,precipitation_hours,snowfall_sum"
    )
    assert (q["past_minutely_15"], q["past_days"], q["forecast_days"]) == ("96", "2", "4")
    assert (q["timezone"], q["timeformat"]) == ("auto", "unixtime")


def test_a_backfill_asks_for_more_days():
    q = weather.query(1.5, -2.5, days=3)
    assert (q["past_minutely_15"], q["past_days"]) == ("288", "3")


# The reply


def test_past_quarters_and_every_day_become_points():
    points = weather.parse(REPLY, TAGS, NOW)
    wet = {"precipitation_mm": 0.3, "snowfall_cm": 0.0, "humidity_pct": 97}
    assert points == [
        influx.Point("weather", TAGS, {"radiation_w_m2": 332.0, "cloud_cover_pct": 66, "sunshine_s": 900,
                                       "precipitation_mm": 0.0, "snowfall_cm": 0.0, "humidity_pct": 89}, T - Q),
        influx.Point("weather", TAGS, {"radiation_w_m2": 336.0, "cloud_cover_pct": 74, "sunshine_s": 412} | wet, T),
        influx.Point("weather_daily", TAGS, {"sunshine_h": 7.88, "radiation_kwh_m2": 3.958, "cloud_cover_pct": 44,
                                             "precipitation_mm": 0.0, "precipitation_h": 0.0, "snowfall_cm": 0.0},
                     MIDNIGHT - DAY),
        influx.Point("weather_daily", TAGS, {"sunshine_h": 9.24, "radiation_kwh_m2": 3.778, "cloud_cover_pct": 52,
                                             "precipitation_mm": 11.2, "precipitation_h": 13.0, "snowfall_cm": 0.0},
                     MIDNIGHT),
        influx.Point("weather_daily", TAGS, {"sunshine_h": 4.9, "radiation_kwh_m2": 1.258, "cloud_cover_pct": 97,
                                             "precipitation_mm": 45.5, "precipitation_h": 17.0, "snowfall_cm": 0.7},
                     MIDNIGHT + DAY),
    ]


def test_the_points_are_valid_line_protocol():
    lines = [influx.to_line(p) for p in weather.parse(REPLY, TAGS, NOW)]
    assert lines[1] == (
        "weather,controller=car radiation_w_m2=336.0,cloud_cover_pct=74i,sunshine_s=412i,"
        f"precipitation_mm=0.3,snowfall_cm=0.0,humidity_pct=97i {T}"
    )


def test_missing_values_are_left_out():
    reply = json.loads(json.dumps(REPLY))
    reply["minutely_15"]["cloud_cover"] = [None, 74, None]
    reply["minutely_15"]["shortwave_radiation"][0] = None
    reply["minutely_15"]["sunshine_duration"][0] = None
    for key in ("precipitation", "snowfall", "relative_humidity_2m"):
        reply["minutely_15"][key][0] = None
    points = weather.parse(reply, TAGS, NOW)
    assert [p.time for p in points if p.measurement == "weather"] == [T]  # the first had nothing


@pytest.mark.parametrize("reply", [
    {},
    {"minutely_15": {"time": [T]}, "daily": REPLY["daily"]},
    {"minutely_15": REPLY["minutely_15"], "daily": {"time": [MIDNIGHT], "sunshine_duration": ["x"]}},
    {"minutely_15": REPLY["minutely_15"], "daily": None},
])
def test_a_reply_that_doesnt_parse(reply):
    with pytest.raises(WeatherError, match="unexpected reply"):
        weather.parse(reply, TAGS, NOW)


# Fetching


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_get_reads_the_json(monkeypatch):
    monkeypatch.setattr(weather.urllib.request, "urlopen", lambda url, timeout: Reply(json.dumps(REPLY).encode()))
    assert weather._get("https://example") == REPLY


def test_get_gives_open_meteos_reason(monkeypatch):
    def refuse(url, timeout):
        body = io.BytesIO(b'{"error": true, "reason": "Latitude must be in range of -90 to 90"}')
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, body)

    monkeypatch.setattr(weather.urllib.request, "urlopen", refuse)
    with pytest.raises(WeatherError, match=r"refused the request \(400\): Latitude must be"):
        weather._get("https://example")


def test_get_without_a_network(monkeypatch):
    def unreachable(url, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(weather.urllib.request, "urlopen", unreachable)
    with pytest.raises(WeatherError, match="not reached: no route to host"):
        weather._get("https://example")


def test_get_with_a_reply_that_isnt_json(monkeypatch):
    monkeypatch.setattr(weather.urllib.request, "urlopen", lambda url, timeout: Reply(b"<html>"))
    with pytest.raises(WeatherError, match="isn't JSON"):
        weather._get("https://example")


# The job


class Writer:
    def __init__(self):
        self.points = []
        self.flushes = 0

    def add(self, point):
        influx.to_line(point)
        self.points.append(point)

    async def flush(self):
        self.flushes += 1
        return True


@pytest.fixture
def open_meteo(monkeypatch):
    asked = []

    async def fetch(latitude, longitude, days=1):
        asked.append((latitude, longitude, days))
        if isinstance(open_meteo_reply[0], Exception):
            raise open_meteo_reply[0]
        return open_meteo_reply[0]

    open_meteo_reply = [REPLY]
    monkeypatch.setattr(weather, "fetch", fetch)
    monkeypatch.setattr(weather.time, "time", lambda: NOW)
    return asked, open_meteo_reply


def test_a_fetch_writes_its_points(open_meteo, caplog):
    caplog.set_level(logging.INFO)
    asked, _ = open_meteo
    writer = Writer()
    assert asyncio.run(Job(config(), writer).once(days=3))
    assert asked == [(1.5, -2.5, 3)]
    assert len(writer.points) == 5 and writer.flushes == 1
    assert {p.tags["controller"] for p in writer.points} == {"car"}
    assert "2 quarters, 3 days; now 336.0 W/m², 74 % cloud, 0.3 mm; today 9.24 h of sunshine, 11.2 mm" in caplog.text


def test_a_failed_fetch_is_logged_and_writes_nothing_new(open_meteo, caplog):
    _, reply = open_meteo
    reply[0] = WeatherError("Open-Meteo not reached: timed out")
    writer = Writer()
    assert not asyncio.run(Job(config(), writer).once())
    assert writer.points == [] and writer.flushes == 1  # what's queued from before still goes out
    assert "not reached: timed out" in caplog.text


def test_run_fetches_at_once_then_stops(open_meteo):
    asked, _ = open_meteo
    job = Job(config(weather_refresh=900.0), Writer())

    async def go():
        task = asyncio.ensure_future(job.run(days=2))
        await asyncio.sleep(0.05)
        job.stop()
        await asyncio.wait_for(task, 1)

    asyncio.run(go())
    assert asked == [(1.5, -2.5, 2)]


def test_the_job_needs_a_location():
    with pytest.raises(ConfigError, match="latitude and longitude"):
        Job(Config("car", "AA:BB:CC:DD:EE:FF"), Writer())


def test_the_job_never_restarts_influxdb():
    assert Job(config()).writer.restart_after is None


def test_main_exits_2_without_a_location(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[controller]\nname = "car"\naddress = "AA:BB:CC:DD:EE:FF"\n')
    assert weather.main([str(path)]) == 2
