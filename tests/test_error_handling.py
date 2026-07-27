"""Regression tests for the code-review findings on
https://github.com/R0Wi/openhab-addons/pull/1#issuecomment-5037252829 :

1. A failed pre-write read must not be reported as a successful write with
   the caller's requested value silently substituted in.
2. A device that cannot be reached must surface as a communication failure
   (503), not as "channel not found" (404).
3. An unrecognized boolean string must be rejected (400), not silently
   coerced to False.

and for the second review round (see the "Second review round" section below):

4. An out-of-range value is a client error (400), not a transport failure
   (503) -- a regression introduced by the fix for finding 2.
5. A write the device actively refuses must not be reported as 200.
6. A scaled write must not lose a step to float truncation.
7. Unknown channel ids in a multi-channel read must not be dropped silently.
"""

import pytest
from fastapi.testclient import TestClient

from stiebel_heatpump.api import build_app
from stiebel_heatpump.protocol import parser
from stiebel_heatpump.protocol.communication import CommunicationService
from stiebel_heatpump.protocol.connector import Connector
from stiebel_heatpump.protocol.transport import SimulatorTransport
from stiebel_heatpump.service import HeatPumpService
from stiebel_heatpump.settings import AppSettings
from stiebel_heatpump.simulator import HeatPumpSimulator


class NeverDataAvailableTransport:
    """Always acks the start-communication handshake but never reports the
    "data available" signal, so establish_request exhausts its retries and
    get_data()/set_data() return an empty response -- simulating a device
    that is connected but not actually responding to requests."""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def write(self, data) -> None:
        pass

    def get(self, timeout: float = 1.0) -> int:
        return parser.ESCAPE


# -- 1. write failure must not be masked as success --------------------------


def test_write_data_raises_when_pre_write_read_fails(thz504_config):
    service = CommunicationService(Connector(NeverDataAvailableTransport()), waiting_time_ms=0)
    service.connect()
    record = thz504_config.channel("p99CoolingHC1Switch")

    with pytest.raises(parser.ProtocolError):
        service.write_data(True, record)


def test_api_write_returns_503_not_200_when_device_unreachable(thz504_config):
    service = HeatPumpService(thz504_config, NeverDataAvailableTransport(), waiting_time_ms=0)
    settings = AppSettings(device_config="unused", transport="simulator")
    app = build_app(settings, config=thz504_config, service=service)
    with TestClient(app) as client:
        resp = client.put("/channels/p99CoolingHC1Switch", json={"value": True})
    assert resp.status_code == 503
    assert "communicate" in resp.json()["detail"].lower()


# -- 2. communication failure must not look like "channel not found" --------


def test_read_request_raises_after_retries_exhausted(thz504_config):
    service = CommunicationService(Connector(NeverDataAvailableTransport()), waiting_time_ms=0)
    service.connect()
    request = thz504_config.request_by_bytes("FB")

    with pytest.raises(parser.ProtocolError):
        service.read_request(request)


def test_api_read_returns_503_not_404_when_device_unreachable(thz504_config):
    service = HeatPumpService(thz504_config, NeverDataAvailableTransport(), waiting_time_ms=0)
    settings = AppSettings(device_config="unused", transport="simulator")
    app = build_app(settings, config=thz504_config, service=service)
    with TestClient(app) as client:
        resp = client.get("/channels/outsideTemperature")
    assert resp.status_code == 503
    assert "communicate" in resp.json()["detail"].lower()


def test_api_unknown_channel_still_404_when_device_is_reachable(thz504_config):
    # Sanity check that the 503 fix didn't blur the other direction: a
    # genuinely unknown channel id against a *working* device is still 404.
    simulator = HeatPumpSimulator(thz504_config)
    service = HeatPumpService(thz504_config, SimulatorTransport(simulator), waiting_time_ms=0)
    settings = AppSettings(device_config="unused", transport="simulator")
    app = build_app(settings, config=thz504_config, service=service)
    with TestClient(app) as client:
        resp = client.get("/channels/doesNotExist")
    assert resp.status_code == 404


# -- 3. unrecognized boolean strings must be rejected, not silently False ----


def test_coerce_rejects_unrecognized_boolean_string(thz504_config):
    simulator = HeatPumpSimulator(thz504_config)
    service = HeatPumpService(thz504_config, SimulatorTransport(simulator), waiting_time_ms=0)
    service.connect()
    with pytest.raises(ValueError):
        service.write_channel("p99CoolingHC1Switch", "banana")
    service.close()


@pytest.mark.parametrize("value,expected", [("true", True), ("ON", True), ("0", False), ("off", False)])
def test_coerce_accepts_known_boolean_strings(thz504_config, value, expected):
    simulator = HeatPumpSimulator(thz504_config)
    service = HeatPumpService(thz504_config, SimulatorTransport(simulator), waiting_time_ms=0)
    service.connect()
    result = service.write_channel("p99CoolingHC1Switch", value)
    assert result.value is expected
    service.close()


