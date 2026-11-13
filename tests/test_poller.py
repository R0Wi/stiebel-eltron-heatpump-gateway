"""Background polling: reads are answered from a cache that a poller keeps
fresh, and writes jump ahead of polling instead of queueing behind a sweep."""

import time

import pytest
from fastapi.testclient import TestClient
from helpers import FlakyTransport, RecordingTransport, wait_until

from stiebel_heatpump.api import build_app
from stiebel_heatpump.models import DataType
from stiebel_heatpump.poller import Poller, frame_key, is_settings_frame
from stiebel_heatpump.protocol.parser import ProtocolError
from stiebel_heatpump.service import HeatPumpService, PollConfig
from stiebel_heatpump.settings import AppSettings
from stiebel_heatpump.simulator import HeatPumpSimulator

# Long intervals: after the first sweep the poller stays quiet during a test.
QUIET = PollConfig(refresh_s=1000, settings_refresh_s=1000, stale_after_s=300)


def make_service(config, wait_ms=50, poll=QUIET, transport_cls=RecordingTransport, **seed):
    transport = transport_cls(HeatPumpSimulator(config, seed_values=seed or None))
    return HeatPumpService(config, transport, waiting_time_ms=wait_ms, poll=poll), transport


def warm(service) -> None:
    """Fill the cache synchronously (no background thread)."""
    for request in service.config.requests:
        assert service.poll_frame(request)


def test_cached_reads_cost_no_serial_traffic(thz504_config):
    service, transport = make_service(thz504_config, outsideTemperature=12.3)
    warm(service)
    conversations = len(transport.starts)

    started = time.monotonic()
    value = service.read_channel("outsideTemperature")
    all_values = service.read_all()
    settings = service.read_all({DataType.SETTINGS})
    elapsed = time.monotonic() - started

    assert value.value == pytest.approx(12.3)
    assert value.updated_at is not None
    assert len(all_values) > 30 and len(settings) > 50
    assert len(transport.starts) == conversations
    assert elapsed < 0.2


def test_fresh_bypasses_the_cache(thz504_config):
    service, transport = make_service(thz504_config)
    warm(service)
    conversations = len(transport.starts)

    service.read_channel("outsideTemperature")
    assert len(transport.starts) == conversations
    service.read_channel("outsideTemperature", fresh=True)
    assert len(transport.starts) == conversations + 1


def test_polling_disabled_reads_live_every_time(thz504_config):
    service, transport = make_service(thz504_config, poll=None)
    service.read_channel("outsideTemperature")
    service.read_channel("outsideTemperature")
    assert len(transport.starts) == 2


def test_uncached_frame_is_read_through(thz504_config):
    service, transport = make_service(thz504_config, outsideTemperature=3.0)
    assert service.read_channel("outsideTemperature").value == pytest.approx(3.0)
    assert len(transport.starts) == 1
    service.read_channel("flowTemperature")  # same FB frame, now cached
    assert len(transport.starts) == 1


def test_background_poller_fills_the_cache_and_stops_on_close(thz504_config):
    service, _ = make_service(thz504_config, wait_ms=5)
    service.start_polling()
    try:
        total = len(thz504_config.requests)
        assert wait_until(lambda: len(service._cache) == total)  # noqa: SLF001
        assert service.link_status()["link"] == "up"
    finally:
        service.close()
    assert service._poller is None  # noqa: SLF001


def test_write_overtakes_a_running_sweep(thz504_config):
    service, _ = make_service(thz504_config, wait_ms=100)
    total = len(thz504_config.requests)
    service.start_polling()
    try:
        time.sleep(0.3)  # sweep is under way: 36 frames * 0.1 s ~ 3.6 s
        started = time.monotonic()
        result = service.write_channel("p99CoolingHC1Switch", True)
        elapsed = time.monotonic() - started

        assert result.value is True
        assert len(service._cache) < total, "sweep already over, test proves nothing"  # noqa: SLF001
        assert elapsed < 1.2, f"write waited {elapsed:.1f}s behind the sweep"
    finally:
        service.close()


def test_read_through_is_not_starved_by_the_poller(thz504_config):
    """The poller re-takes the line the moment it releases it; an API read that
    needs the device must still get its turn after at most one frame."""
    service, _ = make_service(thz504_config, wait_ms=100)
    total = len(thz504_config.requests)
    service.start_polling()
    try:
        time.sleep(0.3)  # sweep under way: ~3.6 s in total
        started = time.monotonic()
        service.read_channel("programDhwSu2End")  # a Settings frame the sweep has not reached yet
        elapsed = time.monotonic() - started

        assert len(service._cache) < total, "sweep already over, test proves nothing"  # noqa: SLF001
        assert elapsed < 1.0, f"read waited {elapsed:.1f}s behind the sweep"
    finally:
        service.close()


