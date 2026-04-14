"""Support-side bundle zip tool.

Locks in two things that matter for user trust:

  * ``observations.jsonl`` never ends up inside the zip.
  * ``--redact-emails`` actually replaces email-shaped tokens in every
    included file.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import deja_support_bundle as bundle  # noqa: E402


def _seed(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "deja.log").write_text(
        "2026-04-11 12:00:00 INFO hello ops@example.com\n"
        "2026-04-11 12:00:01 INFO goodbye\n",
        encoding="utf-8",
    )
    (home / "errors.jsonl").write_text(
        json.dumps({
            "code": "auth_failed",
            "message": "contact sam@acme.io",
            "timestamp": "2026-04-11T12:00:01Z",
            "details": {},
        }) + "\n",
        encoding="utf-8",
    )
    (home / "audit.jsonl").write_text(
        json.dumps({
            "ts": "2026-04-11T12:00:02Z",
            "cycle": "c_abc",
            "trigger": {"kind": "signal"},
            "action": "wiki_write",
            "target": "people/foo",
            "reason": "foo",
        }) + "\n",
        encoding="utf-8",
    )
    (home / "feature_flags.json").write_text(
        '{"integrate_shadow_eval": true}\n', encoding="utf-8"
    )
    # THIS FILE MUST NEVER END UP IN THE BUNDLE.
    (home / "observations.jsonl").write_text(
        json.dumps({
            "secret_email": "leak@example.com",
            "ocr": "very private contents",
        }) + "\n",
        encoding="utf-8",
    )


def _zip_members(zpath: Path) -> list[str]:
    with zipfile.ZipFile(zpath) as zf:
        return zf.namelist()


def _zip_read(zpath: Path, name: str) -> str:
    with zipfile.ZipFile(zpath) as zf:
        return zf.read(name).decode("utf-8")


def test_bundle_contains_expected_files(isolated_home, tmp_path):
    home, _ = isolated_home
    _seed(home)

    out = bundle.build_bundle(base=home, out_dir=tmp_path)
    assert out.exists()
    members = set(_zip_members(out))
    expected = {
        "deja.log",
        "errors.jsonl",
        "audit.jsonl",
        "feature_flags.json",
        "machine_info.txt",
        "README.txt",
    }
    assert expected.issubset(members)


def test_bundle_never_includes_observations(isolated_home, tmp_path):
    home, _ = isolated_home
    _seed(home)

    out = bundle.build_bundle(base=home, out_dir=tmp_path)
    members = _zip_members(out)
    assert "observations.jsonl" not in members
    # Belt-and-braces: make sure no file in the bundle contains the
    # private OCR string we planted.
    for name in members:
        contents = _zip_read(out, name)
        assert "very private contents" not in contents
        assert "leak@example.com" not in contents


def test_redact_emails_flag(isolated_home, tmp_path):
    home, _ = isolated_home
    _seed(home)

    out = bundle.build_bundle(base=home, out_dir=tmp_path, redact_emails=True)
    log_text = _zip_read(out, "deja.log")
    err_text = _zip_read(out, "errors.jsonl")
    assert "ops@example.com" not in log_text
    assert "sam@acme.io" not in err_text
    assert "<email>" in log_text
    assert "<email>" in err_text


def test_redact_emails_off_by_default(isolated_home, tmp_path):
    home, _ = isolated_home
    _seed(home)

    out = bundle.build_bundle(base=home, out_dir=tmp_path)
    log_text = _zip_read(out, "deja.log")
    assert "ops@example.com" in log_text


def test_readme_present_and_mentions_observations(isolated_home, tmp_path):
    home, _ = isolated_home
    _seed(home)

    out = bundle.build_bundle(base=home, out_dir=tmp_path)
    readme = _zip_read(out, "README.txt")
    # Privacy posture is stated up front.
    assert "observations.jsonl" in readme
