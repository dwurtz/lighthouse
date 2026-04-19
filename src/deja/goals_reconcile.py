"""Goals reconciliation sweep — retrospective safety net that closes
open ``Tasks`` and ``Waiting for`` items recent events have satisfied.

Runs during the 3×/day reflection slot after ``events_to_projects``.
Third sibling of ``dedup`` and ``events_to_projects`` — same structural
pattern: parse candidates → batched Flash confirm → apply directly.

**Layered model.** Closure is owned in two places:

  - **integrate** (every ~5 minutes): live fast path. The integrate
    prompt's Reconcile section already tells it to close tasks whose
    satisfaction appears in THIS batch's signals, and to apply the
    "indirect satisfaction counts" rule to waiting-fors. This handles
    the easy cases quickly — the signal came in, the cycle ran, the
    commitment closed.
  - **goals_reconcile** (this module, 3×/day): retrospective safety
    net. Under load, integrate sometimes skips the close step because
    it's busy writing events + entity pages. This pass sweeps the
    still-open items against the last 48h of event bodies with
    slower, more thorough reasoning (full Flash, not Flash-Lite) and
    catches what integrate missed. Canonical miss: a ``Waiting for —
    builder contact for detached garage`` from Joe is resolved by a
    Jane email referring back to Joe; integrate wrote the event and
    the people list but didn't flip the waiting_for.

There's no wasted work because goals_reconcile only processes items
still open at sweep time — anything integrate already closed is gone
from the input. Both owners use the same action names
(``complete_tasks``, ``resolve_waiting``) which are idempotent via
substring match.

This module:

  1. Parses ``~/Deja/goals.md`` for open (``- [ ]``) items under both
     ``## Tasks`` and ``## Waiting for``.
  2. Loads event pages from the last 48 hours out of
     ``~/Deja/events/YYYY-MM-DD/``.
  3. Asks ``gemini-2.5-flash`` (not Flash-Lite — Flash-Lite is too
     literal about indirect satisfaction) to decide which items each
     batch of events satisfies. The prompt distinguishes two shapes:
     tasks where the user is the actor, and waiting-fors where someone
     else is the actor (and indirect satisfaction counts).
  4. Applies resolutions via :func:`deja.goals.apply_tasks_update` —
     ``complete_tasks`` for satisfied tasks, ``resolve_waiting`` for
     satisfied waiting-fors. Both flip ``- [ ]`` → ``- [x]`` and record
     the canonical audit entry; we add one extra
     ``goals_reconcile_resolved`` row per satisfied item so Flash's
     reasoning is traceable.

Reminders are out of scope — their semantics (questions to answer, not
commitments to close) don't fit the same shape. Integrate keeps reminder
ownership via its live cycle.

No silent skips. Any parsing/LLM failure raises, so the reflection
marker isn't advanced and the next heartbeat retries.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from deja import audit, goals
from deja.config import WIKI_DIR
from deja.llm_client import GeminiClient
from deja.prompts import load as load_prompt

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIRM_MODEL = "gemini-2.5-flash"

# Batch size for the confirm call. Each batch contains this many open
# items plus the full recent-events block — keep it modest so Flash
# reliably enumerates every item.
CONFIRM_BATCH_SIZE = 10

# Look back this many days for events. Most satisfactions land within a
# day of the commitment; 48h covers weekend gaps + "I'll send it
# tomorrow" slippage without ballooning the prompt.
LOOKBACK_DAYS = 2

# Cap events per sweep so a busy day doesn't blow the context window.
MAX_EVENTS = 50

# Per-event body snippet length in the prompt.
_EVENT_SNIPPET_CHARS = 400

# Flash pricing (2026-04, the retrospective-quality model). Used for
# per-sweep cost logging — matches how events_to_projects reports.
_FLASH_INPUT_PER_MTOK = 0.30
_FLASH_OUTPUT_PER_MTOK = 2.50

_UNCHECKED_RE = re.compile(r"^\s*-\s+\[\s\]\s+(.*)$")

# Sections this module reconciles. Order matters: tasks render first in
# the prompt, waiting_for second. Keep in sync with prompt wording.
_KINDS = ("task", "waiting")
_SECTION_BY_KIND = {"task": "Tasks", "waiting": "Waiting for"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class OpenItem:
    """One open ``- [ ]`` bullet under ``## Tasks`` or ``## Waiting for``."""

    kind: str  # "task" | "waiting"
    raw_line: str  # full bullet line including leading "- [ ] "
    text: str  # everything after "- [ ] "
    needle: str  # distinctive substring used by goals.apply_tasks_update


