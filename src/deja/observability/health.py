"""Live health monitor — writes ``~/.deja/health.json`` every 15s.

The Swift UI polls this file and renders a health chip + loud
failure states. Contract with the Swift sibling agent:

* File path: ``~/.deja/health.json``
* Atomic write (tmp + ``os.replace``) so a partial read never happens.
* Rewritten every 15s so the timestamp itself is freshness proof —
  if Swift sees a stale file, the Python process is wedged or dead.
* ``overall`` is the worst status across all checks (broken > degraded > ok).
* ``detail`` is truncated to 120 chars so the UI has predictable room.
* ``last_error_request_id`` is the request_id of the last
  ``~/.deja/errors.jsonl`` line, to help the user bundle logs for support.

This module is deliberately standalone — no Sentry, no APM, no external
deps beyond ``httpx`` (already pulled in for the proxy call).

Individual checks are ``async def check_<name>(self) -> dict`` methods
returning a check entry. They never raise: every failure mode is
translated into a check status with a user-facing ``detail``/``fix``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


_HEALTH_INTERVAL_SECONDS = 15
_DETAIL_MAX = 120
_APP_VERSION = "0.2.0"  # matches telemetry._VERSION


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _truncate(s: str, n: int = _DETAIL_MAX) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def _health_path() -> Path:
    from deja.config import DEJA_HOME
    return Path(DEJA_HOME) / "health.json"


def _aggregate_overall(checks: list[dict]) -> str:
    statuses = {c.get("status") for c in checks}
    if "broken" in statuses:
        return "broken"
    if "degraded" in statuses:
        return "degraded"
    return "ok"


def _entry(
    id_: str,
    label: str,
    status: str,
    detail: str,
    fix: str | None = None,
    fix_url: str | None = None,
) -> dict:
    return {
        "id": id_,
        "label": label,
        "status": status,
        "detail": _truncate(detail),
        "fix": fix,
        "fix_url": fix_url,
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` via tmp + os.replace. Never partial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".health.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _last_error_request_id() -> str | None:
    """Peek at the last line of ``~/.deja/errors.jsonl`` for its request_id."""
    from deja.config import DEJA_HOME
    path = Path(DEJA_HOME) / "errors.jsonl"
    if not path.exists():
        return None
    try:
        # errors.jsonl grows; read tail only.
        with path.open("rb") as f:
            try:
                f.seek(-4096, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail = f.read().decode("utf-8", errors="replace")
        last = tail.strip().splitlines()[-1] if tail.strip() else ""
        if not last:
            return None
        rid = json.loads(last).get("request_id")
        return rid if isinstance(rid, str) else None
    except Exception:
        return None


def _count_errors_since(since_ts: float) -> int:
    """Count rows in errors.jsonl whose timestamp is >= ``since_ts``."""
    from deja.config import DEJA_HOME
    path = Path(DEJA_HOME) / "errors.jsonl"
    if not path.exists():
        return 0
    n = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = row.get("timestamp") or ""
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt.timestamp() >= since_ts:
                        n += 1
                except Exception:
                    continue
    except Exception:
        return n
    return n


def _count_signals_since(since_ts: float) -> int:
    """Count rows in observations.jsonl with ts >= since_ts.

    Rows look like ``{"ts": "2026-04-12T22:30:00Z", ...}``. Malformed lines
    are skipped silently.
    """
    from deja.config import DEJA_HOME
    observations_log = Path(DEJA_HOME) / "observations.jsonl"
    if not observations_log.exists():
        return 0
    n = 0
    try:
        with observations_log.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = row.get("ts") or row.get("timestamp") or ""
                try:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    if dt.timestamp() >= since_ts:
                        n += 1
                except Exception:
                    continue
    except Exception:
        return n
    return n


def _latest_screen_age_seconds() -> float | None:
    """Age of ``~/.deja/latest_screen_ts.txt`` in seconds, or None if missing."""
    from deja.config import DEJA_HOME
    path = Path(DEJA_HOME) / "latest_screen_ts.txt"
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        ts = float(raw)
        age = _now_ts() - ts
        return max(age, 0.0)
    except Exception:
        return None


def _latest_cycle_age_seconds() -> float | None:
    """Seconds since the last ``wiki_write`` / ``cycle_no_op`` audit row.

    Returns None if audit.jsonl is missing / empty / unparseable.
    """
    from deja.audit import AUDIT_LOG
    if not AUDIT_LOG.exists():
        return None
    latest: float | None = None
    try:
        # Read the file backwards-ish: slurp the tail only.
        with AUDIT_LOG.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in reversed(lines[-500:]):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("action") not in ("wiki_write", "cycle_no_op"):
                continue
            ts = row.get("ts") or ""
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                latest = dt.timestamp()
                break
            except Exception:
                continue
    except Exception:
        return None
    if latest is None:
        return None
    return max(_now_ts() - latest, 0.0)


# ---------------------------------------------------------------------------
# HealthChecker
# ---------------------------------------------------------------------------


class HealthChecker:
    """Runs all checks every 15s and writes ``~/.deja/health.json``.

    ``start()`` returns an awaitable that loops until cancelled. ``run()``
    performs one cycle and returns the written dict, so tests and one-shot
    callers can drive it without scheduling.
    """

    def __init__(self) -> None:
        self._last_proxy_ok_ts: float | None = None

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    async def check_proxy(self) -> dict:
        from deja.llm_client import DEJA_API_URL
        url = f"{DEJA_API_URL}/v1/health"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url)
            if 200 <= r.status_code < 300:
                self._last_proxy_ok_ts = _now_ts()
                return _entry(
                    "proxy",
                    "LLM server",
                    "ok",
                    "Reachable, last success 0s ago",
                    None,
                )
            return _entry(
                "proxy",
                "LLM server",
                "broken",
                f"HTTP {r.status_code} from {url}",
                "Deja's backend is restarting. Give it a minute.",
            )
        except httpx.TimeoutException:
            return _entry(
                "proxy",
                "LLM server",
                "broken",
                f"Unreachable: timeout after 5s ({url})",
                "Deja's backend is restarting. Give it a minute.",
            )
        except Exception as e:
            summary = type(e).__name__ + (f": {e}" if str(e) else "")
            return _entry(
                "proxy",
                "LLM server",
                "broken",
                f"Unreachable: {summary}",
                "Deja's backend is restarting. Give it a minute.",
            )

    async def check_recent_signals(self) -> dict:
        since = _now_ts() - 5 * 60
        n = _count_signals_since(since)
        if n >= 1:
            return _entry(
                "recent_signals",
                "Observing your activity",
                "ok",
                f"{n} signals in the last 5 min",
            )

        # No fresh signals. Is the monitor alive?
        screen_age = _latest_screen_age_seconds()
        monitor_alive = screen_age is not None and screen_age <= 60
        if monitor_alive:
            return _entry(
                "recent_signals",
                "Observing your activity",
                "degraded",
                "No new signals in 5 min, but screen capture is live",
            )
        return _entry(
            "recent_signals",
            "Observing your activity",
            "broken",
            "No signals and the monitor isn't capturing the screen",
            (
                "The background monitor stopped observing. Check System "
                "Settings \u2192 Privacy & Security \u2192 Screen Recording."
            ),
        )

    async def check_latest_screen(self) -> dict:
        age = _latest_screen_age_seconds()
        if age is None:
            return _entry(
                "latest_screen",
                "Screen capture",
                "broken",
                "latest_screen_ts.txt missing",
                (
                    "Screen capture isn't working. Check System Settings "
                    "\u2192 Privacy & Security \u2192 Screen Recording and "
                    "make sure Deja is enabled."
                ),
            )
        if age < 30:
            return _entry(
                "latest_screen",
                "Screen capture",
                "ok",
                f"Last frame {int(age)}s ago",
            )
        if age <= 120:
            return _entry(
                "latest_screen",
                "Screen capture",
                "degraded",
                f"Last frame {int(age)}s ago (expected <30s)",
            )
        return _entry(
            "latest_screen",
            "Screen capture",
            "broken",
            f"Last frame {int(age)}s ago \u2014 capture stalled",
            (
                "Screen capture isn't working. Check System Settings "
                "\u2192 Privacy & Security \u2192 Screen Recording and "
                "make sure Deja is enabled."
            ),
        )

    async def check_wiki(self) -> dict:
        from deja.config import WIKI_DIR
        if not WIKI_DIR.exists():
            return _entry(
                "wiki",
                "Wiki ready",
                "broken",
                f"Wiki directory {WIKI_DIR} is missing",
                "Deja's wiki directory is missing. Re-run setup.",
            )
        if not os.access(WIKI_DIR, os.W_OK):
            return _entry(
                "wiki",
                "Wiki ready",
                "broken",
                f"Wiki directory {WIKI_DIR} is not writable",
                "Deja's wiki directory is missing. Re-run setup.",
            )
        index = WIKI_DIR / "index.md"
        if not index.exists():
            return _entry(
                "wiki",
                "Wiki ready",
                "broken",
                "wiki/index.md is missing",
                "Deja's wiki directory is missing. Re-run setup.",
            )
        return _entry(
            "wiki",
            "Wiki ready",
            "ok",
            f"Writable with index.md at {WIKI_DIR}",
        )

    async def check_goals(self) -> dict:
        from deja.config import WIKI_DIR
        goals = WIKI_DIR / "goals.md"
        if goals.exists():
            try:
                goals.read_text(encoding="utf-8")
                return _entry(
                    "goals",
                    "Goals file",
                    "ok",
                    "goals.md present",
                )
            except Exception as e:
                return _entry(
                    "goals",
                    "Goals file",
                    "degraded",
                    f"goals.md exists but unreadable: {e}",
                )
        return _entry(
            "goals",
            "Goals file",
            "degraded",
            "goals.md missing \u2014 due-reminders surface will be empty",
        )

    async def check_recent_errors(self) -> dict:
        since = _now_ts() - 60 * 60
        n = _count_errors_since(since)
        if n == 0:
            return _entry(
                "recent_errors",
                "Error rate",
                "ok",
                "No recent errors",
            )
        if n < 5:
            return _entry(
                "recent_errors",
                "Error rate",
                "degraded",
                f"{n} errors in the last hour",
            )
        return _entry(
            "recent_errors",
            "Error rate",
            "broken",
            f"{n} errors in the last hour \u2014 something's wrong",
            "Open Deja's support share to bundle logs.",
        )

    async def check_integrate(self) -> dict:
        age = _latest_cycle_age_seconds()
        if age is None:
            return _entry(
                "integrate",
                "Analysis cycles",
                "broken",
                "No wiki_write or cycle_no_op entries in audit.jsonl",
                "The analysis loop stopped running. Quit and relaunch D\u00e9j\u00e0.",
            )
        mins = age / 60
        if mins <= 15:
            return _entry(
                "integrate",
                "Analysis cycles",
                "ok",
                f"Last cycle {int(mins)} min ago",
            )
        if mins <= 45:
            return _entry(
                "integrate",
                "Analysis cycles",
                "degraded",
                f"Last cycle {int(mins)} min ago (expected <15)",
            )
        return _entry(
            "integrate",
            "Analysis cycles",
            "broken",
            f"Last cycle {int(mins)} min ago \u2014 loop stalled",
            "The analysis loop stopped running. Quit and relaunch D\u00e9j\u00e0.",
        )

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        """Run all checks, write health.json atomically, return the payload."""
        check_coros = [
            self.check_proxy(),
            self.check_recent_signals(),
            self.check_latest_screen(),
            self.check_wiki(),
            self.check_goals(),
            self.check_recent_errors(),
            self.check_integrate(),
        ]
        results = await asyncio.gather(*check_coros, return_exceptions=True)

        checks: list[dict] = []
        fallback_ids = [
            "proxy", "recent_signals", "latest_screen",
            "wiki", "goals", "recent_errors", "integrate",
        ]
        for cid, res in zip(fallback_ids, results):
            if isinstance(res, BaseException):
                log.debug("health check %s crashed", cid, exc_info=res)
                checks.append(_entry(
                    cid,
                    cid,
                    "broken",
                    f"check crashed: {type(res).__name__}: {res}",
                ))
            else:
                checks.append(res)

        payload = {
            "timestamp": _utc_now_iso(),
            "overall": _aggregate_overall(checks),
            "checks": checks,
            "app_version": _APP_VERSION,
            "last_error_request_id": _last_error_request_id(),
        }

        try:
            _atomic_write_json(_health_path(), payload)
        except Exception:
            log.exception("health: failed to write health.json")

        return payload

    async def start(self) -> None:
        """Loop ``run()`` every 15s until cancelled. Log startup once."""
        log.info(
            "Health monitor started \u2014 check interval: %ds, "
            "health file: ~/.deja/health.json",
            _HEALTH_INTERVAL_SECONDS,
        )
        try:
            while True:
                try:
                    await self.run()
                except Exception:
                    log.exception("health: run() raised; continuing")
                await asyncio.sleep(_HEALTH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("Health monitor cancelled")
            raise


__all__ = ["HealthChecker"]
