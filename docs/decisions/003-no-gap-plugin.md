# ADR 003: bluetoothd Without Its GAP Plugin

**Status:** Accepted

## Context

The service's connects alternated, cycle after cycle: about 1.3 s, then about 11 s, then 1.3 s again. The old collector did the same (in January its whole runs alternated, about 10 s and 20 s). Reads followed the same pattern (0.3–0.6 s after a fast connect, 0.8–1.0 s after a slow one); finding the device didn't.

An HCI trace (`btmon`, three cycles, 2026-10-18) showed where the time went:

- **The link comes up in about 0.1 s either way.** What differs is the connection interval it starts with: 18.75 ms on a fast cycle, 198.75 ms on a slow one.
- **GATT discovery is what takes the time.** The controller isn't paired, so BlueZ forgets it 30 s after each disconnect and keeps no GATT cache for it: every connect runs a full discovery, about 25 request-reply round trips of two intervals each. That is about 1 s at 18.75 ms and 10 s at 198.75 ms. bleak's `connect()` waits for discovery to finish, so all of it lands in `connect_s`.
- **The kernel connects with the parameters it has stored for the device, and two parties overwrite them in turn:**
  1. **bluetoothd's GAP plugin** reads the BT module's *Peripheral Preferred Connection Parameters* characteristic (0x2a04) during discovery, which asks for 100–200 ms, and loads it into the kernel (`MGMT Load Connection Parameters`). The kernel stores it for the next connect and slows the current link to 198.75 ms.
  2. **The module itself,** some 10 s into a connection, sends an L2CAP *Connection Parameter Update Request* for 10–20 ms. The kernel accepts it, stores it, and speeds up the link.

So a fast cycle stores 100–200 ms and ends in about 4 s, before the module's request comes; the next cycle starts slow, its discovery takes 10 s, the module's request arrives in that time and stores 10–20 ms again; the next is fast. A retry after a failure comes within seconds, before either has run, which is why retries connect fast and the alternation picks up again after them.

The slow side cost 10 s more radio time every other cycle, still inside the 20 s connect timeout. Two of the three failed connects on 2026-10-18 came on the slow side, too few to say whether slow connects fail more often.

The options:

1. **Start bluetoothd without its GAP plugin** (`--noplugin=gap`). The plugin reads the device's name, appearance and preferred connection parameters. The service needs none of them: it finds the controller by address, and the name comes from advertising anyway.
2. **Set the connection parameters from our code.** Not possible: bleak's BlueZ backend has no way to set them.
3. **Leave it.** It works, at 10 s more radio time on every other cycle.

## Decision

bluetoothd runs without its GAP plugin, through a systemd drop-in for `bluetooth.service`, `systemd/bluetooth-noplugin-gap.conf` in this repository, installed as `/etc/systemd/system/bluetooth.service.d/noplugin-gap.conf`:

```ini
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=gap
```

It's part of installation ([Installation](../installation.md#5-keep-connects-fast)). The journal confirms it at bluetoothd's start: `Excluding (cli) gap`.

A trial under `btmon` (2026-10-18, four cycles) showed it working: every connection, five in all, came up at 18.75 ms and stayed there. bluetoothd still sees the 0x2a04 characteristic during discovery but no longer reads it or loads anything into the kernel. `connect_s` was 1.11, 1.38, 0.61 (a retry) and 1.15 s.

## Consequences

- Every connect takes about 1.3 s, and the read after it about 0.4 s: some 10 s less radio time on every other cycle.
- It changes BlueZ for the whole Pi, not just for this service: no Bluetooth device on it gets its name, appearance or preferred parameters read by the GAP plugin. On a Pi that does nothing else with Bluetooth that costs nothing; on one that does, the drop-in may have to be left out.
- It's an install step outside the service, and easy to miss on a new Pi. The symptom is the alternation itself, `connect_s` near 11 s on every other cycle, which [Operations](../operations.md) names.
- The drop-in replaces the unit's `ExecStart` and so carries bluetoothd's path (`/usr/libexec/bluetooth/bluetoothd` on Raspberry Pi OS). A distribution that installs it elsewhere needs the path changed, and a BlueZ upgrade that changes the unit's own `ExecStart` options doesn't reach us.
- The module's own request (10–20 ms) still comes about 10 s into a connection; since the link is already at that interval, it changes nothing.
- Undoing it: with the service stopped, remove the drop-in, `systemctl daemon-reload` and restart `bluetooth`.
