import logging

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe


logger = logging.getLogger(__name__)


@never_cache
@require_safe
def health_check(request):
    """Liveness probe for the desktop launcher, load balancers and uptime monitors.

    Public on purpose (listed in LoginRequiredMiddleware.PUBLIC_URL_NAMES) and must stay
    free of session access so a probe every few seconds never writes a session row.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        logger.exception("Health check could not reach the database.")
        return JsonResponse({"status": "error", "db": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "db": "ok"})
