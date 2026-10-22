"""Failure classification and the recovery ladder (ADR 002).

A failed reading is classified by its error before anything is reset. The car
being away is not a failure: it resets nothing. Anything else climbs the
ladder one rung per consecutive failure, with a wait after each step: retry
twice, power-cycle the Pi's Bluetooth adapter, restart bluetooth.service,
reset the adapter (rebind its driver, which reloads its firmware), and last,
exit and let systemd restart the service. A good reading resets the ladder.

"Adapter" is the Pi's own Bluetooth radio (hci0), which BlueZ calls the
controller. Nothing here touches the charge controller.

The outcomes are the `collector` measurement's `outcome` values, and the rungs
(1 to 6, 0 for none) its `recovery_step`. The root commands run through `sudo -n`, allowed by name
in a sudoers file.
"""
import asyncio
import enum
import logging

from solar_telemetry import ble, modbus

log = logging.getLogger(__name__)

RETRY_WAIT = 5.0
POWER_OFF_WAIT = 1.0  # between power off and power on; at once, power on fails
POWER_ON_TRIES = 3  # a failed power on would leave the adapter off
POWER_CYCLE_SETTLE = 3.0  # BlueZ needs a moment after power on
BLUEZ_RESTART_SETTLE = 3.0
ADAPTER_RESET_SETTLE = 3.0
COMMAND_TIMEOUT = 30.0

POWER_OFF = ("sudo", "-n", "bluetoothctl", "power", "off")
POWER_ON = ("sudo", "-n", "bluetoothctl", "power", "on")
RESTART_BLUEZ = ("sudo", "-n", "systemctl", "restart", "bluetooth")
# Unbinds and binds the adapter's driver, then restarts bluetooth.service:
# scripts/reset-bt-adapter.sh, installed under this name.
RESET_ADAPTER = ("sudo", "-n", "/usr/local/sbin/solar-telemetry-reset-adapter")
GET_THROTTLED = ("vcgencmd", "get_throttled")

# The errors a reading can fail with. Anything else is a bug, not a failure to
# recover from.
FAILURES = (ble.BleError, modbus.ModbusError)


