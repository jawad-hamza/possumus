from functools import lru_cache
from zoneinfo import available_timezones

from django.conf import settings


SYSTEM_DETECTED_TIME_ZONE = ""
COMMON_TIME_ZONES = (
    "Asia/Karachi",
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Berlin",
    "Asia/Dubai",
    "Asia/Kolkata",
)


@lru_cache(maxsize=1)
def valid_time_zone_names():
    return frozenset(available_timezones())


@lru_cache(maxsize=1)
def time_zone_choices():
    valid_names = valid_time_zone_names()
    preferred = [name for name in COMMON_TIME_ZONES if name in valid_names]
    remaining = [name for name in sorted(valid_names) if name not in preferred]
    return [(SYSTEM_DETECTED_TIME_ZONE, "System detected")] + [(name, name) for name in preferred + remaining]


def is_valid_time_zone(value):
    value = (value or "").strip()
    return bool(value) and value in valid_time_zone_names()


def normalize_time_zone_name(value, fallback=""):
    value = (value or "").strip()
    if not value:
        return fallback
    if value in valid_time_zone_names():
        return value
    return fallback


def get_session_time_zone(request):
    return normalize_time_zone_name(request.session.get("browser_time_zone"), "")


def get_user_time_zone_preference(user):
    if not getattr(user, "is_authenticated", False):
        return ""
    try:
        preference = user.preference
    except Exception:
        return ""
    return normalize_time_zone_name(preference.time_zone, "")


def get_effective_time_zone(user=None, session_time_zone="", fallback=None):
    fallback = fallback or settings.TIME_ZONE
    preferred_time_zone = get_user_time_zone_preference(user)
    if preferred_time_zone:
        return preferred_time_zone

    session_time_zone = normalize_time_zone_name(session_time_zone, "")
    if session_time_zone:
        return session_time_zone

    return normalize_time_zone_name(fallback, settings.TIME_ZONE)