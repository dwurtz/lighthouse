"""Meeting transcription and wiki processing pipeline.

Handles the flow from recorded audio chunks to wiki event pages:
  1. Transcribe individual 5-minute WAV chunks via Gemini Flash
  2. Assemble chunk transcripts into a single meeting document
  3. Summarize and create a wiki event page via Gemini Pro

Audio chunks are written by the Swift menu-bar app using
ScreenCaptureKit. Python picks them up from disk, transcribes
progressively (during the meeting), and processes the full
transcript into the wiki after recording stops.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from deja.config import DEJA_HOME, REFLECT_MODEL, WIKI_DIR

log = logging.getLogger(__name__)

MEETING_AUDIO_DIR = DEJA_HOME / "meeting_audio"


async def transcribe_meeting_chunk(wav_path: Path, chunk_index: int) -> str:
    """Transcribe a single 5-minute WAV chunk via Gemini Flash.

    Uses speaker diarization prompt so the transcript labels speakers.
    A 5-min chunk at 16kHz mono ≈ 9.6 MB — well within Gemini limits.
    """
    from deja.llm_client import GeminiClient
    from google.genai import types

    try:
        audio_bytes = wav_path.read_bytes()
    except OSError as e:
        log.warning("Could not read chunk %s: %s", wav_path, e)
        return ""

    if len(audio_bytes) < 4096:
        log.warning("Chunk %s too small (%d bytes), skipping", wav_path, len(audio_bytes))
        return ""

    log.info("Transcribing chunk %d: %s (%d bytes)", chunk_index, wav_path.name, len(audio_bytes))

    prompt = (
        "Transcribe the speech in this audio. Include speaker labels "
        "(Speaker 1, Speaker 2, etc.) when you can distinguish different "
        "voices. Format as:\n\n"
        "Speaker 1: [what they said]\n"
        "Speaker 2: [what they said]\n\n"
        "If you cannot distinguish speakers, just transcribe the words "
        "in order. Return ONLY the transcript — no timestamps, no "
        "commentary, no preamble."
    )

    gemini = GeminiClient()
    try:
        resp = await gemini.client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                max_output_tokens=4096,
                temperature=0.0,
            ),
        )
        raw = (resp.text or "").strip()
        if raw.lower() in ("", "empty", "(empty)", "no speech", "no speech detected"):
            return ""
        return raw
    except Exception:
        log.exception("Transcription failed for chunk %d", chunk_index)
        return ""


async def transcribe_meeting_rolling(session_dir: Path, state: dict) -> None:
    """Background task: poll for new completed chunks and transcribe them.

    Runs every 15 seconds. When a new chunk-NNN.wav appears that hasn't
    been transcribed yet, transcribes it and appends to state['transcripts'].
    This gives near-real-time transcription — by the time the meeting ends,
    most chunks are already done.

    Stops when state['recording'] becomes False and all chunks are processed.
    """
    transcribed_indices: set[int] = set()

    while True:
        # Find all completed chunks (presence of a .done marker or next chunk exists)
        chunks = sorted(session_dir.glob("chunk-*.wav"))
        for chunk_path in chunks:
            # Parse index from filename: chunk-000.wav → 0
            try:
                idx = int(chunk_path.stem.split("-")[1])
            except (IndexError, ValueError):
                continue

            if idx in transcribed_indices:
                continue

            # A chunk is "complete" if:
            # 1. A .done marker exists for it, OR
            # 2. The next chunk exists (meaning this one was finalized), OR
            # 3. Recording has stopped (all existing chunks are final)
            done_marker = chunk_path.with_suffix(".done")
            next_chunk = session_dir / f"chunk-{idx + 1:03d}.wav"
            is_complete = (
                done_marker.exists()
                or next_chunk.exists()
                or not state.get("recording", True)
            )

            if not is_complete:
                continue

            transcript = await transcribe_meeting_chunk(chunk_path, idx)
            if transcript:
                # Insert at correct position (chunks may complete out of order)
                while len(state["transcripts"]) <= idx:
                    state["transcripts"].append("")
                state["transcripts"][idx] = transcript
                state["chunks_transcribed"] = len([t for t in state["transcripts"] if t])
                log.info(
                    "Chunk %d transcribed (%d chars), %d/%d done",
                    idx, len(transcript),
                    state["chunks_transcribed"], len(chunks),
                )
            transcribed_indices.add(idx)

        # If recording stopped and all chunks are transcribed, we're done
        if not state.get("recording", True):
            remaining = [
                c for c in chunks
                if int(c.stem.split("-")[1]) not in transcribed_indices
            ]
            if not remaining:
                log.info("All %d chunks transcribed", len(transcribed_indices))
                break

        await asyncio.sleep(15)


def assemble_meeting_transcript(transcripts: list[str], metadata: dict) -> str:
    """Combine chunk transcripts into a single meeting transcript document."""
    title = metadata.get("title", "Meeting")
    attendees = metadata.get("attendees", [])
    started_at = metadata.get("started_at", "")
    duration_min = metadata.get("duration_min", 0)

    attendee_names = [a.get("name", a.get("email", "")) for a in attendees]

    header = f"# {title}\n\n"
    header += f"**Date**: {started_at[:10] if started_at else 'unknown'}\n"
    if attendee_names:
        header += f"**Attendees**: {', '.join(attendee_names)}\n"
    if duration_min:
        header += f"**Duration**: ~{duration_min} minutes\n"
    header += "\n## Transcript\n\n"

    # Merge chunks, adding a separator between each
    body = "\n\n".join(t for t in transcripts if t)

    return header + body


async def create_meeting_wiki_page(
    transcript: str,
    metadata: dict,
) -> str | None:
    """Use Gemini to create a wiki event page from a recording transcript.

    AI generates the title (even if no calendar event), writes a
    Granola-style summary at the top, cleans up the transcript, and
    links wiki entities.

    Returns the slug of the created page, or None on failure.
    """
    from deja.llm_client import GeminiClient
    from google.genai import types

    calendar_title = metadata.get("title", "")
    attendees = metadata.get("attendees", [])
    started_at = metadata.get("started_at", "")

    # Load wiki index for entity linking
    try:
        from deja.llm.prefilter import load_index_md
        index_md = load_index_md()
    except Exception:
        index_md = ""

    from deja.identity import load_user
    user = load_user()

    attendee_names = [a.get("name", a.get("email", "")) for a in attendees]

    # Use LOCAL time for the date/time — started_at is UTC but the user
    # thinks in their local timezone. Without this, evening recordings
    # get tomorrow's date.
    now_local = datetime.now()
    date_str = now_local.strftime("%Y-%m-%d")
    time_str = now_local.strftime("%H:%M")

    calendar_context = ""
    if calendar_title:
        calendar_context = f"""