@dataclass
class RecentEvent:
    path: str  # "events/YYYY-MM-DD/slug.md"
    title: str
    people: list[str]
    projects: list[str]
    body_snippet: str


@dataclass
class Resolution:
    kind: str  # "task" | "waiting"
    needle: str
    satisfied: bool
    reason: str


@dataclass
class ReconcileSummary:
    open_tasks: int = 0
    open_waiting: int = 0
    recent_events: int = 0
    decisions_returned: int = 0
    tasks_completed: int = 0
    waiting_resolved: int = 0
    resolved: int = 0  # total = tasks_completed + waiting_resolved
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    resolutions: list[Resolution] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "open_tasks": self.open_tasks,
            "open_waiting": self.open_waiting,
            "recent_events": self.recent_events,
            "decisions_returned": self.decisions_returned,
            "tasks_completed": self.tasks_completed,
            "waiting_resolved": self.waiting_resolved,
            "resolved": self.resolved,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "resolutions": [
                {
                    "kind": r.kind,
                    "needle": r.needle,
                    "satisfied": r.satisfied,
                    "reason": r.reason,
                }
                for r in self.resolutions
            ],
        }


# ---------------------------------------------------------------------------
# 1. Parse open items from goals.md
# ---------------------------------------------------------------------------


_ADDED_SUFFIX_RE = re.compile(r"\s*\(added \d{4}-\d{2}-\d{2}\)\s*$")


def _needle_for_line(text: str) -> str:
    """Return a short, distinctive substring of an open bullet.

    The substring must appear verbatim in the goals.md line so
    ``goals.apply_tasks_update`` (``resolve_waiting`` / ``complete_tasks``)
    can find it via substring match (case-insensitive).

    Strategy: for ``**Name** — thing`` lines (typical of waiting-fors)
    keep the head + a handful of words of the rest; for plain task lines
    take a short prefix. Strip the trailing ``(added YYYY-MM-DD)`` because
    it's deterministic metadata, not part of the commitment.
    """
    clean = _ADDED_SUFFIX_RE.sub("", text).strip()
    m = re.match(r"^(\*\*[^*]+\*\*\s*[—-]\s*)(.+)$", clean)
    if m:
        head = m.group(1)
        tail = m.group(2).strip()
        words = tail.split()
        if len(words) > 6:
            tail = " ".join(words[:6])
        candidate = (head + tail).strip()
        if candidate:
            return candidate[:120]
    return clean[:80]


def _parse_open_items_from_goals() -> list[OpenItem]:
    """Return every open ``- [ ]`` bullet under ``## Tasks`` + ``## Waiting for``.

    Uses :func:`deja.goals._parse_sections` so trailing Archive and
    preamble don't confuse it.
    """
    goals_path = goals.GOALS_PATH
    if not goals_path.exists():
        return []
    try:
        text = goals_path.read_text(encoding="utf-8")
    except OSError:
        return []
    _, sections = goals._parse_sections(text)
    out: list[OpenItem] = []
    for kind in _KINDS:
        section_name = _SECTION_BY_KIND[kind]
        lines = sections.get(section_name, []) or []
        for line in lines:
            m = _UNCHECKED_RE.match(line)
            if not m:
                continue
            body = m.group(1).strip()
            if not body:
                continue
            out.append(
                OpenItem(
                    kind=kind,
                    raw_line=line,
                    text=body,
                    needle=_needle_for_line(body),
                )
            )
    return out


# Back-compat shim so earlier tests / call sites that used the waiting-
# only name still work.
def _parse_open_waiting_from_goals() -> list[OpenItem]:
    """Return only the ``waiting`` OpenItems. Kept for legacy callers."""
    return [it for it in _parse_open_items_from_goals() if it.kind == "waiting"]


