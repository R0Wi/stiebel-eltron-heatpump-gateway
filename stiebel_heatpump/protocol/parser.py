"""Pure protocol parsing/encoding, ported from the binding's ``DataParser``.

Every response from the heat pump has the same structure as a request::

    Header:  0x01
    Get/Set: 0x00 for a read (get) response, 0x80 for a write (set) response;
             an error code is stored here in case of a communication problem
    Checksum: 1 byte
    Command:  1 byte, matches the request command
    Data:     only when reading; length depends on the data type
    Footer:   0x10 0x03

Raw bytes on the wire are escaped: ``0x10`` is doubled (``0x10 0x10``) and
``0x2B`` is followed by ``0x18``. These functions de-escape received frames and
escape frames that are sent.

The functions here are deliberately free of any I/O so they can be unit tested
against the exact byte vectors used by the original Java test suite.

Where this port knowingly deviates from ``DataParser`` the divergence is called
out in a ``Deviation:`` note on the function in question, and summarised in the
"Deviations from the binding" section of the README.
"""

from __future__ import annotations

import math
import struct
from typing import Optional, Union

from ..models import ChannelDefinition

# Protocol constants (see DataParser.java)
ESCAPE = 0x10
HEADER_START = 0x01
END = 0x03
GET = 0x00
SET = 0x80
START_COMMUNICATION = 0x02
FOOTER = bytes([ESCAPE, END])
DATA_AVAILABLE = bytes([ESCAPE, START_COMMUNICATION])

# Known error codes returned by the device in the get/set byte position.
_KNOWN_ERRORS: dict[tuple[int, int], str] = {
    (0x01, 0x01): "timing issue",
    (0x01, 0x02): "CRC error in request",
    (0x01, 0x03): "device doesn't know this command (unknown command)",
    (0x01, 0x04): "UNKNOWN Register REQUEST",
}

DecodedValue = Union[bool, int, float]


class ProtocolError(Exception):
    """Raised for malformed frames or device error responses.

    Everything deriving from this means "the conversation with the device went
    wrong" -- it is mapped to a 5xx by the API layer. Bad *caller input* must
    not derive from it (see :class:`ValueOutOfRange`).
    """


class InvalidDataException(ProtocolError):
    """Raised when a single record cannot be parsed from a response."""


class WriteNotConfirmed(ProtocolError):
    """Raised when the device did not confirm a SET frame.

    The write reached the device but was rejected (or the confirmation was
    lost), so the value on the device is still the old one. Distinct from a
    plain :class:`ProtocolError` because the link itself is working.
    """


class ValueOutOfRange(ValueError):
    """Raised when a caller-supplied value violates a record's min/max.

    This is a *client* error, not a device error, so it deliberately derives
    from :class:`ValueError` (-> HTTP 400) rather than from
    :class:`ProtocolError` (-> HTTP 503). The binding used its generic
    ``InvalidDataException`` here, which conflated the two.
    """


def bytes_to_hex(data: bytes) -> str:
    """Uppercase hex string without separators, e.g. ``b'\\x01\\x00'`` -> ``'0100'``."""
    return data.hex().upper()


def hex_to_bytes(text: str) -> bytes:
    """Parse a hex string (with optional whitespace) into bytes."""
    return bytes.fromhex(text.replace(" ", ""))


def _signed_byte(value: int) -> int:
    return value - 256 if value >= 128 else value


def short_to_bytes(value: int) -> bytes:
    """Little-endian 2-byte encoding (``[low, high]``), matching ``shortToByte``."""
    return bytes([value & 0xFF, (value >> 8) & 0xFF])


def calculate_checksum(data: bytes) -> int:
    """Sum of all bytes except the checksum slot (index 2) and the 2-byte footer.

    Returns the low byte of the sum.
    """
    if len(data) < 5:
        raise InvalidDataException("no valid byte[] for calculation of checksum!")
    total = 0
    for i in range(len(data) - 2):
        if i == 2:
            continue
        total += data[i] & 0xFF
    return total & 0xFF


def add_duplicated_bytes(data: bytes) -> bytes:
    """Escape a frame for sending: double ``0x10`` and append ``0x18`` after ``0x2B``.

    The 2-byte header and 2-byte footer are copied verbatim.
    """
    out = bytearray(data[:2])
    for i in range(2, len(data) - 2):
        out.append(data[i])
        if data[i] == 0x10:
            out.append(0x10)
        elif data[i] == 0x2B:
            out.append(0x18)
    out.extend(data[-2:])
    return bytes(out)


