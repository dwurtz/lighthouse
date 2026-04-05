"""lighthouse CLI entry point.

Three subcommands, all thin wrappers:
  lighthouse monitor  — headless signal collection + analysis loop
  lighthouse web      — FastAPI backend for the notch app
  lighthouse status   — print live state (green/red, last signal, etc.)

The notch app (Lighthouse.app) spawns `monitor` and `web` as child processes.
There is no interactive chat CLI — chat happens through the notch.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import os
import signal as _signal
import sys

from lighthouse.config import LIGHTHOUSE_HOME


def _ensure_single_instance(name: str) -> None:
    """Kill any existing process with the same role and write our PID."""
    pid_file = LIGHTHOUSE_HOME / f"{name}.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, _signal.SIGTERM)
            import time
            time.sleep(1)
            try:
                os.kill(old_pid, _signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    pid_file.write_text(str(os.getpid()))
    atexit.register(lambda: pid_file.unlink(missing_ok=True))


def _setup_logging() -> None:
    LIGHTHOUSE_HOME.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LIGHTHOUSE_HOME / "lighthouse.log"),
            logging.StreamHandler(),
        ],
    )


async def _run_monitor() -> None:
    from lighthouse.llm_client import GeminiClient
    from lighthouse.agent.loop import AgentLoop
    from lighthouse.observations.collector import Observer

    gemini = GeminiClient()
    collector = Observer()
    monitor = AgentLoop(gemini, collector)

    # Nightly cleanup used to be scheduled via apscheduler cron at 02:00,
    # but that silently missed fires when macOS was in maintenance sleep
    # at the scheduled time. Nightly is now triggered by the monitor
    # loop's own catch-up logic (see AgentLoop._maybe_run_catchup_nightly),
    # which checks on startup and at the start of every analysis cycle
    # whether nightly has run since the most recent 02:00 threshold.
    # Simpler, survives sleep, no external scheduler dependency.

    print("Monitor running. Press Ctrl+C to stop.")
    try:
        await monitor.run()
    except KeyboardInterrupt:
        monitor.stop()


def _show_status() -> None:
    """Print a short liveness summary."""
    import json
    from datetime import datetime, timezone

    signal_log = LIGHTHOUSE_HOME / "observations.jsonl"
    last_signal = None
    if signal_log.exists():
        for line in reversed(signal_log.read_text().splitlines()[-20:]):
            line = line.strip()
            if not line:
                continue
            try:
                last_signal = json.loads(line)
                break
            except json.JSONDecodeError:
                continue

    print(f"Home: {LIGHTHOUSE_HOME}")
    if last_signal:
        ts = last_signal.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.astimezone()
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            print(f"Last signal: {ts} ({int(age)}s ago — {last_signal.get('source', '?')})")
            print(f"Monitor: {'RUNNING' if age < 120 else 'STALE'}")
        except Exception:
            print(f"Last signal: {ts} (could not parse)")
    else:
        print("No signals yet")


def main() -> None:
    parser = argparse.ArgumentParser(description="lighthouse — David's personal agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("monitor", help="Run the headless signal/analysis loop")
    sub.add_parser("status", help="Print liveness summary")
    web_p = sub.add_parser("web", help="Start the FastAPI backend for the notch app")
    web_p.add_argument("--port", type=int, default=5055)
    link_p = sub.add_parser(
        "linkify",
        help="Sweep the wiki and wrap unlinked entity mentions in [[slug]] syntax",
    )
    link_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing",
    )
    onboard_p = sub.add_parser(
        "onboard",
        help="One-time wiki bootstrap from historical context (sent email, iMessage, WhatsApp)",
    )
    onboard_p.add_argument(
        "--days",
        type=int,
        default=30,
        help="How many days of history to ingest per source (default: 30)",
    )
    onboard_p.add_argument(
        "--force",
        action="store_true",
        help="Re-run onboarding steps even if the marker says they're done",
    )
    onboard_p.add_argument(
        "--only",
        choices=["email", "imessage", "whatsapp"],
        default=None,
        help="Run only one step instead of all pending steps",
    )

    args = parser.parse_args()
    command = args.command or "monitor"

    _setup_logging()
    LIGHTHOUSE_HOME.mkdir(parents=True, exist_ok=True)

    if command == "monitor":
        _ensure_single_instance("monitor")
        asyncio.run(_run_monitor())
    elif command == "web":
        _ensure_single_instance("web")
        from lighthouse.web import run_web
        run_web(port=args.port)
    elif command == "status":
        _show_status()
    elif command == "linkify":
        _run_linkify(dry_run=args.dry_run)
    elif command == "onboard":
        asyncio.run(_run_onboard(days=args.days, force=args.force, only=args.only))
    else:
        parser.print_help()
        sys.exit(1)


async def _run_onboard(*, days: int, force: bool, only: str | None) -> None:
    """Run one or all onboarding backfill steps from the CLI.

    Default behavior runs every pending step in order (sent email →
    iMessage → WhatsApp). ``--only`` restricts to one source.
    ``--force`` ignores the marker and re-runs. Progress is printed
    per batch so the user can watch the wiki fill up in real time.
    All the real work lives in ``lighthouse.onboarding``.
    """
    from lighthouse.onboarding import run_all_pending_steps

    def _progress(info: dict) -> None:
        step = info.get("step", "?")
        batch = info.get("batch")
        total = info.get("total_batches")
        pages = info.get("pages_written")
        print(f"  [{step}] batch {batch}/{total} — {pages} page update(s) so far")

    if only:
        print(f"Onboarding: running {only} step ({days} days)…")
    else:
        print(f"Onboarding: running all pending steps ({days} days)…")

    try:
        summaries = await run_all_pending_steps(
            days=days,
            force=force,
            only=only,
            on_progress=_progress,
        )
    except ValueError as e:
        print(f"Error: {e}")
        return

    if not summaries:
        print("Nothing to do.")
        return

    # Per-step summary output.
    any_work = False
    for s in summaries:
        step = s.get("step", "?")
        if s.get("skipped") == "already_done":
            print(f"  [{step}] already done (use --force to re-run)")
            continue
        if s.get("skipped") == "no_self_page":
            print(
                f"  [{step}] skipped — no self-page in the wiki. Create "
                "`Lighthouse/people/<your-slug>.md` with YAML frontmatter "
                "containing `self: true` and `email: you@example.com`, "
                "then re-run `lighthouse onboard`."
            )
            return
        if s.get("skipped") in ("imessage_no_access", "whatsapp_no_access"):
            fix = s.get("fix", "")
            print(f"  [{step}] skipped — access denied. {fix}")
            continue
        if s.get("skipped") in ("imessage_db_missing", "whatsapp_db_missing"):
            print(f"  [{step}] skipped — database not present on this Mac")
            continue
        if s.get("skipped"):
            print(f"  [{step}] skipped — {s.get('skipped')}")
            continue
        any_work = True
        print(
            f"  [{step}] done — {s.get('pages_written', 0)} page update(s) "
            f"from {s.get('observations_fetched', 0)} item(s) across "
            f"{s.get('batches_run', 0)} batch(es)"
        )

    if not any_work:
        print("No new onboarding work was performed.")
    else:
        print("Onboarding complete.")


def _run_linkify(*, dry_run: bool) -> None:
    """Run the deterministic wiki linkifier once and print a report."""
    from lighthouse.wiki_linkify import linkify_wiki

    report = linkify_wiki(dry_run=dry_run)
    mode = " (dry-run)" if dry_run else ""
    print(f"linkify{mode}: scanned {report.pages_scanned} pages")
    if report.pages_changed:
        print(
            f"  added {report.links_added} link(s) across "
            f"{report.pages_changed} page(s)"
        )
        for slug, n in sorted(report.links_by_slug.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {n:>3}× [[{slug}]]")
    else:
        print("  no changes")
    if report.broken_refs:
        print(f"  {len(report.broken_refs)} broken ref(s):")
        for src, target in report.broken_refs:
            print(f"    {src} → [[{target}]]")


if __name__ == "__main__":
    main()
