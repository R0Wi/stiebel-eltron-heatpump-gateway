"""FastAPI integration tests against the simulator, including dynamic OpenAPI."""

import pytest
from fastapi.testclient import TestClient

from stiebel_heatpump.api import build_app
from stiebel_heatpump.protocol.transport import SimulatorTransport
from stiebel_heatpump.service import HeatPumpService
from stiebel_heatpump.settings import AppSettings
from stiebel_heatpump.simulator import HeatPumpSimulator


@pytest.fixture
def client(thz504_config):
    simulator = HeatPumpSimulator(
        thz504_config,
        seed_values={"version": 7.59, "outsideTemperature": 12.3, "p99CoolingHC1Switch": False},
    )
    service = HeatPumpService(thz504_config, SimulatorTransport(simulator), waiting_time_ms=0)
    settings = AppSettings(device_config="unused", transport="simulator")
    app = build_app(settings, config=thz504_config, service=service)
    with TestClient(app) as test_client:
        yield test_client


def test_root(client):
    body = client.get("/").json()
    assert body["configuration"] == "LWZ_THZ504_7_59"
    assert body["channels"] > 100


def test_version_endpoint(client):
    assert client.get("/version").json() == {"version": "7.59"}


def test_list_channels_filtered(client):
    writable = client.get("/channels", params={"writable": True}).json()
    assert writable
    assert all(item["writable"] for item in writable)


def test_read_single_channel(client):
    body = client.get("/channels/outsideTemperature").json()
    assert body["channel_id"] == "outsideTemperature"
    assert body["value"] == pytest.approx(12.3, abs=0.05)


def test_read_unknown_channel_404(client):
    assert client.get("/channels/doesNotExist").status_code == 404


def test_write_channel(client):
    resp = client.put("/channels/p99CoolingHC1Switch", json={"value": True})
    assert resp.status_code == 200
    assert resp.json()["value"] is True
    # read back
    assert client.get("/channels/p99CoolingHC1Switch").json()["value"] is True


def test_write_non_writable_409(client):
    resp = client.put("/channels/outsideTemperature", json={"value": 5})
    assert resp.status_code == 409


def test_read_values_by_type(client):
    resp = client.get("/values", params={"data_type": "Sensor"})
    assert resp.status_code == 200
    assert any(v["channel_id"] == "outsideTemperature" for v in resp.json())


def test_openapi_reflects_config(client):
    spec = client.get("/openapi.json").json()

    # channel_id path parameter is constrained to the configured ids
    param = spec["paths"]["/channels/{channel_id}"]["get"]["parameters"][0]
    assert param["name"] == "channel_id"
    assert "outsideTemperature" in param["schema"]["enum"]

    # a per-channel value schema is generated from the config
    values_schema = spec["components"]["schemas"]["HeatPumpValues"]["properties"]
    assert values_schema["outsideTemperature"]["type"] == "number"
    assert values_schema["p99CoolingHC1Switch"]["type"] == "boolean"

    # informational extension
    assert spec["x-heatpump-channels"]["configuration"] == "LWZ_THZ504_7_59"
