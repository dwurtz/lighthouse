"""Shadow-mode Graphiti ingest — feed triaged signals into the knowledge graph.

This module runs ALONGSIDE the existing wiki-write pipeline. Every signal
that passes triage gets an ``add_episode()`` call into a local Kuzu-backed
Graphiti instance.  All failures are caught and logged — this module must
never crash or slow down the main analysis cycle.

Initialization is lazy: the Graphiti instance, Kuzu driver, and OpenAI
clients are created on the first ``ingest_signal()`` call and reused for
the lifetime of the process.

Requirements:
    pip install graphiti-core kuzu
    OPENAI_API_KEY environment variable must be set.

DB location: ~/.deja/graphiti.db  (created on first use)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy singleton state
# ---------------------------------------------------------------------------

_graphiti_instance = None
_init_attempted = False  # Avoid retrying a failed init every cycle


async def _ensure_graphiti():
    """Lazy-initialize the Graphiti instance. Returns it or None on failure."""
    global _graphiti_instance, _init_attempted

    if _graphiti_instance is not None:
        return _graphiti_instance

    if _init_attempted:
        # Already failed once this process — don't retry every cycle.
        return None

    _init_attempted = True

    try:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            # Fallback: read from ~/.deja/openai_key (macOS `open` doesn't
            # inherit shell env vars, so the app process may not see them).
            key_file = Path.home() / ".deja" / "openai_key"
            if key_file.is_file():
                api_key = key_file.read_text().strip()
                if api_key:
                    log.info("graphiti_ingest: loaded OPENAI_API_KEY from %s", key_file)
        if not api_key:
            log.warning(
                "graphiti_ingest: OPENAI_API_KEY not set and ~/.deja/openai_key "
                "not found — shadow ingest disabled"
            )
            return None

        from graphiti_core import Graphiti
        from graphiti_core.driver.kuzu_driver import KuzuDriver
        from graphiti_core.llm_client.openai_client import OpenAIClient
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
        from graphiti_core.cross_encoder.openai_reranker_client import (
            OpenAIRerankerClient,
        )

        db_path = str(Path.home() / ".deja" / "graphiti.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        log.info("graphiti_ingest: initializing Kuzu at %s", db_path)

        driver = KuzuDriver(db=db_path)

        # ---- FTS index workaround ----
        # KuzuDriver.setup_schema() doesn't install fts or create the
        # fulltext indices that add_episode requires.
        try:
            import kuzu as _kuzu
            from graphiti_core.graph_queries import get_fulltext_indices
            from graphiti_core.driver.driver import GraphProvider as _GP

            _conn = _kuzu.Connection(driver.db)
            _conn.execute("INSTALL fts;")
            _conn.execute("LOAD EXTENSION fts;")
            for _q in get_fulltext_indices(_GP.KUZU):
                try:
                    _conn.execute(_q)
                except RuntimeError as _e:
                    if "already exists" not in str(_e).lower():
                        raise
            _conn.close()
        except Exception:
            log.debug("graphiti_ingest: FTS setup (may already exist)", exc_info=True)

        # small_model bumped to gpt-4.1-mini to fix attribute cross-pollution.
        llm = OpenAIClient(
            config=LLMConfig(api_key=api_key, small_model="gpt-4.1-mini")
        )
        embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(api_key=api_key))
        reranker = OpenAIRerankerClient(config=LLMConfig(api_key=api_key))

        graphiti = Graphiti(
            graph_driver=driver,
            llm_client=llm,
            embedder=embedder,
            cross_encoder=reranker,
        )

        await graphiti.build_indices_and_constraints()

        _graphiti_instance = graphiti
        log.info("graphiti_ingest: initialized successfully")
        return _graphiti_instance

    except Exception:
        log.warning("graphiti_ingest: initialization failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Source-type mapping
# ---------------------------------------------------------------------------

# Map Deja signal sources to Graphiti EpisodeType values.
_MESSAGE_SOURCES = {"imessage", "whatsapp", "email", "microphone", "voice", "chat"}


def _episode_source(signal_source: str):
    """Return the appropriate EpisodeType for a signal source string."""
    try:
        from graphiti_core.nodes import EpisodeType
    except ImportError:
        return None

    if signal_source in _MESSAGE_SOURCES:
        return EpisodeType.message
    return EpisodeType.text


def _make_episode_name(signal: dict) -> str:
    """Generate a short episode name from the signal."""
    source = signal.get("source", "unknown")
    sender = signal.get("sender", "")
    ts = signal.get("timestamp", "")
    parts = [source]
    if sender:
        parts.append(str(sender)[:60])
    if ts:
        parts.append(str(ts)[:19])
    return ":".join(parts)


def _parse_timestamp(ts) -> datetime:
    """Coerce a timestamp (str, datetime, or None) to a timezone-aware datetime."""
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ingest_signal(signal: dict) -> None:
    """Ingest one triaged signal dict into the Graphiti knowledge graph.

    This is the only public entry point. It is designed to be called via
    ``asyncio.create_task()`` — fire-and-forget from the analysis cycle.

    All exceptions are caught and logged; this function never raises.

    ``signal`` is a dict with at least: source, sender, text, timestamp.
    """
    try:
        graphiti = await _ensure_graphiti()
        if graphiti is None:
            return  # init failed or no API key — already logged

        from deja.graphiti_schema import ENTITY_TYPES, EDGE_TYPES, EDGE_TYPE_MAP

        text = signal.get("text", "")
        if not text or not text.strip():
            return

        source = (signal.get("source") or "unknown").lower()
        sender = signal.get("sender") or ""
        ep_source = _episode_source(source)
        if ep_source is None:
            log.debug("graphiti_ingest: graphiti_core not importable, skipping")
            return

        name = _make_episode_name(signal)
        source_desc = f"{source} from {sender}" if sender else source
        ref_time = _parse_timestamp(signal.get("timestamp"))

        await graphiti.add_episode(
            name=name,
            episode_body=text,
            source=ep_source,
            source_description=source_desc,
            reference_time=ref_time,
            entity_types=ENTITY_TYPES,
            edge_types=EDGE_TYPES,
            edge_type_map=EDGE_TYPE_MAP,
        )

        log.info(
            "graphiti_ingest: OK — %s (%d chars)",
            name[:80],
            len(text),
        )

    except Exception:
        log.warning("graphiti_ingest: add_episode failed", exc_info=True)
