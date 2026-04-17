"""One-shot cleanup: strip retired frontmatter fields + fix doubled blocks.

Scans every ``~/Deja/people/*.md`` and ``~/Deja/projects/*.md`` and:

1. Drops the retired keys ``company``, ``domains``, ``keywords`` from
   the frontmatter. Nothing reads these fields; they survived only
   because the integrate prompt's preserve-list kept naming them.

2. Repairs pages where a second, malformed ``---keywords: []\\n---``
   block got prepended to the body by a past integrate write. The
   second block parses as body text in Obsidian and clutters reading.

Dry-run by default — pass ``--apply`` to write. A backup of every
modified file is saved next to the original as ``.bak-frontmatter``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required — run via `uv run`", file=sys.stderr)
    sys.exit(1)


RETIRED_KEYS = {"company", "domains", "keywords"}
WIKI_DIR = Path.home() / "Deja"

# Matches a well-formed leading YAML block (possibly empty).
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Matches a malformed doubled block like "---keywords: []\n---" or
# "---\nkeywords: []\n---" that some past writes produced AFTER the
# real frontmatter closer. Captures the whole malformed span so we can
# strip it verbatim.
_DOUBLED_RE = re.compile(
    r"\A(---\s*\n.*?\n---\s*\n)"  # group 1: the real FM block
    r"(---[^\n]*\n(?:.*?\n)?---\s*\n)",  # group 2: the malformed second block
    re.DOTALL,
)


def _clean_one(path: Path) -> tuple[bool, list[str]]:
    """Return (changed, reasons). Does NOT write — caller applies."""
    original = path.read_text(encoding="utf-8")
    text = original
    reasons: list[str] = []

    # 1. Strip the malformed doubled block first, if present.
    m = _DOUBLED_RE.match(text)
    if m:
        text = m.group(1) + text[m.end() :]
        reasons.append("removed doubled frontmatter block")

    # 2. Rewrite the real YAML block, dropping retired keys.
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        raw_yaml = fm_match.group(1)
        try:
            data = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError:
            # Unparseable — leave alone, flag it.
            reasons.append("YAML parse error — not touched")
            return (bool(reasons), reasons)

        if not isinstance(data, dict):
            # Non-dict frontmatter (unexpected) — skip.
            reasons.append("non-dict frontmatter — not touched")
            return (bool(reasons), reasons)

        dropped = [k for k in RETIRED_KEYS if k in data]
        if dropped:
            for k in dropped:
                del data[k]
            reasons.append(f"dropped keys: {', '.join(dropped)}")

            if data:
                new_yaml = yaml.safe_dump(
                    data, default_flow_style=False, sort_keys=False
                ).rstrip()
                new_block = f"---\n{new_yaml}\n---\n"
            else:
                # All keys removed — page has no useful frontmatter.
                # Emit an empty ``---\n---`` block rather than dropping
                # the delimiters entirely, so Obsidian keeps treating
                # this as a page with frontmatter (even if empty).
                new_block = "---\n---\n"
            text = new_block + text[fm_match.end() :]

    changed = text != original
    if changed and "_changed_written" not in reasons:
        reasons.append("file rewritten")
    return (changed, reasons)


def main(apply: bool) -> None:
    targets: list[Path] = []
    for sub in ("people", "projects"):
        d = WIKI_DIR / sub
        if d.is_dir():
            targets.extend(sorted(d.glob("*.md")))

    print(f"Scanning {len(targets)} pages ({'APPLY' if apply else 'DRY-RUN'})")
    print()

    to_change: list[tuple[Path, list[str]]] = []
    for p in targets:
        try:
            changed, reasons = _clean_one(p)
        except Exception as e:
            print(f"  ERROR {p.name}: {e}", file=sys.stderr)
            continue
        if changed:
            to_change.append((p, reasons))

    if not to_change:
        print("Nothing to clean. Every page is already tidy.")
        return

    for p, reasons in to_change:
        rel = p.relative_to(WIKI_DIR)
        print(f"  {rel}")
        for r in reasons:
            print(f"    • {r}")

    print()
    print(f"{len(to_change)} files would change.")

    if not apply:
        print("Dry run. Re-run with --apply to write.")
        return

    # Apply — with .bak-frontmatter backups.
    for p, _ in to_change:
        backup = p.with_suffix(p.suffix + ".bak-frontmatter")
        backup.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        # Re-clean (idempotent) and write.
        _, _reasons = _clean_one(p)
        # Need to actually rewrite — _clean_one is pure. Redo the work
        # for write.
        original = p.read_text(encoding="utf-8")
        text = original
        m = _DOUBLED_RE.match(text)
        if m:
            text = m.group(1) + text[m.end() :]
        fm_match = _FRONTMATTER_RE.match(text)
        if fm_match:
            try:
                data = yaml.safe_load(fm_match.group(1)) or {}
            except yaml.YAMLError:
                continue
            if isinstance(data, dict):
                for k in RETIRED_KEYS:
                    data.pop(k, None)
                if data:
                    new_yaml = yaml.safe_dump(
                        data, default_flow_style=False, sort_keys=False
                    ).rstrip()
                    new_block = f"---\n{new_yaml}\n---\n"
                else:
                    new_block = "---\n---\n"
                text = new_block + text[fm_match.end() :]
        p.write_text(text, encoding="utf-8")

    print(f"Wrote {len(to_change)} files. Backups at *.bak-frontmatter.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Write changes (default dry-run).")
    args = ap.parse_args()
    main(args.apply)
