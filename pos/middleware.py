from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone

from .services.user_timezones import get_effective_time_zone, get_session_time_zone


class ModuleStateMiddleware:
    """Which feature modules are on is worked out once per request, not once per question.

    Decorators, forms, the navigation and templates all ask pos.services.modules; inside
    this scope the first answer is kept, and it is always dropped when the request ends.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .services.modules import begin_request_scope, end_request_scope

        token = begin_request_scope()
        try:
            return self.get_response(request)
        finally:
            end_request_scope(token)


class LoginRequiredMiddleware:
    """Deny anonymous access by default; only the named public routes stay open."""

    PUBLIC_URL_NAMES = frozenset(
        {
            "home",
            "landing_plan_checkout",
            "start_trial",
            "signup",
            "resend_verification_email",
            "verify_signup_email",
            "google_auth_start",
            "google_auth_callback",
            "lemonsqueezy_webhook",
            "login",
            "password_reset",
            "password_reset_done",
            "password_reset_confirm",
            "logout",
            "setup_admin",
            "set_client_time_zone",
            "health_check",
            "license_page",
        }
    )
    # The admin and the API run their own authentication.
    PUBLIC_PATH_PREFIXES = ("/admin/", "/api/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.user.is_authenticated:
            return None
        match = request.resolver_match
        if match is not None and match.url_name in self.PUBLIC_URL_NAMES:
            return None
        if request.path.startswith(self.PUBLIC_PATH_PREFIXES):
            return None
        # The company logo shows on the sign-in pages; media is only served by Django when this flag is on.
        if getattr(settings, "SERVE_MEDIA_FILES", settings.DEBUG) and request.path.startswith(settings.MEDIA_URL):
            return None
        if request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("accept", ""):
            return JsonResponse(
                {"status": "error", "message": "Sign in again to continue.", "redirect": reverse("login")},
                status=401,
            )
        return redirect_to_login(request.get_full_path(), reverse("login"))


class UserTimeZoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session_time_zone = get_session_time_zone(request)
        effective_time_zone = get_effective_time_zone(
            getattr(request, "user", None),
            session_time_zone=session_time_zone,
            fallback=settings.TIME_ZONE,
        )
        timezone.activate(effective_time_zone)
        request.session_time_zone = session_time_zone
        request.effective_time_zone = effective_time_zone
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
