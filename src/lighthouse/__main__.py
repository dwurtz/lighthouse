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
    parser = argparse.ArgumentParser(
        description="Lighthouse — a personal AI agent that observes your digital life "
                    "and maintains a living wiki about the people and projects that matter to you."
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "configure",
        help="Interactive first-run setup — store API key in keychain, create self-page",
    )
    sub.add_parser(
        "health",
        help="Run all startup checks and print current configuration state",
    )
    sub.add_parser("monitor", help="Run the headless observe/integrate/reflect loop")
    sub.add_parser("status", help="Print liveness summary")
    web_p = sub.add_parser("web", help="Start the FastAPI backend for the popover")
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

    if command == "configure":
        _run_configure()
    elif command == "health":
        _run_health()
    elif command == "monitor":
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


def _run_configure() -> None:
    """Interactive first-run setup.

    Walks the user through the minimum setup needed to boot Lighthouse
    from a fresh clone: API key, self-page, default prompts, health
    check. Designed to be safe to re-run — detects existing config and
    asks before overwriting.
    """
    import getpass
    from lighthouse.config import LIGHTHOUSE_HOME, WIKI_DIR
    from lighthouse.secrets import get_api_key, store_api_key, api_key_source

    print("Lighthouse setup")
    print("=" * 60)
    print()

    # --- 1. Gemini API key (stored in macOS Keychain) ---
    print("[1/4] Gemini API key")
    existing = get_api_key()
    source = api_key_source()
    if existing:
        print(f"  already configured ({source})")
        ans = input("  replace it? [y/N] ").strip().lower()
        if ans != "y":
            print("  keeping existing key")
        else:
            key = getpass.getpass("  new GEMINI_API_KEY: ").strip()
            if key:
                store_api_key(key)
                print("  stored in macOS keychain (service=lighthouse)")
            else:
                print("  empty input, skipping")
    else:
        print("  get a free key at https://aistudio.google.com/app/apikey")
        key = getpass.getpass("  GEMINI_API_KEY: ").strip()
        if not key:
            print("  no key entered — setup cannot continue")
            sys.exit(1)
        store_api_key(key)
        print("  stored in macOS keychain (service=lighthouse)")
    print()

    # --- 2. Wiki layout + default prompts ---
    print("[2/4] Wiki layout")
    print(f"  wiki lives at: {WIKI_DIR}")
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    (WIKI_DIR / "people").mkdir(exist_ok=True)
    (WIKI_DIR / "projects").mkdir(exist_ok=True)
    (WIKI_DIR / "prompts").mkdir(exist_ok=True)

    # Copy bundled default prompts + CLAUDE.md if missing
    from pathlib import Path
    repo_defaults = Path(__file__).parent / "default_assets"
    if repo_defaults.is_dir():
        for src in repo_defaults.glob("prompts/*.md"):
            dst = WIKI_DIR / "prompts" / src.name
            if not dst.exists():
                dst.write_text(src.read_text())
                print(f"  created prompts/{src.name}")
        for fname in ("CLAUDE.md",):
            src = repo_defaults / fname
            dst = WIKI_DIR / fname
            if src.exists() and not dst.exists():
                dst.write_text(src.read_text())
                print(f"  created {fname}")
    else:
        print(f"  (default assets not found at {repo_defaults} — skipping defaults)")

    # Ensure the wiki is a git repo
    try:
        from lighthouse.wiki_git import ensure_repo
        ensure_repo()
        print("  wiki git repo ready")
    except Exception as e:
        print(f"  warning: git init skipped ({e})")
    print()

    # --- 3. Self-page (identity) ---
    print("[3/4] Identity self-page")
    from lighthouse.identity import load_user
    user = load_user()
    if not user.is_generic:
        print(f"  already configured: {user.name} <{user.email}>")
    else:
        name = input("  full name (e.g. Jane Doe): ").strip()
        if not name:
            print("  skipping — run `lighthouse configure` again later to add")
        else:
            email = input("  email: ").strip()
            first_default = name.split()[0] if name else ""
            first = input(f"  preferred first name [{first_default}]: ").strip() or first_default
            slug = _slugify(name)
            self_path = WIKI_DIR / "people" / f"{slug}.md"
            if self_path.exists():
                print(f"  {self_path} already exists — not overwriting")
            else:
                frontmatter = ["---", "self: true"]
                if email:
                    frontmatter.append("emails:")
                    frontmatter.append(f"  - {email}")
                frontmatter.append(f"preferred_name: {first}")
                frontmatter.append("---")
                body = f"\n# {name}\n\nA short bio — role, current focus, key relationships. The agent reads this at the top of every prompt to know who it's working for.\n"
                self_path.write_text("\n".join(frontmatter) + body)
                print(f"  created {self_path.relative_to(WIKI_DIR.parent)}")
    print()

    # --- 4. Health check ---
    print("[4/4] Health check")
    _run_health(indent="  ")
    print()
    print("=" * 60)
    print("Setup complete. Next steps:")
    print()
    print("  1. Grant macOS permissions in System Settings → Privacy & Security:")
    print("     - Full Disk Access → Lighthouse.app + python3")
    print("     - Screen & Audio Recording → Lighthouse.app")
    print("     - Contacts → Lighthouse.app")
    print("  2. Start the agent:")
    print("     python -m lighthouse monitor           # headless CLI")
    print("     open Lighthouse.app                     # menu-bar app (if built)")
    print()


def _run_health(indent: str = "") -> None:
    """Print current config + all startup checks, then exit."""
    from lighthouse.health_check import run_health_checks
    from lighthouse.config import LIGHTHOUSE_HOME, WIKI_DIR, INTEGRATE_MODEL, VISION_MODEL, REFLECT_MODEL
    from lighthouse.secrets import api_key_source

    if not indent:
        print("Lighthouse health check")
        print("=" * 60)
        print()
        print(f"  LIGHTHOUSE_HOME  = {LIGHTHOUSE_HOME}")
        print(f"  WIKI_DIR         = {WIKI_DIR}")
        print(f"  INTEGRATE_MODEL  = {INTEGRATE_MODEL}")
        print(f"  VISION_MODEL     = {VISION_MODEL}")
        print(f"  REFLECT_MODEL    = {REFLECT_MODEL}")
        print(f"  API key source   = {api_key_source()}")
        print()

    results = run_health_checks()
    any_fail = False
    for r in results:
        icon = "✓" if r.ok else "✗"
        print(f"{indent}{icon} {r.name}: {r.detail}")
        if not r.ok:
            any_fail = True
            print(f"{indent}  fix: {r.fix}")
    if not indent and any_fail:
        print()
        print("One or more checks failed. Address the fixes above, then re-run.")
        sys.exit(1)


def _slugify(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "unnamed"


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
