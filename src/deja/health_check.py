"""Startup health check for the monitor.

Runs once at monitor boot. Probes every external dependency the system
expects to have, and for each failure writes:

  1. A WARNING line to deja.log
  2. A `startup` entry to log.md (inside the wiki) with the problem and
     the exact fix steps, so David sees it in Obsidian

The goal is to replace the current "error spam forever" pattern with a
single loud diagnostic shot at boot. Once a source is known-broken, the
corresponding collector's subsequent failures stay in deja.log but
are rate-limited to one per minute rather than one per signal cycle.

The output looks like this in log.md:

    - **[2026-04-04 21:30]** startup — iMessage collection disabled:
      grant Full Disk Access to Deja.app in System Settings →
      Privacy & Security → Full Disk Access, then restart the app.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass

from deja.config import IMESSAGE_DB, WHATSAPP_DB, WIKI_DIR

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix: str  # one-line actionable fix, or empty if ok


def _check_sqlite(label: str, path) -> CheckResult:
    """Try to open a SQLite database read-only. Returns a CheckResult."""
    fix = (
        f"Grant Full Disk Access to Deja.app (and/or the Python binary "
        f"at venv/bin/python) in System Settings → Privacy & Security → "
        f"Full Disk Access, then restart the app."
    )
    if not path.exists():
        return CheckResult(
            name=label,
            ok=False,
            detail=f"{path} does not exist",
            fix=f"{label} not installed on this Mac — nothing to do.",
        )
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return CheckResult(name=label, ok=True, detail=str(path), fix="")
    except sqlite3.Error as e:
        return CheckResult(
            name=label,
            ok=False,
            detail=f"{e}",
            fix=fix,
        )


def _check_wiki() -> list[CheckResult]:
    results: list[CheckResult] = []
    fix_missing = (
        f"Wiki should live at {WIKI_DIR}. Check permissions, or restore "
        f"from git / backups."
    )
    if not WIKI_DIR.exists():
        results.append(CheckResult(
            name="wiki directory",
            ok=False,
            detail=f"{WIKI_DIR} does not exist",
            fix=fix_missing,
        ))
        return results
    results.append(CheckResult(name="wiki directory", ok=True, detail=str(WIKI_DIR), fix=""))

    # Required files
    for fname in ("CLAUDE.md", "index.md"):
        p = WIKI_DIR / fname
        if p.exists():
            results.append(CheckResult(name=f"wiki/{fname}", ok=True, detail="present", fix=""))
        else:
            results.append(CheckResult(
                name=f"wiki/{fname}",
                ok=False,
                detail="missing",
                fix=f"Create {p} — the monitor cannot run without the schema doc or index.",
            ))

    # Required prompt files
    prompts_dir = WIKI_DIR / "prompts"
    if not prompts_dir.exists():
        results.append(CheckResult(
            name="wiki/prompts/",
            ok=False,
            detail="missing",
            fix=f"Create {prompts_dir} and populate with integrate.md, reflect.md, describe_screen.md, prefilter.md, chat.md.",
        ))
    else:
        required = ("integrate.md", "reflect.md", "describe_screen.md", "prefilter.md", "chat.md")
        missing = [n for n in required if not (prompts_dir / n).exists()]
        if missing:
            results.append(CheckResult(
                name="wiki/prompts/",
                ok=False,
                detail=f"missing files: {', '.join(missing)}",
                fix=f"Recreate the missing prompt files in {prompts_dir}.",
            ))
        else:
            results.append(CheckResult(name="wiki/prompts/", ok=True, detail=f"{len(required)} files", fix=""))

    # Git repo
    if (WIKI_DIR / ".git").exists():
        results.append(CheckResult(name="wiki git repo", ok=True, detail=".git/ present", fix=""))
    else:
        results.append(CheckResult(
            name="wiki git repo",
            ok=False,
            detail="not initialized",
            fix=f"Run `cd {WIKI_DIR} && git init -b main && git add -A && git commit -m 'initial snapshot'`",
        ))

    return results


def _check_ffmpeg() -> CheckResult:
    """ffmpeg is required for the push-to-record microphone feature."""
    path = shutil.which("ffmpeg")
    if path:
        return CheckResult(name="ffmpeg", ok=True, detail=path, fix="")
    return CheckResult(
        name="ffmpeg",
        ok=False,
        detail="not found on PATH",
        fix="Install ffmpeg with `brew install ffmpeg` to enable the Listen button in the popover.",
    )


def _check_screenshot_enabled() -> CheckResult:
    """Surface whether the screenshot collector is enabled.

    This isn't a failure — users can legitimately disable it if macOS
    Screen Recording permission is unstable or they want a visual-free
    mode. Reporting it as a check makes the state visible in log.md so
    it's not mysterious later.
    """
    from deja.config import SCREENSHOT_ENABLED
    if SCREENSHOT_ENABLED:
        return CheckResult(
            name="screenshot collector",
            ok=True,
            detail="enabled",
            fix="",
        )
    return CheckResult(
        name="screenshot collector",
        ok=True,  # disabled is a valid user choice, not a failure
        detail="DISABLED via config.yaml (screenshot_enabled: false)",
        fix="Set screenshot_enabled: true in ~/.deja/config.yaml to re-enable.",
    )


def _check_user_profile() -> CheckResult:
    """Confirm the wiki has a self-page with an email in frontmatter.

    Without this, prompts fall back to generic "the user" language and
    outbound email sends (diagnostic heartbeats, nightly reports) silently
    no-op. Surfacing it at boot is cheap and means a new user sees the
    exact fix in log.md without having to dig through source.
    """
    from deja.identity import load_user

    user = load_user()
    fix = (
        "Create a page at Deja/people/<your-slug>.md starting with "
        "YAML frontmatter: `---\\nself: true\\nemail: you@example.com\\n"
        "preferred_name: Your First Name\\n---` then a short bio. Restart "
        "the monitor."
    )
    if user.is_generic:
        return CheckResult(
            name="user profile",
            ok=False,
            detail="no self-page found (no people/*.md has `self: true` in frontmatter)",
            fix=fix,
        )
    if not user.email:
        return CheckResult(
            name="user profile",
            ok=False,
            detail=f"self-page {user.slug} has no email in frontmatter — outbound email sends will no-op",
            fix=fix,
        )
    return CheckResult(
        name="user profile",
        ok=True,
        detail=f"{user.name} <{user.email}>",
        fix="",
    )


def _check_gemini_key() -> CheckResult:
    """Verify the Gemini API key is resolvable from env or keychain."""
    from deja.secrets import get_api_key, api_key_source
    key = get_api_key()
    source = api_key_source()
    if key:
        return CheckResult(
            name="gemini api key",
            ok=True,
            detail=f"{key[:8]}… (source: {source})",
            fix="",
        )
    return CheckResult(
        name="gemini api key",
        ok=False,
        detail="not configured (checked env vars and macOS keychain)",
        fix="Run `deja configure` to store the key in the macOS keychain, or export GEMINI_API_KEY in your shell.",
    )


def _OLD_check_gemini_key() -> CheckResult:
    """Gemini API key must be set as an environment variable."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return CheckResult(name="gemini api key", ok=True, detail=f"{key[:8]}…", fix="")
    return CheckResult(
        name="gemini api key",
        ok=False,
        detail="GEMINI_API_KEY not set in environment",
        fix="Set GEMINI_API_KEY in your shell or launch.sh so the monitor and web subprocesses inherit it.",
    )


