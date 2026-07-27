"""Protocol parser tests, using the exact byte vectors from the binding's
Java test suite (DataParserTest / CommunicationServiceTests)."""

import pytest

from stiebel_heatpump.protocol import parser
from stiebel_heatpump.protocol.parser import (
    InvalidDataException,
    add_duplicated_bytes,
    calculate_checksum,
    compose_record,
    create_request_message,
    fix_duplicated_bytes,
    hex_to_bytes,
    parse_record,
    parse_records,
)


def test_checksum_matches_binding():
    # Version request 01 00 FE FD 10 03 -> checksum 0xFE
    message = hex_to_bytes("0100FEFD1003")
    assert calculate_checksum(message) == 0xFE


def test_create_request_message_version():
    assert parser.bytes_to_hex(create_request_message(b"\xFD")) == "0100FEFD1003"


def test_escaping_roundtrip():
    # 0x10 must be doubled, 0x2B followed by 0x18, header/footer untouched.
    frame = bytes([0x01, 0x00, 0x00, 0x10, 0x2B, 0x05, 0x10, 0x03])
    escaped = add_duplicated_bytes(frame)
    #             header |  00  10->10 10  2B->2B 18  05  | footer
    assert parser.bytes_to_hex(escaped) == "0100" + "00" + "1010" + "2B18" + "05" + "1003"
    assert fix_duplicated_bytes(escaped) == frame


def test_parse_record_invalid_length(thz504_config):
    response = bytes([0x01, 0x80, 0x8C, 0x0B, 0x10, 0x03])
    record = next(c for c in thz504_config.channels if c.channel_id == "p99CoolingHC1Switch")
    with pytest.raises(InvalidDataException) as excinfo:
        parse_record(response, record)
    assert str(excinfo.value) == (
        "Response (00)01 80 8C 0B (04)10 03  does not have a valid length of at least 8 bytes"
    )


@pytest.mark.parametrize(
    "request_byte,response1,response2,expected",
    [
        ("0A091A", "01002E0A091A01FF1003", "0100300A091B00011003", {"electrDHWDay": 1511}),
        ("0A091E", "0100770A091E00451003", "01003A0A091F00071003", {"electrHCDay": 7069}),
        ("0B0287", "0100960B028700011003", None, {"p99CoolingHC1Switch": True}),
        (
            "F4",
            "01005AF400810000011400000119010F011500000101600800640100000000D40000000000E30200000000071003",
            None,
            {"insideTemperatureRC": 22.7, "seasonMode": 1, "heatSetpointTemperatureHC1": 27.1},
        ),
        (
            "FB",
            "01006AFBFDA8FFF4019B018C027602048001FDA800C401A9600807012C012C0000001900220000FFF5010F0000033F"
            "08E10000000000000000055400BE00000000019301A201D90196017A0000000007271003",
            None,
            {"extractFanSpeed": 25, "supplyFanSpeed": 34, "exhaustFanSpeed": 0},
        ),
    ],
)
def test_parse_records_get_values(thz504_config, request_byte, response1, response2, expected):
    request = thz504_config.request_by_bytes(request_byte)
    resp1 = hex_to_bytes(response1)
    resp2 = hex_to_bytes(response2) if response2 else None
    result = parse_records(resp1, request.records, resp2)
    for key, value in expected.items():
        assert result[key] == value


def test_parse_records_display_bits(thz504_config):
    request = thz504_config.request_by_bytes("0A0176")
    result = parse_records(hex_to_bytes("0100990a01760413"), request.records)
    assert result["Compressor"] is True
    assert result["PumpHc"] is True
    assert result["HeatingDhw"] is True
    assert result["Cooling"] is False
    assert result["BoosterHc"] is False


def test_compose_record_cooling_switch(thz504_config):
    record = next(c for c in thz504_config.channels if c.channel_id == "p99CoolingHC1Switch")
    read_response = bytearray(hex_to_bytes("0100960B028700001003"))
    compose_record(True, read_response, record)
    assert parser.bytes_to_hex(read_response) == "0180160B028700011003"
    # And it decodes back to True.
    assert parse_record(read_response, record) is True


def test_compose_record_time_quarter(thz504_config):
    start = next(c for c in thz504_config.channels if c.channel_id == "programDhwMo1Start")
    end = next(c for c in thz504_config.channels if c.channel_id == "programDhwMo1End")
    frame = bytearray(hex_to_bytes("0100330A171180801003"))
    compose_record(45, frame, start)   # 45 -> 0x2D
    compose_record(68, frame, end)     # 68 -> 0x44
    assert parser.bytes_to_hex(frame) == "0180240A17112D441003"


@pytest.mark.parametrize(
    "payload",
    [
        bytes([0x10, 0x10]),  # two literal escape bytes in a row
        bytes([0x10, 0x03]),  # looks like a footer once escaped
        bytes([0x2B, 0x10, 0x03]),
        bytes([0x10]),
    ],
)
def test_escaping_roundtrip_for_tricky_payloads(payload):
    """The binding's `findReplace` re-scans from index 0 after every
    substitution, so `10 10 10 10` collapses to a single `0x10` there. A
    de-escape must be the exact inverse of the escape."""
    frame = bytes([0x01, 0x00, 0x00]) + payload + parser.FOOTER
    escaped = parser.add_duplicated_bytes(frame)
    assert parser.fix_duplicated_bytes(escaped) == frame


@pytest.mark.parametrize(
    "payload,expect_early_break",
    [
        (bytes([0x10, 0x03]), False),  # escaped payload, NOT the footer
        (bytes([0x2B]), False),
        (b"", True),
    ],
)
def test_is_frame_end_only_matches_the_real_footer(payload, expect_early_break):
    frame = bytes([0x01, 0x00, 0x00]) + payload + parser.FOOTER
    escaped = parser.add_duplicated_bytes(frame)
    # the real footer, at the very end, is always recognised
    assert parser.is_frame_end(escaped)
    # ... and no earlier prefix of the frame is mistaken for it
    early = [i for i in range(4, len(escaped)) if parser.is_frame_end(escaped, i)]
    assert early == [], f"frame would be truncated at {early}"
