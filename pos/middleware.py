from django.conf import settings
from django.utils import timezone

from .services.user_timezones import get_effective_time_zone, get_session_time_zone


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