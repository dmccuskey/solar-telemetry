"""The BLE client, against a fake bleak that plays back the recorded replies."""
import asyncio

import pytest
from bleak.exc import BleakDBusError, BleakError

from solar_telemetry import ble
from solar_telemetry.modbus import ExceptionReply, FrameError, crc16
from test_fixtures import load_frames

ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeDevice:
    def __init__(self, address: str):
        self.address = address


class FakeClient:
    """Answers each request with `replies[request]`, a list of chunks sent as
    notifications. A request with no reply gets no answer."""

    def __init__(self, replies: dict[bytes, list[bytes]] | None = None, connect_error=None, disconnect_error=None):
        self.replies = replies or {}
        self.connect_error = connect_error
        self.disconnect_error = disconnect_error
        self.disconnected_callback = None
        self.notify = None
        self.disconnects = 0
        self.writes = []

    async def connect(self):
        if self.connect_error:
            raise self.connect_error

    async def start_notify(self, uuid, callback):
        assert uuid == ble.NOTIFY_CHAR_UUID
        self.notify = callback

    async def write_gatt_char(self, uuid, data, response):
        assert uuid == ble.WRITE_CHAR_UUID and response is False
        self.writes.append(bytes(data))
        asyncio.get_running_loop().create_task(self._answer(bytes(data)))

    async def _answer(self, request):
        for chunk in self.replies.get(request, []):
            await asyncio.sleep(0.01)
            self.notify(None, bytearray(chunk))

    async def disconnect(self):
        self.disconnects += 1
        if self.disconnect_error:
            raise self.disconnect_error

    def drop(self):
        """The link goes down, as bleak reports it."""
        self.disconnected_callback(self)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(ble, "DISCONNECT_SETTLE", 0)
    monkeypatch.setattr(ble, "READ_TIMEOUT", 0.2)


def recorded(name: str) -> tuple[bytes, bytes]:
    frame = load_frames()[name]
    return bytes.fromhex(frame["request"]), bytes.fromhex(frame["reply"])


def connection(client: FakeClient) -> ble.Connection:
    conn = ble.Connection(client)
    client.disconnected_callback = lambda _client: conn._on_disconnect()
    client.notify = conn._on_notify
    return conn


def read(conn: ble.Connection, register: int, words: int):
    return asyncio.run(conn.read(255, register, words))


# find


def patch_scanner(monkeypatch, found=None, scanned=(), find_error=None, scan_error=None):
    scans = []

    async def find_device_by_address(address, timeout):
        if find_error:
            raise find_error
        return found

    async def discover(timeout):
        scans.append(timeout)
        if scan_error:
            raise scan_error
        return list(scanned)

    monkeypatch.setattr(ble.BleakScanner, "find_device_by_address", find_device_by_address)
    monkeypatch.setattr(ble.BleakScanner, "discover", discover)
    return scans


def test_find_by_address_needs_no_scan(monkeypatch):
    device = FakeDevice(ADDRESS)
    scans = patch_scanner(monkeypatch, found=device)
    assert asyncio.run(ble.find(ADDRESS)) is device
    assert scans == []


def test_find_falls_back_to_a_scan(monkeypatch):
    device = FakeDevice(ADDRESS.lower())
    patch_scanner(monkeypatch, scanned=[FakeDevice("11:22:33:44:55:66"), device])
    assert asyncio.run(ble.find(ADDRESS)) is device


def test_not_found_counts_the_other_devices(monkeypatch):
    patch_scanner(monkeypatch, scanned=[FakeDevice("11:22:33:44:55:66"), FakeDevice("22:33:44:55:66:77")])
    with pytest.raises(ble.DeviceNotFound) as e:
        asyncio.run(ble.find(ADDRESS))
    assert e.value.others == 2


def test_not_found_in_an_empty_scan(monkeypatch):
    patch_scanner(monkeypatch)
    with pytest.raises(ble.DeviceNotFound) as e:
        asyncio.run(ble.find(ADDRESS))
    assert e.value.others == 0


def test_in_progress_is_bluez_busy_without_a_scan(monkeypatch):
    busy = BleakDBusError("org.bluez.Error.InProgress", ["Operation already in progress"])
    scans = patch_scanner(monkeypatch, find_error=busy)
    with pytest.raises(ble.BluezBusy):
        asyncio.run(ble.find(ADDRESS))
    assert scans == []


def test_in_progress_during_the_scan_is_bluez_busy(monkeypatch):
    busy = BleakDBusError("org.bluez.Error.InProgress", ["Operation already in progress"])
    patch_scanner(monkeypatch, scan_error=busy)
    with pytest.raises(ble.BluezBusy):
        asyncio.run(ble.find(ADDRESS))


@pytest.mark.parametrize("error", [
    BleakDBusError("org.bluez.Error.NotReady", ["Resource Not Ready"]),
    BleakError("No Bluetooth adapters found."),
])
def test_other_scan_errors_are_the_adapter(monkeypatch, error):
    patch_scanner(monkeypatch, find_error=error)
    with pytest.raises(ble.AdapterError):
        asyncio.run(ble.find(ADDRESS))


# connect