def test_api_write_invalid_boolean_string_returns_400(thz504_config):
    simulator = HeatPumpSimulator(thz504_config)
    service = HeatPumpService(thz504_config, SimulatorTransport(simulator), waiting_time_ms=0)
    settings = AppSettings(device_config="unused", transport="simulator")
    app = build_app(settings, config=thz504_config, service=service)
    with TestClient(app) as client:
        resp = client.put("/channels/p99CoolingHC1Switch", json={"value": "banana"})
    assert resp.status_code == 400


# ===========================================================================
# Second review round: failure modes that survived (or were introduced by) the
# fixes above.
# ===========================================================================


def _sim_app(config, simulator=None):
    simulator = simulator or HeatPumpSimulator(config)
    service = HeatPumpService(config, SimulatorTransport(simulator), waiting_time_ms=0)
    settings = AppSettings(device_config="unused", transport="simulator")
    return build_app(settings, config=config, service=service)


class RejectingSimulator(HeatPumpSimulator):
    """Answers reads normally but refuses every SET, like a device that does
    not accept the register being written."""

    def _build_response(self, frame: bytes) -> bytes:
        if len(frame) >= 2 and frame[1] == parser.SET:
            error = bytearray(
                [parser.HEADER_START, 0x03, 0x00, frame[3], parser.ESCAPE, parser.END]
            )
            error[2] = parser.calculate_checksum(error)
            return bytes(error)
        return super()._build_response(frame)


# -- 4. an out-of-range value is a client error, not a transport failure -----


def test_out_of_range_write_returns_400_not_503(thz504_config):
    """`InvalidDataException` used to derive from `ProtocolError`, so the
    global "device unreachable -> 503" handler swallowed range violations and
    reported them as `Could not communicate with the heat pump`."""
    app = _sim_app(thz504_config)
    with TestClient(app) as client:
        resp = client.put("/channels/p01RoomTemperatureStandardMode", json={"value": 99})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "allowed range" in detail
    assert "communicate" not in detail.lower()


def test_out_of_range_write_raises_value_error(thz504_config):
    service = HeatPumpService(
        thz504_config, SimulatorTransport(HeatPumpSimulator(thz504_config)), waiting_time_ms=0
    )
    service.connect()
    with pytest.raises(parser.ValueOutOfRange):
        service.write_channel("p01RoomTemperatureStandardMode", 99)
    # a client error must never be mistaken for a device/transport error
    assert not isinstance(parser.ValueOutOfRange("x"), parser.ProtocolError)
    service.close()


# -- 5. a write the device refuses must not be a 200 ------------------------


def test_device_rejected_write_returns_502(thz504_config):
    """The device answers (so the link is fine) but refuses the SET; the value
    on the device is unchanged, so this must not be reported as success."""
    app = _sim_app(thz504_config, RejectingSimulator(thz504_config, seed_values={
        "p99CoolingHC1Switch": False,
    }))
    with TestClient(app) as client:
        resp = client.put("/channels/p99CoolingHC1Switch", json={"value": True})
        assert resp.status_code == 502
        assert "did not confirm" in resp.json()["detail"]
        # and the device really did keep the old value
        assert client.get("/channels/p99CoolingHC1Switch").json()["value"] is False


# -- 6. scaled writes must not lose a step ----------------------------------


@pytest.mark.parametrize("value", [21.7, 22.5, 10.3, 29.9])
def test_scaled_write_round_trips(thz504_config, value):
    """`int(21.7 / 0.1)` is 216, not 217 -- the binding wrote 21.6 C when
    21.7 C was requested."""
    app = _sim_app(thz504_config)
    with TestClient(app) as client:
        resp = client.put("/channels/p01RoomTemperatureStandardMode", json={"value": value})
        assert resp.status_code == 200
        assert resp.json()["value"] == pytest.approx(value)
        read_back = client.get("/channels/p01RoomTemperatureStandardMode").json()["value"]
    assert read_back == pytest.approx(value)


# -- 7. unknown ids in a multi-read must not be dropped silently ------------


def test_values_with_unknown_id_returns_404(thz504_config):
    app = _sim_app(thz504_config)
    with TestClient(app) as client:
        resp = client.get("/values", params={"ids": "outsideTemperature,doesNotExist"})
    assert resp.status_code == 404
    assert "doesNotExist" in resp.json()["detail"]


def test_values_with_known_ids_still_works(thz504_config):
    app = _sim_app(thz504_config)
    with TestClient(app) as client:
        resp = client.get("/values", params={"ids": "outsideTemperature"})
    assert resp.status_code == 200
    assert [v["channel_id"] for v in resp.json()] == ["outsideTemperature"]
