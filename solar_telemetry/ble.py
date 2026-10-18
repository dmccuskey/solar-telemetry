"""The BLE client for Renogy's BT-1 module: find, connect, read registers, disconnect.

Modbus requests go to characteristic ffd1, replies come back as notifications
on fff1. Each step is awaited separately and fails with its own error type, so
the service can tell the car being away from a stuck BlueZ (ADR 001, ADR 002).
"""
import asyncio
import logging

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakDBusError, BleakError

from solar_telemetry import modbus

log = logging.getLogger(__name__)

NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"

FIND_TIMEOUT = 5.0
SCAN_TIMEOUT = 5.0
CONNECT_TIMEOUT = 20.0  # connects take about 1.3 s; 11 s when bluetoothd's GAP plugin slows the link (Installation)
READ_TIMEOUT = 5.0  # replies came after 0.1 to 0.8 s
DISCONNECT_SETTLE = 0.3  # lets BlueZ finish tearing down before the next connect

BLUEZ_BUSY = "org.bluez.Error.InProgress"


class BleError(Exception):
    """A step of talking to the controller failed."""


class DeviceNotFound(BleError):
    """The controller didn't show up. `others` is how many other devices the
    fallback scan saw: some means the adapter works and the car is probably
    away, none suggests the adapter is dead."""

    def __init__(self, address: str, others: int):
        self.address = address
        self.others = others
        super().__init__(f"{address} not found; the scan saw {others} other devices")


class BluezBusy(BleError):
    """BlueZ refused with InProgress: a discovery or connect is stuck in it.
    Restarting bluetooth.service clears it; power-cycling the adapter doesn't."""


class AdapterError(BleError):
    """The Bluetooth adapter couldn't scan at all: missing, off, or not ready."""


class ConnectFailed(BleError):
    """The controller was found, but connecting or subscribing failed."""


class ReadTimeout(BleError):
    """No complete reply within the timeout."""


def _is_busy(error: BaseException) -> bool:
    return isinstance(error, BleakDBusError) and error.dbus_error == BLUEZ_BUSY


async def find(address: str) -> tuple[BLEDevice, int]:
    """The controller's device and its signal strength (dBm, from the
    advertisement that found it), looked up by MAC address, then by a scan.

    The scan also catches a device whose advertisement the lookup missed, and
    counts what else is in range for DeviceNotFound. After InProgress there's
    no scan: it would collide with the stuck operation.
    """
    address = address.upper()
    found = []

    def wanted(device: BLEDevice, adv: AdvertisementData) -> bool:
        if device.address.upper() != address:
            return False
        found.append(adv.rssi)
        return True

    try:
        device = await BleakScanner.find_device_by_filter(wanted, timeout=FIND_TIMEOUT)
    except BleakError as e:
        if _is_busy(e):
            raise BluezBusy(str(e)) from e
        raise AdapterError(str(e)) from e
    if device:
        return device, found[-1]

    try:
        devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT, return_adv=True)
    except BleakError as e:
        if _is_busy(e):
            raise BluezBusy(str(e)) from e
        raise AdapterError(str(e)) from e
    for device, adv in devices.values():
        if device.address.upper() == address:
            return device, adv.rssi
    raise DeviceNotFound(address, others=len(devices))


class Connection:
    """A connection to the BT module, subscribed to its replies. Reads go one
    at a time: the module answers in the order asked, with nothing to match a
    reply to its request but the order."""

    def __init__(self, client: BleakClient):
        self._client = client
        self._buffer = bytearray()
        self._arrived = asyncio.Event()
        self._lost = False

    @classmethod
    async def connect(cls, device: BLEDevice) -> "Connection":
        client = BleakClient(
            device, timeout=CONNECT_TIMEOUT, disconnected_callback=lambda _client: connection._on_disconnect()
        )
        connection = cls(client)
        try:
            await connection._client.connect()
            # bleak reconnects by itself when a link drops while connecting, and
            # still reports that drop: the link it returns with is up.
            connection._lost = False
            await connection._client.start_notify(NOTIFY_CHAR_UUID, connection._on_notify)
        except (BleakError, asyncio.TimeoutError, EOFError) as e:
            await connection.disconnect()
            if _is_busy(e):
                raise BluezBusy(str(e)) from e
            raise ConnectFailed(f"{type(e).__name__}: {e}") from e
        return connection

    def _on_notify(self, _char, data: bytearray) -> None:
        self._buffer += data
        self._arrived.set()

    def _on_disconnect(self) -> None:
        self._lost = True
        self._arrived.set()

    async def read(self, device_id: int, register: int, words: int) -> tuple[int, ...]:
        """The values of `words` registers from `register` on.

        Raises ReadTimeout, ConnectFailed if the link drops, or modbus's
        FrameError or ExceptionReply. A reply that isn't Modbus from
        `device_id` fails as soon as its first bytes arrive.
        """
        request = modbus.read_request(device_id, register, words)
        if self._lost:
            raise ConnectFailed("not connected")
        self._buffer.clear()
        self._arrived.clear()
        try:
            await self._client.write_gatt_char(WRITE_CHAR_UUID, request, response=False)
        except (BleakError, EOFError) as e:
            raise ConnectFailed(f"write failed: {type(e).__name__}: {e}") from e

        try:
            async with asyncio.timeout(READ_TIMEOUT):
                while True:
                    await self._arrived.wait()
                    self._arrived.clear()
                    if self._lost:
                        raise ConnectFailed(f"disconnected during a read, after {len(self._buffer)} bytes")
                    length = modbus.reply_length(bytes(self._buffer), device_id)
                    if length is not None and len(self._buffer) >= length:
                        break
        except TimeoutError:
            raise ReadTimeout(
                f"read of {words} words from {register:#06x}: {len(self._buffer)} bytes in {READ_TIMEOUT} s"
            ) from None
        return modbus.parse_read_reply(bytes(self._buffer), device_id, words)

    async def disconnect(self) -> None:
        """Disconnect. Never raises: a failed teardown is logged, and the next
        connect shows whether it mattered."""
        try:
            await self._client.disconnect()
        except (BleakError, asyncio.TimeoutError, EOFError) as e:
            log.warning("disconnect failed: %s: %s", type(e).__name__, e)
        await asyncio.sleep(DISCONNECT_SETTLE)

    async def __aenter__(self) -> "Connection":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.disconnect()
