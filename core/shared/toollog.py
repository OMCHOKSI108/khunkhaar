"""Structured logging for MCP tool execution.

Every tool call is logged with its arguments, but only after redaction, so the log file is safe
to keep, attach to a report, or hand to an agent. Secrets never reach disk here.
"""

from __future__ import annotations

import json
import logging
import time
from functools import wraps
from typing import Any, Callable

from . import paths
from .redaction import redact_obj

_CONFIGURED: set[str] = set()


def get_logger(name: str, filename: str) -> logging.Logger:
    logger = logging.getLogger(f"khunkhaar.{name}")
    if name in _CONFIGURED:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(paths.logs_dir() / filename, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    _CONFIGURED.add(name)
    return logger


def logged(logger: logging.Logger) -> Callable:
    """Wrap a tool function: record start, duration, outcome, and a redacted argument echo."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            safe_args = redact_obj(kwargs)
            logger.info("CALL %s args=%s", func.__name__, json.dumps(safe_args, default=str)[:1500])
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                logger.exception("ERROR %s after %sms: %s", func.__name__,
                                 round((time.monotonic() - started) * 1000, 2), type(exc).__name__)
                raise
            elapsed = round((time.monotonic() - started) * 1000, 2)
            if isinstance(result, dict):
                logger.info("DONE %s %sms ok=%s status=%s error=%s", func.__name__, elapsed,
                            result.get("ok"), result.get("status_code"), result.get("error_kind"))
            else:
                logger.info("DONE %s %sms", func.__name__, elapsed)
            return result

        return wrapper

    return decorator


def logged_async(logger: logging.Logger) -> Callable:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            logger.info("CALL %s args=%s", func.__name__, json.dumps(redact_obj(kwargs), default=str)[:1500])
            try:
                result = await func(*args, **kwargs)
            except Exception:
                logger.exception("ERROR %s after %sms", func.__name__,
                                 round((time.monotonic() - started) * 1000, 2))
                raise
            logger.info("DONE %s %sms ok=%s", func.__name__,
                        round((time.monotonic() - started) * 1000, 2),
                        result.get("ok") if isinstance(result, dict) else None)
            return result

        return wrapper

    return decorator
