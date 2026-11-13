"""The serial link must heal itself: an absent or re-plugged adapter is a
temporary 503, never a 500 that needs a process restart."""

import sys

import pytest
from fake_pump import FakePump
from fastapi.testclient import TestClient

from stiebel_heatpump.api import build_app
from stiebel_heatpump.protocol.transport import NoDataAvailable, SerialTransport, TransportError
from stiebel_heatpump.settings import AppSettings

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")


@pytest.fixture
def link(tmp_path):
    return str(tmp_path / "ttyFAKE")


def make_client(config, link):
    settings = AppSettings(
        device_config="unused", transport="serial", port=link, waiting_time_ms=0, poll_enabled=False
    )
    return TestClient(build_app(settings, config=config), raise_server_exceptions=False)


def test_missing_port_is_503_then_recovers_when_the_pump_appears(thz504_config, link):
    pump = FakePump(thz504_config, link, seed_values={"outsideTemperature": 12.3})
    with make_client(thz504_config, link) as client:
        resp = client.get("/channels/outsideTemperature")
        assert resp.status_code == 503
        assert "serial port" in resp.json()["detail"].lower()
        assert client.get("/health").json()["link"] == "down"
        assert client.get("/health/ready").status_code == 503

        pump.plug()
        try:
            resp = client.get("/channels/outsideTemperature")
            assert resp.status_code == 200
            assert resp.json()["value"] == pytest.approx(12.3)
            assert client.get("/health").json()["link"] == "up"
            assert client.get("/health/ready").status_code == 200
        finally:
            pump.unplug()


def test_unplug_and_replug_recovers_without_restart(thz504_config, link):
    pump = FakePump(thz504_config, link, seed_values={"outsideTemperature": 8.5})
    pump.plug()
    with make_client(thz504_config, link) as client:
        try:
            assert client.get("/channels/outsideTemperature").status_code == 200
            pump.unplug()
            assert client.get("/channels/outsideTemperature").status_code == 503
            assert client.get("/health/ready").status_code == 503

            pump.plug()
            resp = client.get("/channels/outsideTemperature")
            assert resp.status_code == 200
            assert resp.json()["value"] == pytest.approx(8.5)
            assert client.put("/channels/p99CoolingHC1Switch", json={"value": True}).status_code == 200
        finally:
            pump.unplug()


def test_open_failure_is_a_transport_error_not_a_raw_exception(link):
    transport = SerialTransport(link, 115200)
    with pytest.raises(TransportError):
        transport.open()
    with pytest.raises(TransportError):
        transport.write(b"\x02")  # not open


def test_stale_bytes_do_not_survive_a_reconnect(thz504_config, link):
    pump = FakePump(thz504_config, link)
    pump.plug()
    try:
        transport = SerialTransport(link, 115200)
        transport.open()
        transport._queue.put(0x55)  # noqa: SLF001 - bytes left over from an aborted conversation
        transport.close()
        transport.open()
        with pytest.raises(NoDataAvailable):
            transport.get(timeout=0.1)
        transport.close()
    finally:
        pump.unplug()
