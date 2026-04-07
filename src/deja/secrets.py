"""macOS Keychain-backed secret storage.

The Gemini API key (and any other credentials Deja needs) lives in
the user's login keychain — never in plaintext on disk, never in git,
never in shell history. This module is the only place the rest of the
codebase reads or writes those secrets.

Resolution order for ``get_api_key()``:

    1. ``GEMINI_API_KEY`` environment variable (for dev / CI / override)
    2. ``GOOGLE_API_KEY`` environment variable (legacy alias)
    3. macOS login keychain under service=``deja`` account=``gemini-api-key``

Writes always go to the keychain via ``security add-generic-password``.
``security`` is part of macOS's base system, so there's no extra
dependency. The keychain is unlocked automatically when the user logs
in, so runtime reads are transparent — no password prompt unless the
user has explicitly locked the keychain.

Why Keychain over ``~/.deja/env`` or ``~/.zshrc``:
- Encrypted at rest (the keychain db is AES-encrypted)
- Automatically locked when the Mac is locked
- Not caught by ``grep -r ~/`` or by backups that scrape plaintext files
- Not visible to shoulder-surfers who peek at ``.zshrc``
- No risk of accidentally committing to git via an editor auto-save
- No risk of leaking into shell history
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

# Keychain item coordinates. Service name identifies the app; account
# name identifies the specific credential within that app. Both are
# arbitrary strings — we pick stable names so writes and reads always
# target the same slot.
_KEYCHAIN_SERVICE = "deja"
_KEYCHAIN_SERVICE_LEGACY = "lighthouse"  # fallback for migration
_KEYCHAIN_ACCOUNT_GEMINI = "gemini-api-key"


def _read_keychain(service: str, account: str) -> str | None:
    """Return a secret from the login keychain, or None if not present.

    Uses ``security find-generic-password -w`` which prints only the
    password (no metadata) to stdout. Returns None on any failure so
    callers can fall through to other sources.
    """
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.debug("keychain read failed: %s", e)
        return None
    if r.returncode != 0:
        return None
    value = (r.stdout or "").strip()
    return value or None


def _write_keychain(service: str, account: str, value: str) -> None:
    """Store or replace a secret in the login keychain.

    The ``-U`` flag updates the entry if it already exists instead of
    erroring out. The ``-T ""`` flag means no specific application is
    pre-authorized to read without prompting — any process running as
    the user can read via ``security find-generic-password``, but the
    keychain still requires the login session to be unlocked.

    Raises subprocess.CalledProcessError on failure so setup flows can
    surface the real error message.
    """
    subprocess.run(
        [
            "security", "add-generic-password",
            "-s", service,
            "-a", account,
            "-w", value,
            "-U",
            "-T", "",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _delete_keychain(service: str, account: str) -> bool:
    """Remove a secret from the login keychain. Returns True on success."""
    r = subprocess.run(
        ["security", "delete-generic-password", "-s", service, "-a", account],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Process-level cache so repeated calls don't spawn fresh `security`
# subprocesses — each subprocess may trigger a keychain ACL check, and
# even when the check is cached by macOS it's wasted work. Cleared by
# callers via clear_cache() after a write.
_cached_key: str | None = None
_cache_valid: bool = False


def get_api_key() -> str | None:
    """Return the Gemini API key from env or keychain, or None if unset.

    Environment variables take precedence so developers can override the
    stored key without touching the keychain (useful for tests, CI, or
    testing against a different API tenant). The result is cached for
    the lifetime of the process so hot paths (every LLM call, every
    health check) don't re-spawn ``security``.
    """
    global _cached_key, _cache_valid
    if _cache_valid:
        return _cached_key

    env_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if env_key:
        _cached_key = env_key
        _cache_valid = True
        return env_key

    # Try new service name first, fall back to legacy for migration
    key = _read_keychain(_KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT_GEMINI)
    if not key:
        key = _read_keychain(_KEYCHAIN_SERVICE_LEGACY, _KEYCHAIN_ACCOUNT_GEMINI)
        if key:
            log.info(
                "Read API key from legacy keychain service '%s'. "
                "Run `deja configure` to migrate it to service '%s'.",
                _KEYCHAIN_SERVICE_LEGACY,
                _KEYCHAIN_SERVICE,
            )
    _cached_key = key
    _cache_valid = True
    return _cached_key


def _clear_cache() -> None:
    """Invalidate the process-level cache. Called after write/delete."""
    global _cached_key, _cache_valid
    _cached_key = None
    _cache_valid = False


def store_api_key(key: str) -> None:
    """Persist the Gemini API key to the user's login keychain.

    Overwrites any existing entry. Pre-authorizes ``/usr/bin/security``
    and the currently-running Python binary in the item's ACL so future
    reads don't trigger keychain prompts — this is critical because the
    agent calls ``get_api_key()`` from hot paths (every LLM request)
    and an unbounded prompt loop is the failure mode we're avoiding.
    """
    import sys
    key = key.strip()
    if not key:
        raise ValueError("api key is empty")

    # Resolve the real Python binary (follow symlinks) so the ACL matches
    # the actual caller identity that macOS sees at subprocess time.
    py_real = os.path.realpath(sys.executable)

    subprocess.run(
        [
            "security", "add-generic-password",
            "-s", _KEYCHAIN_SERVICE,
            "-a", _KEYCHAIN_ACCOUNT_GEMINI,
            "-w", key,
            "-T", "/usr/bin/security",
            "-T", py_real,
            "-U",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _clear_cache()


def clear_api_key() -> bool:
    """Remove the Gemini API key from the keychain. Returns True if removed."""
    result = _delete_keychain(_KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT_GEMINI)
    _clear_cache()
    return result


def api_key_source() -> str:
    """Return a human-readable description of where the key is resolved from.

    Used by the ``deja health`` command to make the provenance of
    the key visible. Does not return the key itself.
    """
    if os.environ.get("GEMINI_API_KEY"):
        return "env:GEMINI_API_KEY"
    if os.environ.get("GOOGLE_API_KEY"):
        return "env:GOOGLE_API_KEY"
    if _read_keychain(_KEYCHAIN_SERVICE, _KEYCHAIN_ACCOUNT_GEMINI):
        return "keychain:deja/gemini-api-key"
    if _read_keychain(_KEYCHAIN_SERVICE_LEGACY, _KEYCHAIN_ACCOUNT_GEMINI):
        return "keychain:lighthouse/gemini-api-key (legacy)"
    return "(not configured)"
