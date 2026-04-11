"""On-device FastVLM weight management for the vision pipeline.

Owns:
  - Resolution of the FastVLM snapshot cache directory
  - Apple Silicon + disk space pre-flight checks
  - Download progress for the setup panel
  - The actual download driver that fetches FastVLM 0.5B weights
    via ``huggingface_hub.snapshot_download``

``vision_local.py`` calls ``fastvlm_path()`` to get the local snapshot
directory and passes it to ``mlx_vlm.load()``. The setup-panel API at
``/api/setup/model-status`` and ``/api/setup/download-model`` reads
the shared progress dict here.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import sys
import threading
import time
from pathlib import Path

from deja.config import DEJA_HOME

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache directory
#
# Weights live OUTSIDE ~/.deja/ in macOS's canonical Application Support
# directory. Rationale:
#   - Survives `rm -rf ~/.deja` during a fresh-install test (saves 15-20 min
#     + 1.4 GB of bandwidth per loop).
#   - Standard Apple convention for app-owned persistent data.
#   - A real uninstall keeps weights by default unless the user opts in.
#
# Override with $DEJA_MODELS_DIR for tests.
# ---------------------------------------------------------------------------


def _resolve_models_dir() -> Path:
    env = os.environ.get("DEJA_MODELS_DIR")
    if env:
        return Path(env).expanduser()
    return (
        Path.home() / "Library" / "Application Support" / "com.deja.app" / "models"
    )


MODELS_DIR = _resolve_models_dir()

# FastVLM 0.5B (Apple) — 10 files, ~1.4 GB total
VISION_REPO = "apple/FastVLM-0.5B"
VISION_SIZE_MB = 1400


def fastvlm_path() -> Path | None:
    """Return the local snapshot directory for FastVLM 0.5B.

    Returns None if the model hasn't been downloaded yet. The path
    points at a HuggingFace snapshot directory (with symlinks into
    the blob store) suitable for ``mlx_vlm.load(str(path))``.
    """
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None

    # config.json must exist for the model to load at all
    cached = try_to_load_from_cache(
        repo_id=VISION_REPO,
        filename="config.json",
        cache_dir=str(MODELS_DIR),
    )
    if cached is None or not isinstance(cached, str):
        return None
    return Path(cached).parent


def is_vision_downloaded() -> bool:
    return fastvlm_path() is not None


def is_all_downloaded() -> bool:
    return is_vision_downloaded()


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

MIN_FREE_GB = 4  # need ~1.4 GB for weights + headroom


def check_platform() -> tuple[bool, str]:
    """Verify Apple Silicon. Returns (ok, error_message)."""
    if sys.platform != "darwin":
        return False, "On-device AI requires macOS"
    if platform.machine() != "arm64":
        return False, "On-device AI requires Apple Silicon (M1/M2/M3/M4)"
    return True, ""


def check_disk_space() -> tuple[bool, str]:
    """Verify enough free disk for the weights. Returns (ok, error_message)."""
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(MODELS_DIR).free
        free_gb = free / (1024 ** 3)
        if free_gb < MIN_FREE_GB:
            return (
                False,
                f"Not enough disk space — need {MIN_FREE_GB} GB free, have {free_gb:.1f} GB",
            )
        return True, ""
    except Exception as e:
        return False, f"Could not check disk space: {e}"


# ---------------------------------------------------------------------------
# Shared download progress (single source of truth for the setup panel)
# ---------------------------------------------------------------------------

_TOTAL_BYTES = VISION_SIZE_MB * 1024 * 1024

_progress_lock = threading.Lock()
_download_progress: dict = {
    "status": "idle",          # idle, downloading, ready, error
    "progress": 0.0,           # 0.0 to 1.0
    "message": "",
    "current_file": "",
    "phase": "idle",           # idle, vision, done
    "bytes_downloaded": 0,
    "bytes_total": _TOTAL_BYTES,
    "total_size_mb": VISION_SIZE_MB,
    "vision_ready": False,
}


def get_status() -> dict:
    """Snapshot of the current download/load status."""
    with _progress_lock:
        snap = dict(_download_progress)
    snap["vision_ready"] = is_vision_downloaded()
    # Back-compat alias for the Swift setup panel (previously checked
    # `binaries_present` to mean "llama.cpp binaries bundled"; now it
    # just means "runtime available", which is always true when we
    # reach this code path since mlx-vlm ships with the app).
    snap["binaries_present"] = True
    if snap["vision_ready"]:
        if snap["status"] != "downloading":
            snap["status"] = "ready"
            snap["progress"] = 1.0
            snap["phase"] = "done"
            snap["bytes_downloaded"] = snap.get("bytes_total", _TOTAL_BYTES)
            if not snap.get("message"):
                snap["message"] = "Model ready"
    return snap


def _set(**kwargs) -> None:
    with _progress_lock:
        _download_progress.update(kwargs)


# ---------------------------------------------------------------------------
# Download driver
# ---------------------------------------------------------------------------

_RETRY_WAITS = (5, 15, 45)  # seconds between attempts; len = max attempts


def _make_progress_tqdm():
    """tqdm subclass that streams per-byte progress into our dict.

    snapshot_download creates one tqdm per file plus one overall bar.
    We track the overall bar by summing all per-instance .n values
    through a shared class-level counter.
    """
    import tqdm as _tqdm_mod

    shared = {"total_bytes": 0, "last_write": 0.0, "last_frac": -1.0}

    class _ProgressTqdm(_tqdm_mod.tqdm):
        _prev_n = 0

        def update(self, n=1):
            ret = super().update(n)
            delta = int(self.n or 0) - self._prev_n
            self._prev_n = int(self.n or 0)
            if delta > 0:
                shared["total_bytes"] += delta
            now = time.monotonic()
            frac = shared["total_bytes"] / _TOTAL_BYTES if _TOTAL_BYTES else 0.0
            if (
                now - shared["last_write"] >= 0.1
                or abs(frac - shared["last_frac"]) >= 0.01
                or shared["total_bytes"] >= _TOTAL_BYTES
            ):
                shared["last_write"] = now
                shared["last_frac"] = frac
                clamped = max(0.0, min(1.0, frac))
                with _progress_lock:
                    _download_progress["bytes_downloaded"] = shared["total_bytes"]
                    _download_progress["progress"] = clamped
            return ret

    return _ProgressTqdm


def download_all() -> bool:
    """Download FastVLM 0.5B weights.

    Updates ``_download_progress`` so the setup panel can show progress.
    Returns True on success, False on error.
    """
    ok, err = check_platform()
    if not ok:
        _set(status="error", message=err)
        return False

    ok, err = check_disk_space()
    if not ok:
        _set(status="error", message=err)
        return False

    if is_all_downloaded():
        _set(
            status="ready",
            progress=1.0,
            phase="done",
            message="Model ready",
            bytes_downloaded=_TOTAL_BYTES,
        )
        return True

    _set(
        status="downloading",
        progress=0.0,
        message="Downloading FastVLM 0.5B (~1.4 GB)...",
        bytes_downloaded=0,
        phase="vision",
    )

    try:
        from huggingface_hub import snapshot_download
        from huggingface_hub import errors as hf_errors
    except ImportError:
        _set(status="error", message="huggingface_hub not installed")
        return False

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"

    tqdm_cls = _make_progress_tqdm()

    last_error: Exception | None = None
    max_attempts = len(_RETRY_WAITS)

    for attempt in range(1, max_attempts + 1):
        try:
            snapshot_download(
                repo_id=VISION_REPO,
                cache_dir=str(MODELS_DIR),
                tqdm_class=tqdm_cls,
            )
            break
        except (hf_errors.RepositoryNotFoundError,
                hf_errors.EntryNotFoundError,
                hf_errors.RevisionNotFoundError,
                PermissionError) as e:
            log.error("Permanent failure downloading FastVLM: %s", e)
            _set(status="error", message=f"Failed to download FastVLM: {str(e)[:120]}")
            return False
        except ImportError as e:
            if "hf_transfer" in str(e).lower():
                log.warning("hf_transfer unavailable; falling back to stdlib")
                os.environ.pop("HF_HUB_ENABLE_HF_TRANSFER", None)
                continue
            log.exception("ImportError during FastVLM download")
            _set(status="error", message=f"Failed to download FastVLM: {str(e)[:120]}")
            return False
        except Exception as e:
            last_error = e
            if attempt >= max_attempts:
                break
            wait = _RETRY_WAITS[attempt - 1]
            log.warning(
                "FastVLM download failed (attempt %d/%d): %s — retrying in %ds",
                attempt, max_attempts, e, wait,
            )
            _set(message=f"Network hiccup — retrying in {wait}s (attempt {attempt}/{max_attempts})")
            time.sleep(wait)
    else:
        log.error("Giving up on FastVLM after %d attempts: %s", max_attempts, last_error)
        _set(
            status="error",
            message=f"Failed to download FastVLM after {max_attempts} attempts: {str(last_error)[:120]}",
        )
        return False

    if not is_vision_downloaded():
        _set(status="error", message="Download completed but FastVLM files missing")
        return False

    _set(
        status="ready",
        progress=1.0,
        phase="done",
        message="Model ready",
        current_file="",
        bytes_downloaded=_TOTAL_BYTES,
    )
    log.info("FastVLM 0.5B downloaded successfully")
    return True


def download_all_async() -> threading.Thread:
    """Kick off ``download_all`` in a background daemon thread."""
    t = threading.Thread(target=download_all, daemon=True)
    t.start()
    return t
