"""Data models describing heat pump channels (data points).

These mirror the ``RecordDefinition`` of the openHAB binding but are enriched
with a few derived helpers (``value_kind``, ``writable``) so the REST/OpenAPI
layer can describe every channel generically, purely from configuration.
"""

from __future__ import annotations

import enum
from typing import Optional

from pydantic import BaseModel, Field


class DataType(str, enum.Enum):
    """Category of a channel, taken verbatim from the binding config."""

    SENSOR = "Sensor"
    STATUS = "Status"
    SETTINGS = "Settings"


class ValueKind(str, enum.Enum):
    """The Python/JSON type a channel decodes to."""

    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"


class ChannelDefinition(BaseModel):
    """A single data point of the heat pump.

    Records sharing the same ``request_byte`` (and ``request_byte2``) are read
    from the device with a single serial round trip.
    """

    channel_id: str = Field(..., description="Unique channel identifier.")
    request_byte: str = Field(..., description="Hex string of the request command, e.g. 'FB'.")
    request_byte2: Optional[str] = Field(
        None,
        description="Optional second request for values that span two commands.",
    )
    data_type: DataType = Field(..., description="Sensor, Status or Settings.")
    position: int = Field(..., description="Byte offset of the value inside the response frame.")
    length: int = Field(..., description="Number of bytes: 1, 2 or 4.")
    scale: float = Field(1.0, description="Multiplier applied to the raw integer value.")
    bit_position: int = Field(0, description="If > 0, the value is a single bit at this position.")
    min: float = Field(0.0, description="Minimum settable value (Settings only).")
    max: float = Field(0.0, description="Maximum settable value (Settings only).")
    step: float = Field(0.0, description="Step size for settable values.")
    unit: Optional[str] = Field(None, description="Optional unit of measurement.")

    @property
    def request_key(self) -> tuple[str, Optional[str]]:
        """Grouping key: records with the same key share one request."""
        return (self.request_byte.upper(), self.request_byte2.upper() if self.request_byte2 else None)

    @property
    def writable(self) -> bool:
        """Only ``Settings`` channels can be written back to the device."""
        return self.data_type == DataType.SETTINGS

    @property
    def value_kind(self) -> ValueKind:
        """Decoded value type, derived exactly like the binding's ``parseRecord``.

        * a defined ``bit_position`` -> boolean
        * a full byte constrained to 0/1 -> boolean
        * a non-unity scale -> floating point number
        * otherwise -> integer
        """
        if self.bit_position > 0:
            return ValueKind.BOOLEAN
        if self.scale == 1 and self.min == 0 and self.max == 1 and self.step == 0:
            return ValueKind.BOOLEAN
        if self.length == 4:
            return ValueKind.INTEGER
        if self.scale != 1.0:
            return ValueKind.NUMBER
        return ValueKind.INTEGER


class ChannelValue(BaseModel):
    """A channel identifier paired with its decoded value."""

    channel_id: str
    value: object = Field(..., description="Decoded value (bool, int or float).")
    unit: Optional[str] = None
    data_type: DataType
    writable: bool
