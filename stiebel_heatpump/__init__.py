"""Stiebel Eltron heat pump REST API.

A standalone, framework-independent port of the business logic of the openHAB
``stiebelheatpump`` binding. It talks to Stiebel Eltron / Tecalor LWZ / THZ heat
pumps over their USB serial interface and exposes a generic, configuration-driven
REST API built with FastAPI.

The package is split into small, testable layers:

* :mod:`stiebel_heatpump.protocol.parser` -- the wire protocol (checksum,
  byte (de)escaping, record encode/decode). Pure functions, no I/O.
* :mod:`stiebel_heatpump.protocol.transport` -- byte transports (real serial
  port via ``pyserial`` and an in-memory simulator).
* :mod:`stiebel_heatpump.protocol.connector` -- the request/response handshake.
* :mod:`stiebel_heatpump.protocol.communication` -- high level read/write.
* :mod:`stiebel_heatpump.config_loader` / :mod:`stiebel_heatpump.models` --
  the channel (data point) definitions loaded from configuration.
* :mod:`stiebel_heatpump.service` -- a thread-safe facade over a connection.
* :mod:`stiebel_heatpump.api` -- the FastAPI application factory.
"""

__version__ = "1.0.0"
