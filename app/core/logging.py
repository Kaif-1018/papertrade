"""Structured logging configuration using structlog.

Includes:
- JSON or console formatted output
- Contextvars merge (request_id, etc.)
- Automatic redaction of credentials (passwords, tokens, authorization, secrets)
- Query string sanitation for WebSocket URLs in access logs
- Dedicated audit logger instance
"""

from collections.abc import MutableMapping
import logging
import re
import sys
from typing import Any

import structlog

REDACT_KEYS = ("password", "token", "authorization", "secret")


def redact_sensitive_data(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Processor to automatically redact sensitive credentials from structured log entries."""
    for key in list(event_dict.keys()):
        key_lower = key.lower()
        if any(secret_term in key_lower for secret_term in REDACT_KEYS):
            event_dict[key] = "[REDACTED]"
    return event_dict


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


def setup_logging(log_level: str = "INFO", log_format: str = "console") -> None:
    """Configure structlog and standard logging."""
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if log_format.lower() == "json"
        else structlog.dev.ConsoleRenderer()
    )

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
            redact_sensitive_data,
            structlog.processors.UnicodeDecoder(),
            renderer,
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


# Dedicated audit logger for compliance and security events
audit_logger = structlog.stdlib.get_logger("audit")
