"""Structured logging configuration using structlog."""

import logging
import re
import sys

import structlog


class RedactWebSocketQueryFilter(logging.Filter):
    """Sanitizes query parameters from /ws/ route access logs to prevent token leaks.

    Browser WebSockets must pass JWT credentials via query params (?token=...).
    This filter intercepts access log records and removes the query string so
    sensitive tokens never appear in log sinks.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            args_list = list(record.args)
            for i, arg in enumerate(args_list):
                if isinstance(arg, str) and "/ws/" in arg and "?" in arg:
                    args_list[i] = arg.split("?")[0]
            record.args = tuple(args_list)

        if isinstance(record.msg, str) and "/ws/" in record.msg and "?" in record.msg:
            record.msg = re.sub(r"(/ws/[^?\s]+)\?[^\s]+", r"\1", record.msg)

        return True


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog with console rendering for development."""

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )

    # Attach redaction filter to uvicorn access logger
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.addFilter(RedactWebSocketQueryFilter())
