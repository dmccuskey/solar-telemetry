# ADR 005: Weather from Open-Meteo, in a Job of Its Own

**Status:** Accepted (2026-10-19); precipitation, snowfall and humidity added 2026-10-22 (see [Amendment](#amendment-2026-10-22-precipitation-and-humidity))

## Context

The readings say what the panel made, not what it could have made. A day of 2 W at noon is clouds, or dirt, shade or a failing panel, and the readings alone can't tell which. Weather for the panel's location, beside the readings, can: low power under a low sun is the weather; low power under a high sun is worth a look.

What's wanted is sunlight (irradiance, W/m²) and cloud cover, at a resolution near the readings' 5 minutes, for the past and the next few days, for any location, free, with no account.

- **Open-Meteo** gives all of it: 15-minute values (from national weather models, such as NOAA's HRRR in North America), daily sunshine and radiation sums, a forecast, and a backfill of past days, with no key, free for non-commercial use, under CC BY 4.0.
- **The US National Weather Service** API is free and keyless, but covers the US only and gives cloud cover, not irradiance.
- **A sensor at the panel** measures what the panel actually gets, but is hardware to buy, mount and read.
- **Other weather APIs** (OpenWeather, Solcast) need an account and a key, and their free tiers limit history or resolution.

The numbers are model estimates for the spot, not measurements: good enough to tell a cloudy day from a sunny one, and a sunny hour from an overcast one. Open-Meteo revises them for a day or two after the fact.

Where to fetch it: the service already has a config, an InfluxDB writer and a schedule, but its job is the Bluetooth side, which ADR 002 keeps free of anything that can hang or fail for other reasons. A call over the internet can do both.

## Decision

- **Weather comes from Open-Meteo:** every 15 minutes, sunlight on a flat surface, cloud cover and sunshine; per local day, sunshine hours, radiation and mean cloud cover, for the last two days, today and the next three. Two measurements, `weather` and `weather_daily`, in the same database, with the same `controller` tag ([Data](../data.md#weather)).
- **A job of its own,** `solar-telemetry-weather`, a second small long-running service. It shares the package, the config file and the InfluxDB writer, but not the process: a slow or failing fetch can't delay or crash a reading.
- **How often is a setting,** `[weather] refresh_s`, 15 minutes by default, at the marks on the clock plus a minute. Open-Meteo's values are 15-minute ones, so a shorter refresh gets each new value sooner, not finer ones.
- **Every fetch rewrites the last day,** so revisions and the latest forecast replace the old values, and a failed fetch's gap is filled by the next. The forecast as it was isn't kept.
- **It never restarts InfluxDB.** The service does that (ADR 002); two processes doing it would restart it twice.
- **Flat-surface radiation,** not radiation on the panel's tilt. On a car, as here, the panel is flat; a tilted panel could use Open-Meteo's tilted irradiance later, with its tilt and direction in the config.

## Consequences

- The dashboard can set PV power against the sun available, and a day's PV energy against the day's sunlight: a yield that should stay level whatever the weather, and drops when something is wrong at the panel.
- The Pi needs internet access for the weather, but nothing else depends on it: without it, the readings go on and the weather has a gap until the fetch works again (up to a day back is filled in).
- The location goes in the config, on the Pi; it's a home or a parking spot, so it stays out of anything shared.
- A second unit to install and watch, though it needs no sudoers rules and no Bluetooth.
- Open-Meteo is one provider: if it changes its API or terms, the job's request and parsing change, not the measurements.

## Amendment 2026-10-22: Precipitation and Humidity

Connects slowed from about 1.2 s to 1.4–1.7 s when days of rain set in, with the car and the Pi unmoved: water absorbs and scatters 2.4 GHz. Snow on the panel would stop it under a clear sky. Both can't be read from cloud cover. So the job also fetches, from the same request: every 15 minutes, precipitation (mm), snowfall (cm) and relative humidity; per day, precipitation (mm), hours of precipitation and snowfall (cm). New fields only ([Data](../data.md#weather)).