def fix_duplicated_bytes(data: bytes) -> bytes:
    """De-escape a received frame: collapse ``0x10 0x10`` and ``0x2B 0x18``.

    Operates on everything except the trailing footer, then re-appends ``0x10 0x03``.

    Deviation: the binding's ``findReplace`` restarts its search at index 0 after
    every substitution, so a run of escaped bytes is collapsed repeatedly --
    ``10 10 10 10`` (two escaped ``0x10`` payload bytes) becomes a single
    ``0x10`` there instead of two. ``bytes.replace`` scans left to right without
    re-examining what it already emitted, which is what the protocol actually
    requires; ``add_duplicated_bytes`` round-trips correctly here and does not
    in the binding.
    """
    if len(data) < 2:
        return data
    body = bytes(data[:-2])
    body = body.replace(b"\x10\x10", b"\x10")
    body = body.replace(b"\x2b\x18", b"\x2b")
    return body + FOOTER


def is_frame_end(buffer: "bytes | bytearray", length: Optional[int] = None) -> bool:
    """Whether ``buffer[:length]`` ends with a real ``0x10 0x03`` footer.

    Deviation: a payload byte ``0x10`` is escaped as ``0x10 0x10``, so a payload
    ``0x10 0x03`` arrives on the wire as ``10 10 03`` -- whose last two bytes
    look exactly like the footer. The binding's receive loop breaks there and
    truncates the frame mid-payload. Escaped payload bytes always come in
    pairs, so the real footer is the one that leaves an *odd* number of
    consecutive ``0x10`` bytes in front of the ``0x03``.
    """
    if length is None:
        length = len(buffer)
    if length < 2 or buffer[length - 1] != END or buffer[length - 2] != ESCAPE:
        return False
    escapes = 0
    index = length - 2
    while index >= 0 and buffer[index] == ESCAPE:
        escapes += 1
        index -= 1
    return escapes % 2 == 1


def _get_bit_from_bytes(data: bytes, bit_position: int) -> bool:
    pos_byte = bit_position // 8
    pos_bit = bit_position % 8
    return ((data[pos_byte] >> (8 - (pos_bit + 1))) & 0x0001) >= 1


def _set_bit(byte_value: int, bit_position: int, value: bool) -> int:
    pos_bit = bit_position % 8
    new_int = 1 if value else 0
    old = (0xFF7F >> pos_bit) & byte_value & 0x00FF
    return ((new_int << (8 - (pos_bit + 1))) | old) & 0xFF


def check_known_errors(response: bytes) -> None:
    """Raise :class:`ProtocolError` if the frame carries a known device error."""
    if len(response) < 2:
        return
    key = (response[0], response[1])
    if key in _KNOWN_ERRORS:
        raise ProtocolError(f"decode: {_KNOWN_ERRORS[key]} (Response: {bytes_to_hex(response)})")


def verify_header(response: bytes) -> None:
    """Validate header start, get/set byte and checksum. Raises on failure."""
    if len(response) < 4:
        raise ProtocolError(f"invalid response length: {bytes_to_hex(response)}")
    if response[0] != HEADER_START:
        raise ProtocolError(f"no header start: {bytes_to_hex(response)}")
    if response[1] != GET and response[1] != SET:
        check_known_errors(response)
        raise ProtocolError(f"response is neither get nor set: {bytes_to_hex(response)}")
    if response[2] != calculate_checksum(response):
        raise ProtocolError(f"invalid checksum: {bytes_to_hex(response)}")


def header_check(response: bytes) -> bool:
    """Boolean variant of :func:`verify_header`."""
    try:
        verify_header(response)
        return True
    except ProtocolError:
        return False


def parse_record(response: bytes, record: ChannelDefinition) -> DecodedValue:
    """Decode a single channel value from a response frame.

    Returns ``bool`` for switches/contacts, ``int`` for integer values and
    ``float`` for scaled decimal values.
    """
    if len(response) < 2:
        raise InvalidDataException(
            f"Response does not have a valid length of bytes {bytes_to_hex(response)}"
        )
    needed = record.position + record.length
    if len(response) < needed:
        raise InvalidDataException(
            f"Response {_formatted(response)} does not have a valid length of at least "
            f"{needed} bytes"
        )

    try:
        if record.length == 1:
            raw = response[record.position : record.position + 1]
            number: int = _signed_byte(response[record.position])
        elif record.length == 2:
            raw = response[record.position : record.position + 2]
            number = struct.unpack(">h", raw)[0]
        elif record.length == 4:
            raw = response[record.position : record.position + 4]
            return struct.unpack(">i", raw)[0]
        else:
            raise InvalidDataException(f"Unsupported record length {record.length}")

        if record.bit_position > 0:
            return _get_bit_from_bytes(raw, record.bit_position)
        if record.scale == 1 and record.min == 0 and record.max == 1 and record.step == 0:
            return number != 0
        if record.scale != 1.0:
            scaled = number * record.scale
            # Java Math.round(x*100)/100.0 == floor(x*100 + 0.5)/100
            return math.floor(scaled * 100.0 + 0.5) / 100.0
        return number
    except InvalidDataException:
        raise
    except Exception as exc:  # noqa: BLE001 - mirror the binding's broad catch
        raise InvalidDataException(
            f"Response {bytes_to_hex(response)} could not be parsed for record "
            f"{record.channel_id}"
        ) from exc


