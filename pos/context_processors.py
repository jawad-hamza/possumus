from django.utils import timezone

from pos.services.access import (
    get_navigation_access,
    get_navigation_manage_access,
    get_user_role_label,
    get_user_role_summary,
    is_admin,
    is_cashier,
)
from pos.services.user_timezones import get_session_time_zone, get_user_time_zone_preference


def app_shell(request):
    active_session = None

    from pos.models import CompanyProfile, SystemSettings

    active_company = CompanyProfile.objects.filter(is_active=True).first()
    system_settings = SystemSettings.load()

    if getattr(request, "user", None) and request.user.is_authenticated:
        from pos.models import UserSessionLog

        log_id = request.session.get("user_session_log_id")
        if log_id:
            active_session = (
                UserSessionLog.objects.filter(id=log_id, user=request.user, logout_at__isnull=True)
                .only("login_at")
                .first()
            )

    user = getattr(request, "user", None)
    session_time_zone = getattr(request, "session_time_zone", get_session_time_zone(request))
    effective_time_zone = getattr(request, "effective_time_zone", timezone.get_current_timezone_name())
    return {
        "is_admin_user": is_admin(user),
        "is_cashier_user": is_cashier(user),
        "user_role_label": get_user_role_label(user),
        "user_role_summary": get_user_role_summary(user),
        "nav_access": get_navigation_access(user),
        "manage_access": get_navigation_manage_access(user),
        "active_user_session": active_session,
        "active_company": active_company,
        "system_settings": system_settings,
        "session_time_zone": session_time_zone,
        "preferred_time_zone": get_user_time_zone_preference(user),
        "effective_time_zone": effective_time_zone,
    }