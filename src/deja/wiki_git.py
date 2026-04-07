"""Git-backed version history for the Déjà wiki.

The wiki lives at ~/Lighthouse/ and is a plain directory of
markdown files. Inspired by Karpathy's observation that a wiki is just a git
repo of markdown — this module initializes a git repository inside the wiki
directory and provides a best-effort helper to auto-commit changes after
every wiki edit, replacing the older .backups/ timestamped-copy scheme.

All functions are best-effort: they log failures via the standard logging
module and never raise. External callers only need ensure_repo(),
commit_changes(), and current_head().
"""

from __future__ import annotations
from deja.config import WIKI_DIR

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)



_GITIGNORE_CONTENTS = """.backups/
.obsidian/workspace*
"""

_GIT_USER_NAME = "Déjà"
_GIT_USER_EMAIL = "deja@localhost"


def _run_git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run a git command inside WIKI_DIR, capturing stdout/stderr."""
    return subprocess.run(
        ["git", *args],
        cwd=str(WIKI_DIR),
        capture_output=True,
        text=True,
        shell=False,
        check=check,
    )


def _is_repo() -> bool:
    """Return True if WIKI_DIR is already a git repository."""
    if not WIKI_DIR.exists():
        return False
    result = _run_git("rev-parse", "--git-dir")
    return result.returncode == 0


def _ensure_identity() -> None:
    """Set a local user.name/user.email if not already configured."""
    name = _run_git("config", "--local", "user.name")
    if name.returncode != 0 or not name.stdout.strip():
        res = _run_git("config", "--local", "user.name", _GIT_USER_NAME)
        if res.returncode != 0:
            logger.warning("wiki_git: failed to set user.name: %s", res.stderr.strip())
    email = _run_git("config", "--local", "user.email")
    if email.returncode != 0 or not email.stdout.strip():
        res = _run_git("config", "--local", "user.email", _GIT_USER_EMAIL)
        if res.returncode != 0:
            logger.warning("wiki_git: failed to set user.email: %s", res.stderr.strip())


def ensure_repo() -> bool:
    """Initialize a git repo in the wiki directory if one doesn't exist.

    Creates:
      - git init (with a sensible default branch name, e.g. 'main')
      - .gitignore containing '.backups/' and '.obsidian/workspace*'
      - An initial commit with any existing files if this is the first run
    Returns True if the repo is now usable, False on failure.
    Safe to call repeatedly — no-op if already initialized.
    """
    try:
        if not WIKI_DIR.exists():
            WIKI_DIR.mkdir(parents=True, exist_ok=True)

        if _is_repo():
            _ensure_identity()
            return True

        init = _run_git("init", "-b", "main")
        if init.returncode != 0:
            logger.warning("wiki_git: git init failed: %s", init.stderr.strip())
            return False

        _ensure_identity()

        gitignore_path = WIKI_DIR / ".gitignore"
        try:
            if not gitignore_path.exists():
                gitignore_path.write_text(_GITIGNORE_CONTENTS)
        except OSError as exc:
            logger.warning("wiki_git: failed to write .gitignore: %s", exc)

        add = _run_git("add", "-A")
        if add.returncode != 0:
            logger.warning("wiki_git: initial git add failed: %s", add.stderr.strip())
            return False

        status = _run_git("status", "--porcelain")
        if status.returncode != 0:
            logger.warning(
                "wiki_git: git status after init failed: %s", status.stderr.strip()
            )
            return False

        if status.stdout.strip():
            commit = _run_git("commit", "-m", "initial wiki snapshot")
        else:
            commit = _run_git(
                "commit", "--allow-empty", "-m", "initial wiki snapshot"
            )

        if commit.returncode != 0:
            logger.warning(
                "wiki_git: initial commit failed: %s", commit.stderr.strip()
            )
            return False

        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("wiki_git: ensure_repo raised: %s", exc)
        return False


def commit_changes(message: str) -> bool:
    """Stage all changes in the wiki and create a commit with the given message.

    If there are no changes, do nothing (no empty commits).
    Returns True if a commit was made, False otherwise.
    Best-effort: swallows errors, logs them, and never raises.
    """
    try:
        if not _is_repo():
            logger.warning("wiki_git: commit_changes called but wiki is not a repo")
            return False

        _ensure_identity()

        add = _run_git("add", "-A")
        if add.returncode != 0:
            logger.warning("wiki_git: git add failed: %s", add.stderr.strip())
            return False

        status = _run_git("status", "--porcelain")
        if status.returncode != 0:
            logger.warning("wiki_git: git status failed: %s", status.stderr.strip())
            return False

        if not status.stdout.strip():
            return False

        commit = _run_git("commit", "-m", message)
        if commit.returncode != 0:
            logger.warning("wiki_git: git commit failed: %s", commit.stderr.strip())
            return False

        return True
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("wiki_git: commit_changes raised: %s", exc)
        return False


def current_head() -> str:
    """Return the short hash of HEAD, or empty string if not a repo."""
    try:
        if not _is_repo():
            return ""
        result = _run_git("rev-parse", "--short", "HEAD")
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("wiki_git: current_head raised: %s", exc)
        return ""
