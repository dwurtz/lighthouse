"""Setup/onboarding routes — re-exports the router from deja.setup_api."""

from deja.setup_api import router  # noqa: F401

__all__ = ["router"]
