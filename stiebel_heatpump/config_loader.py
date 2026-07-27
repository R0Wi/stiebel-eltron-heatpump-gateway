"""Load channel definitions from configuration files.

Two formats are supported:

* the **XML** files shipped with the openHAB binding (``HeatpumpConfig/*.xml``),
  so existing protocol definitions can be reused as-is;
* a native **YAML** format for anyone who prefers to author definitions freshly.

Channels sharing the same request command are grouped into :class:`Request`
objects; one request equals one serial round trip.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

from .models import ChannelDefinition, DataType


@dataclass
class Request:
    """A group of channels read/written with a single command."""

    request_byte: str
    request_byte2: Optional[str]
    records: list[ChannelDefinition] = field(default_factory=list)

    @property
    def request_bytes(self) -> bytes:
        return bytes.fromhex(self.request_byte)

    @property
    def request_bytes2(self) -> Optional[bytes]:
        return bytes.fromhex(self.request_byte2) if self.request_byte2 else None


class HeatPumpConfig:
    """The full set of channels for one heat pump firmware variant."""

    def __init__(self, channels: list[ChannelDefinition], name: str = "") -> None:
        self.name = name
        self.channels = channels
        self.requests = self._group_requests(channels)
        self._by_id = {c.channel_id: c for c in channels}

    @staticmethod
    def _group_requests(channels: list[ChannelDefinition]) -> list[Request]:
        requests: list[Request] = []
        index: dict[tuple[str, Optional[str]], Request] = {}
        for channel in channels:
            key = channel.request_key
            request = index.get(key)
            if request is None:
                request = Request(request_byte=key[0], request_byte2=key[1])
                index[key] = request
                requests.append(request)
            request.records.append(channel)
        return requests

    def channel(self, channel_id: str) -> Optional[ChannelDefinition]:
        return self._by_id.get(channel_id)

    def request_for_channel(self, channel_id: str) -> Optional[Request]:
        for request in self.requests:
            if any(r.channel_id == channel_id for r in request.records):
                return request
        return None

    def request_by_bytes(self, request_byte: str) -> Optional[Request]:
        target = request_byte.upper()
        for request in self.requests:
            if request.request_byte == target:
                return request
        return None


def _to_float(value: Optional[str], default: float = 0.0) -> float:
    return float(value) if value not in (None, "") else default


def _to_int(value: Optional[str], default: int = 0) -> int:
    return int(value) if value not in (None, "") else default


def load_xml(path: Union[str, Path]) -> HeatPumpConfig:
    """Load channel definitions from a binding-style XML config file."""
    path = Path(path)
    tree = ET.parse(path)
    root = tree.getroot()
    channels: list[ChannelDefinition] = []
    for record in root.findall("record"):
        attrib = record.attrib
        channels.append(
            ChannelDefinition(
                channel_id=attrib["channelid"],
                request_byte=attrib["requestByte"],
                request_byte2=attrib.get("requestByte2"),
                data_type=DataType(attrib["dataType"]),
                position=_to_int(attrib.get("position")),
                length=_to_int(attrib.get("length")),
                scale=_to_float(attrib.get("scale"), 1.0),
                bit_position=_to_int(attrib.get("bitPosition")),
                min=_to_float(attrib.get("min")),
                max=_to_float(attrib.get("max")),
                step=_to_float(attrib.get("step")),
                unit=attrib.get("unit"),
            )
        )
    return HeatPumpConfig(channels, name=path.stem)


def load_yaml(path: Union[str, Path]) -> HeatPumpConfig:
    """Load channel definitions from a native YAML config file."""
    import yaml

    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    raw_channels = data.get("channels", data if isinstance(data, list) else [])
    channels = [ChannelDefinition(**item) for item in raw_channels]
    return HeatPumpConfig(channels, name=data.get("name", path.stem) if isinstance(data, dict) else path.stem)


def load_config(path: Union[str, Path]) -> HeatPumpConfig:
    """Load a config file, dispatching on the file extension."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return load_yaml(path)
    if suffix == ".xml":
        return load_xml(path)
    raise ValueError(f"Unsupported config format: {path.suffix}")
