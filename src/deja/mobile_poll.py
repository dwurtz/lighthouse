"""Poll the Deja proxy's mobile inbox and route notes into cos.

The proxy queues POSTs from iOS Shortcuts (Action Button, Back Tap,
screenshot automations, share-sheet) into ``/v1/inbox``. This module
drains the queue every ``POLL_INTERVAL_SEC`` seconds and hands each
item to ``chief_of_staff.invoke_command_sync`` with ``source="mobile"``,
so mobile notes land in the same conversations/ store as voice, email,
and notch chat.

Run via ``deja mobile poll`` (foreground) or by enabling the launchd
agent (followup). Auth uses the user's existing Google bearer token
from ``~/.deja/auth.json`` / Keychain via ``deja.auth.get_auth_token``.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from deja.auth import get_auth_token
from deja.llm_client import DEJA_API_URL

log = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5.0
_IDLE_BACKOFF_MAX_SEC = 60.0


async def _drain_once() -> list[dict]:
    """Fetch and drain pending mobile inbox items."""
    token = get_auth_token()
    if not token:
        raise RuntimeError("No Deja auth token — run `deja configure` first.")
    url = f"{DEJA_API_URL}/v1/inbox/drain"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers=headers)
    if resp.status_code == 401:
        raise RuntimeError("Deja proxy rejected token (401). Token expired?")
    resp.raise_for_status()
    body = resp.json()
    return body.get("items") or []


def _route_item(item: dict) -> None:
    """Hand one mobile item to cos in command mode."""
    from deja import chief_of_staff

    text = (item.get("text") or "").strip()
    src = item.get("source") or "mobile"
    if not text:
        return
    if not chief_of_staff.is_enabled():
        log.warning("mobile: cos disabled; dropping item id=%s", item.get("id"))
        return
    log.info(
        "mobile: routing item id=%s source=%s chars=%d",
        item.get("id"), src, len(text),
    )
    rc, reply, stderr = chief_of_staff.invoke_command_sync(
        user_message=text,
        source=f"mobile:{src}",
    )
    if rc != 0:
        log.warning(
            "mobile: cos invocation rc=%s stderr=%s",
            rc, (stderr or "")[-200:],
        )


async def run_loop() -> None:
    """Foreground polling loop. Exponential backoff on idle / errors."""
    idle_sleep = POLL_INTERVAL_SEC
    while True:
        try:
            items = await _drain_once()
        except Exception as e:
            log.warning("mobile: drain failed: %s", e)
            await asyncio.sleep(min(idle_sleep * 2, _IDLE_BACKOFF_MAX_SEC))
            idle_sleep = min(idle_sleep * 2, _IDLE_BACKOFF_MAX_SEC)
            continue
        if items:
            idle_sleep = POLL_INTERVAL_SEC
            for item in items:
                try:
                    _route_item(item)
                except Exception:
                    log.exception("mobile: route failed for item %s", item.get("id"))
        else:
            idle_sleep = POLL_INTERVAL_SEC
        await asyncio.sleep(idle_sleep)


def create_mobile_key(label: str = "mobile") -> str:
    """Ask the proxy for a new mobile key, return the plaintext for paste."""
    token = get_auth_token()
    if not token:
        raise RuntimeError("No Deja auth token — run `deja configure` first.")
    url = f"{DEJA_API_URL}/v1/inbox/keys"
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.post(url, headers=headers, json={"label": label}, timeout=15)
    r.raise_for_status()
    return (r.json() or {}).get("key") or ""
