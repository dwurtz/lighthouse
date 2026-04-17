"""deja CLI entry point.

Three subcommands, all thin wrappers:
  deja monitor  — headless signal collection + analysis loop
  deja web      — FastAPI backend for the notch app
  deja status   — print live state (green/red, last signal, etc.)

The notch app (Deja.app) spawns `monitor` and `web` as child processes.
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

from deja.config import DEJA_HOME


def _ensure_single_instance(name: str) -> None:
    """Kill any existing process with the same role and write our PID."""
    pid_file = DEJA_HOME / f"{name}.pid"
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
    DEJA_HOME.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(DEJA_HOME / "deja.log"),
            logging.StreamHandler(),
        ],
    )


async def _run_monitor() -> None:
    from deja.llm_client import GeminiClient
    from deja.agent.loop import AgentLoop
    from deja.observations.collector import Observer
    from deja.telemetry import track, track_heartbeat

    track("monitor_started")

    gemini = GeminiClient()
    collector = Observer()
    monitor = AgentLoop(gemini, collector)

    # Periodic heartbeat — sends system state every 10 minutes
    import asyncio
    async def _heartbeat_loop():
        while True:
            await asyncio.sleep(600)
            track_heartbeat()

    asyncio.get_event_loop().create_task(_heartbeat_loop())

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

    signal_log = DEJA_HOME / "observations.jsonl"
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

    print(f"Home: {DEJA_HOME}")
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
        description="Deja — a personal AI agent that observes your digital life "
                    "and maintains a living wiki about the people and projects that matter to you."
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "configure",
        help="Interactive first-run setup — configure identity and permissions",
    )
    sub.add_parser(
        "health",
        help="Run all startup checks and print current configuration state",
    )
    sub.add_parser("monitor", help="Run the headless observe/integrate/reflect loop")
    sub.add_parser(
        "mcp",
        help="Start the MCP server (stdio transport) — connects to Claude Desktop/Code",
    )
    sub.add_parser("status", help="Print liveness summary")
    sub.add_parser("web", help="Start the FastAPI backend on Unix socket (~/.deja/deja.sock)")
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
        choices=["email", "imessage", "whatsapp", "calendar", "meet"],
        default=None,
        help="Run only one step instead of all pending steps",
    )
    sub.add_parser(
        "briefing",
        help="Print the same daily-briefing view the MCP agent sees (profile + goals + active projects + recent narratives)",
    )
    cos_p = sub.add_parser(
        "cos",
        help="Chief-of-staff loop — spawn a local `claude` after each substantive cycle to decide whether to notify or act",
    )
    cos_sub = cos_p.add_subparsers(dest="cos_command")
    cos_sub.add_parser("status", help="Show whether the loop is enabled and where config lives")
    cos_sub.add_parser("enable", help="Enable the chief-of-staff loop (creates config on first run)")
    cos_sub.add_parser("disable", help="Disable the chief-of-staff loop")
    cos_sub.add_parser("test", help="Fire the loop once with a synthetic payload — good for verifying claude CLI + MCP wiring")
    cos_sub.add_parser("tail", help="Tail the invocations log to see what Claude has been deciding and doing")
    wh_p = sub.add_parser(
        "webhooks",
        help="Manage outbound webhooks (Claude Code Routines, Slack, etc.) that fire after each integrate cycle",
    )
    wh_sub = wh_p.add_subparsers(dest="wh_command")
    wh_sub.add_parser("list", help="Show configured webhooks")
    wh_add = wh_sub.add_parser("add", help="Register a new webhook")
    wh_add.add_argument("--name", required=True, help="Display name (e.g. 'chief-of-staff')")
    wh_add.add_argument("--url", required=True, help="POST target URL")
    wh_rm = wh_sub.add_parser("remove", help="Remove a webhook by name")
    wh_rm.add_argument("name")
    wh_test = wh_sub.add_parser("test", help="Fire a synthetic cycle payload at all configured webhooks")
    wh_test.add_argument("--name", help="Only fire the webhook with this name")
    trail_p = sub.add_parser(
        "trail",
        aliases=["hermes-trail"],  # old name, kept working
        help="Show recent agent audit entries (trigger.kind=mcp) — see what the chief-of-staff has been doing",
    )
    trail_p.add_argument(
        "--hours",
        type=int,
        default=24,
        help="How many hours of trail to show (default: 24)",
    )
    trail_p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max entries to show (default: 100)",
    )

    args = parser.parse_args()
    command = args.command or "monitor"

    _setup_logging()
    DEJA_HOME.mkdir(parents=True, exist_ok=True)

    if command == "configure":
        _run_configure()
    elif command == "health":
        _run_health()
    elif command == "mcp":
        _run_mcp()
    elif command == "monitor":
        _ensure_single_instance("monitor")
        asyncio.run(_run_monitor())
    elif command == "web":
        _ensure_single_instance("web")
        from deja.web import run_web
        run_web()
    elif command == "status":
        _show_status()
    elif command == "linkify":
        _run_linkify(dry_run=args.dry_run)
    elif command == "onboard":
        asyncio.run(_run_onboard(days=args.days, force=args.force, only=args.only))
    elif command == "briefing":
        from deja.mcp_server import _daily_briefing
        print(_daily_briefing())
    elif command == "webhooks":
        _run_webhooks(getattr(args, "wh_command", None), args)
    elif command == "cos":
        _run_cos(getattr(args, "cos_command", None))
    elif command in ("trail", "hermes-trail"):
        _run_trail(hours=args.hours, limit=args.limit)
    else:
        parser.print_help()
        sys.exit(1)


def _run_configure() -> None:
    """Interactive first-run setup.

    Walks the user through the minimum setup needed to boot Deja
    from a fresh clone: API key, self-page, default prompts, health
    check. Designed to be safe to re-run — detects existing config and
    asks before overwriting.
    """
    from deja.config import DEJA_HOME, WIKI_DIR

    print("Deja setup")
    print("=" * 60)
    print()

    # --- 1. Wiki layout ---
    print("[1/4] Wiki layout")
    print(f"  wiki lives at: {WIKI_DIR}")
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    (WIKI_DIR / "people").mkdir(exist_ok=True)
    (WIKI_DIR / "projects").mkdir(exist_ok=True)
    # Prompts are bundled inside the package — no wiki copy needed.

    # Ensure the wiki is a git repo
    try:
        from deja.wiki_git import ensure_repo
        ensure_repo()
        print("  wiki git repo ready")
    except Exception as e:
        print(f"  warning: git init skipped ({e})")
    print()

    # --- 2. Self-page (identity) ---
    print("[2/4] Identity self-page")
    from deja.identity import load_user
    user = load_user()
    if not user.is_generic:
        print(f"  already configured: {user.name} <{user.email}>")
    else:
        name = input("  full name (e.g. Jane Doe): ").strip()
        if not name:
            print("  skipping — run `deja configure` again later to add")
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

    # --- 3. MCP auto-install ---
    print("[3/4] MCP server configuration")
    from deja.mcp_install import install_on_all, print_install_report
    mcp_results = install_on_all()
    print_install_report(mcp_results, indent="  ")
    print()

    # --- 4. Health check ---
    print("[4/4] Health check")
    _run_health(indent="  ")
    print()
    print("=" * 60)
    print("Setup complete. Next steps:")
    print()
    print("  1. Grant macOS permissions in System Settings → Privacy & Security:")
    print("     - Full Disk Access → Deja.app + python3")
    print("     - Screen & Audio Recording → Deja.app")
    print("     - Contacts → Deja.app")
    print("  2. Restart any AI clients that were just configured (Claude, Cursor, etc.)")
    print("  3. Start the agent:")
    print("     python -m deja monitor           # headless CLI")
    print("     open Deja.app                     # menu-bar app (if built)")
    print()


def _run_health(indent: str = "") -> None:
    """Print current config + all startup checks, then exit."""
    from deja.health_check import run_health_checks
    from deja.config import DEJA_HOME, WIKI_DIR, INTEGRATE_MODEL, VISION_MODEL, REFLECT_MODEL
    from deja.llm_client import DEJA_API_URL

    if not indent:
        print("Deja health check")
        print("=" * 60)
        print()
        print(f"  DEJA_HOME        = {DEJA_HOME}")
        print(f"  WIKI_DIR         = {WIKI_DIR}")
        print(f"  INTEGRATE_MODEL  = {INTEGRATE_MODEL}")
        print(f"  VISION_MODEL     = {VISION_MODEL}")
        print(f"  REFLECT_MODEL    = {REFLECT_MODEL}")
        print(f"  DEJA_API_URL     = {DEJA_API_URL}")
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


def _run_cos(sub_command: str | None) -> None:
    """Manage the chief-of-staff loop."""
    import shutil as _shutil
    from deja import chief_of_staff as cos

    if sub_command in (None, "status"):
        claude_path = _shutil.which("claude") or "(not installed)"
        enabled = cos.is_enabled()
        print(f"Chief-of-staff loop: {'ENABLED' if enabled else 'disabled'}")
        print(f"  claude CLI:      {claude_path}")
        print(f"  config dir:      {cos.COS_DIR}")
        print(f"  system prompt:   {cos.COS_SYSTEM_PROMPT}"
              + ("" if cos.COS_SYSTEM_PROMPT.exists() else "  (not created yet)"))
        print(f"  MCP config:      {cos.COS_MCP_CONFIG}"
              + ("" if cos.COS_MCP_CONFIG.exists() else "  (not created yet)"))
        print(f"  invocation log:  {cos.COS_LOG}")
        if enabled:
            print("\nFires after each substantive integrate cycle. Disable with `deja cos disable`.")
        else:
            print("\nEnable with `deja cos enable`, then test with `deja cos test`.")
        return

    if sub_command == "enable":
        cos.enable()
        print(f"enabled — fires after substantive cycles. Tail with `deja cos tail`.")
        return

    if sub_command == "disable":
        cos.disable()
        print("disabled")
        return

    if sub_command == "test":
        if not cos.is_enabled():
            print("(cos is disabled — `deja cos enable` first)")
            return
        cos.invoke(
            cycle_id="test-cos-manual",
            narrative="Synthetic test invocation from `deja cos test`. "
                      "If Claude sees this, it should recognize it as a "
                      "test and return SILENT (no email) after optionally "
                      "reading the briefing to verify MCP wiring.",
            wiki_updates=[],
            tasks_update={},
            due_reminders=[],
            new_t1_signal_count=1,
        )
        print("invocation fired in background — tail with `deja cos tail` or `deja trail --hours 1`")
        import time as _time
        _time.sleep(2)
        return

    if sub_command == "tail":
        if not cos.COS_LOG.exists():
            print(f"(no invocations yet — log at {cos.COS_LOG})")
            return
        import json as _json
        lines = cos.COS_LOG.read_text(encoding="utf-8").splitlines()
        for line in lines[-10:]:
            try:
                entry = _json.loads(line)
            except Exception:
                continue
            ts = entry.get("ts", "")[:19].replace("T", " ")
            rc = entry.get("rc")
            cycle = entry.get("cycle_id", "")
            print(f"[{ts}] cycle={cycle} rc={rc}")
            stdout = (entry.get("stdout") or "").strip()
            if stdout:
                for ln in stdout.splitlines()[-6:]:
                    print(f"    {ln}")
            stderr = (entry.get("stderr") or "").strip()
            if stderr and rc != 0:
                print(f"  stderr: {stderr[:300]}")
            print()
        return

    print("unknown cos subcommand — try: status | enable | disable | test | tail")


def _run_webhooks(sub_command: str | None, args) -> None:
    """List / add / remove / test outbound webhooks."""
    import yaml
    from deja.webhooks import WEBHOOKS_CONFIG, _load_webhooks, emit_cycle_complete

    if sub_command in (None, "list"):
        webhooks = _load_webhooks()
        if not webhooks:
            print(f"(no webhooks configured at {WEBHOOKS_CONFIG})")
            return
        print(f"Configured webhooks ({WEBHOOKS_CONFIG}):\n")
        for w in webhooks:
            flag = "✓" if w.enabled else "✗"
            print(f"  [{flag}] {w.name}  →  {w.url}")
        return

    if sub_command == "add":
        existing = _load_webhooks()
        if any(w.name == args.name for w in existing):
            print(f"(webhook '{args.name}' already exists — remove it first)")
            return
        entries = [{"name": w.name, "url": w.url, "enabled": w.enabled} for w in existing]
        entries.append({"name": args.name, "url": args.url, "enabled": True})
        WEBHOOKS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        WEBHOOKS_CONFIG.write_text(
            yaml.safe_dump({"webhooks": entries}, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        print(f"added webhook '{args.name}' → {args.url}")
        return

    if sub_command == "remove":
        existing = _load_webhooks()
        kept = [w for w in existing if w.name != args.name]
        if len(kept) == len(existing):
            print(f"(no webhook named '{args.name}')")
            return
        entries = [{"name": w.name, "url": w.url, "enabled": w.enabled} for w in kept]
        WEBHOOKS_CONFIG.write_text(
            yaml.safe_dump({"webhooks": entries}, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        print(f"removed webhook '{args.name}'")
        return

    if sub_command == "test":
        target_name = getattr(args, "name", None)
        emit_cycle_complete(
            cycle_id="test-cycle-manual",
            narrative=(
                "This is a synthetic test webhook from `deja webhooks test`. "
                "Nothing actually happened; this is just verifying your receiver "
                "is reachable and returning 2xx."
            ),
            wiki_updates=[{"category": "projects", "slug": "clawvisor"}],
            tasks_update={"add_tasks": ["test task — ignore"]},
            due_reminders=[],
            new_t1_signal_count=1,
        )
        # emit_cycle_complete is async (daemon thread); give it a beat
        # to fire before we return control so the user sees the audit
        # line written.
        import time as _time
        _time.sleep(1)
        print("test webhook fired — check `deja trail --hours 1` for the audit entry")
        return

    print("unknown webhooks subcommand — try: list | add | remove | test")


def _run_trail(hours: int, limit: int) -> None:
    """Print agent (MCP-triggered) audit entries from the last N hours.

    Tails ``~/.deja/audit.jsonl`` and shows only rows where
    ``trigger.kind == "mcp"``. Gives the user visibility into every
    write the chief-of-staff loop (or any MCP client) has made,
    without grepping JSON by hand.
    """
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from deja.audit import AUDIT_LOG

    if not AUDIT_LOG.exists():
        print("(no audit log at %s — nothing yet)" % AUDIT_LOG)
        return

    cutoff = _dt.now(_tz.utc) - _td(hours=hours)
    rows: list[dict] = []
    try:
        for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            trig = entry.get("trigger") or {}
            if trig.get("kind") != "mcp":
                continue
            try:
                ts = _dt.fromisoformat(entry["ts"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts < cutoff:
                continue
            rows.append(entry)
    except OSError as e:
        print(f"(couldn't read audit log: {e})")
        return

    if not rows:
        print(f"(no agent activity in the last {hours}h)")
        return

    rows = rows[-limit:]
    print(f"Agent activity — last {hours}h, showing {len(rows)} entries\n")
    for e in rows:
        ts_str = e.get("ts", "")[:19].replace("T", " ")
        action = e.get("action", "")
        target = e.get("target", "")
        reason = e.get("reason", "")
        print(f"  [{ts_str}] {action:<24}  {target}")
        if reason:
            print(f"                          └─ {reason}")


def _run_mcp() -> None:
    """Start the MCP server over stdio for Claude Desktop / Claude Code."""
    from deja.mcp_server import run_mcp_server
    asyncio.run(run_mcp_server())


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
    All the real work lives in ``deja.onboarding``.
    """
    from deja.onboarding import run_all_pending_steps

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
                "`Deja/people/<your-slug>.md` with YAML frontmatter "
                "containing `self: true` and `email: you@example.com`, "
                "then re-run `deja onboard`."
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
    from deja.wiki_linkify import linkify_wiki

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
