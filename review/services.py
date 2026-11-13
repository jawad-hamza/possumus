"""Rules for review notes: when the tool is on, who may do what, and what a valid note looks like."""
import math
from urllib.parse import parse_qsl, urlencode

from django.conf import settings

from pos.services.access import is_admin

from .models import ReviewSwitch

TEXT_MAX = 2000
AUTHOR_MAX = 80
PATH_MAX = 500
QUERY_MAX = 500
LABEL_MAX = 200
BODY_MAX_BYTES = 16 * 1024

# Fire-and-forget budgets per signed-in user (pos.services.throttling counts in the cache).
CREATE_LIMIT = (30, 10 * 60)
RESOLVE_LIMIT = (120, 10 * 60)
READ_LIMIT = (600, 10 * 60)


def server_default_enabled():
    return bool(getattr(settings, "REVIEW_NOTES_ENABLED", False))


def tool_state():
    """(enabled, mode): the owner's switch wins; "server" follows the REVIEW_NOTES_ENABLED setting."""
    mode = ReviewSwitch.current_mode()
    if mode == ReviewSwitch.MODE_ON:
        return True, mode
    if mode == ReviewSwitch.MODE_OFF:
        return False, mode
    return server_default_enabled(), mode


def tool_enabled():
    return tool_state()[0]


def can_manage_notes(user):
    """The review page, the switch and resolving anyone's note: admins and the owner."""
    return is_admin(user)


def can_resolve(user, note):
    if not getattr(user, "is_authenticated", False):
        return False
    return can_manage_notes(user) or (note.user_id is not None and note.user_id == user.id)


def display_name(user):
    if not getattr(user, "is_authenticated", False):
        return ""
    return (user.get_full_name() or user.get_username() or "").strip()[:AUTHOR_MAX]


def _clean(value, limit):
    return " ".join(str(value if value is not None else "").split())[:limit]


def normalize_query(query):
    """The same filters in any order are the same screen."""
    query = str(query or "").strip().lstrip("?")
    if not query:
        return ""
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    return urlencode(pairs)[:QUERY_MAX]


def clean_path(path):
    path = str(path or "").strip()
    if not path.startswith("/") or path.startswith("//") or len(path) > PATH_MAX:
        return None
    if any(ch in path for ch in "?#\\") or any(ord(ch) < 32 for ch in path):
        return None
    return path


def _number(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(low, min(high, number))


def clean_anchor(raw):
    if not isinstance(raw, dict):
        return None
    tag = _clean(raw.get("tag"), 30).lower()
    if tag and not all(ch.isalnum() or ch == "-" for ch in tag):
        tag = ""
    scope = raw.get("scope") if raw.get("scope") in {"content", "viewport"} else "viewport"
    return {
        "selector": str(raw.get("selector") or "")[:500],
        "tag": tag,
        "hint": _clean(raw.get("hint"), 200),
        "xFrac": _number(raw.get("xFrac"), 0, 1, 0.5),
        "yFrac": _number(raw.get("yFrac"), 0, 1, 0.5),
        "pageX": _number(raw.get("pageX"), -1_000_000, 1_000_000, 0),
        "pageY": _number(raw.get("pageY"), -1_000_000, 1_000_000, 0),
        "scope": scope,
    }


def validate_note(payload, user):
    """Return (fields, error). fields are ready for ReviewNote(**fields)."""
    if not isinstance(payload, dict):
        return None, "Invalid note."
    text = str(payload.get("text") or "").strip()
    if not text:
        return None, "Please type a note."
    if len(text) > TEXT_MAX:
        return None, f"Keep the note under {TEXT_MAX} characters."
    author = _clean(payload.get("author"), AUTHOR_MAX) or display_name(user)
    if not author:
        return None, "Please enter your name."
    path = clean_path(payload.get("path"))
    if path is None:
        return None, "The screen could not be recognised."
    anchor = clean_anchor(payload.get("anchor"))
    if anchor is None:
        return None, "The spot on the screen could not be recognised."
    return {
        "author_name": author,
        "text": text,
        "page_path": path,
        "page_query": normalize_query(payload.get("query")),
        "screen_label": _clean(payload.get("screen"), LABEL_MAX),
        "anchor": anchor,
    }, None


def serialize_note(note, user=None, manage=None):
    """manage: pass can_manage_notes(user) when serialising many notes, to ask it only once."""
    if user is None or not getattr(user, "is_authenticated", False):
        allowed = False
    else:
        if manage is None:
            manage = can_manage_notes(user)
        allowed = bool(manage) or (note.user_id is not None and note.user_id == user.id)
    return {
        "id": note.id,
        "author": note.author_name,
        "text": note.text,
        "path": note.page_path,
        "query": note.page_query,
        "url": note.page_url,
        "screen": note.screen_label,
        "anchor": note.anchor or {},
        "createdAt": note.created_at.isoformat() if note.created_at else None,
        "resolved": note.resolved,
        "resolvedAt": note.resolved_at.isoformat() if note.resolved_at else None,
        "resolvedBy": display_name(note.resolved_by) if note.resolved_by_id else "",
        "canResolve": allowed,
    }
