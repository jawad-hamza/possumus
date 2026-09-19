from django.conf import settings
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


def _shell_notice(user):
    """The trial reminder shown in the page head; None once a licence or subscription is in force."""
    if not getattr(user, "is_authenticated", False):
        return None
    from django.urls import reverse

    from pos.services.billing import workspace_owner_onboarding
    from pos.services.licensing import is_desktop_mode

    onboarding = workspace_owner_onboarding(user)
    if onboarding is None or not onboarding.trial_is_active:
        return None
    if not (user.is_staff or user.is_superuser or onboarding.user_id == user.id):
        return None
    if onboarding.subscription_is_active or onboarding.license_is_active:
        return None
    days = onboarding.trial_days_remaining
    return {
        "label": f"Trial: {days} day{'s' if days != 1 else ''} left",
        "url": reverse("license_page" if is_desktop_mode() else "billing_page"),
    }


def app_shell(request):
    active_session = None

    from pos.models import CompanyProfile, SystemSettings, trial_days
    from pos.services.currency import prime_currency_symbol
    from pos.services.modules import enabled_module_map

    active_company = CompanyProfile.objects.filter(is_active=True).first()
    system_settings = SystemSettings.load()
    currency_symbol = system_settings.currency_symbol or ""
    prime_currency_symbol(currency_symbol)

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
        # {module key: on/off} from the per-request module state; costs no query here.
        "feature_modules": enabled_module_map(),
        "active_user_session": active_session,
        "active_company": active_company,
        "system_settings": system_settings,
        "currency_symbol": currency_symbol,
        "currency_code": system_settings.currency_code or "",
        "trial_days": trial_days(),
        "shell_notice": _shell_notice(user),
        "brand_name": getattr(settings, "PUBLIC_BRAND_NAME", "") or "ZehnMS",
        "session_time_zone": session_time_zone,
        "preferred_time_zone": get_user_time_zone_preference(user),
        "effective_time_zone": effective_time_zone,
    }