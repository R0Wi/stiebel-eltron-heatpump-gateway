import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from stiebel_heatpump.config_loader import load_xml  # noqa: E402

DEVICE_CONFIGS = ROOT / "device_configs"
THZ504 = DEVICE_CONFIGS / "LWZ_THZ504_7_59.xml"
THZ303_206 = DEVICE_CONFIGS / "LWZ_THZ303_2_06.xml"


@pytest.fixture(scope="session")
def thz504_config():
    return load_xml(THZ504)


@pytest.fixture(scope="session")
def thz303_206_config():
    return load_xml(THZ303_206)
