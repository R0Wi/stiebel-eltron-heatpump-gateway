from pathlib import Path

import pytest

from stiebel_heatpump.config_loader import load_config, load_xml, load_yaml
from stiebel_heatpump.models import DataType, ValueKind

ROOT = Path(__file__).resolve().parent.parent
DEVICE_CONFIGS = ROOT / "device_configs"


def test_load_all_xml_configs():
    for xml in (ROOT / "device_configs").glob("*.xml"):
        config = load_xml(xml)
        assert config.channels, f"{xml.name} produced no channels"
        # every channel must belong to exactly one request group
        grouped = sum(len(r.records) for r in config.requests)
        assert grouped == len(config.channels)


def test_grouping_shares_request(thz504_config):
    # All FB sensor channels share a single request round trip.
    fb = thz504_config.request_by_bytes("FB")
    assert fb is not None
    assert len(fb.records) > 5
    assert all(r.request_byte.upper() == "FB" for r in fb.records)


def test_value_kinds(thz504_config):
    cooling = thz504_config.channel("p99CoolingHC1Switch")
    assert cooling.value_kind == ValueKind.BOOLEAN
    assert cooling.writable is True
    inside = thz504_config.channel("insideTemperatureRC")
    assert inside.value_kind == ValueKind.NUMBER
    assert inside.data_type == DataType.SENSOR


def test_load_yaml_native_format():
    config = load_yaml(ROOT / "config" / "device.example.yaml")
    ids = {c.channel_id for c in config.channels}
    assert "outsideTemperature" in ids
    assert config.channel("coolingEnabled").value_kind == ValueKind.BOOLEAN


def test_load_config_dispatch():
    assert load_config(ROOT / "device_configs" / "LWZ_THZ504_7_59.xml").channels


BINDING_CONFIGS = (
    ROOT.parent
    / "bundles"
    / "org.openhab.binding.stiebelheatpump"
    / "src"
    / "main"
    / "resources"
    / "HeatpumpConfig"
)

# The only channels device_configs/ adds on top of the binding's own files:
# the writable clock registers the binding has no code path for (its setTime
# writes the read-only FC frame instead). See "Deviations from the binding" in
# the README.
_EXPECTED_EXTRA_CHANNELS = {
    "pClockDay", "pClockMonth", "pClockYear", "pClockHour", "pClockMinutes",
}


@pytest.mark.skipif(not BINDING_CONFIGS.is_dir(), reason="binding sources not present")
@pytest.mark.parametrize("path", sorted(DEVICE_CONFIGS.glob("*.xml")), ids=lambda p: p.name)
def test_device_config_matches_binding_resource(path):
    """The copies under device_configs/ must not drift from the binding's own
    XML files beyond the documented clock-register addition."""
    binding = BINDING_CONFIGS / path.name
    assert binding.is_file(), f"no binding counterpart for {path.name}"

    ours = {c.channel_id: c for c in load_xml(path).channels}
    theirs = {c.channel_id: c for c in load_xml(binding).channels}

    assert set(ours) - set(theirs) <= _EXPECTED_EXTRA_CHANNELS
    assert not set(theirs) - set(ours), "channels dropped from the copy"
    for channel_id, record in theirs.items():
        assert ours[channel_id] == record, f"{channel_id} differs from the binding"
