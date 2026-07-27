"""End-to-end communication tests replaying the exact byte scripts from the
binding's CommunicationServiceTests, verifying both decoded values and the
bytes written to the wire."""

from datetime import datetime

import pytest

from stiebel_heatpump.protocol import parser
from stiebel_heatpump.protocol.communication import CommunicationService
from stiebel_heatpump.protocol.connector import Connector
from stiebel_heatpump.protocol.parser import hex_to_bytes
from stiebel_heatpump.protocol.transport import NoDataAvailable, SimulatorTransport
from stiebel_heatpump.service import HeatPumpService
from stiebel_heatpump.simulator import HeatPumpSimulator


class ScriptedTransport:
    """Returns a fixed sequence of received bytes and records everything written."""

    def __init__(self, rx: bytes) -> None:
        self._rx = rx
        self._index = 0
        self.written = bytearray()

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def get(self, timeout: float = 1.0) -> int:
        if self._index >= len(self._rx):
            raise NoDataAvailable()
        value = self._rx[self._index]
        self._index += 1
        return value


ESC = bytes([parser.ESCAPE])
DATA = bytes(parser.DATA_AVAILABLE)


def make_service(rx: bytes):
    transport = ScriptedTransport(rx)
    service = CommunicationService(Connector(transport), waiting_time_ms=0)
    service.connect()
    return service, transport


def test_set_cooling_writes_expected_bytes(thz504_config):
    current = hex_to_bytes("0100960B028700001003")
    set_ok = hex_to_bytes("01808C0B1003")
    rx = ESC + DATA + current + ESC + DATA + set_ok
    service, transport = make_service(rx)

    record = thz504_config.channel("p99CoolingHC1Switch")
    result = service.write_data(True, record)

    assert result == {"p99CoolingHC1Switch": True}

    get_hex = "0100950B02871003"
    set_hex = "0180160B028700011003"
    expected = "02" + get_hex + "10" + "02" + set_hex + "10"
    assert parser.bytes_to_hex(transport.written) == expected


@pytest.mark.parametrize(
    "set_failed_response,snippet",
    [
        ("01018C0B1003", "timing issue"),
        ("01028D0B1003", "CRC error in request"),
        ("01038E0B1003", "unknown command"),
        ("01048F0B1003", "UNKNOWN Register REQUEST"),
    ],
)
def test_set_cooling_failure(thz504_config, set_failed_response, snippet):
    """A SET the device refuses must be reported as a failure, not as a
    success carrying the old value (which is what the binding does)."""
    current = hex_to_bytes("0100960B028700001003")
    rx = ESC + DATA + current + ESC + DATA + hex_to_bytes(set_failed_response)
    service, _ = make_service(rx)

    record = thz504_config.channel("p99CoolingHC1Switch")
    with pytest.raises(parser.WriteNotConfirmed) as excinfo:
        service.write_data(True, record)

    message = str(excinfo.value)
    # the device's own reason is surfaced ...
    assert snippet in message
    # ... along with the value the device kept (unchanged machine state = off)
    assert "'p99CoolingHC1Switch': False" in message


def test_set_time_quarter_pair_writes_expected_bytes(thz504_config):
    current = hex_to_bytes("0100330A171180801003")
    after = hex_to_bytes("0100A40A17112D441003")
    rx = ESC + DATA + current + ESC + DATA + after
    service, transport = make_service(rx)

    start = thz504_config.channel("programDhwMo1Start")
    end = thz504_config.channel("programDhwMo1End")
    result = service.write_time_quarter_pair(45, 68, start, end)

    assert result == {"programDhwMo1Start": 45, "programDhwMo1End": 68}

    get_hex = "0100330A17111003"
    set_hex = "0180240A17112D441003"
    expected = "02" + get_hex + "10" + "02" + set_hex + "10"
    assert parser.bytes_to_hex(transport.written) == expected


