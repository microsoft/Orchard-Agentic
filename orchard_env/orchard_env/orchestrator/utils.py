"""Utility functions."""

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

from orchard_env.orchestrator.settings import settings

# Context variable for request ID
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_SERVICE_CAPABILITY_PATH = re.compile(r"/s/[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


class CapabilityPathRedactionFilter(logging.Filter):
    """Remove service bearer tokens from every log record and argument."""

    @staticmethod
    def _redact(value):
        if isinstance(value, str):
            return _SERVICE_CAPABILITY_PATH.sub("/s/<redacted>", value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: self._redact(value) for key, value in record.args.items()
            }
        return True


class JSONFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data: dict[str, Any] = {
            "timestamp": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add request ID if available
        request_id = request_id_var.get("")
        if request_id:
            log_data["request_id"] = request_id

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add extra fields
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)

        return json.dumps(log_data)


def setup_logging() -> None:
    """Configure application logging."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.log_level.upper()))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create console handler
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(CapabilityPathRedactionFilter())

    if settings.log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    root_logger.addHandler(handler)


def generate_request_id() -> str:
    """Generate a unique request ID."""
    return str(uuid.uuid4())


def generate_sandbox_id() -> str:
    """Generate a unique sandbox ID."""
    return str(uuid.uuid4())[:8]


def generate_job_id() -> str:
    """Generate a unique job ID."""
    return str(uuid.uuid4())