def patch_client(monkeypatch, client: FakeClient):
    def make(device, timeout, disconnected_callback):
        assert timeout == ble.CONNECT_TIMEOUT
        client.disconnected_callback = disconnected_callback
        return client

    monkeypatch.setattr(ble, "BleakClient", make)


def test_connect_subscribes(monkeypatch):
    client = FakeClient()
    patch_client(monkeypatch, client)
    asyncio.run(ble.Connection.connect(FakeDevice(ADDRESS)))
    assert client.notify is not None


@pytest.mark.parametrize("error", [asyncio.TimeoutError(), BleakError("failed to connect"), EOFError()])
def test_connect_failure_disconnects(monkeypatch, error):
    client = FakeClient(connect_error=error)
    patch_client(monkeypatch, client)
    with pytest.raises(ble.ConnectFailed):
        asyncio.run(ble.Connection.connect(FakeDevice(ADDRESS)))
    assert client.disconnects == 1


def test_connect_in_progress_is_bluez_busy(monkeypatch):
    client = FakeClient(connect_error=BleakDBusError("org.bluez.Error.InProgress", []))
    patch_client(monkeypatch, client)
    with pytest.raises(ble.BluezBusy):
        asyncio.run(ble.Connection.connect(FakeDevice(ADDRESS)))


# read


def test_read_a_recorded_reply():
    request, reply = recorded("dynamic_35")
    conn = connection(FakeClient({request: [reply]}))
    words = read(conn, 0x0100, 35)
    assert len(words) == 35 and words[0] == 93


def test_read_joins_chunks():
    request, reply = recorded("dynamic_35")
    chunks = [reply[:1], reply[1:3], reply[3:20], reply[20:]]
    conn = connection(FakeClient({request: chunks}))
    assert read(conn, 0x0100, 35)[0] == 93


def test_reads_in_turn_get_their_own_replies():
    replies = {recorded(name)[0]: [recorded(name)[1]] for name in ("dynamic_35", "battery_type")}
    conn = connection(FakeClient(replies))

    async def both():
        return await conn.read(255, 0x0100, 35), await conn.read(255, 0xE004, 1)

    dynamic, battery_type = asyncio.run(both())
    assert len(dynamic) == 35 and battery_type == (1,)


def test_no_reply_is_a_timeout():
    conn = connection(FakeClient())
    with pytest.raises(ble.ReadTimeout):
        read(conn, 0x0100, 35)


def test_half_a_reply_is_a_timeout():
    request, reply = recorded("dynamic_35")
    conn = connection(FakeClient({request: [reply[:30]]}))
    with pytest.raises(ble.ReadTimeout, match="30 bytes"):
        read(conn, 0x0100, 35)


def test_the_module_console_fails_at_once(monkeypatch):
    monkeypatch.setattr(ble, "READ_TIMEOUT", 5.0)
    request, _ = recorded("dynamic_35")
    _, console = recorded("too_many_words")
    conn = connection(FakeClient({request: [console]}))
    with pytest.raises(FrameError):
        read(conn, 0x0100, 35)  # with a 5 s timeout, a FrameError means the first bytes were enough


def test_bad_crc():
    request, reply = recorded("dynamic_35")
    conn = connection(FakeClient({request: [reply[:-1] + bytes([reply[-1] ^ 1])]}))
    with pytest.raises(FrameError, match="CRC"):
        read(conn, 0x0100, 35)


def test_exception_reply():
    request, _ = recorded("dynamic_35")
    refused = bytes.fromhex("ff8302") + crc16(bytes.fromhex("ff8302"))
    conn = connection(FakeClient({request: [refused]}))
    with pytest.raises(ExceptionReply) as e:
        read(conn, 0x0100, 35)
    assert e.value.code == 2


def test_a_dropped_link_fails_the_read_at_once(monkeypatch):
    monkeypatch.setattr(ble, "READ_TIMEOUT", 5.0)
    client = FakeClient()
    conn = connection(client)

    async def read_then_drop():
        task = asyncio.create_task(conn.read(255, 0x0100, 35))
        await asyncio.sleep(0.01)
        client.drop()
        await asyncio.wait_for(task, 1.0)

    with pytest.raises(ble.ConnectFailed, match="disconnected"):
        asyncio.run(read_then_drop())


def test_no_read_after_the_link_dropped():
    client = FakeClient()
    conn = connection(client)
    client.drop()
    with pytest.raises(ble.ConnectFailed):
        read(conn, 0x0100, 35)
    assert client.writes == []


def test_a_failed_write_is_a_connect_failure():
    client = FakeClient()

    async def fail(*_args, **_kwargs):
        raise BleakError("Not connected")

    client.write_gatt_char = fail
    with pytest.raises(ble.ConnectFailed):
        read(connection(client), 0x0100, 35)


# disconnect


def test_disconnect_never_raises():
    client = FakeClient(disconnect_error=EOFError())
    asyncio.run(connection(client).disconnect())
    assert client.disconnects == 1


def test_context_manager_disconnects():
    client = FakeClient()

    async def use():
        async with connection(client):
            pass

    asyncio.run(use())
    assert client.disconnects == 1
