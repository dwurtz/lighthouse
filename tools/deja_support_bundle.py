"""Package a small, privacy-safe Deja support bundle.

Produces ``~/Downloads/deja-support-<UTC-timestamp>.zip`` containing the
minimum a Deja engineer needs to triage a bug report:

  * last 1000 lines of  ``~/.deja/deja.log``
  * last  500 rows  of  ``~/.deja/errors.jsonl``
  * last  500 rows  of  ``~/.deja/audit.jsonl``
  * ``~/.deja/feature_flags.json`` (verbatim if present)
  * ``machine_info.txt`` — ``sw_vers``, ``sysctl hw.model``, Python
                           version, Deja version
  * ``README.txt``       — what's inside + privacy posture

PRIVACY — DO NOT ADD ``observations.jsonl`` TO THIS BUNDLE.

observations.jsonl contains raw OCR from every screenshot, full iMessage
bodies, Gmail subject/sender/body snippets, window titles, and clipboard
contents. It's the single richest source of user PII in the whole
product and has no business leaving the user's machine in a support
artifact. If a future maintainer thinks they need it to diagnose
something: they don't. Reproduce the bug on a test account instead, or
ask the user to paste a specific row. The ``observations.jsonl`` file
is explicitly NOT referenced by this script for that reason.

Usage:

    ./venv/bin/python tools/deja_support_bundle.py
    ./venv/bin/python tools/deja_support_bundle.py --redact-emails
    ./venv/bin/python tools/deja_support_bundle.py --path ~/.deja \
        --out ~/Downloads
"""

from __future__ import annotations

import argparse
import io
import json
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


LOG_TAIL_LINES = 1000
JSONL_TAIL_ROWS = 500

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)


def _tail(path: Path, n: int) -> str:
    """Return the last ``n`` lines of ``path`` as a single string."""
    if not path.exists():
        return ""
    buf: deque[str] = deque(maxlen=n)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            buf.append(line)
    return "".join(buf)


def _redact_emails(text: str) -> str:
    return EMAIL_RE.sub("<email>", text)


def _machine_info() -> str:
    chunks: list[str] = []

    def _run(cmd: list[str]) -> str:
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception as e:  # pragma: no cover - best effort
            return f"(error running {' '.join(cmd)}: {e})"

    chunks.append("== sw_vers ==")
    chunks.append(_run(["sw_vers"]) or "(unavailable)")
    chunks.append("")
    chunks.append("== hw.model ==")
    chunks.append(_run(["sysctl", "-n", "hw.model"]) or "(unavailable)")
    chunks.append("")
    chunks.append("== Python ==")
    chunks.append(f"{platform.python_version()}  ({sys.executable})")
    chunks.append("")
    chunks.append("== Deja version ==")
    chunks.append(_deja_version())
    return "\n".join(chunks) + "\n"


def _deja_version() -> str:
    try:
        from importlib.metadata import version
        return version("deja")
    except Exception:
        pass
    try:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("version"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "unknown"


README = """\
Deja support bundle
===================

This zip is a narrow, privacy-aware snapshot of a Deja installation
intended for support triage. It contains:

  deja.log            last {log_lines} lines of the runtime log
  errors.jsonl        last {jsonl_rows} typed-error records
  audit.jsonl         last {jsonl_rows} agent-action audit records
  feature_flags.json  the user's active feature flags
  machine_info.txt    OS version, hardware model, Python + Deja versions

It DOES NOT contain:

  * observations.jsonl — raw OCR from screenshots, full iMessage
    bodies, email bodies, clipboard contents. Excluded by design.
  * ~/Deja/ wiki pages (personal notes about people and projects).
  * Screenshots, audio recordings, or OAuth tokens.

If --redact-emails was passed when this bundle was built, any
email-address-shaped tokens in the included files were replaced with
``<email>`` before archiving.

Generated: {ts}
""".format(
    log_lines=LOG_TAIL_LINES,
    jsonl_rows=JSONL_TAIL_ROWS,
    ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
)


# Hard block: anyone who adds ``observations.jsonl`` to this tuple is
# bypassing an intentional privacy guarantee. Don't.
EXCLUDED_FILENAMES = frozenset({"observations.jsonl"})


def build_bundle(
    *,
    base: Path,
    out_dir: Path,
    redact_emails: bool = False,
) -> Path:
    """Build the zip and return the final path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"deja-support-{ts}.zip"

    log_text = _tail(base / "deja.log", LOG_TAIL_LINES)
    errors_text = _tail(base / "errors.jsonl", JSONL_TAIL_ROWS)
    audit_text = _tail(base / "audit.jsonl", JSONL_TAIL_ROWS)
    ff_path = base / "feature_flags.json"
    ff_text = ff_path.read_text(encoding="utf-8") if ff_path.exists() else "{}\n"

    if redact_emails:
        log_text = _redact_emails(log_text)
        errors_text = _redact_emails(errors_text)
        audit_text = _redact_emails(audit_text)
        ff_text = _redact_emails(ff_text)

    machine_text = _machine_info()

    # Sanity check: ensure no excluded file sneaks in via copy-paste.
    members = {
        "deja.log": log_text,
        "errors.jsonl": errors_text,
        "audit.jsonl": audit_text,
        "feature_flags.json": ff_text,
        "machine_info.txt": machine_text,
        "README.txt": README,
    }
    for name in members:
        if name in EXCLUDED_FILENAMES:
            raise AssertionError(
                f"refusing to include {name} — see privacy comment"
            )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members.items():
            zf.writestr(name, text)

    return out_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build a privacy-safe Deja support zip."
    )
    ap.add_argument(
        "--path",
        default=str(Path.home() / ".deja"),
        help="Deja home directory (default: ~/.deja).",
    )
    ap.add_argument(
        "--out",
        default=str(Path.home() / "Downloads"),
        help="Directory to drop the zip into (default: ~/Downloads).",
    )
    ap.add_argument(
        "--redact-emails",
        action="store_true",
        help="Replace email-shaped tokens with <email> before archiving.",
    )
    args = ap.parse_args(argv)

    out = build_bundle(
        base=Path(args.path).expanduser(),
        out_dir=Path(args.out).expanduser(),
        redact_emails=args.redact_emails,
    )
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