def parse_records(
    response: bytes,
    records: list[ChannelDefinition],
    response2: Optional[bytes] = None,
) -> dict[str, DecodedValue]:
    """Decode every channel of a request from one (or two) response frames.

    When ``response2`` is given (two-command values), short values are combined
    as ``value2 * 1000 + value1`` -- exactly like the binding.

    Deviation: the binding casts that sum back to a 16-bit ``short``, so a
    counter above 32767 wraps to a negative number (e.g. 100999 -> -30073). The
    combined value is kept intact here.
    """
    result: dict[str, DecodedValue] = {}
    if len(response) < 2:
        return result
    if response2 is not None and len(response2) < 2:
        return result

    for record in records:
        try:
            value = parse_record(response, record)
            if response2 is not None and _is_short_result(value, record):
                value2 = parse_record(response2, record)
                value = value2 * 1000 + value
            result[record.channel_id] = value
        except InvalidDataException:
            continue
    return result


def _is_short_result(value: DecodedValue, record: ChannelDefinition) -> bool:
    # In the binding the two-command combine only applies to `Short` results,
    # i.e. non-boolean integer values of length 1 or 2.
    return isinstance(value, int) and not isinstance(value, bool) and record.length != 4


def scaled_to_raw(value: float, scale: float) -> int:
    """Convert a scaled (human) value into the raw integer stored on the device.

    Deviation: the binding truncates (``(short) (value / scale)``), which loses
    a step whenever the division lands just below the intended integer in
    binary floating point -- ``21.7 / 0.1`` is ``216.99999999999997``, so a
    requested 21.7 C was silently written as 21.6 C. Rounding half up (the same
    ``Math.round`` semantics ``parse_record`` already reproduces on the way
    back) makes the write round-trip through ``parse_record`` exactly.
    """
    return int(math.floor(value / scale + 0.5))


def compose_record(new_value: DecodedValue, response: bytearray, record: ChannelDefinition) -> bytearray:
    """Write ``new_value`` into a copy-of-read ``response`` and turn it into a set frame.

    Mutates ``response`` in place: flips the get byte to ``SET``, encodes the
    value at the record's position and recomputes the checksum. Returns the same
    ``bytearray`` for convenience.

    Raises :class:`ValueOutOfRange` (a ``ValueError``) if the value violates the
    record's ``min``/``max``.
    """
    response[1] = SET

    if isinstance(new_value, bool):
        if record.bit_position > 0:
            response[record.position] = _set_bit(
                response[record.position], record.bit_position, new_value
            )
        else:
            response[record.position] = 1 if new_value else 0
    else:
        if new_value > record.max or new_value < record.min:
            raise ValueOutOfRange(
                f"The record {record.channel_id} cannot be set to {new_value}; "
                f"allowed range is {record.min}<-->{record.max}!"
            )
        if isinstance(new_value, float):
            short_value = scaled_to_raw(new_value, record.scale)
        else:
            short_value = int(new_value)

        encoded = short_to_bytes(short_value)  # [low, high]
        if record.length == 1:
            response[record.position] = encoded[0]
        elif record.length == 2:
            response[record.position] = encoded[1]  # high byte first (big-endian)
            response[record.position + 1] = encoded[0]

    response[2] = calculate_checksum(response)
    return response


def create_request_message(request_byte: bytes, get_or_set: int = GET) -> bytes:
    """Build and escape a request frame ready to send to the device."""
    message = bytearray([HEADER_START, get_or_set, 0x00])
    message.extend(request_byte)
    message.extend(FOOTER)
    message[2] = calculate_checksum(message)
    return add_duplicated_bytes(bytes(message))


def _formatted(data: bytes) -> str:
    """Reproduce DataParser.bytesToHex(bytes, true) with (nn) position markers."""
    out = []
    for j, b in enumerate(data):
        if j % 4 == 0:
            out.append(f"({j:02d})")
        out.append(f"{b:02X} ")
    return "".join(out)
