#!/usr/bin/env python3
import json, urllib.parse, urllib.request
import ast
import os
import signal
import subprocess
import time


INFLUX_URL = "http://127.0.0.1:8086/write"
DB = "renogy"
MEAS = "wanderer"
INI = "/home/pi/renogy-bt/my_wanderer.ini"
PY = "/home/pi/renogy-bt/.venv/bin/python3"
EXAMPLE = "/home/pi/renogy-bt/example.py"

# Fields you likely care about for curves
FIELDS = [
    "battery_voltage", "battery_current",
    "pv_voltage", "pv_current", "pv_power",
    "load_voltage", "load_current", "load_power",
    "controller_temperature", "battery_temperature",
    "battery_percentage",

    "battery_power_w", "battery_power_abs_w",
    "pv_power_calc_w", "load_power_calc_w",
]

DEBUG = True
BLE_TIMEOUT = 30  # seconds
BLE_RETRIES = 2 # if this changes, need to update TimeoutStartSec in renogy-poll.service

def bt_power_cycle():
    # requires: pi can run bluetoothctl via sudo without password
    subprocess.run(["sudo", "-n", "bluetoothctl", "power", "off"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False
    )
    time.sleep(2)
    subprocess.run(["sudo", "-n", "bluetoothctl", "power", "on"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False
    )
    time.sleep(3)


def _run_example(timeout_s: int) -> str:
    # start_new_session=True makes a new process group so we can kill everything on timeout
    p = subprocess.Popen(
        [PY, EXAMPLE, INI],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = p.communicate(timeout=timeout_s)
        return out
    except subprocess.TimeoutExpired as e:
        # terminate whole process group (example.py + anything it spawned)
        print("Renogy BLE poll timed out; terminating process group")
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            p.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.communicate()
        raise e

def get_reading(retries: int = 2) -> dict:
    last_err = None
    did_bt_reset = False

    for attempt in range(1, retries + 1):
        try:
            out = _run_example(timeout_s=BLE_TIMEOUT)

            if DEBUG:
                print("=== example.py output (captured) ===")
                print(out)
                print("=== end example.py output ===")

            last_jsonish = None
            for line in out.splitlines()[::-1]:
                s = line.strip()
                if "=> {" in s:
                    last_jsonish = s.split("=>", 1)[1].strip()
                    break
                if s.startswith("{") and s.endswith("}"):
                    last_jsonish = s
                    break

            if not last_jsonish:
                raise RuntimeError("Couldn't find payload in output")

            return ast.literal_eval(last_jsonish)

        except subprocess.TimeoutExpired:
            last_err = RuntimeError(f"Renogy BLE poll timed out (attempt {attempt}/{retries})")
        except Exception as e:
            last_err = RuntimeError(f"Renogy BLE poll failed (attempt {attempt}/{retries}): {e}")

        print(last_err)

        # One self-heal attempt: power-cycle Bluetooth if we fail once
        if not did_bt_reset:
            print("Power-cycling Bluetooth and retrying...")
            bt_power_cycle()
            did_bt_reset = True
        else:
            time.sleep(1.0 * attempt)  # small backoff after reset already tried

    raise last_err if last_err else RuntimeError("Renogy BLE poll failed")

def add_derived_fields(data: dict) -> dict:
    def to_f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    bv = to_f(data.get("battery_voltage"))
    bc = to_f(data.get("battery_current"))
    pv = to_f(data.get("pv_voltage"))
    pc = to_f(data.get("pv_current"))
    lv = to_f(data.get("load_voltage"))
    lc = to_f(data.get("load_current"))

    if bv is not None and bc is not None:
        data["battery_power_w"] = bv * bc
        data["battery_power_abs_w"] = abs(data["battery_power_w"])

    if pv is not None and pc is not None:
        data["pv_power_calc_w"] = pv * pc

    if lv is not None and lc is not None:
        data["load_power_calc_w"] = lv * lc

    return data

def lp_escape(s: str) -> str:
    return s.replace(" ", r"\ ").replace(",", r"\,").replace("=", r"\=")

def write_point(payload):
    tags = {
        "device": payload.get("__device", "unknown"),
        "model": payload.get("model", "unknown"),
        "status": payload.get("charging_status", "unknown"),
    }
    fields = {}
    for k in FIELDS:
        v = payload.get(k, None)
        if v is None:
            continue
        # ints/floats only (Influx line protocol)
        if isinstance(v, (int, float)):
            fields[k] = v

    if not fields:
        raise RuntimeError("No numeric fields to write")

    tag_str = ",".join([f"{lp_escape(k)}={lp_escape(str(v))}" for k,v in tags.items()])
    field_str = ",".join([f"{k}={v}" for k,v in fields.items()])
    ts_ns = int(time.time() * 1e9)

    line = f"{MEAS},{tag_str} {field_str} {ts_ns}\n".encode("utf-8")

    params = urllib.parse.urlencode({"db": DB}).encode("ascii")
    req = urllib.request.Request(INFLUX_URL + "?" + params.decode("ascii"), data=line, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status not in (204, 200):
            raise RuntimeError(f"Influx write failed: {resp.status}")

def main():
    print("=== START RENOGY SCRIPT ===")
    print("Reading data")
    payload = get_reading()
    print("Processing data")
    payload = add_derived_fields(payload)

    # Print exactly what we collected
    if DEBUG:
        print("= RENOGY PAYLOAD (raw parsed dict) =")
        for k in sorted(payload.keys()):
            print(f"{k}: {payload[k]}")
        print("= END PAYLOAD =")

    print("Writing sample to DB")
    write_point(payload)
    print("Processing complete")
    print("=== END RENOGY SCRIPT ===")
    # print("\n\n")

if __name__ == "__main__":
    main()
