from pos.services.access import get_user_role_label, is_admin, is_cashier


def app_shell(request):
    active_session = None
    active_company = None
    system_settings = None

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

    return {
        "is_admin_user": is_admin(getattr(request, "user", None)),
        "is_cashier_user": is_cashier(getattr(request, "user", None)),
        "user_role_label": get_user_role_label(getattr(request, "user", None)),
        "active_user_session": active_session,
        "active_company": active_company,
        "system_settings": system_settings,
    }
