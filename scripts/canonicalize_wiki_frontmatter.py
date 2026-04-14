"""One-time cleanup — canonicalize every wiki page's frontmatter.

Walks ~/Deja and rewrites any page whose frontmatter is corrupted
(all keys on one line between `---...---`) to proper multi-line YAML.
Safe to run repeatedly: files with already-clean frontmatter are
skipped with zero writes.

Usage:
    ./venv/bin/python scripts/canonicalize_wiki_frontmatter.py
    ./venv/bin/python scripts/canonicalize_wiki_frontmatter.py --dry-run

The repair logic lives in deja.wiki.canonicalize_frontmatter — this
script is just a file walker that invokes it. New writes go through
write_page() which auto-canonicalizes, so this script exists only to
clean up the backlog of pre-existing corrupted files.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Scan only, don't write")
    parser.add_argument("--wiki", default=None, help="Override wiki path (defaults to $DEJA_WIKI or ~/Deja)")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from deja.wiki import canonicalize_frontmatter

    wiki_dir = Path(args.wiki) if args.wiki else Path.home() / "Deja"
    if not wiki_dir.is_dir():
        print(f"error: wiki dir not found: {wiki_dir}", file=sys.stderr)
        return 1

    total = 0
    repaired = 0
    for md in wiki_dir.rglob("*.md"):
        if ".backups" in md.parts:
            continue
        try:
            content = md.read_text(encoding="utf-8")
        except Exception as e:
            print(f"skip (read error): {md} — {e}")
            continue
        total += 1
        new_content, was_repaired = canonicalize_frontmatter(content)
        if not was_repaired:
            continue
        repaired += 1
        rel = md.relative_to(wiki_dir)
        print(f"repair: {rel}")
        if not args.dry_run:
            md.write_text(new_content)

    verb = "would repair" if args.dry_run else "repaired"
    print(f"\n{verb} {repaired} of {total} files scanned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