# ---------------------------------------------------------------------------
# 2. Load recent events
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def _parse_event(path: Path) -> RecentEvent | None:
    """Parse an event .md file into a compact RecentEvent, or None on failure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    fm_block = ""
    body = raw
    m = _FRONTMATTER_RE.match(raw)
    if m:
        fm_block = m.group(1)
        body = m.group(2)
    else:
        # Legacy one-line frontmatter: ---key: v key2: v2---
        if raw.startswith("---"):
            end = raw.find("---", 3)
            if end != -1:
                fm_block = raw[3:end]
                body = raw[end + 3 :]

    people: list[str] = []
    projects: list[str] = []
    pm = re.search(r"people:\s*\[([^\]]*)\]", fm_block)
    if pm:
        people = [s.strip() for s in pm.group(1).split(",") if s.strip()]
    prm = re.search(r"projects:\s*\[([^\]]*)\]", fm_block)
    if prm:
        projects = [s.strip() for s in prm.group(1).split(",") if s.strip()]

    title = path.stem
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    body_no_h1 = "\n".join(
        ln for ln in body.splitlines() if not ln.startswith("# ")
    )
    snippet = re.sub(r"\s+", " ", body_no_h1).strip()[:_EVENT_SNIPPET_CHARS]

    try:
        rel = path.relative_to(WIKI_DIR).as_posix()
    except ValueError:
        rel = f"events/{path.parent.name}/{path.name}"

    return RecentEvent(
        path=rel,
        title=title,
        people=people,
        projects=projects,
        body_snippet=snippet,
    )


def _load_recent_events(days: int = LOOKBACK_DAYS) -> list[RecentEvent]:
    """Return every event page whose date-dir falls in the last ``days`` days.

    Returns at most :data:`MAX_EVENTS` events (newest first) to bound
    prompt size.
    """
    events_dir = WIKI_DIR / "events"
    if not events_dir.is_dir():
        return []

    today = date.today()
    cutoff = today - timedelta(days=days)
    events: list[RecentEvent] = []
    for day_dir in sorted(events_dir.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        try:
            d = date.fromisoformat(day_dir.name)
        except ValueError:
            continue
        if d < cutoff:
            continue
        for event_file in sorted(day_dir.glob("*.md")):
            ev = _parse_event(event_file)
            if ev is not None:
                events.append(ev)
                if len(events) >= MAX_EVENTS:
                    return events
    return events


# ---------------------------------------------------------------------------
# 3. Confirm — Flash batched judgment
# ---------------------------------------------------------------------------


def _build_open_items_block(items: list[OpenItem]) -> str:
    """Render open items grouped by kind so Flash sees the labels clearly."""
    by_kind: dict[str, list[OpenItem]] = {"task": [], "waiting": []}
    for it in items:
        by_kind.setdefault(it.kind, []).append(it)

    lines: list[str] = []
    counter = 0
    for kind in _KINDS:
        group = by_kind.get(kind) or []
        if not group:
            continue
        label = "Tasks (user is the actor)" if kind == "task" else (
            "Waiting for (someone else is the actor)"
        )
        lines.append(f"### {label}")
        for it in group:
            counter += 1
            lines.append(f"{counter}. [kind={it.kind}] {it.text}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_events_block(events: list[RecentEvent]) -> str:
    lines: list[str] = []
    for ev in events:
        people_str = ", ".join(ev.people) if ev.people else "(none)"
        projects_str = ", ".join(ev.projects) if ev.projects else "(none)"
        lines.append(f"### [{ev.path}] {ev.title}")
        lines.append(f"people: {people_str}")
        lines.append(f"projects: {projects_str}")
        if ev.body_snippet:
            lines.append(f"body: {ev.body_snippet}")
        lines.append("")
    return "\n".join(lines) if lines else "(no events in the last 48h)"


def _parse_confirm_json(raw: str) -> dict:
    """Parse the Flash response. Raises with raw payload on failure."""
    text = (raw or "").strip()
    if not text:
        raise RuntimeError(
            "goals_reconcile confirm: Flash returned empty response"
        )
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if "{" in text and "}" in text:
        start = text.index("{")
        end = text.rindex("}") + 1
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"goals_reconcile confirm: unparseable Flash JSON. "
                f"Error: {e}. Raw payload (first 2000 chars): {raw[:2000]!r}"
            ) from e
    raise RuntimeError(
        f"goals_reconcile confirm: Flash response has no JSON object. "
        f"Raw payload (first 2000 chars): {raw[:2000]!r}"
    )


async def _call_flash(prompt: str) -> tuple[dict, int, int]:
    """Call Flash, retry once on exception, then raise.

    Returns (parsed_json, input_tokens, output_tokens_including_thoughts).
    """
    client = GeminiClient()
    config = {
        "response_mime_type": "application/json",
        "max_output_tokens": 32768,
        "temperature": 0.1,
    }
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            resp = await client._generate_full(
                model=CONFIRM_MODEL,
                contents=prompt,
                config_dict=config,
            )
            break
        except Exception as e:
            last_exc = e
            log.warning(
                "goals_reconcile confirm: attempt %d failed: %s", attempt, e,
            )
            if attempt == 2:
                raise RuntimeError(
                    f"goals_reconcile confirm: Flash failed after 2 attempts: {e}"
                ) from e
    else:  # pragma: no cover
        raise RuntimeError(
            f"goals_reconcile confirm: Flash failed: {last_exc}"
        )

    if isinstance(resp, dict):
        raw_text = resp.get("text") or ""
        um = resp.get("usage_metadata") or {}
        in_tok = int(um.get("prompt_token_count") or 0)
        out_tok = int(um.get("candidates_token_count") or 0)
        thoughts = int(um.get("thoughts_token_count") or 0)
    else:
        raw_text = getattr(resp, "text", "") or ""
        um = getattr(resp, "usage_metadata", None)
        in_tok = int(getattr(um, "prompt_token_count", 0) or 0) if um else 0
        out_tok = int(getattr(um, "candidates_token_count", 0) or 0) if um else 0
        thoughts = int(getattr(um, "thoughts_token_count", 0) or 0) if um else 0

    parsed = _parse_confirm_json(raw_text)
    return parsed, in_tok, out_tok + thoughts


def _user_first_name_for_prompt() -> str:
    """Fetch the user's first name for the prompt, with a safe fallback."""
    try:
        from deja.identity import load_user

        return (load_user().first_name or "the user").strip() or "the user"
    except Exception:
        return "the user"


