"""Configuration for lighthouse.

Reads from ``~/.lighthouse/config.yaml`` with sensible defaults. Everything
is static at import time — no reload. Every path, model name, and timing
constant the agent uses lives here, so the whole surface is visible in one
place.

Config keys accept both new names and legacy names during the rename
transition, so existing ``config.yaml`` files on disk continue to work
without edits. The documented key is the new name; the legacy key is read
as a fallback.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

LIGHTHOUSE_HOME = Path(
    os.environ.get("LIGHTHOUSE_HOME")
    or os.environ.get("WORKAGENT_HOME")  # legacy env var
    or (Path.home() / ".lighthouse")
)
WIKI_DIR = Path(
    os.environ.get("LIGHTHOUSE_WIKI")
    or os.environ.get("WORKAGENT_WIKI")  # legacy env var
    or (Path.home() / "Lighthouse")
)

# Directory-level migration from any of the previous layouts. Runs once
# on first import after each rename, no-op thereafter. The chain covers:
#   ~/.workagent     → ~/.lighthouse
#   ~/WorkAgent Wiki → ~/Lighthouse Wiki → ~/Lighthouse
_is_default_home = LIGHTHOUSE_HOME == Path.home() / ".lighthouse"
_is_default_wiki = WIKI_DIR == Path.home() / "Lighthouse"

_LEGACY_HOME = Path.home() / ".workagent"
if _is_default_home and _LEGACY_HOME.exists() and not LIGHTHOUSE_HOME.exists():
    try:
        _LEGACY_HOME.rename(LIGHTHOUSE_HOME)
    except OSError:
        pass

# Wiki: two-hop chain from the oldest layout forward. Each step only
# fires if the source exists and the final target doesn't.
_ORIGINAL_WIKI = Path.home() / "WorkAgent Wiki"
_INTERMEDIATE_WIKI = Path.home() / "Lighthouse Wiki"
if _is_default_wiki and not WIKI_DIR.exists():
    if _ORIGINAL_WIKI.exists() and not _INTERMEDIATE_WIKI.exists():
        try:
            _ORIGINAL_WIKI.rename(_INTERMEDIATE_WIKI)
        except OSError:
            pass
    if _INTERMEDIATE_WIKI.exists():
        try:
            _INTERMEDIATE_WIKI.rename(WIKI_DIR)
        except OSError:
            pass

# Observation stream — the append-only log of everything the agent has
# seen. Renamed from the legacy signal_log.jsonl; if the old file exists
# on disk but the new one doesn't, we migrate it on import.
OBSERVATIONS_LOG = LIGHTHOUSE_HOME / "observations.jsonl"
_LEGACY_SIGNAL_LOG = LIGHTHOUSE_HOME / "signal_log.jsonl"
if _LEGACY_SIGNAL_LOG.exists() and not OBSERVATIONS_LOG.exists():
    try:
        _LEGACY_SIGNAL_LOG.rename(OBSERVATIONS_LOG)
    except OSError:
        pass

# Integration audit log — structured record of each integration pass
# (which pages updated, why). Renamed from the legacy analysis_log.jsonl.
INTEGRATIONS_LOG = LIGHTHOUSE_HOME / "integrations.jsonl"
_LEGACY_ANALYSIS_LOG = LIGHTHOUSE_HOME / "analysis_log.jsonl"
if _LEGACY_ANALYSIS_LOG.exists() and not INTEGRATIONS_LOG.exists():
    try:
        _LEGACY_ANALYSIS_LOG.rename(INTEGRATIONS_LOG)
    except OSError:
        pass

CONVERSATION_PATH = LIGHTHOUSE_HOME / "conversation.json"

# Source databases (macOS)
IMESSAGE_DB = Path.home() / "Library" / "Messages" / "chat.db"
WHATSAPP_DB = (
    Path.home() / "Library" / "Group Containers"
    / "group.net.whatsapp.WhatsApp.shared" / "ChatStorage.sqlite"
)

# ---------------------------------------------------------------------------
# User-overridable config (~/.lighthouse/config.yaml)
# ---------------------------------------------------------------------------

_raw: dict = {}
_config_path = LIGHTHOUSE_HOME / "config.yaml"
if _config_path.exists():
    try:
        _raw = yaml.safe_load(_config_path.read_text()) or {}
    except Exception:
        _raw = {}


def _get(new_key: str, legacy_key: str, default):
    """Read a config value, preferring the new key but accepting legacy.

    Keeps ``config.yaml`` files from before the rename working verbatim.
    If both keys are present the new one wins.
    """
    if new_key in _raw:
        return _raw[new_key]
    if legacy_key in _raw:
        return _raw[legacy_key]
    return default


# Agent loop timing (seconds)
OBSERVE_INTERVAL = _get("observe_interval", "signal_interval", 3)
INTEGRATE_INTERVAL = _get("integrate_interval", "match_interval", 300)
SCREENSHOT_HASH_THRESHOLD = _raw.get("screenshot_hash_threshold", 12)

# LLM models — two, used for different cadences
INTEGRATE_MODEL = _get(
    "integrate_model", "cycle_model", "gemini-2.5-flash-lite"
)  # prefilter + integration — text-only fast path, every few minutes
VISION_MODEL = _get(
    "vision_model", "vision_model", "gemini-2.5-flash"
)  # screen description — tuned independently from integrate because the
# tools/vision_eval.py harness showed Flash beats Flash-Lite 15/15 on real
# fixtures (4x more wiki-link grounding) and also beats Pro 10/5 at 1/4
# the cost. Re-run the eval after any Gemini release to re-check.
REFLECT_MODEL = _get(
    "reflect_model", "nightly_model", "gemini-2.5-pro"
)  # reflection — deeper reasoning a few times a day

# Hours of the day (local time, 0-23) when a new reflection "slot" begins.
# Reflection runs once per slot: on the first agent heartbeat after the
# hour that observes the previous run predates today's slot. Default is
# three slots — overnight deep clean, late-morning sweep, end-of-workday
# pass — which gives stale commitments and wrong facts a ~8h ceiling on
# how long they linger before Pro revisits them. Fewer slots = cheaper
# and less intrusive; more slots = tighter feedback loop.
_slot_raw = _get("reflect_slot_hours", "reflect_slot_hour", [2, 11, 18])
if isinstance(_slot_raw, int):
    _slot_raw = [_slot_raw]
REFLECT_SLOT_HOURS = tuple(sorted({int(h) % 24 for h in _slot_raw}))

# Apps the screen-description collector should skip
IGNORED_APPS = set(_raw.get("ignored_apps", ["cmux", "Activity Monitor", "Python", "Terminal"]))

# Identity — which people/*.md page is the user themselves. Optional; if
# unset, ``lighthouse.identity.load_user()`` scans people/ for the first
# page marked ``self: true`` in frontmatter. See src/lighthouse/identity.py.
USER_SLUG = _raw.get("user_slug", "")

# Vision eval retention — when true, the agent loop retains a copy of
# every screenshot after vision describes it (plus a .txt sidecar with
# the description) to LIGHTHOUSE_HOME / "vision_retention/". Used to
# build a real-usage fixture corpus for vision model A/B evaluation.
# Off by default because screenshots accumulate fast and carry PII.
VISION_RETENTION = bool(_raw.get("vision_retention", False))
VISION_RETENTION_DIR = LIGHTHOUSE_HOME / "vision_retention"

# Kill switch for the screenshot collector. Set to false in config.yaml
# if macOS Screen Recording permission is unstable or if you want to
# run Lighthouse without any visual observation (messaging-only mode).
# Disabling this stops ``screencapture`` from being invoked entirely,
# so no more TCC prompts. All other observation sources keep working.
SCREENSHOT_ENABLED = bool(_raw.get("screenshot_enabled", True))