@pytest.mark.parametrize(
    "request_byte,response1,response2,expected",
    [
        ("0A091A", "01002E0A091A01FF1003", "0100300A091B00011003", {"electrDHWDay": 1511}),
        ("0B0287", "0100960B028700011003", None, {"p99CoolingHC1Switch": True}),
        ("0A1710", "0100A80A171031451003", None, {"programDhwMo0Start": 49, "programDhwMo0End": 69}),
        ("0A1711", "0100330A171180801003", None,
         {"programDhwMo1Start": -128, "programDhwMo1End": -128}),
    ],
)
def test_read_request(thz504_config, request_byte, response1, response2, expected):
    if response2:
        rx = ESC + DATA + hex_to_bytes(response1) + ESC + DATA + hex_to_bytes(response2)
    else:
        rx = ESC + DATA + hex_to_bytes(response1)
    service, _ = make_service(rx)

    request = thz504_config.request_by_bytes(request_byte)
    result = service.read_request(request)
    for key, value in expected.items():
        assert result[key] == value


def _sim_service(config):
    service = HeatPumpService(config, SimulatorTransport(HeatPumpSimulator(config)), waiting_time_ms=0)
    service.connect()
    return service


def test_set_time_writes_individual_clock_registers(thz504_config):
    """Firmware 7.59 sets the clock through the individual 0A0122..0A0126
    registers (as FHEM does), not by writing the read-only FC register."""
    service = _sim_service(thz504_config)

    now = datetime.now()
    result = service.set_time()

    expected = {
        "pClockDay": now.day,
        "pClockMonth": now.month,
        "pClockYear": now.year % 100,
        "pClockHour": now.hour,
        "pClockMinutes": now.minute,
    }
    # values reported back and actually stored on the device match "now"
    for channel_id, value in expected.items():
        assert result[channel_id] == value
        assert service.read_channel(channel_id).value == value

    assert result["lastUpdate"].startswith(now.strftime("%Y-%m-%d %H:%M"))
    # the device derives the weekday itself and has no settable seconds register,
    # so neither is written
    assert "weekday" not in {k for k in expected}


def test_set_time_fallback_writes_fc_register(thz303_206_config):
    """Older 2.x firmware has no individual clock registers, so the date/time
    fields are composed straight into the FC register instead."""
    service = _sim_service(thz303_206_config)

    now = datetime.now()
    result = service.set_time()

    for channel_id, value in (
        ("day", now.day),
        ("month", now.month),
        ("year", now.year % 100),
        ("hours", now.hour),
        ("minutes", now.minute),
    ):
        assert result[channel_id] == value
        assert service.read_channel(channel_id).value == value


@pytest.mark.parametrize("minute", [16, 43])  # encode to 0x10 / 0x2B -> must be escaped
def test_write_escapes_payload_bytes(thz504_config, minute):
    """A payload byte of 0x10 or 0x2B has to be escaped on the wire, otherwise
    the frame is corrupted (regression test for the set path)."""
    service = _sim_service(thz504_config)
    service.write_channel("pClockMinutes", minute)
    assert service.read_channel("pClockMinutes").value == minute


def test_receive_data_not_truncated_by_escaped_payload():
    """A payload byte 0x10 followed by 0x03 is escaped to `10 10 03` on the
    wire; its last two bytes look exactly like the `10 03` footer. The binding
    breaks out of its receive loop there and truncates the frame."""
    frame = bytearray([0x01, 0x00, 0x00, 0x0B, 0x10, 0x03, 0x7F]) + parser.FOOTER
    frame[2] = parser.calculate_checksum(frame)
    on_the_wire = parser.add_duplicated_bytes(bytes(frame))

    connector = Connector(ScriptedTransport(ESC + DATA + on_the_wire))
    received = connector.get_data(b"\x01\x00\x00\x0b\x10\x03")

    assert received == bytes(frame)
    assert parser.header_check(received)