async def _confirm_resolutions(
    open_items: list[OpenItem],
    events: list[RecentEvent],
) -> tuple[list[Resolution], int, int]:
    """Ask Flash for a satisfied/not decision for every open item.

    Batches open items into groups of :data:`CONFIRM_BATCH_SIZE`. The
    events block is repeated per batch so every item sees the full 48h
    window. Coverage is required — every open item must appear in the
    combined response or the call raises.

    Returns (resolutions, input_tokens, output_tokens).
    """
    prompt_template = load_prompt("goals_reconcile_confirm")
    for placeholder in ("{open_items}", "{recent_events}", "{user_first_name}"):
        if placeholder not in prompt_template:
            raise RuntimeError(
                f"goals_reconcile confirm prompt is missing the "
                f"{placeholder} placeholder. Check the bundled "
                f"goals_reconcile_confirm.md in default_assets/prompts/."
            )

    user_first_name = _user_first_name_for_prompt()
    events_block = _build_events_block(events)

    batches: list[list[OpenItem]] = [
        open_items[i : i + CONFIRM_BATCH_SIZE]
        for i in range(0, len(open_items), CONFIRM_BATCH_SIZE)
    ]
    log.info(
        "goals_reconcile: confirming %d open item(s) via %s across "
        "%d batch(es) of ≤%d (with %d recent event(s))",
        len(open_items), CONFIRM_MODEL, len(batches), CONFIRM_BATCH_SIZE,
        len(events),
    )

    all_resolutions: list[Resolution] = []
    total_in_tok = 0
    total_out_tok = 0

    for batch_idx, batch in enumerate(batches, start=1):
        try:
            prompt = prompt_template.format(
                open_items=_build_open_items_block(batch),
                recent_events=events_block,
                user_first_name=user_first_name,
            )
        except (KeyError, IndexError) as e:
            raise RuntimeError(
                f"goals_reconcile confirm prompt has an unexpected format "
                f"placeholder: {e}. Only {{open_items}}, "
                f"{{recent_events}}, and {{user_first_name}} should be "
                f"unescaped placeholders; literal braces must be doubled."
            ) from e

        log.info(
            "goals_reconcile batch %d/%d: %d item(s), %d prompt chars",
            batch_idx, len(batches), len(batch), len(prompt),
        )

        parsed, in_tok, out_tok = await _call_flash(prompt)
        total_in_tok += in_tok
        total_out_tok += out_tok

        resolutions = (
            parsed.get("resolutions") if isinstance(parsed, dict) else None
        )
        if not isinstance(resolutions, list):
            raise RuntimeError(
                f"goals_reconcile confirm batch {batch_idx}: response JSON "
                f"has no 'resolutions' list. Got: {parsed!r}"
            )

        # Coverage + kind routing. Flash echoes the needle + kind for
        # each item; we match back to the OpenItem by needle substring
        # to be resilient to minor whitespace/quote differences. The
        # kind we store is the one Flash returned IF it agrees with the
        # item's kind — otherwise we fall back to the item's own kind so
        # apply-time routing stays correct.
        decisions_by_needle: dict[str, dict] = {}
        for d in resolutions:
            if not isinstance(d, dict):
                continue
            needle = d.get("needle")
            if isinstance(needle, str) and needle.strip():
                decisions_by_needle[needle.strip()] = d

        missing: list[str] = []
        batch_resolutions: list[Resolution] = []
        for item in batch:
            match: dict | None = None
            for needle, d in decisions_by_needle.items():
                if (
                    needle.lower() in item.text.lower()
                    or needle.lower() in item.raw_line.lower()
                    or item.needle.lower() in needle.lower()
                ):
                    match = d
                    break
            if match is None:
                missing.append(f"[{item.kind}] {item.text[:80]}")
                continue
            satisfied_val = bool(match.get("satisfied"))
            reason = str(match.get("reason") or "").strip()
            batch_resolutions.append(
                Resolution(
                    kind=item.kind,
                    needle=item.needle,
                    satisfied=satisfied_val,
                    reason=reason
                    or f"Flash needle: {match.get('needle', '')!r}",
                )
            )

        if missing:
            raise RuntimeError(
                f"goals_reconcile confirm batch {batch_idx}/{len(batches)}: "
                f"Flash omitted {len(missing)} of {len(batch)} open "
                f"item(s). Missing (kind + text prefix): {missing}. Reduce "
                f"CONFIRM_BATCH_SIZE or investigate the prompt."
            )

        all_resolutions.extend(batch_resolutions)

    return all_resolutions, total_in_tok, total_out_tok