def test_read_through_and_poller_do_not_read_the_same_frame_twice(thz504_config):
    service, transport = make_service(thz504_config, wait_ms=20)
    service.start_polling()
    try:
        # Ask for every Sensor/Status/Settings value while the first sweep runs.
        service.read_all()
        service.read_all({DataType.SETTINGS})
        assert wait_until(lambda: len(service._cache) == len(thz504_config.requests))  # noqa: SLF001
        time.sleep(0.3)  # let any redundant poll show up
    finally:
        service.close()
    # One conversation per frame, plus a little slack for a poll that was already in flight.
    assert len(transport.starts) <= len(thz504_config.requests) + 3


def test_written_value_is_served_immediately(thz504_config):
    service, transport = make_service(thz504_config)
    warm(service)
    service.write_channel("p01RoomTemperatureStandardMode", 22.5)
    conversations = len(transport.starts)

    value = service.read_channel("p01RoomTemperatureStandardMode")

    assert value.value == pytest.approx(22.5)
    assert len(transport.starts) == conversations  # from the patched cache


def test_failing_polls_keep_values_until_stale_then_refuse(thz504_config):
    poll = PollConfig(refresh_s=1000, settings_refresh_s=1000, stale_after_s=1.5)
    service, transport = make_service(thz504_config, wait_ms=0, poll=poll, transport_cls=FlakyTransport,
                                      outsideTemperature=7.0)
    request = thz504_config.request_by_bytes("FB")
    assert service.poll_frame(request)

    transport.broken = True
    assert service.poll_frame(request) is False
    assert service.read_channel("outsideTemperature").value == pytest.approx(7.0)  # still served

    def refused() -> bool:
        try:
            service.read_channel("outsideTemperature")
        except ProtocolError:
            return True
        return False

    assert wait_until(refused, timeout=5)  # the cached value ages out

    transport.broken = False
    assert service.poll_frame(request)
    assert service.read_channel("outsideTemperature").value == pytest.approx(7.0)  # recovered


def test_set_time_drops_cached_clock_frames(thz504_config):
    service, _ = make_service(thz504_config, wait_ms=0)
    warm(service)
    assert frame_key(thz504_config.request_by_bytes("FC")) in service._cache  # noqa: SLF001
    service.set_time()
    assert frame_key(thz504_config.request_by_bytes("FC")) not in service._cache  # noqa: SLF001


def test_api_exposes_updated_at_and_fresh(thz504_config):
    service, transport = make_service(thz504_config, wait_ms=5, outsideTemperature=1.5)
    settings = AppSettings(device_config="unused", transport="simulator")
    with TestClient(build_app(settings, config=thz504_config, service=service)) as client:
        # The app starts the poller; wait for its first sweep, after which it is quiet.
        assert wait_until(lambda: len(service._cache) == len(thz504_config.requests))  # noqa: SLF001
        conversations = len(transport.starts)
        body = client.get("/channels/outsideTemperature").json()
        assert body["updated_at"] is not None
        assert len(client.get("/values").json()) > 30
        assert len(transport.starts) == conversations
        assert client.get("/channels/outsideTemperature", params={"fresh": True}).status_code == 200
        assert len(transport.starts) == conversations + 1


# -- scheduling ---------------------------------------------------------------


class FakeClock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


def drain(poller):
    order = []
    while (request := poller._next()[0]) is not None:  # noqa: SLF001
        order.append(request)
    return order


def test_sensor_and_status_frames_are_polled_before_settings(thz504_config):
    poller = Poller(None, thz504_config.requests, 60, 900, clock=FakeClock())
    order = drain(poller)

    assert len(order) == len(thz504_config.requests)
    kinds = [is_settings_frame(request) for request in order]
    assert kinds == sorted(kinds), "a Settings frame was scheduled before a Sensor/Status frame"
    assert not all(kinds) and any(kinds)


def test_frames_are_due_again_after_their_interval_and_on_demand(thz504_config):
    clock = FakeClock()
    poller = Poller(None, thz504_config.requests, 60, 900, clock=clock)
    drain(poller)
    request, wait = poller._next()  # noqa: SLF001
    assert request is None and wait == pytest.approx(60.0)

    clock.now += 61  # only the 60 s frames come back; settings wait for 900 s
    again = drain(poller)
    assert again and not any(is_settings_frame(r) for r in again)

    key = frame_key(thz504_config.request_by_bytes("17"))
    poller.mark_due(key)
    assert [frame_key(r) for r in drain(poller)] == [key]
