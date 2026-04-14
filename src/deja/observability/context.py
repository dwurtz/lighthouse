"""Request-scoped context and log filtering for Deja observability.

A single contextvar threads a short request id (``req_<12 hex>``) through
every async frame in a given call tree. Log records get a
``[req_xxx] `` prefix injected by :class:`RequestIDLogFilter` whenever
a request is active, so any ``log.info`` line inside the scope is
automatically correlated without callers having to pass the id around.

The id space is intentionally small and opaque — it's a correlation
handle, not a security token. ``req_<12 hex>`` = 48 bits of entropy,
plenty for distinguishing concurrent requests within a process while
staying short enough for a user to read over the phone.

Public surface:
    new_request_id()          — mint + bind a fresh id
    current_request_id()      — peek at the active id (may be None)
    request_scope()           — async + sync contextmanager: enter, work, exit
    RequestIDLogFilter        — logging.Filter installed at import time
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import secrets
from typing import Iterator

_REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "deja_request_id", default=None
)


def new_request_id() -> str:
    """Generate a fresh request id, bind it to the contextvar, and return it.

    Format: ``req_<12 lowercase hex chars>`` — 48 bits of entropy.
    """
    rid = "req_" + secrets.token_hex(6)
    _REQUEST_ID.set(rid)
    return rid


def current_request_id() -> str | None:
    """Return the request id bound to the current context, or None."""
    return _REQUEST_ID.get()


@contextlib.contextmanager
def request_scope(request_id: str | None = None) -> Iterator[str]:
    """Context manager that pushes a request id onto the contextvar.

    Works for both sync (``with``) and async (``async with`` via
    ``@asynccontextmanager``-compatible use — but since contextvars
    respect asyncio task boundaries, the plain ``contextmanager`` works
    correctly inside ``async def`` as well). Nests cleanly: inner scope
    generates a new id, outer id is restored on exit.
    """
    rid = request_id or ("req_" + secrets.token_hex(6))
    token = _REQUEST_ID.set(rid)
    try:
        yield rid
    finally:
        _REQUEST_ID.reset(token)


class RequestIDLogFilter(logging.Filter):
    """Inject ``request_id`` + ``[req_xxx] `` prefix into every record.

    ``record.request_id`` is the bare id (or ``""``) so formatters that
    want it as a field can use ``%(request_id)s``. ``record.req_prefix``
    is the bracketed, space-suffixed form suitable for prepending to the
    default message format without producing ``[] `` noise when no
    request is active.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        rid = _REQUEST_ID.get()
        if rid:
            record.request_id = rid
            record.req_prefix = f"[{rid}] "
        else:
            record.request_id = ""
            record.req_prefix = ""
        return True


def install_log_filter() -> None:
    """Attach :class:`RequestIDLogFilter` to the root logger (idempotent)."""
    root = logging.getLogger()
    for f in root.filters:
        if isinstance(f, RequestIDLogFilter):
            return
    root.addFilter(RequestIDLogFilter())


# Install at import time so any logger that exists now or later
# inherits the filter via root propagation.
install_log_filter()