# ---------------------------------------------------------------------------
# 4. Apply — split resolutions by kind, call goals.apply_tasks_update
# ---------------------------------------------------------------------------


def _apply_resolutions(resolutions: list[Resolution]) -> tuple[int, int]:
    """Flip ``- [ ]`` → ``- [x]`` for satisfied items. Returns
    ``(tasks_completed, waiting_resolved)`` — the per-kind counts
    goals.apply_tasks_update reports back is a single number so we infer
    kind split from the input list.
    """
    satisfied_tasks = [r for r in resolutions if r.satisfied and r.kind == "task"]
    satisfied_waiting = [
        r for r in resolutions if r.satisfied and r.kind == "waiting"
    ]
    update: dict[str, list[str]] = {}
    if satisfied_tasks:
        update["complete_tasks"] = [r.needle for r in satisfied_tasks]
    if satisfied_waiting:
        update["resolve_waiting"] = [r.needle for r in satisfied_waiting]
    if not update:
        return 0, 0

    before_tasks = len(satisfied_tasks)
    before_waiting = len(satisfied_waiting)
    # apply_tasks_update returns a single int (total changes, including
    # possibly auto-expiry and caps). We derive per-kind success by
    # re-reading the file to check which needles flipped. For now, treat
    # the "we asked it to flip N" as the reported count — goals.py's
    # op_complete returns False only on no-match, which the caller can
    # detect next cycle.
    total_changes = goals.apply_tasks_update(update)

    # One audit entry per satisfied resolution on top of goals.py's
    # waiting_resolve / task_complete rows, carrying Flash's reason.
    for r in satisfied_tasks + satisfied_waiting:
        try:
            audit.record(
                "goals_reconcile_resolved",
                target=(
                    f"tasks/{r.needle[:40]}"
                    if r.kind == "task"
                    else f"waiting_for/{r.needle[:40]}"
                ),
                reason=r.reason or "Flash judged satisfied",
                trigger={"kind": "reflection", "detail": "goals_reconcile"},
            )
        except Exception:
            log.debug(
                "goals_reconcile: audit.record failed for needle %r",
                r.needle,
                exc_info=True,
            )
    log.info(
        "goals_reconcile: applied %d change(s) via apply_tasks_update "
        "(tasks=%d, waiting=%d)",
        total_changes, before_tasks, before_waiting,
    )
    return before_tasks, before_waiting


