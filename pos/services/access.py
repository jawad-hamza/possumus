from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone


ADMIN_ROLE = "admin"
CASHIER_ROLE = "cashier"
ROLE_GROUPS = {
    ADMIN_ROLE: "Admin",
    CASHIER_ROLE: "Cashier",
}


def ensure_role_groups():
    groups = {}
    for role, name in ROLE_GROUPS.items():
        groups[role], _ = Group.objects.get_or_create(name=name)
    return groups


def is_first_user_setup_required():
    return not get_user_model().objects.exists()


def is_admin(user):
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    return user.groups.filter(name=ROLE_GROUPS[ADMIN_ROLE]).exists()


def is_cashier(user):
    if not getattr(user, "is_authenticated", False):
        return False
    return user.groups.filter(name=ROLE_GROUPS[CASHIER_ROLE]).exists()


def get_user_role_label(user):
    if is_admin(user):
        return "Admin"
    if is_cashier(user):
        return "Cashier"
    return "Unassigned"


def default_home_url(user):
    if is_cashier(user):
        return reverse("pos_screen")
    return reverse("dashboard")


def assign_role(user, role):
    groups = ensure_role_groups()
    user.groups.remove(*groups.values())

    selected_group = groups.get(role)
    if selected_group:
        user.groups.add(selected_group)

    update_fields = []
    if role == ADMIN_ROLE:
        user.is_staff = True
        update_fields.append("is_staff")
    elif role == CASHIER_ROLE:
        user.is_staff = False
        user.is_superuser = False
        update_fields.extend(["is_staff", "is_superuser"])

    if update_fields:
        user.save(update_fields=update_fields)
    return user


def _client_ip(request):
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip() or None


def open_user_session(request, user):
    from pos.models import UserSessionLog

    if not request.session.session_key:
        request.session.save()

    session_key = request.session.session_key
    UserSessionLog.objects.filter(session_key=session_key, logout_at__isnull=True).update(logout_at=timezone.now())
    log = UserSessionLog.objects.create(
        user=user,
        role_name=get_user_role_label(user),
        session_key=session_key,
        ip_address=_client_ip(request),
    )
    request.session["user_session_log_id"] = log.id
    request.session.modified = True
    return log


def close_user_session(request):
    from pos.models import UserSessionLog

    log_id = request.session.get("user_session_log_id")
    if not log_id:
        return

    UserSessionLog.objects.filter(id=log_id, logout_at__isnull=True).update(logout_at=timezone.now())
    request.session.pop("user_session_log_id", None)
    request.session.modified = True


def user_can_access_sale(user, sale):
    return is_admin(user) or (is_cashier(user) and sale.sold_by_id == getattr(user, "id", None))


def role_required(*allowed_roles):
    normalized_roles = {role.lower() for role in allowed_roles}

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if is_first_user_setup_required():
                return redirect("setup_admin")

            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), reverse("login"))

            if is_admin(request.user):
                return view_func(request, *args, **kwargs)

            if CASHIER_ROLE in normalized_roles and is_cashier(request.user):
                return view_func(request, *args, **kwargs)

            if is_cashier(request.user):
                messages.error(request, "Cashier accounts can only access POS and their own receipts.")
                return redirect(default_home_url(request.user))

            messages.error(request, "Your account does not have access to this area.")
            return redirect("login")

        return wrapped

    return decorator


admin_required = role_required(ADMIN_ROLE)
pos_access_required = role_required(ADMIN_ROLE, CASHIER_ROLE)
