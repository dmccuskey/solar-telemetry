"""The weather job: Open-Meteo's sunlight and cloud cover beside the readings (ADR 005).

At each mark of `[weather] refresh_s` on the clock, plus a minute, and once at
its start, it fetches the weather for the config's latitude and longitude from
Open-Meteo and writes two measurements, tagged like the readings:

- `weather`: one point per 15 minutes, for those already past, timestamped at
  the end of its 15 minutes: the last day's.
- `weather_daily`: one point per local day, timestamped at its local midnight:
  the last two days, today, and the next three (a forecast).

Each fetch rewrites the points it got, so Open-Meteo's revisions of the past
and its latest forecast replace the old ones. A failed fetch is logged, and
the next one fills the gap. It runs apart from the service, so the internet
being down never touches the Bluetooth side, and it never restarts InfluxDB:
the service does that.

The data is Open-Meteo's (https://open-meteo.com), under CC BY 4.0: weather
model estimates for the spot, not a measurement at the panel.

Usage: solar-telemetry-weather [--days N] <config.toml>
"""
import argparse
import asyncio
import json
import logging
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from solar_telemetry import influx, service
from solar_telemetry.service import Config, ConfigError

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 30.0
# A minute after each mark: the quarter that just ended is in by then.
MARK_OFFSET = 60.0
QUARTERS_PER_DAY = 96
PAST_DAYS = 2  # daily values before today
FORECAST_DAYS = 4  # daily values from today on: today and the next three

MINUTELY = ("shortwave_radiation", "cloud_cover", "sunshine_duration")
DAILY = ("sunshine_duration", "shortwave_radiation_sum", "cloud_cover_mean")


class WeatherError(Exception):
    """Open-Meteo refused the request, couldn't be reached, or sent a reply that doesn't parse."""


def query(latitude: float, longitude: float, days: int = 1) -> dict[str, str]:
    """The request's parameters: the last `days` of 15-minute values, and the daily ones."""
    return {
        "latitude": f"{latitude:g}",
        "longitude": f"{longitude:g}",
        "minutely_15": ",".join(MINUTELY),
        "past_minutely_15": str(days * QUARTERS_PER_DAY),
        "forecast_minutely_15": "1",
        "daily": ",".join(DAILY),
        "past_days": str(max(days, PAST_DAYS)),
        "forecast_days": str(FORECAST_DAYS),
        "timezone": "auto",  # days are local days, the zone from the coordinates
        "timeformat": "unixtime",
    }


def parse(reply: dict, tags: dict[str, str], now: float) -> list[influx.Point]:
    """The points in an Open-Meteo reply. Values it has no data for (null) are
    left out, and a time with none at all gets no point."""
    try:
        points = []
        minutely = reply["minutely_15"]
        for i, t in enumerate(minutely["time"]):
            if t > now:  # the quarter in progress, or later: a forecast
                continue
            fields = _fields(minutely, i, {
                "radiation_w_m2": ("shortwave_radiation", float),
                "cloud_cover_pct": ("cloud_cover", _whole),
                "sunshine_s": ("sunshine_duration", _whole),
            })
            if fields:
                points.append(influx.Point("weather", tags, fields, int(t)))
        daily = reply["daily"]
        for i, t in enumerate(daily["time"]):
            fields = _fields(daily, i, {
                "sunshine_h": ("sunshine_duration", lambda s: round(s / 3600, 2)),
                "radiation_kwh_m2": ("shortwave_radiation_sum", lambda mj: round(mj / 3.6, 3)),
                "cloud_cover_pct": ("cloud_cover_mean", _whole),
            })
            if fields:
                points.append(influx.Point("weather_daily", tags, fields, int(t)))
        return points
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise WeatherError(f"unexpected reply: {e!r}") from e


def _fields(values: dict, i: int, wanted: dict) -> dict[str, float | int]:
    fields = {}
    for name, (key, convert) in wanted.items():
        value = values[key][i]
        if value is not None:
            fields[name] = convert(value)
    return fields