class Outcome(enum.StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"  # the car is away: other devices are in range
    BLUEZ_BUSY = "bluez_busy"
    CONNECT_FAILED = "connect_failed"
    READ_TIMEOUT = "read_timeout"
    BAD_REPLY = "bad_reply"
    ADAPTER_ERROR = "adapter_error"  # the scan failed, or saw nothing at all


class Step(enum.StrEnum):
    NONE = "none"
    RETRY = "retry"
    POWER_CYCLE_ADAPTER = "power_cycle_adapter"
    RESTART_BLUEZ = "restart_bluez"
    RESET_ADAPTER = "reset_adapter"
    EXIT = "exit"


# The ladder, bottom first. A rung is its position from 1; 0 is none.
LADDER = (
    Step.RETRY, Step.RETRY, Step.POWER_CYCLE_ADAPTER, Step.RESTART_BLUEZ, Step.RESET_ADAPTER, Step.EXIT,
)


class RecoveryExhausted(Exception):
    """Every step of the ladder failed: exit, and let systemd restart the service."""


def classify(error: BaseException) -> Outcome:
    """The outcome for a reading that failed with `error`, one of FAILURES."""
    match error:
        case ble.DeviceNotFound(others=0):
            return Outcome.ADAPTER_ERROR
        case ble.DeviceNotFound():
            return Outcome.NOT_FOUND
        case ble.BluezBusy():
            return Outcome.BLUEZ_BUSY
        case ble.AdapterError():
            return Outcome.ADAPTER_ERROR
        case ble.ConnectFailed():
            return Outcome.CONNECT_FAILED
        case ble.ReadTimeout():
            return Outcome.READ_TIMEOUT
        case modbus.ModbusError():
            return Outcome.BAD_REPLY
    raise TypeError(f"not a reading failure: {type(error).__name__}: {error}")


class Ladder:
    """Which recovery step comes next. Each rung is taken at most once per climb."""

    def __init__(self):
        self.rung = 0

    @property
    def step(self) -> Step:
        return LADDER[self.rung - 1] if self.rung else Step.NONE

    def on_success(self) -> None:
        if self.rung:
            log.info("recovered after rung %d (%s)", self.rung, self.step)
        self.rung = 0

    def on_failure(self, outcome: Outcome) -> Step:
        """The step to take after a failed reading.

        The car being away takes none, and starts the ladder over: a scan that
        sees other devices shows the adapter works. BlueZ busy goes straight to
        restarting bluetooth.service, the only fix for a stuck InProgress.
        """
        if outcome is Outcome.NOT_FOUND:
            self.rung = 0
        else:
            rung = min(self.rung + 1, len(LADDER))
            if outcome is Outcome.BLUEZ_BUSY:
                rung = max(rung, LADDER.index(Step.RESTART_BLUEZ) + 1)
            self.rung = rung
        return self.step


async def recover(step: Step) -> None:
    """Take a recovery step and wait for it to settle. A command that fails is
    logged and the step counts as taken: the next failure climbs past it.
    Raises RecoveryExhausted for Step.EXIT."""
    match step:
        case Step.NONE:
            return
        case Step.RETRY:
            log.info("recovery: retry in %g s", RETRY_WAIT)
            await asyncio.sleep(RETRY_WAIT)
        case Step.POWER_CYCLE_ADAPTER:
            log.warning("recovery: power-cycling the Bluetooth adapter")
            await run(*POWER_OFF)
            for _ in range(POWER_ON_TRIES):
                await asyncio.sleep(POWER_OFF_WAIT)
                if await run(*POWER_ON) is not None:
                    break
            await asyncio.sleep(POWER_CYCLE_SETTLE)
        case Step.RESTART_BLUEZ:
            log.warning("recovery: restarting bluetooth.service")
            await run(*RESTART_BLUEZ)
            await asyncio.sleep(BLUEZ_RESTART_SETTLE)
        case Step.RESET_ADAPTER:
            log.warning("recovery: resetting the Bluetooth adapter (rebinding its driver)")
            await run(*RESET_ADAPTER)
            await asyncio.sleep(ADAPTER_RESET_SETTLE)
        case Step.EXIT:
            raise RecoveryExhausted("every recovery step failed")


# get_throttled's bits: https://www.raspberrypi.com/documentation/computers/os.html#get_throttled
THROTTLED_FLAGS = {
    0: "under-voltage now",
    1: "frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage since boot",
    17: "frequency capped since boot",
    18: "throttled since boot",
    19: "soft temperature limit since boot",
}


async def throttled() -> int | None:
    """`vcgencmd get_throttled` as a number, or None where it can't be read
    (not a Pi, or the command failed). Anything but 0 is a power or heat
    problem, which no Bluetooth reset fixes."""
    output = await run(*GET_THROTTLED)
    return None if output is None else parse_throttled(output)


def parse_throttled(output: str) -> int | None:
    """The value from `throttled=0x50005`, or None if the output isn't that."""
    _, sep, value = output.strip().partition("throttled=")
    try:
        return int(value, 16) if sep else None
    except ValueError:
        return None


def describe_throttled(value: int) -> str:
    """The flags set in a get_throttled value, for the log: "none" for 0."""
    flags = [name for bit, name in THROTTLED_FLAGS.items() if value & (1 << bit)]
    unknown = value & ~sum(1 << bit for bit in THROTTLED_FLAGS)
    if unknown:
        flags.append(f"unknown bits {unknown:#x}")
    return ", ".join(flags) or "none"


async def run(*command: str) -> str | None:
    """Run a command and return its output, or None (logged) if it couldn't
    run, timed out or exited non-zero."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except OSError as e:
        log.error("%s: %s", " ".join(command), e)
        return None
    try:
        async with asyncio.timeout(COMMAND_TIMEOUT):
            output, _ = await process.communicate()
    except TimeoutError:
        process.kill()
        await process.wait()
        log.error("%s: no exit after %g s", " ".join(command), COMMAND_TIMEOUT)
        return None
    text = output.decode(errors="replace").strip()
    if process.returncode != 0:
        log.error("%s: exit %d: %s", " ".join(command), process.returncode, text)
        return None
    return text