## Calendar event (linked)
- Title: {calendar_title}
- Attendees: {', '.join(attendee_names)}
Use this context to inform the page, but the AI-generated title should
reflect what was ACTUALLY discussed, not just the calendar invite name.
"""

    user_notes = metadata.get("user_notes", "").strip()
    notes_context = ""
    if user_notes:
        notes_context = f"""
## User's notes (include verbatim in the event page under a "## Notes" section)
{user_notes}
"""

    prompt = f"""You are {user.first_name}'s personal assistant. Create a wiki event page from this recording.

## Wiki index (for [[wiki-links]])
{index_md}
{calendar_context}{notes_context}
## Recording info
- Date: {date_str}
- Time: {time_str}
- Duration: {metadata.get('duration_min', 0)} minutes

## Raw transcript
{transcript[:50000]}

## Instructions

Create a polished event page with:

1. **YAML frontmatter**: date, time, people (slugs from wiki index), projects (slugs)

2. **AI-generated title as H1** — a concise, descriptive title based on what was actually discussed. NOT the calendar invite title. Examples:
   - "Ruby's spring schedule planning with Sara"
   - "Blade & Rose theme review"
   - "Quick check-in about the roof project"
   - "David thinking through relocation timeline"

3. **Summary section** — 3-5 bullet points at the top covering:
   - Key topics discussed
   - Decisions made
   - Action items / commitments
   - Open questions

4. **User's notes** — if provided above, include them VERBATIM under a "## Notes" section. Do not edit, summarize, or rephrase the user's notes.

5. **Cleaned-up transcript** — the full transcript, but:
   - Fix obvious speech-to-text errors
   - Add paragraph breaks at topic changes
   - Label speakers when distinguishable
   - Remove filler words (um, uh, like) only when excessive
   - Keep in a collapsible `<details>` block

Use [[wiki-links]] for any people or projects that appear in the wiki index.