def _whole(value: float) -> int:
    return int(round(value))


async def fetch(latitude: float, longitude: float, days: int = 1) -> dict:
    url = f"{URL}?{urllib.parse.urlencode(query(latitude, longitude, days))}"
    return await asyncio.to_thread(_get, url)


def _get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as e:
        # Open-Meteo says why in the body: {"error": true, "reason": "..."}
        try:
            reason = json.loads(e.read()).get("reason")
        except (ValueError, AttributeError):
            reason = None
        raise WeatherError(f"Open-Meteo refused the request ({e.code}): {reason or e.reason}") from e
    except OSError as e:  # refused, timed out, or no route
        raise WeatherError(f"Open-Meteo not reached: {getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise WeatherError(f"Open-Meteo's reply isn't JSON: {e}") from e


class Job:
    def __init__(self, config: Config, writer: influx.Writer | None = None):
        if config.latitude is None or config.longitude is None:
            raise ConfigError("weather: needs [weather] latitude and longitude")
        self.config = config
        self.writer = writer or influx.Writer(config.influx_url, config.database, restart_after=None)
        self.tags = {"controller": config.controller}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, days: int = 1) -> None:
        """Fetch at once, for `days` back, then at each mark, until stop()."""
        log.info(
            "weather for %s from Open-Meteo: every %g s; InfluxDB %s, database %s",
            self.config.controller, self.config.weather_refresh, self.config.influx_url, self.config.database,
        )
        while not self._stop.is_set():
            await self.once(days)
            days = 1
            wait = service.until_next_mark(time.time(), self.config.weather_refresh, MARK_OFFSET)
            try:
                await asyncio.wait_for(self._stop.wait(), wait)
            except TimeoutError:
                pass

    async def once(self, days: int = 1) -> bool:
        """One fetch and write. Returns whether the fetch worked; a failed
        write leaves the points queued for the next."""
        try:
            reply = await fetch(self.config.latitude, self.config.longitude, days)
            points = parse(reply, self.tags, time.time())
        except WeatherError as e:
            log.warning("%s", e)
            await self.writer.flush()  # anything left from before
            return False
        for point in points:
            self.writer.add(point)
        await self.writer.flush()
        self._log(points)
        return True

    def _log(self, points: list[influx.Point]) -> None:
        quarters = [p for p in points if p.measurement == "weather"]
        days = [p for p in points if p.measurement == "weather_daily" and p.time <= time.time()]
        latest = quarters[-1].fields if quarters else {}
        today = days[-1].fields if days else {}
        log.info(
            "weather: %d quarters, %d days; now %s W/m², %s %% cloud; today %s h of sunshine",
            len(quarters), len(points) - len(quarters),
            latest.get("radiation_w_m2", "-"), latest.get("cloud_cover_pct", "-"), today.get("sunshine_h", "-"),
        )


async def serve(config: Config, days: int) -> int:
    job = Job(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, job.stop)
    await job.run(days)
    log.info("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solar-telemetry-weather", description=__doc__.split("\n")[0])
    parser.add_argument("config", help="the config file (TOML), with a [weather] section")
    parser.add_argument("--days", type=int, default=1, choices=range(1, 93), metavar="N",
                        help="on the first fetch, the 15-minute values of the last N days (1 to 92; default 1)")
    args = parser.parse_args(argv)
    handler = logging.StreamHandler()
    formatter = service.JournalFormatter if service.to_journal(handler.stream) else logging.Formatter
    handler.setFormatter(formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        config = service.load_config(args.config)
        if config.latitude is None:
            raise ConfigError("weather: needs [weather] latitude and longitude")
    except ConfigError as e:
        log.error("config: %s", e)
        return 2
    return asyncio.run(serve(config, args.days))


if __name__ == "__main__":
    sys.exit(main())
