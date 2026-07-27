"""Console entrypoint: ``python -m stiebel_heatpump.main`` or ``stiebel-heatpump-api``."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .api import build_app
from .settings import AppSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="Stiebel Eltron heat pump gateway REST API")
    parser.add_argument("--config", help="Path to an app YAML config file.", default="config/app.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper())
    settings = AppSettings.load(args.config)
    app = build_app(settings)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
