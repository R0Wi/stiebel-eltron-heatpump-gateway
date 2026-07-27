"""FastAPI application factory.

The set of endpoints is fixed and generic, but their OpenAPI description is
generated *from the loaded configuration*: the valid channel ids become an enum
on the path parameters, and a ``HeatPumpValues`` schema lists every channel with
its concrete JSON type, unit and (for settings) allowed range. Point the service
at a different device-definition file and the published spec changes with it.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional, Union

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config_loader import HeatPumpConfig, load_config
from .models import ChannelDefinition, DataType, ValueKind
from .protocol.parser import ProtocolError, WriteNotConfirmed
from .service import ChannelNotFound, ChannelNotWritable, HeatPumpService
from .settings import AppSettings

logger = logging.getLogger(__name__)


# -- response / request models ----------------------------------------------


class ChannelInfo(BaseModel):
    channel_id: str
    data_type: DataType
    value_kind: ValueKind
    writable: bool
    unit: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None

    @classmethod
    def from_definition(cls, record: ChannelDefinition) -> "ChannelInfo":
        settings_only = record.writable
        return cls(
            channel_id=record.channel_id,
            data_type=record.data_type,
            value_kind=record.value_kind,
            writable=record.writable,
            unit=record.unit,
            min=record.min if settings_only else None,
            max=record.max if settings_only else None,
            step=record.step if settings_only else None,
        )


class WriteRequest(BaseModel):
    value: Union[bool, int, float, str]


class ValueResponse(BaseModel):
    channel_id: str
    value: Union[bool, int, float, None]
    unit: Optional[str] = None
    data_type: DataType
    writable: bool


# -- app factory -------------------------------------------------------------


def get_service(request: Request) -> HeatPumpService:
    service: Optional[HeatPumpService] = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Heat pump service is not available")
    return service


def build_app(
    settings: AppSettings,
    config: Optional[HeatPumpConfig] = None,
    service: Optional[HeatPumpService] = None,
) -> FastAPI:
    """Create the FastAPI application for the given settings/config."""
    config = config or load_config(settings.device_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = service or HeatPumpService.from_settings(settings, config)
        app.state.service = svc
        if settings.connect_on_startup:
            try:
                svc.connect()
            except Exception:  # noqa: BLE001 - stay up even if the device is absent
                logger.exception("Could not connect to heat pump on startup")
        try:
            yield
        finally:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error while closing heat pump service")

    app = FastAPI(
        title=settings.title,
        version="1.0.0",
        description=(
            f"Generic REST API for a Stiebel Eltron / Tecalor heat pump "
            f"(configuration: `{config.name}`, {len(config.channels)} channels)."
        ),
        lifespan=lifespan,
    )
    app.state.config = config

    # Order does not matter: Starlette walks the exception's MRO and picks the
    # most specific registered handler, so WriteNotConfirmed (a ProtocolError)
    # gets 502 while everything else protocol-related gets 503.
    app.add_exception_handler(ProtocolError, _protocol_error_handler)
    app.add_exception_handler(WriteNotConfirmed, _write_not_confirmed_handler)
    _register_routes(app)
    _install_dynamic_openapi(app, config)
    return app


async def _protocol_error_handler(request: Request, exc: ProtocolError) -> JSONResponse:
    """Map a device/transport communication failure to 503, everywhere.

    This applies to every route, not just channel reads/writes: an
    unreachable or misbehaving heat pump is a transport-level problem, and
    must never be reported as "channel not found" (404) or silently papered
    over with a stale/guessed value.
    """
    logger.warning("Heat pump communication error: %s", exc)
    return JSONResponse(
        status_code=503,
        content={"detail": f"Could not communicate with the heat pump: {exc}"},
    )


async def _write_not_confirmed_handler(request: Request, exc: WriteNotConfirmed) -> JSONResponse:
    """Map a write the device refused to confirm to 502.

    The serial link is working -- the device answered, it just did not accept
    the SET -- so this is neither "device unreachable" (503) nor a success. It
    must not be a 200: the value on the device is still the old one.
    """
    logger.warning("Heat pump did not confirm write: %s", exc)
    return JSONResponse(status_code=502, content={"detail": str(exc)})


def _register_routes(app: FastAPI) -> None:
    @app.get("/", tags=["meta"], summary="Service information")
    def root(request: Request) -> dict:
        config: HeatPumpConfig = request.app.state.config
        return {
            "name": app.title,
            "configuration": config.name,
            "channels": len(config.channels),
            "requests": len(config.requests),
        }

    @app.get("/health", tags=["meta"], summary="Liveness probe")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/version", tags=["meta"], summary="Heat pump firmware version")
    def version(service: HeatPumpService = Depends(get_service)) -> dict:
        return {"version": service.get_version()}

    @app.get("/channels", tags=["channels"], summary="List all configured channels",
             response_model=list[ChannelInfo])
    def list_channels(
        request: Request,
        data_type: Optional[DataType] = Query(None, description="Filter by channel category."),
        writable: Optional[bool] = Query(None, description="Filter by writability."),
    ) -> list[ChannelInfo]:
        config: HeatPumpConfig = request.app.state.config
        infos = [ChannelInfo.from_definition(c) for c in config.channels]
        if data_type is not None:
            infos = [i for i in infos if i.data_type == data_type]
        if writable is not None:
            infos = [i for i in infos if i.writable == writable]
        return infos

    @app.get("/values", tags=["values"], summary="Read multiple channel values",
             response_model=list[ValueResponse])
    def read_values(
        ids: Optional[str] = Query(None, description="Comma-separated channel ids to read."),
        data_type: Optional[DataType] = Query(
            None,
            description=(
                "Read all channels of this category. Ignored when `ids` is given; "
                "when neither is given, Sensor and Status channels are read."
            ),
        ),
        service: HeatPumpService = Depends(get_service),
    ) -> list[ValueResponse]:
        if ids:
            channel_ids = [part.strip() for part in ids.split(",") if part.strip()]
            try:
                values = service.read_channels(channel_ids)
            except ChannelNotFound as exc:
                raise HTTPException(
                    status_code=404, detail=f"Unknown channel(s): {exc.args[0]}"
                ) from exc
        else:
            types = {data_type} if data_type else None
            values = service.read_all(types)
        return [ValueResponse(**v.model_dump()) for v in values]

    @app.get("/channels/{channel_id}", tags=["values"], summary="Read a single channel value",
             response_model=ValueResponse)
    def read_channel(channel_id: str, service: HeatPumpService = Depends(get_service)) -> ValueResponse:
        try:
            value = service.read_channel(channel_id)
        except ChannelNotFound as exc:
            raise HTTPException(status_code=404, detail=f"Unknown channel '{channel_id}'") from exc
        return ValueResponse(**value.model_dump())

    @app.put("/channels/{channel_id}", tags=["values"], summary="Write a settings channel",
             response_model=ValueResponse)
    def write_channel(
        channel_id: str,
        body: WriteRequest = Body(...),
        service: HeatPumpService = Depends(get_service),
    ) -> ValueResponse:
        try:
            value = service.write_channel(channel_id, body.value)
        except ChannelNotFound as exc:
            raise HTTPException(status_code=404, detail=f"Unknown channel '{channel_id}'") from exc
        except ChannelNotWritable as exc:
            raise HTTPException(
                status_code=409, detail=f"Channel '{channel_id}' is not writable"
            ) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ValueResponse(**value.model_dump())

    @app.post("/actions/set-time", tags=["actions"], summary="Sync heat pump clock to system time")
    def set_time(service: HeatPumpService = Depends(get_service)) -> dict:
        return service.set_time()


# -- dynamic OpenAPI ---------------------------------------------------------


def _openapi_type(kind: ValueKind) -> dict:
    if kind == ValueKind.BOOLEAN:
        return {"type": "boolean"}
    if kind == ValueKind.NUMBER:
        return {"type": "number"}
    return {"type": "integer"}


def _install_dynamic_openapi(app: FastAPI, config: HeatPumpConfig) -> None:
    channel_ids = [c.channel_id for c in config.channels]

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )

        # 1) Constrain the {channel_id} path parameter to the configured ids.
        for path, item in schema.get("paths", {}).items():
            if "{channel_id}" not in path:
                continue
            for operation in item.values():
                for param in operation.get("parameters", []):
                    if param.get("name") == "channel_id" and param.get("in") == "path":
                        param["schema"] = {"type": "string", "enum": channel_ids}
                        param["description"] = "One of the configured channel ids."

        # 2) A schema describing every channel value, keyed by channel id.
        properties: dict[str, dict] = {}
        for record in config.channels:
            prop = _openapi_type(record.value_kind)
            description = f"{record.data_type.value} channel"
            if record.unit:
                description += f" [{record.unit}]"
            if record.writable:
                description += f" (writable, {record.min}..{record.max})"
            prop["description"] = description
            properties[record.channel_id] = prop
        schema.setdefault("components", {}).setdefault("schemas", {})["HeatPumpValues"] = {
            "type": "object",
            "description": "All channels of the configured device with their value types.",
            "properties": properties,
        }

        # 3) Group channels by category as an informational extension.
        schema["x-heatpump-channels"] = {
            "configuration": config.name,
            "counts": {
                data_type.value: sum(1 for c in config.channels if c.data_type == data_type)
                for data_type in DataType
            },
        }

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
