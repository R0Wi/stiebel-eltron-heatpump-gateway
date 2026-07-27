"""Tests for the in-memory simulator and the full service stack running on it."""

import pytest

from stiebel_heatpump.protocol.transport import SimulatorTransport
from stiebel_heatpump.service import ChannelNotWritable, HeatPumpService
from stiebel_heatpump.simulator import HeatPumpSimulator


@pytest.fixture
def service(thz504_config):
    simulator = HeatPumpSimulator(
        thz504_config,
        seed_values={
            "version": 7.59,
            "outsideTemperature": -3.4,
            "p99CoolingHC1Switch": False,
        },
    )
    svc = HeatPumpService(thz504_config, SimulatorTransport(simulator), waiting_time_ms=0)
    svc.connect()
    yield svc
    svc.close()


def test_version(service):
    assert service.get_version() == "7.59"


def test_read_seeded_sensor(service):
    value = service.read_channel("outsideTemperature")
    assert value.value == pytest.approx(-3.4, abs=0.05)
    assert value.data_type.value == "Sensor"


def test_read_write_roundtrip(service):
    before = service.read_channel("p99CoolingHC1Switch")
    assert before.value is False

    written = service.write_channel("p99CoolingHC1Switch", True)
    assert written.value is True

    after = service.read_channel("p99CoolingHC1Switch")
    assert after.value is True


def test_write_non_settings_rejected(service):
    with pytest.raises(ChannelNotWritable):
        service.write_channel("outsideTemperature", 5)


def test_read_all_returns_sensors(service):
    values = service.read_all()
    ids = {v.channel_id for v in values}
    assert "outsideTemperature" in ids
    assert all(v.data_type.value in ("Sensor", "Status") for v in values)


def test_read_all_includes_two_command_channels(service):
    # electrDHWDay/electrHCDay are read via request_byte *and* request_byte2
    # (two independent commands combined into one value). The simulator used
    # to only ever store a response frame for the primary request_byte, so
    # the secondary command came back as an "unknown command" error and the
    # whole request silently vanished from read_all()'s result.
    values = service.read_all()
    ids = {v.channel_id for v in values}
    assert "electrDHWDay" in ids
    assert "electrHCDay" in ids


def test_two_command_channel_reads_without_error(service):
    value = service.read_channel("electrDHWDay")
    assert isinstance(value.value, int)


def test_two_command_channel_seed_value_round_trips(thz504_config):
    # Seeding must split the value across both stored frames the same way
    # parse_records() recombines them on read (value2 * 1000 + value1), or a
    # seeded two-command channel would silently read back wrong. 1511 encodes
    # to the exact "01FF"/"0001" pair captured in the original Java test suite
    # (CommunicationServiceTests: response "...01FF1003" / response2
    # "...00011003" for electrDHWDay), so this also cross-checks the split
    # against a real device capture, not just parse_records()'s own formula.
    simulator = HeatPumpSimulator(thz504_config, seed_values={"electrDHWDay": 1511})
    service = HeatPumpService(thz504_config, SimulatorTransport(simulator), waiting_time_ms=0)
    service.connect()

    assert service.read_channel("electrDHWDay").value == 1511

    service.close()
