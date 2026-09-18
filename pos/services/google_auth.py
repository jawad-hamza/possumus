import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.urls import reverse

GOOGLE_STATE_SESSION_KEY = "google_oauth_state"
GOOGLE_NONCE_SESSION_KEY = "google_oauth_nonce"
GOOGLE_PLAN_SESSION_KEY = "google_oauth_plan"
GOOGLE_NEXT_URL_SESSION_KEY = "google_oauth_next"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


def google_sign_in_enabled():
    return bool(
        getattr(settings, "GOOGLE_SIGN_IN_ENABLED", False)
        and getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "").strip()
        and getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    )


def google_oauth_redirect_uri(request):
    configured_redirect = getattr(settings, "GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if configured_redirect:
        return configured_redirect
    return request.build_absolute_uri(reverse("google_auth_callback"))


def get_google_oauth_session(request):
    return {
        "state": request.session.get(GOOGLE_STATE_SESSION_KEY, ""),
        "nonce": request.session.get(GOOGLE_NONCE_SESSION_KEY, ""),
        "plan_code": request.session.get(GOOGLE_PLAN_SESSION_KEY, ""),
        "next_url": request.session.get(GOOGLE_NEXT_URL_SESSION_KEY, ""),
    }


def clear_google_oauth_session(request):
    for key in (
        GOOGLE_STATE_SESSION_KEY,
        GOOGLE_NONCE_SESSION_KEY,
        GOOGLE_PLAN_SESSION_KEY,
        GOOGLE_NEXT_URL_SESSION_KEY,
    ):
        request.session.pop(key, None)
    request.session.modified = True


def build_google_authorization_url(request, *, plan_code="", next_url=""):
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    request.session[GOOGLE_STATE_SESSION_KEY] = state
    request.session[GOOGLE_NONCE_SESSION_KEY] = nonce
    request.session[GOOGLE_PLAN_SESSION_KEY] = (plan_code or "").strip()
    request.session[GOOGLE_NEXT_URL_SESSION_KEY] = (next_url or "").strip()
    request.session.modified = True

    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": google_oauth_redirect_uri(request),
        "response_type": "code",
        "scope": getattr(settings, "GOOGLE_OAUTH_SCOPE", "openid email profile"),
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    return f"{settings.GOOGLE_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_google_authorization_code(request, code):
    try:
        response = requests.post(
            settings.GOOGLE_OAUTH_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": google_oauth_redirect_uri(request),
                "grant_type": "authorization_code",
            },
            timeout=getattr(settings, "GOOGLE_OAUTH_TIMEOUT", 10),
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise ValueError(
            "Google sign-in could not complete. Check the OAuth client ID, secret, and callback URL."
        ) from exc
    except ValueError as exc:
        raise ValueError("Google sign-in returned an unreadable token response.") from exc

    if not payload.get("id_token"):
        raise ValueError("Google sign-in did not return an identity token.")
    return payload


def verify_google_identity_token(id_token_value, *, expected_nonce=""):
    if not id_token_value:
        raise ValueError("Google sign-in did not return an identity token.")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
    except ImportError as exc:
        raise ValueError(
            "Google sign-in verification requires the 'google-auth' package. Install the updated requirements.txt."
        ) from exc

    request_adapter = google_requests.Request(session=requests.Session())
    try:
        claims = google_id_token.verify_oauth2_token(
            id_token_value,
            request_adapter,
            settings.GOOGLE_OAUTH_CLIENT_ID,
        )
    except Exception as exc:
        raise ValueError("Google sign-in returned an invalid identity token.") from exc

    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise ValueError("Google sign-in returned an unexpected token issuer.")
    if expected_nonce and claims.get("nonce") != expected_nonce:
        raise ValueError("Google sign-in could not be verified. Please try again.")
    return claims