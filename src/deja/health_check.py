"""Startup health check for the monitor.

Runs once at monitor boot. Probes every external dependency the system
expects to have, and for each failure writes:

  1. A WARNING line to ``deja.log`` (Python stdlib logger).
  2. A ``health_check`` entry in ``~/.deja/audit.jsonl`` via
     ``audit.record()`` with the problem and the exact fix steps, so
     the failure is grep-able after the fact.

The goal is to replace the current "error spam forever" pattern with a
single loud diagnostic shot at boot. Once a source is known-broken, the
corresponding collector's subsequent failures stay in deja.log but
are rate-limited to one per minute rather than one per signal cycle.
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
    p = WIKI_DIR / "index.md"
    if p.exists():
        results.append(CheckResult(name="wiki/index.md", ok=True, detail="present", fix=""))
    else:
        results.append(CheckResult(
            name="wiki/index.md",
            ok=False,
            detail="missing",
            fix=f"Create {p} — the monitor cannot run without the wiki index.",
        ))

    # Required prompt files
    prompts_dir = WIKI_DIR / "prompts"
    if not prompts_dir.exists():
        results.append(CheckResult(
            name="wiki/prompts/",
            ok=False,
            detail="missing",
            fix=f"Create {prompts_dir} and populate with integrate.md, dedup_confirm.md, describe_screen.md, prefilter.md, command.md, onboard.md.",
        ))
    else:
        required = (
            "integrate.md",
            "dedup_confirm.md",
            "describe_screen.md",
            "prefilter.md",
            "command.md",
            "onboard.md",
        )
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
    mode. Reporting it as a check makes the state visible in audit.jsonl so
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
    exact fix in audit.jsonl without having to dig through source.
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


def _check_server() -> CheckResult:
    """Check connectivity to the Deja API server (or direct dev mode)."""
    if os.environ.get("GEMINI_API_KEY"):
        return CheckResult(
            name="llm backend",
            ok=True,
            detail="direct mode (dev) — GEMINI_API_KEY is set",
            fix="",
        )
    from deja.llm_client import DEJA_API_URL
    try:
        import httpx
        r = httpx.get(f"{DEJA_API_URL}/v1/health", timeout=5)
        if r.status_code < 500:
            return CheckResult(
                name="llm backend",
                ok=True,
                detail=f"server reachable at {DEJA_API_URL}",
                fix="",
            )
        return CheckResult(
            name="llm backend",
            ok=False,
            detail=f"server returned {r.status_code}",
            fix=f"Check the Deja API server at {DEJA_API_URL}.",
        )
    except Exception as e:
        return CheckResult(
            name="llm backend",
            ok=False,
            detail=f"server unreachable at {DEJA_API_URL}: {e}",
            fix=f"Ensure the Deja API server is running at {DEJA_API_URL}, or set GEMINI_API_KEY for direct mode.",
        )


def run_health_checks() -> list[CheckResult]:
    """Run every probe and return the results list."""
    results: list[CheckResult] = []
    results.append(_check_server())
    results.extend(_check_wiki())
    results.append(_check_user_profile())
    results.append(_check_sqlite("iMessage", IMESSAGE_DB))
    results.append(_check_sqlite("WhatsApp", WHATSAPP_DB))
    results.append(_check_ffmpeg())
    results.append(_check_screenshot_enabled())
    return results


def report_health_checks() -> set[str]:
    """Run all checks, log failures to deja.log AND write one
    ``health_check`` entry per failure to audit.jsonl for later grep.

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

    # Emit a single summary entry + one detail entry per failure.
    try:
        from deja import audit
        if failures:
            audit.record(
                "health_check",
                target="startup/summary",
                reason=(
                    f"{ok_count}/{len(results)} checks passed — "
                    f"{len(failures)} issue(s) need attention"
                ),
                trigger={"kind": "startup", "detail": "boot"},
            )
            for r in results:
                if r.ok:
                    continue
                audit.record(
                    "health_check",
                    target=f"startup/{r.name}",
                    reason=f"{r.detail}. Fix: {r.fix}",
                    trigger={"kind": "startup", "detail": "boot"},
                )
        else:
            audit.record(
                "health_check",
                target="startup/summary",
                reason=f"all {len(results)} checks passed",
                trigger={"kind": "startup", "detail": "boot"},
            )
    except Exception:
        log.debug("failed to write startup audit", exc_info=True)

    return failures
