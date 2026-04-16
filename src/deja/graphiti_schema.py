"""Entity + edge types for Deja's Graphiti knowledge graph.

Copied from prototypes/graphiti/schema.py — production code must not
import from prototypes/.  Keep in sync manually; once the prototype is
retired this becomes the single source of truth.

See schema.py docstring for design rationale.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Entity types
# --------------------------------------------------------------------------


class Person(BaseModel):
    """A real human. Maps to ~/Deja/people/*.md."""

    role: str | None = Field(
        default=None,
        description="Current job title or primary role.",
    )
    company: str | None = Field(
        default=None,
        description="Current employer name.",
    )
    emails: list[str] = Field(
        default_factory=list,
        description="Known email addresses, lowercased.",
    )
    phones: list[str] = Field(
        default_factory=list,
        description="Phone numbers in E.164 form when possible.",
    )
    relationship_to_user: str | None = Field(
        default=None,
        description="How this person relates to the Deja user.",
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Other names this person is known by.",
    )
    context: str | None = Field(
        default=None,
        description="One-line disambiguation of who this person is relative to the user.",
    )


class Project(BaseModel):
    """An ongoing initiative, goal, life thread, or situation."""

    status: Literal["active", "paused", "closed"] = Field(
        default="active",
        description="Lifecycle state.",
    )
    domain: str | None = Field(
        default=None,
        description="Loose category — 'home', 'work', 'health', 'family', 'finance', etc.",
    )


class Event(BaseModel):
    """Something that happened at a specific time."""

    date: str | None = Field(default=None, description="ISO date YYYY-MM-DD.")
    time: str | None = Field(default=None, description="24-hour local time HH:MM if known.")
    participants: list[str] = Field(
        default_factory=list,
        description="Slugs or names of the people involved.",
    )


class Organization(BaseModel):
    """A company, school, vendor, agency, or other formal entity."""

    kind: str | None = Field(default=None, description="Type of organization.")
    domain: str | None = Field(default=None, description="Primary website / email domain.")


class Task(BaseModel):
    """Something the user committed to do."""

    due_date: str | None = Field(default=None, description="ISO date the task is due.")
    priority: Literal["low", "medium", "high"] | None = Field(default=None)
    status: Literal["open", "done", "archived"] = Field(default="open")


class WaitingFor(BaseModel):
    """Something someone else owes the user."""

    requested_at: str | None = Field(default=None)
    expected_by: str | None = Field(default=None)
    last_nudge_at: str | None = Field(default=None)
    nudge_count: int = Field(default=0)
    status: Literal["open", "delivered", "abandoned"] = Field(default="open")


class Application(BaseModel):
    """A software application, tool, service, or website."""

    kind: str | None = Field(default=None)
    vendor: str | None = Field(default=None)


class Document(BaseModel):
    """A specific document, file, article, paper, or message thread."""

    kind: str | None = Field(default=None)
    source: str | None = Field(default=None)


class Location(BaseModel):
    """A physical place."""

    address: str | None = Field(default=None)
    city: str | None = Field(default=None)
    region: str | None = Field(default=None)
    kind: str | None = Field(default=None)


class Topic(BaseModel):
    """A subject, theme, or domain of interest."""

    domain: str | None = Field(default=None)


# --------------------------------------------------------------------------
# Edge types
# --------------------------------------------------------------------------


class WorksAt(BaseModel):
    start_date: str | None = Field(default=None)
    end_date: str | None = Field(default=None)
    role: str | None = Field(default=None)


class SpouseOf(BaseModel):
    pass


class ParentOf(BaseModel):
    pass


class Attends(BaseModel):
    pass


class InvolvedIn(BaseModel):
    role: str | None = Field(default=None)


class CommittedTo(BaseModel):
    made_at: str | None = Field(default=None)


class Owes(BaseModel):
    promised_at: str | None = Field(default=None)
    promise_text: str | None = Field(default=None)


class MentionedBy(BaseModel):
    pass


# --------------------------------------------------------------------------
# Exported bundles
# --------------------------------------------------------------------------

ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Project": Project,
    "Event": Event,
    "Organization": Organization,
    "Task": Task,
    "WaitingFor": WaitingFor,
    "Application": Application,
    "Document": Document,
    "Location": Location,
    "Topic": Topic,
}

EDGE_TYPES: dict[str, type[BaseModel]] = {
    "WORKS_AT": WorksAt,
    "SPOUSE_OF": SpouseOf,
    "PARENT_OF": ParentOf,
    "ATTENDS": Attends,
    "INVOLVED_IN": InvolvedIn,
    "COMMITTED_TO": CommittedTo,
    "OWES": Owes,
    "MENTIONED_BY": MentionedBy,
}

EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Person", "Organization"): ["WORKS_AT"],
    ("Person", "Person"): ["SPOUSE_OF", "PARENT_OF"],
    ("Person", "Event"): ["ATTENDS", "MENTIONED_BY"],
    ("Person", "Project"): ["INVOLVED_IN"],
    ("Person", "Task"): ["COMMITTED_TO"],
    ("Person", "WaitingFor"): ["OWES"],
    ("Project", "Event"): ["MENTIONED_BY"],
    ("Organization", "Event"): ["MENTIONED_BY"],
    ("Task", "Event"): ["MENTIONED_BY"],
    ("WaitingFor", "Event"): ["MENTIONED_BY"],
}
