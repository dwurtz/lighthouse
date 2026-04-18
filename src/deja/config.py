"""Configuration for Deja.

Reads from ``~/.deja/config.yaml`` with sensible defaults. Everything
is static at import time — no reload. Every path, model name, and timing
constant the agent uses lives here, so the whole surface is visible in one
place.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DEJA_HOME = Path(
    os.environ.get("DEJA_HOME")
    or (Path.home() / ".deja")
)

# Ensure ~/.deja/ exists with restricted permissions (owner-only).
# This runs once at import time before any data is written.
if not DEJA_HOME.exists():
    DEJA_HOME.mkdir(parents=True, exist_ok=True)
    DEJA_HOME.chmod(0o700)
elif DEJA_HOME.stat().st_mode & 0o077:
    # Fix permissions if they're too open
    DEJA_HOME.chmod(0o700)
WIKI_DIR = Path(
    os.environ.get("DEJA_WIKI")
    or (Path.home() / "Deja")
)

# Observation stream — the append-only log of everything the agent has seen.
OBSERVATIONS_LOG = DEJA_HOME / "observations.jsonl"

CONVERSATION_PATH = DEJA_HOME / "conversation.json"

# QMD collection name. Single source of truth — every QMD query/search
# call in the codebase must pass this (or qmd will default to searching
# everything). Historically dedup used "Deja" while wiki_retriever used
# "wiki", causing wiki_retriever to silently return zero hits because
# "wiki" never existed. All callers now import this constant.
QMD_COLLECTION = "Deja"
QMD_DB_PATH = Path.home() / ".cache" / "qmd" / "index.sqlite"

# Source databases (macOS)
IMESSAGE_DB = Path.home() / "Library" / "Messages" / "chat.db"
WHATSAPP_DB = (
    Path.home() / "Library" / "Group Containers"
    / "group.net.whatsapp.WhatsApp.shared" / "ChatStorage.sqlite"
)

# ---------------------------------------------------------------------------
# User-overridable config (~/.deja/config.yaml)
# ---------------------------------------------------------------------------

_raw: dict = {}
_config_path = DEJA_HOME / "config.yaml"
if _config_path.exists():
    try:
        _raw = yaml.safe_load(_config_path.read_text()) or {}
    except Exception:
        _raw = {}


# Agent loop timing (seconds)
OBSERVE_INTERVAL = _raw.get("observe_interval", 3)
INTEGRATE_INTERVAL = _raw.get("integrate_interval", 300)
SCREENSHOT_HASH_THRESHOLD = _raw.get("screenshot_hash_threshold", 12)

# LLM models — two, used for different cadences
INTEGRATE_MODEL = _raw.get(
    "integrate_model", "gemini-2.5-flash"
)  # integrate cycle — upgraded from Flash-Lite 2026-04-12 after shadow
# eval showed Flash-Lite was hallucinating "ambient" reminders from
# unrelated signals (verification-code / Mercury / Chime patterns).
# Flash caught none of these; correctly closed an existing reminder
# instead of creating a new one. ~4.2× more expensive per cycle but
# meaningfully more disciplined. See docs/integrate-model-eval-plan.md.
VISION_MODEL = _raw.get(
    "vision_model", "gemini-2.5-flash"
)  # screen description — tuned independently from integrate because the
# tools/vision_eval.py harness showed Flash beats Flash-Lite 15/15 on real
# fixtures (4x more wiki-link grounding) and also beats Pro 10/5 at 1/4
# the cost. Re-run the eval after any Gemini release to re-check.
REFLECT_MODEL = _raw.get(
    "reflect_model", "gemini-3.1-pro-preview"
)  # reflection — deeper reasoning a few times a day
CHAT_MODEL = _raw.get(
    "chat_model", "gemini-3.1-pro-preview"
)  # chat — most capable model for interactive conversations with tools

# Hours of the day (local time, 0-23) when a new reflection "slot" begins.
# Reflection runs once per slot: on the first agent heartbeat after the
# hour that observes the previous run predates today's slot. Default is
# three slots — overnight deep clean, late-morning sweep, end-of-workday
# pass — which gives stale commitments and wrong facts a ~8h ceiling on
# how long they linger before Pro revisits them. Fewer slots = cheaper
# and less intrusive; more slots = tighter feedback loop.
_slot_raw = _raw.get("reflect_slot_hours", [2, 11, 18])
if isinstance(_slot_raw, int):
    _slot_raw = [_slot_raw]
REFLECT_SLOT_HOURS = tuple(sorted({int(h) % 24 for h in _slot_raw}))

# Apps the screen-description collector should skip
IGNORED_APPS = set(_raw.get("ignored_apps", ["cmux", "Activity Monitor", "Python", "Terminal"]))

# Identity — which people/*.md page is the user themselves. Optional; if
# unset, ``deja.identity.load_user()`` scans people/ for the first
# page marked ``self: true`` in frontmatter. See src/deja/identity.py.
USER_SLUG = _raw.get("user_slug", "")

# Vision eval retention — when true, the agent loop retains a copy of
# every screenshot after vision describes it (plus a .txt sidecar with
# the description) to DEJA_HOME / "vision_retention/". Used to
# build a real-usage fixture corpus for vision model A/B evaluation.
# Off by default because screenshots accumulate fast and carry PII.
VISION_RETENTION = bool(_raw.get("vision_retention", False))
VISION_RETENTION_DIR = DEJA_HOME / "vision_retention"

# Integrate shadow eval (Flash vs Flash-Lite vs Pro 3.1 A/B). OFF.
# 303 recorded cycles were enough to confirm Flash is the right
# production model — Flash-Lite under-disciplined, Pro 3.1 under-
# attentive. The scaffolding in llm_client.py is kept for the next
# model comparison; re-enable by flipping this flag to True.
INTEGRATE_SHADOW_EVAL = False

# Integrate Claude shadow — when True, fires a parallel `claude -p`
# subprocess with the exact same integrate prompt on every cycle, lands
# in ~/.deja/integrate_shadow/<ts>.json alongside the Gemini production
# result for offline diffing. Does NOT affect production: Gemini still
# drives wiki writes. Toggle this after a day or so of shadow data to
# decide whether Claude should become the production integrator.
INTEGRATE_CLAUDE_SHADOW = bool(_raw.get("integrate_claude_shadow", False))

# Which integrator drives wiki writes.
#   "gemini"        — default; Gemini Flash is production, optional Claude shadow
#   "claude_vision" — Claude Opus 4.7 with native PNG input is production;
#                     Gemini Flash becomes the parallel shadow
# On claude_vision, a Gemini fallback kicks in if the Claude subprocess
# fails / times out so a bad shadow doesn't take the cycle down.
INTEGRATE_MODE = str(_raw.get("integrate_mode", "gemini"))

# Kill switch for the screenshot collector. Set to false in config.yaml
# if macOS Screen Recording permission is unstable or if you want to
# run Deja without any visual observation (messaging-only mode).
# Disabling this stops ``screencapture`` from being invoked entirely,
# so no more TCC prompts. All other observation sources keep working.
SCREENSHOT_ENABLED = bool(_raw.get("screenshot_enabled", True))
