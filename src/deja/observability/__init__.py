"""Deja observability — request ids, typed errors, two-sink reporting.

Modeled after tru.link's pattern: file-based, stdlib-only, no external
APM. Public API re-exported here so call sites use a single module.
"""

from __future__ import annotations

from deja.observability.context import (
    RequestIDLogFilter,
    current_request_id,
    install_log_filter,
    new_request_id,
    request_scope,
)
from deja.observability.errors import (
    AuthError,
    ConfigError,
    DejaError,
    LLMError,
    ProxyUnavailable,
    RateLimitError,
    ToolError,
)
from deja.observability.health import HealthChecker
from deja.observability.reporter import report_error

__all__ = [
    "AuthError",
    "HealthChecker",
    "ConfigError",
    "DejaError",
    "LLMError",
    "ProxyUnavailable",
    "RateLimitError",
    "RequestIDLogFilter",
    "ToolError",
    "current_request_id",
    "install_log_filter",
    "new_request_id",
    "report_error",
    "request_scope",
]
