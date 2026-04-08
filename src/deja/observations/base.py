"""Base class for observation collectors.

Every signal source (iMessage, WhatsApp, email, calendar, etc.) implements
a collector that inherits from ``BaseObserver``.  The ``Observer``
orchestrator in ``collector.py`` iterates over registered observers
instead of hard-coding each source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from deja.observations.types import Observation


class BaseObserver(ABC):
    """Base class for all observation collectors."""

    @abstractmethod
    def collect(self) -> list[Observation]:
        """Collect new observations. Returns list of Observation objects."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name for logging (e.g. 'iMessage', 'Email')."""
        ...

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