def run_health_checks() -> list[CheckResult]:
    """Run every probe and return the results list."""
    results: list[CheckResult] = []
    results.append(_check_gemini_key())
    results.extend(_check_wiki())
    results.append(_check_user_profile())
    results.append(_check_sqlite("iMessage", IMESSAGE_DB))
    results.append(_check_sqlite("WhatsApp", WHATSAPP_DB))
    results.append(_check_ffmpeg())
    results.append(_check_screenshot_enabled())
    return results


def report_health_checks() -> set[str]:
    """Run all checks, log failures to deja.log AND write one
    ``startup`` entry per failure to log.md so they're visible in Obsidian.

    Returns a set of failure names. Callers can use this to suppress
    redundant per-cycle errors for already-known-broken sources.
    """
    results = run_health_checks()
    failures: set[str] = set()
    ok_count = 0
    for r in results:
        if r.ok:
            log.info("startup check [%s]: OK (%s)", r.name, r.detail)
            ok_count += 1
        else:
            failures.add(r.name)
            log.warning("startup check [%s]: FAIL — %s", r.name, r.detail)
            log.warning("    fix: %s", r.fix)

    # Emit a single summary entry + one detail entry per failure to log.md.
    try:
        from deja.activity_log import append_log_entry
        if failures:
            append_log_entry(
                "startup",
                f"{ok_count}/{len(results)} checks passed — {len(failures)} issue(s) need attention (see below)",
            )
            for r in results:
                if r.ok:
                    continue
                append_log_entry("startup", f"{r.name} — {r.detail}. Fix: {r.fix}")
        else:
            append_log_entry("startup", f"all {len(results)} checks passed")
    except Exception:
        log.debug("failed to write startup report to log.md", exc_info=True)

    return failures
