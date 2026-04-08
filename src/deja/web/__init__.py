"""deja.web — FastAPI backend for the Deja notch app.

Re-exports the ``app`` instance and ``run_web`` entry point so that
``from deja.web import app`` and ``from deja.web import run_web``
continue to work after the module-to-package conversion.
"""

from deja.web.app import app, create_app, run_web  # noqa: F401

__all__ = ["app", "create_app", "run_web"]
