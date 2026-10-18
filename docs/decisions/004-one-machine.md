# ADR 004: InfluxDB Beside the Collector, Grafana Best on the Same Machine

**Status:** Accepted (recorded 2026-10-18, for choices made in January 2026)

## Context

The collector needs somewhere to keep its readings and something to graph them with. When it was first set up in January 2026 the choices were:

- **Where to store:** on the Pi that reads the controller, or on another machine.
- **What to store in:** a time-series database (InfluxDB was the usual pick; SQLite also works), or Home Assistant's own history.
- **What to graph with:** Grafana, or Home Assistant's dashboards. Renogy's phone app shows the current values but no history, which is why the project exists.

Two things shaped the answer:

- **Collecting must not depend on anything else.** The readings are the record. If the network, or a second machine, is down, readings taken meanwhile should still be kept, with their own timestamps.
- **The hardware at hand.** The setup it was built on has two Pis: a Pi 3, free to be placed in a window within Bluetooth range of the car, and a Pi 4, always on, on Ethernet beside the router, already running other jobs. A Pi 3 runs the collector and InfluxDB 1.x with room to spare; Grafana wants more than it has, and the Pi 4 had it.

Home Assistant was considered and left out. It's built around devices and automations; graphs are one feature of many. Its history lives in Home Assistant itself, so readings would depend on it running, and there's no writing back readings taken while it was down. The graphs wanted here (daily min and max, energy per day, mixed units on two axes) are what Grafana does.

InfluxDB 1.x fits the data: points are timestamped, a new field needs no schema change, queries group by time (`GROUP BY time(1d)`), and Grafana has a data source for it. The 1.x line, with InfluxQL, runs well on a Pi 3; 2.x and 3.x need more and change the query language. Raspberry Pi OS ships 1.6, which does everything the collector needs; 1.8 is the last of the line, and an upgrade within 1.x needs no change here.

## Decision

- **InfluxDB 1.x runs on the same machine as the collector,** as `influxdb.service`, and the collector writes to it at `127.0.0.1`. The collector restarts it when writes keep failing ([ADR 002](002-long-running-service.md)), which it can only do because it's local.
- **Grafana reads that InfluxDB,** over the network if it runs elsewhere.
- **The recommended setup is one machine for everything,** within Bluetooth range of the controller: the collector, InfluxDB and Grafana. A Pi 4 or bigger (a mini PC, a Mac mini) runs all three. That suits the setup this is most likely to be used for: a fixed install, with the machine near the controller in an RV, a cabin or a house.
- **Splitting it is fine** where the machine in range is small or has to sit somewhere else: the collector and InfluxDB on the machine near the controller, Grafana on another. That's how ours runs, because the car is parked out of reach of the server by the router: a Pi 3 in a window collects, and a Pi 4 runs Grafana.
- **No Home Assistant** in the project. Anyone who wants it can read the same InfluxDB.

## Consequences

- Readings are kept while the network or the Grafana machine is down; only the collector's own machine has to be up. When InfluxDB itself is down, the collector's queue holds the readings ([Operations](../operations.md#when-influxdb-is-down)).
- One machine means one thing to install, update and power, and Grafana talks to InfluxDB at `localhost`, with none of the name-resolution trouble of reaching another machine ([Installation](../installation.md#8-graph-it-in-grafana)).
- Split across two machines, the dashboard depends on the network between them, and on the collector's Pi being reachable: its Wi-Fi settings matter ([Installation](../installation.md#prepare-the-pi)).
- InfluxDB 1.x is old. If it stops being packaged, moving to 2.x or later means new dashboard queries and changes to how the collector writes (2.x takes 1.x-style writes only with authentication).
- No automations or alerts come with it. Grafana has alerting, if needed.