Return ONLY the markdown content (including frontmatter).
Do not wrap in code fences. Start with --- for the frontmatter.
"""

    gemini = GeminiClient()
    try:
        resp = await gemini.client.aio.models.generate_content(
            model=REFLECT_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=16384,
                temperature=0.2,
            ),
        )
        content = (resp.text or "").strip()
    except Exception:
        log.exception("Meeting wiki page creation failed")
        return None

    # Strip markdown fences if present
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    # Extract the AI-generated title from the H1 for the slug
    import re
    ai_title = calendar_title or "recording"
    h1_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if h1_match:
        ai_title = h1_match.group(1).strip()

    slug_base = re.sub(r"[^a-z0-9]+", "-", ai_title.lower()).strip("-")[:60]
    slug = f"{date_str}/{slug_base}"

    # Write the event page
    try:
        from deja.wiki import write_page
        write_page("events", slug, content)

        from deja.wiki_git import commit_changes
        from deja.wiki_catalog import rebuild_index
        rebuild_index()
        commit_changes(f"recording: {ai_title}")

        log.info("Created event page: events/%s", slug)
        return slug
    except Exception:
        log.exception("Failed to write event page")
        return None


async def process_completed_meeting(session_dir: Path, state: dict) -> dict:
    """Full post-meeting processing pipeline.

    Called after recording stops. Ensures all chunks are transcribed,
    assembles the full transcript, creates the wiki page, emits an
    observation, and cleans up audio files.

    Returns summary dict.
    """
    import hashlib

    # 1. Make sure rolling transcription finishes
    state["recording"] = False
    await transcribe_meeting_rolling(session_dir, state)

    transcripts = state.get("transcripts", [])
    if not any(transcripts):
        log.warning("No transcript content from meeting")
        return {"status": "empty", "reason": "no speech detected in any chunk"}

    metadata = state.get("metadata", {})

    # Calculate duration
    started = state.get("started_at")
    if started:
        try:
            start_dt = datetime.fromisoformat(started)
            duration_min = int((datetime.now(timezone.utc) - start_dt).total_seconds() / 60)
            metadata["duration_min"] = duration_min
        except Exception:
            pass

    # 2. Assemble full transcript
    full_transcript = assemble_meeting_transcript(transcripts, metadata)

    # 3. Save raw transcript to disk
    transcript_path = session_dir / "transcript.md"
    transcript_path.write_text(full_transcript, encoding="utf-8")
    log.info("Saved raw transcript to %s", transcript_path)

    # 4. Create wiki event page
    slug = await create_meeting_wiki_page(full_transcript, metadata)

    # 5. Emit observation so integrate cycle can update entity pages
    title = metadata.get("title", "Meeting")
    attendee_names = [a.get("name", a.get("email", "")) for a in metadata.get("attendees", [])]
    duration = metadata.get("duration_min", 0)

    obs_text = (
        f"MEETING RECORDED: {title} ({duration} min)\n"
        f"Attendees: {', '.join(attendee_names[:5])}\n"
    )
    if slug:
        obs_text += f"Wiki page: events/{slug}\n"

    # Append first ~500 chars of transcript as preview
    preview = "\n".join(t for t in transcripts if t)[:500]
    if preview:
        obs_text += f"\nTranscript preview:\n{preview}"

    ts = datetime.now(timezone.utc).isoformat()
    session_id = state.get("session_id", "unknown")
    id_key = "meeting-" + hashlib.md5(f"{session_id}-{title}".encode()).hexdigest()[:16]

    obs_path = DEJA_HOME / "observations.jsonl"
    try:
        with open(obs_path, "a") as f:
            f.write(json.dumps({
                "source": "meeting_recording",
                "sender": f"Meeting with {', '.join(attendee_names[:3])}" if attendee_names else f"Meeting: {title}",
                "text": obs_text[:2000],
                "timestamp": ts,
                "id_key": id_key,
            }) + "\n")
    except Exception:
        log.exception("Failed to persist meeting observation")

    # 6. Log to activity log
    try:
        from deja.activity_log import append_log_entry
        append_log_entry(
            "meeting",
            f"Recorded: {title} ({duration}min, {len(transcripts)} chunks)"
            + (f" → events/{slug}" if slug else ""),
        )
    except Exception:
        pass

    # 7. Clean up audio files (keep transcript)
    for wav in session_dir.glob("chunk-*.wav"):
        try:
            wav.unlink()
        except OSError:
            pass
    for done in session_dir.glob("chunk-*.done"):
        try:
            done.unlink()
        except OSError:
            pass

    return {
        "status": "processed",
        "slug": slug,
        "chunks_transcribed": len([t for t in transcripts if t]),
        "duration_min": duration,
        "transcript_path": str(transcript_path),
    }
