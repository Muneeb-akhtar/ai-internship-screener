"""Structured logging with structlog.

Logs are key/value events instead of free text, e.g.
    {"event": "request", "method": "GET", "path": "/health", "status": 200, ...}
In prod they are printed as JSON (easy to search/ship); in dev as coloured text.
"""

import logging

import structlog


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # include per-request context
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
