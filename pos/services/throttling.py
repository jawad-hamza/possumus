"""Cache-backed attempt counters for sign-in and other abuse-prone public actions."""
import hashlib
import math
import time

from django.conf import settings
from django.core.cache import cache


LOGIN_SCOPE = "login"
LOGIN_IP_SCOPE = "login-ip"
RESEND_EMAIL_SCOPE = "resend-email"
RESEND_IP_SCOPE = "resend-ip"
PASSWORD_RESET_EMAIL_SCOPE = "pwreset-email"
PASSWORD_RESET_IP_SCOPE = "pwreset-ip"
RESTORE_PASSWORD_SCOPE = "restore-password"
SIGNUP_IP_SCOPE = "signup-ip"


def proxy_headers_trusted():
    """X-Forwarded-For is attacker-controlled unless a proxy we run sets it."""
    explicit = getattr(settings, "TRUST_PROXY", None)
    if explicit is not None:
        return bool(explicit)
    return bool(getattr(settings, "USE_X_FORWARDED_HOST", False))


def client_ip(request, default="unknown"):
    if proxy_headers_trusted():
        forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
        if forwarded:
            return forwarded.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip() or default


def _cache_key(kind, scope, identifier):
    digest = hashlib.sha256(str(identifier).strip().lower().encode("utf-8")).hexdigest()[:40]
    return f"throttle:{kind}:{scope}:{digest}"


def attempt_count(scope, identifier):
    return int(cache.get(_cache_key("count", scope, identifier)) or 0)


def register_attempt(scope, identifier, *, window_seconds):
    """Count one attempt inside a fixed window and return the new total."""
    key = _cache_key("count", scope, identifier)
    if cache.add(key, 1, timeout=window_seconds):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        # The window expired between add() and incr().
        cache.set(key, 1, timeout=window_seconds)
        return 1


def clear_attempts(scope, identifier):
    cache.delete_many([_cache_key("count", scope, identifier), _cache_key("lock", scope, identifier)])


def lock(scope, identifier, *, seconds):
    cache.set(_cache_key("lock", scope, identifier), time.time() + seconds, timeout=seconds)


def lock_seconds_remaining(scope, identifier):
    expires_at = cache.get(_cache_key("lock", scope, identifier))
    if not expires_at:
        return 0
    return max(0, int(math.ceil(float(expires_at) - time.time())))


def allow_action(scope, identifier, *, limit, window_seconds):
    """Rate limit for fire-and-forget actions: True while the caller is inside the budget."""
    return register_attempt(scope, identifier, window_seconds=window_seconds) <= limit


def login_max_attempts():
    return max(1, int(getattr(settings, "LOGIN_MAX_ATTEMPTS", 5)))


def login_ip_max_attempts():
    # A whole shop usually shares one address, so the per-address budget is wider
    # than the per-account one.
    return max(1, int(getattr(settings, "LOGIN_IP_MAX_ATTEMPTS", login_max_attempts() * 5)))


def login_lockout_seconds():
    return max(1, int(getattr(settings, "LOGIN_LOCKOUT_SECONDS", 900)))


def _login_identity(request, identifier):
    return f"{(identifier or '').strip().lower()}|{client_ip(request)}"


def login_lock_seconds_remaining(request, identifier):
    return max(
        lock_seconds_remaining(LOGIN_SCOPE, _login_identity(request, identifier)),
        lock_seconds_remaining(LOGIN_IP_SCOPE, client_ip(request)),
    )


def register_login_failure(request, identifier):
    """Count a failed sign-in. Returns True when this failure triggered a lockout."""
    window = login_lockout_seconds()
    identity = _login_identity(request, identifier)
    ip_address = client_ip(request)
    locked_now = False

    if register_attempt(LOGIN_SCOPE, identity, window_seconds=window) >= login_max_attempts():
        lock(LOGIN_SCOPE, identity, seconds=window)
        locked_now = True
    if register_attempt(LOGIN_IP_SCOPE, ip_address, window_seconds=window) >= login_ip_max_attempts():
        lock(LOGIN_IP_SCOPE, ip_address, seconds=window)
        locked_now = True
    return locked_now


def clear_login_failures(request, identifier):
    # Only the per-account counter resets: a correct password for one account must
    # not wipe the address-wide budget an attacker is burning on other accounts.
    clear_attempts(LOGIN_SCOPE, _login_identity(request, identifier))


def lockout_message(seconds_remaining):
    minutes = max(1, int(math.ceil(seconds_remaining / 60)))
    return (
        "Too many failed sign-in attempts. For your security, sign-in is paused for about "
        f"{minutes} minute{'s' if minutes != 1 else ''}. Try again after that."
    )
