# Data

What the service writes to InfluxDB: two measurements, one tag, and the fields of each, with example queries. The optional weather job writes two more.

| measurement | one point per | holds |
|---|---|---|
| [`charge_controller`](#charge_controller) | good reading | the controller's readings |
| [`collector`](#collector) | attempt, good or failed | how the attempt went |
| [`weather`](#weather) | 15 minutes | sunlight and cloud cover, from the weather job |
| [`weather_daily`](#weather_daily) | local day | sunshine and sunlight per day, with a forecast, from the weather job |

Both go to one database, `solar` by default (the `database` setting), which the service creates on its first write with InfluxDB's default retention policy: kept forever. At one reading every 5 minutes that's about 105,000 points a year per measurement, a few MB. Points are timestamped when the reading was taken, in seconds.

## The Tag

Every point has one tag, `controller`: the `name` setting from the config. A second controller, read by a second service, is one more tag value.

## charge_controller

One point per good reading. Each field's name carries its unit. All of them come from one read of the controller's registers 0x0100 to 0x0122.

| field | type | what |
|---|---|---|
| `battery_v` | float | battery voltage |
| `battery_charge_a` | float | charging current into the battery; the controller reports no discharge current |
| `battery_soc_pct` | int | state of charge, the controller's estimate: a rough guide, not for alerts |
| `pv_v`, `pv_a` | float | solar panel voltage and current |
| `pv_w` | int | solar charging power, as the controller reports it |
| `load_v`, `load_a` | float | load output voltage and current |
| `load_w` | int | load output power |
| `load_on` | bool | whether the load output is switched on |
| `controller_temp_c` | int | the controller's temperature |
| `charging_state` | int | 0 deactivated, 1 activated, 2 mppt, 3 equalizing, 4 boost, 5 floating, 6 current limiting |
| `faults` | int | fault and warning bits; 0 is none. See [Faults](#faults) |
| `pv_total_wh` | int | solar energy generated, lifetime |
| `load_total_wh` | int | energy drawn by the load, lifetime |
| `charge_total_ah`, `discharge_total_ah` | int | charge into and out of the battery, lifetime |
| `operating_days` | int | days the controller has run |
| `over_discharges` | int | times the battery was over-discharged, by the controller's measure, lifetime |
| `full_charges` | int | times the battery reached full charge, lifetime |

**Not stored:** the battery temperature (it needs a sensor that isn't fitted, and reads 0 without one); the controller's "today" figures (its daily minimum and maximum voltage, Ah and Wh today), since they reset at an unknown time and contradict each other; the model, which is logged when the service starts. Daily figures come from the readings and the lifetime totals instead: see [Daily Energy](#daily-energy).

The lifetime energy totals are in Wh: Renogy's document says "kWh/10000", but the values only make sense as Wh.

### Faults

`faults` is the controller's 32-bit fault word, stored raw:

| bit | fault |
|---|---|
| 30 | charge MOSFET short |
| 29 | anti-reverse MOSFET short |
| 28 | solar panel reversed |
| 27 | solar panel working-point over-voltage |
| 26 | solar panel counter-current |
| 25 | solar input over-voltage |
| 24 | solar input short |
| 23 | solar input overpower |
| 22 | ambient temperature too high |
| 21 | controller temperature too high |
| 20 | load overpower or over-current |
| 19 | load short |
| 18, 17 | battery under-voltage and over-voltage warnings (Renogy's document gives them both ways round) |
| 16 | battery over-discharge |

The other bits are reserved.

## collector

One point per attempt to read the controller, whether it worked or not. A cycle that recovers from a failure writes several, seconds apart.

| field | type | what |
|---|---|---|
| `outcome` | string | how the attempt ended. See [Outcomes](#outcomes) |
| `find_s` | float | seconds to find the controller |
| `connect_s` | float | seconds to connect |
| `rssi_dbm` | int | the controller's Bluetooth signal strength at the Pi, in dBm, from the advertisement that found it. Missing when it wasn't found. Around -86 at the car; the closer to 0, the stronger |
| `read_s` | float | seconds to read the registers (the first reading after a start also reads the model) |
| `recovery_step` | int | the recovery step this attempt's failure led to: 0 none, 1 and 2 retry, 3 power-cycle the Bluetooth adapter, 4 restart `bluetooth.service`, 5 reset the Bluetooth adapter (rebind its driver), 6 exit. Before 2026-10-22, 5 was exit |
| `throttled` | int | `vcgencmd get_throttled`: 0 is fine; anything else is undervoltage or heat. Missing where it can't be read |
| `queued` | int | points earlier cycles couldn't write to InfluxDB, still queued when this cycle began. 0 while writes work; it climbs while InfluxDB is down, and the cycle after it's back is the last to show it |

The timings are there only for the steps that finished: a failed connect has `find_s` and `rssi_dbm`, and no `connect_s`.

### Outcomes

| outcome | meaning | recovery |
|---|---|---|
| `ok` | a good reading | none; resets the ladder |
| `not_found` | the controller isn't in range, but other devices are: the car is away | none; the next try comes after the away interval |
| `bluez_busy` | BlueZ refused with "in progress" | straight to step 4, restarting `bluetooth.service`; then step 5, resetting the adapter, if it happens again |
| `connect_failed` | found, but connecting failed, or the link dropped | the next step |
| `read_timeout` | no complete reply in time | the next step |
| `bad_reply` | a reply that isn't a valid Modbus reply | the next step |
| `adapter_error` | the scan failed, or saw no devices at all | the next step |

How the steps work is in [Operations: Recovery](operations.md#recovery).

## weather

Written by the weather job, `solar-telemetry-weather`, when the config has a `[weather]` location ([ADR 005](decisions/005-weather-from-open-meteo.md)). The values are [Open-Meteo](https://open-meteo.com)'s (CC BY 4.0): weather-model estimates for the spot, not measurements at the panel. One point per 15 minutes, timestamped at the end of its 15 minutes, only for those already past. Each fetch rewrites the last day's points, with Open-Meteo's revisions. The tag is the same `controller` as the readings'.

| field | type | what |
|---|---|---|
| `radiation_w_m2` | float | sunlight on a flat surface (global horizontal irradiance), the mean over the 15 minutes, in W/m² |
| `cloud_cover_pct` | int | cloud cover |
| `sunshine_s` | int | seconds of sunshine in the 15 minutes |

A value Open-Meteo has no data for is left out.

## weather_daily

Also from the weather job: one point per local day (the zone from the location), timestamped at the day's local midnight, for the last two days, today and the next three. Today's and the coming days' are a forecast, rewritten on every fetch until the day is over; the last two days' are revised by Open-Meteo for a while after.

| field | type | what |
|---|---|---|
| `sunshine_h` | float | hours of sunshine |
| `radiation_kwh_m2` | float | the day's sunlight on a flat surface, in kWh/m² |
| `cloud_cover_pct` | int | mean cloud cover |

## Example Queries

InfluxQL, for InfluxDB 1.x and Grafana's InfluxDB data source. Add `tz('Europe/Berlin')` (your time zone) to a query grouped by day, or the days are UTC.

The latest reading:

```sql
SELECT * FROM charge_controller ORDER BY time DESC LIMIT 1
```

Battery voltage, hourly, over the last week:

```sql
SELECT mean(battery_v), min(battery_v), max(battery_v) FROM charge_controller
WHERE time > now() - 7d GROUP BY time(1h)
```

### Daily Energy

The day's rise in a lifetime total is more accurate than adding up 5-minute power samples:

```sql
SELECT spread(pv_total_wh) AS generated_wh, spread(load_total_wh) AS used_wh FROM charge_controller
WHERE time > now() - 30d GROUP BY time(1d)
```

### How the Collector Is Doing

When the car was away, by day:

```sql
SELECT count(outcome) FROM collector WHERE outcome = 'not_found' AND time > now() - 30d GROUP BY time(1d)
```

Connect times:

```sql
SELECT mean(connect_s), max(connect_s) FROM collector WHERE time > now() - 7d GROUP BY time(1h)
```

Each connect beside the signal it had:

```sql
SELECT outcome, rssi_dbm, connect_s FROM collector WHERE time > now() - 1d
```

PV power beside the sunlight available, hourly (the weather job):

```sql
SELECT mean(pv_w) FROM charge_controller WHERE time > now() - 1d GROUP BY time(1h);
SELECT mean(radiation_w_m2) FROM weather WHERE time > now() - 1d GROUP BY time(1h)
```

Tomorrow's sunshine, from the forecast (the one local midnight in the next 24 hours):

```sql
SELECT sunshine_h FROM weather_daily WHERE time > now() AND time < now() + 1d
```

Every recovery step taken:

```sql
SELECT outcome, recovery_step, throttled FROM collector WHERE recovery_step > 0
```
