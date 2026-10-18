# Hardware

What we found about the controller's Bluetooth module, the signal between it and the Pi, and the Pi's power supply, and what to check for each. Which machine to run it all on is in [ADR 004](decisions/004-one-machine.md).

## The BT Module

The controller (a Renogy Wanderer, which reports its model as `RNG-CTRL-WND10`) talks Bluetooth LE through a BT-1 module plugged into its communication port.

- **It advertises as `BT-TH-`** followed by eight hex digits, the last four bytes of its MAC address: `BT-TH-CCDDEEFF` for `AA:BB:CC:DD:EE:FF`. `tools/scan_ble.py` lists it ([Installation](installation.md#3-configure-it)).
- **No pairing.** The module takes connections without a PIN or bonding, so BlueZ forgets it 30 s after each disconnect.
- **One connection at a time.** While the phone app or another tool is connected, the service can't connect ([Operations](operations.md#running-it)).
- **It picks its own connection speed.** Its *Peripheral Preferred Connection Parameters* ask for 100 to 200 ms between packets, but some 10 s into a connection it asks for 10 to 20 ms. How that made every other connect slow, and the fix, are in [ADR 003](decisions/003-no-gap-plugin.md).

Its GATT layout, from `tools/gatt_dump.py` (which, through bleak, doesn't list the standard Generic Access and Generic Attribute services):

| service | characteristic | properties | used for |
|---|---|---|---|
| `180a` Device Information | `2a23` to `2a2a`, `2a50` | read | the standard ones: manufacturer, model, serial number, revisions. Not read by the service |
| `fff0` | `fff1` | read, notify | Modbus replies, as notifications |
| `ffd0` | `ffd1` | read, write, write without response | Modbus requests |
| | `ffd2` | read, notify | unused |
| | `ffd3`, `ffd4`, `ffd5` | write; read; read, write | unused |
| `f000ffd0-0451-4000-b000-000000000000` | `f000ffd1-...` | write, write without response | unused. `0451` is Texas Instruments' vendor ID: likely the chip's own service, such as a firmware update. Nothing should write to it |

The protocol over `ffd1` and `fff1` is Modbus RTU: [ADR 001](decisions/001-own-ble-client.md).

## The Signal

A weak signal shows up as `connect_failed` and `read_timeout` outcomes, and, when the Pi can't hear the module's advertising at all, as `not_found`, which looks like the car being away. Check the signal when setting up, and again after moving the Pi or the controller.

- **In Grafana,** the dashboard's **Signal strength** panel shows `rssi_dbm`, from the advertisement that found the controller on each attempt ([Data: collector](data.md#collector)).
- **On the Pi,** `tools/scan_ble.py` lists every device in range, strongest first. Stop the service first: a scan alongside its connect can collide with it.

  ```bash
  sudo systemctl stop solar-telemetry
  /opt/solar-telemetry/.venv/bin/python /opt/solar-telemetry/tools/scan_ble.py
  sudo systemctl start solar-telemetry
  ```

The closer to 0, the stronger. Ours read reliably at -72 to -86 dBm; at about -96 dBm it still read, with an occasional timeout on the first try. Small moves matter: a foot or so can change it noticeably. The phone app is no guide: a phone reached the module at more than 30 ft, through walls, where the Pi's own antenna may not.

## Power

On a Pi 3, a power supply that sags under load causes undervoltage, and undervoltage shows up as dropped Bluetooth connections and failed reads, not as anything that looks like power. No Bluetooth reset fixes it.

- **Check:** `vcgencmd get_throttled`. `throttled=0x0` is good; anything else is undervoltage or heat, now or since boot. The service logs it with every failure and records it in `throttled` ([Data](data.md#collector)).
- **The label isn't enough.** Of three USB supplies we tried, only one kept a Pi 3 at `0x0`: the one that came with a Pi, with its own cable. A 2.4 A supply and a 3 A supply both caused undervoltage. Use the official supply, or one you've watched at `0x0` for a day of readings.
- **A quiet kernel log doesn't prove the voltage is fine:** an undervoltage that never recovers is logged once, at boot, and then nothing. `get_throttled` is the check.
