"""``waiting_time_ms`` is enforced on the bus: between *any* two conversations,
including ones triggered by separate API calls -- not only inside one call."""

import time

from fastapi.testclient import TestClient
from helpers import RecordingTransport

from stiebel_heatpump.api import build_app
from stiebel_heatpump.service import HeatPumpService
from stiebel_heatpump.settings import AppSettings
from stiebel_heatpump.simulator import HeatPumpSimulator

WAIT = 0.05


def make_service(config):
    transport = RecordingTransport(HeatPumpSimulator(config))
    return HeatPumpService(config, transport, waiting_time_ms=int(WAIT * 1000)), transport


def gaps(starts):
    return [b - a for a, b in zip(starts, starts[1:], strict=False)]


def test_idle_bus_is_not_delayed(thz504_config):
    service, _ = make_service(thz504_config)
    started = time.monotonic()
    service.read_channel("outsideTemperature")
    assert time.monotonic() - started < WAIT


def test_separate_calls_are_spaced_by_waiting_time(thz504_config):
    service, transport = make_service(thz504_config)
    for channel in ("outsideTemperature", "flowTemperature", "dHWTemperature", "insideTemperatureRC"):
        service.read_channel(channel)
    assert len(transport.starts) >= 4
    assert all(gap >= WAIT * 0.9 for gap in gaps(transport.starts))


def test_write_is_spaced_from_its_own_read_and_from_the_next_call(thz504_config):
    service, transport = make_service(thz504_config)
    service.write_channel("p99CoolingHC1Switch", True)  # pre-write read + SET
    service.read_channel("outsideTemperature")
    assert len(transport.starts) == 3
    assert all(gap >= WAIT * 0.9 for gap in gaps(transport.starts))


def test_two_command_request_is_spaced(thz504_config):
    service, transport = make_service(thz504_config)
    service.read_channel("electrDHWDay")  # request_byte + request_byte2 frames
    assert len(transport.starts) == 2
    assert gaps(transport.starts)[0] >= WAIT * 0.9


def test_spacing_holds_across_http_requests(thz504_config):
    service, transport = make_service(thz504_config)
    settings = AppSettings(device_config="unused", transport="simulator", poll_enabled=False)
    with TestClient(build_app(settings, config=thz504_config, service=service)) as client:
        for _ in range(3):
            assert client.get("/channels/outsideTemperature").status_code == 200
    assert all(gap >= WAIT * 0.9 for gap in gaps(transport.starts))
