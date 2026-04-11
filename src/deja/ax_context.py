"""Lightweight macOS accessibility context for vision prompt grounding.

Reads a few attributes from the macOS Accessibility API — frontmost
app name, focused window title, focused UI element role + label +
value — and returns a dict of populated fields. Used by the vision
pipeline to give FastVLM an explicit anchor ("this is Superhuman, the
user is focused on a text field labeled 'Subject'") so the model
doesn't have to waste attention guessing app identity from pixels.

Why accessibility instead of OCR: the AX tree gives us *semantic*
info (role, label) that isn't in the pixels, and *textual* info
(window title, widget value) that vision has to OCR unreliably. For
a 0.5B model every free token of grounding is a win.

Empty-field discipline: every value in the returned dict is guaranteed
non-empty and non-whitespace. Callers can iterate the dict or call
``format_for_prompt()`` to render it and trust that there are no
``"App: "`` lines with nothing after the colon.

Permission: requires Accessibility access (already granted for the
typing-deferral and push-to-talk features). When the grant is
missing, AX calls return non-zero error codes and this module returns
an empty dict — graceful degradation, never raises.

Performance: 4-5 lightweight AX attribute reads, typically <10ms per
capture. Safe to call on every observation cycle / vision call.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy AX bindings
#
# pyobjc doesn't ship an ``ApplicationServices`` / ``HIServices`` wrapper in
# the Deja bundle, but we can load the AX C functions directly through
# ``objc.loadBundle`` + ``objc.loadBundleFunctions``. We do this once,
# lazily, on first use — so importing this module on a non-Darwin host or
# in a venv without pyobjc doesn't explode.
# ---------------------------------------------------------------------------

_binding_lock = threading.Lock()
_AXUIElementCreateApplication = None  # noqa: N816
_AXUIElementCopyAttributeValue = None  # noqa: N816
_NSWorkspace = None
_binding_failed = False


def _ensure_bindings() -> bool:
    """Load the AX functions on first use. Returns True iff they're ready.

    Subsequent calls are fast: set-once flags avoid re-entering the
    loadBundle machinery.
    """
    global _AXUIElementCreateApplication, _AXUIElementCopyAttributeValue
    global _NSWorkspace, _binding_failed

    if _AXUIElementCreateApplication is not None:
        return True
    if _binding_failed:
        return False

    with _binding_lock:
        if _AXUIElementCreateApplication is not None:
            return True
        if _binding_failed:
            return False

        try:
            import objc
            from AppKit import NSWorkspace

            hi_services_ns: dict = {}
            hi_services_bundle = objc.loadBundle(
                "HIServices",
                hi_services_ns,
                bundle_path=(
                    "/System/Library/Frameworks/ApplicationServices.framework"
                    "/Frameworks/HIServices.framework"
                ),
            )
            # Signatures use @ (generic id) for AXUIElementRef so pyobjc's
            # CF-as-id autorelease handling works on both fresh references
            # and returned sub-elements. Return type for
            # AXUIElementCopyAttributeValue is AXError (int32).
            #
            # loadBundleFunctions wants the NSBundle (from loadBundle's
            # return value), not the namespace dict — easy mix-up because
            # loadBundle takes the namespace as its second positional arg.
            objc.loadBundleFunctions(
                hi_services_bundle,
                hi_services_ns,
                [
                    ("AXUIElementCreateApplication", b"@i"),
                    ("AXUIElementCopyAttributeValue", b"i@@o^@"),
                ],
            )

            _AXUIElementCreateApplication = hi_services_ns[
                "AXUIElementCreateApplication"
            ]
            _AXUIElementCopyAttributeValue = hi_services_ns[
                "AXUIElementCopyAttributeValue"
            ]
            _NSWorkspace = NSWorkspace
            return True
        except Exception:
            log.debug("AX bindings unavailable", exc_info=True)
            _binding_failed = True
            return False


def _ax_copy(element, attribute: str) -> str | None:
    """Read one attribute and return it as a clean string or None.

    Returns None for any AX error, nil result, or all-whitespace value.
    Callers never see "" — the skip-if-empty discipline starts here.
    """
    try:
        err, value = _AXUIElementCopyAttributeValue(element, attribute, None)
    except Exception:
        return None
    if err != 0 or value is None:
        return None
    try:
        s = str(value).strip()
    except Exception:
        return None
    return s or None


def capture() -> dict:
    """Snapshot frontmost-app + focused-window + focused-element context.

    Returns a dict containing only populated fields. Possible keys:

      - ``app``: localized name of the frontmost application
      - ``window_title``: title of the focused window
      - ``focused_role``: human-readable role of the focused element
        ("text field", "button", "scroll area")
      - ``focused_label``: title or description of the focused element
      - ``focused_value``: current value of the focused element, truncated
        to keep the prompt small (text fields can contain entire emails)

    Missing fields are omitted entirely. An empty dict means either no
    AX access, no frontmost app, or the frontmost app has broken AX
    support (common in Electron). Callers should handle the empty case
    by skipping the grounding block, not by injecting empty values.
    """
    if not _ensure_bindings():
        return {}

    result: dict = {}

    try:
        app = _NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return {}

        name = app.localizedName()
        if name:
            s = str(name).strip()
            if s:
                result["app"] = s

        pid = app.processIdentifier()
        if pid <= 0:
            return result

        ax_app = _AXUIElementCreateApplication(pid)

        # Focused window + its title
        err, window = _AXUIElementCopyAttributeValue(
            ax_app, "AXFocusedWindow", None
        )
        if err == 0 and window is not None:
            title = _ax_copy(window, "AXTitle")
            if title:
                result["window_title"] = title[:200]

        # Focused UI element + role + label + value
        err, element = _AXUIElementCopyAttributeValue(
            ax_app, "AXFocusedUIElement", None
        )
        if err == 0 and element is not None:
            role = _ax_copy(element, "AXRoleDescription") or _ax_copy(
                element, "AXRole"
            )
            if role:
                result["focused_role"] = role[:60]

            # Prefer AXTitle, fall back to AXDescription, then AXHelp.
            label = (
                _ax_copy(element, "AXTitle")
                or _ax_copy(element, "AXDescription")
                or _ax_copy(element, "AXHelp")
            )
            if label:
                result["focused_label"] = label[:120]

            value = _ax_copy(element, "AXValue")
            if value:
                # Cap value length — text fields can be huge (full email
                # drafts, chat messages in progress). 200 chars is enough
                # context for FastVLM to know what the user is typing
                # without blowing the prompt budget.
                result["focused_value"] = value[:200]
    except Exception:
        log.debug("AX context capture failed", exc_info=True)

    return result


def format_for_prompt(ctx: dict) -> str:
    """Render an AX context dict as a prompt block, or empty string.

    Returns ``""`` when ``ctx`` is empty or has no renderable fields, so
    callers can concatenate the result directly into a template without
    leaving a trailing "# Current UI context" header with nothing under it.

    When populated, returns a two-section block ending in two newlines so
    the next section in the template starts cleanly:

        # Current UI context

        App: Superhuman
        Window: Inbox — david@davidwurtz.com
        Focused: text field "Subject"

    """
    if not ctx:
        return ""

    lines: list[str] = []
    if app := ctx.get("app"):
        lines.append(f"App: {app}")
    if title := ctx.get("window_title"):
        lines.append(f"Window: {title}")

    # Build a single "Focused:" line from whatever focused_* fields exist.
    focused_parts: list[str] = []
    if role := ctx.get("focused_role"):
        focused_parts.append(role)
    if label := ctx.get("focused_label"):
        focused_parts.append(f'"{label}"')
    if value := ctx.get("focused_value"):
        focused_parts.append(f"(value: {value!r})")
    if focused_parts:
        lines.append("Focused: " + " ".join(focused_parts))

    if not lines:
        return ""

    body = "\n".join(lines)
    return f"# Current UI context\n\n{body}\n\n"