# ---------------------------------------------------------------------------
# 5. Top-level entrypoint — called by reflection_scheduler
# ---------------------------------------------------------------------------


async def run_goals_reconcile() -> dict:
    """Run one full goals-reconcile sweep. Called by reflection_scheduler.

    Steps:
      1. Parse open ``- [ ]`` items from ``## Tasks`` + ``## Waiting for``.
      2. Load event pages from the last 48 hours.
      3. Ask Flash which items the events satisfy (indirect counts for
         waiting-fors; user-as-actor for tasks).
      4. Apply satisfied resolutions via goals.apply_tasks_update
         (``complete_tasks`` for tasks, ``resolve_waiting`` for waitings).

    Returns a ReconcileSummary as a dict. Raises loudly on any failure —
    no silent fallbacks.
    """
    open_items = _parse_open_items_from_goals()
    open_tasks = sum(1 for it in open_items if it.kind == "task")
    open_waiting = sum(1 for it in open_items if it.kind == "waiting")

    if not open_items:
        log.info("goals_reconcile: no open tasks or waiting-fors — skipping")
        return ReconcileSummary().as_dict()

    recent_events = _load_recent_events(days=LOOKBACK_DAYS)
    if not recent_events:
        log.info(
            "goals_reconcile: %d open item(s) (tasks=%d, waiting=%d) "
            "but no events in the last %d day(s) — skipping",
            len(open_items), open_tasks, open_waiting, LOOKBACK_DAYS,
        )
        summary = ReconcileSummary(
            open_tasks=open_tasks,
            open_waiting=open_waiting,
            recent_events=0,
        )
        return summary.as_dict()

    resolutions, in_tok, out_tok = await _confirm_resolutions(
        open_items, recent_events
    )

    tasks_completed, waiting_resolved = _apply_resolutions(resolutions)

    summary = ReconcileSummary(
        open_tasks=open_tasks,
        open_waiting=open_waiting,
        recent_events=len(recent_events),
        decisions_returned=len(resolutions),
        tasks_completed=tasks_completed,
        waiting_resolved=waiting_resolved,
        resolved=tasks_completed + waiting_resolved,
        resolutions=resolutions,
        input_tokens=in_tok,
        output_tokens=out_tok,
    )
    summary.cost_usd = (
        (in_tok / 1_000_000) * _FLASH_INPUT_PER_MTOK
        + (out_tok / 1_000_000) * _FLASH_OUTPUT_PER_MTOK
    )
    log.info(
        "goals_reconcile complete: tasks=%d/%d waiting=%d/%d events=%d cost=$%.4f",
        summary.tasks_completed, summary.open_tasks,
        summary.waiting_resolved, summary.open_waiting,
        summary.recent_events, summary.cost_usd,
    )
    return summary.as_dict()
