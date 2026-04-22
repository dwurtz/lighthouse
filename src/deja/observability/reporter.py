"""Two-sink error reporting.

Every :class:`DejaError` worth surfacing flows through :func:`report_error`:

* always appended to ``~/.deja/errors.jsonl`` (one JSON object per line,
  grows unbounded for now — rotation is a later concern),
* optionally atomic-written to ``~/.deja/latest_error.json`` when
  ``visible_to_user`` is True (Swift polls this file every 2s to show a
  toast),
* always logged at ERROR level with the request-id prefix injected by
  :class:`RequestIDLogFilter`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from deja.observability.errors import DejaError

log = logging.getLogger(__name__)


def _errors_log_path() -> Path:
    from deja.config import DEJA_HOME
    return Path(DEJA_HOME) / "errors.jsonl"


def _append_error_line(payload: dict) -> None:
    path = _errors_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, default=str)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# Consecutive ProxyUnavailable count. Reset on success via
# ``mark_proxy_ok``. First failure in a run of failures is suppressed
# from the user-visible toast — laptop wake, Render cold starts, and
# brief network hiccups resolve on their own within seconds and don't
# need a scary red banner.
_consecutive_proxy_fails = 0


def mark_proxy_ok() -> None:
    """Reset the proxy-failure debounce counter after a successful call."""
    global _consecutive_proxy_fails
    _consecutive_proxy_fails = 0


def _is_proxy_error(err: DejaError) -> bool:
    from deja.observability.errors import ProxyUnavailable
    return isinstance(err, ProxyUnavailable)


def report_error(err: DejaError, *, visible_to_user: bool = True) -> None:
    """Record an error to both sinks and log it.

    * ``errors.jsonl`` — always appended.
    * ``latest_error.json`` — rewritten atomically iff ``visible_to_user``.
    * root logger — ``log.error`` with the request-id prefix.

    This function never raises: sink failures are logged and swallowed
    so a disk issue in the reporter can't cascade into the caller's
    error path.

    Transient ``ProxyUnavailable`` debouncing: the first consecutive
    failure is suppressed from the user-visible toast (still logged to
    errors.jsonl for debugging). This absorbs laptop-wake blips, Render
    cold starts, and brief network hiccups. A second consecutive
    failure escalates normally — if the proxy is actually down, the
    user sees it on the next retry.
    """
    global _consecutive_proxy_fails
    payload = err.to_payload()

    try:
        _append_error_line(payload)
    except Exception:
        log.exception("report_error: failed to append errors.jsonl")

    if visible_to_user and _is_proxy_error(err):
        _consecutive_proxy_fails += 1
        if _consecutive_proxy_fails < 2:
            log.info(
                "Suppressed transient ProxyUnavailable (count=%d) — "
                "not escalating to user-visible toast yet.",
                _consecutive_proxy_fails,
            )
            visible_to_user = False

    if visible_to_user:
        try:
            err.to_user_error_file()
        except Exception:
            log.exception("report_error: failed to write latest_error.json")

    try:
        log.error(
            "%s [%s] %s details=%s",
            type(err).__name__,
            err.code,
            err.user_message,
            err.details,
        )
    except Exception:
        pass
