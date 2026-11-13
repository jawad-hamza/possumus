from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.views import redirect_to_login
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from pos.services.billing import workspace_owner_onboarding, workspace_requires_billing
from pos.services.modules import (
    access_module_enabled,
    module_disabled_message,
    module_enabled,
    module_for_access_key,
    module_within_ceiling,
)


ADMIN_ROLE = "admin"
CASHIER_ROLE = "cashier"
CUSTOM_ROLE_PREFIX = "custom:"
CUSTOM_ROLE_GROUP_PREFIX = "CustomRole:"
ACCESS_NONE = "none"
ACCESS_VIEW = "view"
ACCESS_MANAGE = "manage"
ACCESS_CHOICES = (
    (ACCESS_NONE, "No access"),
    (ACCESS_VIEW, "View only"),
    (ACCESS_MANAGE, "Manage"),
)
ROLE_GROUPS = {
    ADMIN_ROLE: "Admin",
    CASHIER_ROLE: "Cashier",
}
MODULE_DEFINITIONS = (
    {
        "key": "dashboard",
        "label": "Dashboard",
        "description": "Business KPIs and urgent activity.",
        "url_name": "dashboard",
    },
    {
        "key": "pos",
        "label": "POS",
        "description": "Checkout, receipts, and returns.",
        "url_name": "pos_screen",
    },
    {
        "key": "products",
        "label": "Products",
        "description": "Product catalog and pricing.",
        "url_name": "product_list",
    },
    {
        "key": "stock_control",
        "label": "Stock control",
        "description": "Stock corrections and stock take.",
        "url_name": "stock_adjustments",
    },
    {
        "key": "purchases",
        "label": "Purchases",
        "description": "Purchase bills, supplier buying, and returns.",
        "url_name": "purchase_list",
    },
    {
        "key": "expenses",
        "label": "Expenses",
        "description": "Expense entry and bill payments.",
        "url_name": "expense_list",
    },
    {
        "key": "parties",
        "label": "Parties",
        "description": "Customers, suppliers, and settlements.",
        "url_name": "parties_home",
    },
    {
        "key": "distribution",
        "label": "Distribution",
        "description": "Sales orders, load forms, settlements, stock returns and recovery.",
        "url_name": "distribution_home",
    },
    {
        "key": "accounts",
        "label": "Accounts",
        "description": "Ledgers, statements, and finance controls.",
        "url_name": "accounts_home",
    },
    {
        "key": "reports",
        "label": "Reports",
        "description": "Sales, stock, and business reports.",
        "url_name": "reports_home",
    },
    {
        "key": "settings",
        "label": "Settings",
        "description": "Business rules, users, branding, and setup.",
        "url_name": "settings_page",
    },
)
MODULE_LOOKUP = {module["key"]: module for module in MODULE_DEFINITIONS}
ACCESS_RANK = {
    ACCESS_NONE: 0,
    ACCESS_VIEW: 1,
    ACCESS_MANAGE: 2,
}


def ensure_role_groups():
    groups = {}
    for role, name in ROLE_GROUPS.items():
        groups[role], _ = Group.objects.get_or_create(name=name)
    return groups


def is_first_user_setup_required():
    # Unverified signups are inactive, so a junk signup can never lock the real owner out.
    return not get_user_model().objects.filter(is_active=True).exists()


def is_workspace_owner(user):
    """The registered owner (or a superuser) can never be demoted or disabled by other admins."""
    if user is None:
        return False
    return bool(user.is_superuser or getattr(user, "owner_onboarding", None) is not None)


def get_owner_onboarding(user):
    if not getattr(user, "is_authenticated", False):
        return None
    return getattr(user, "owner_onboarding", None)


def user_has_verified_owner_account(user):
    onboarding = get_owner_onboarding(user)
    return onboarding is not None and onboarding.email_verified


def user_requires_business_setup(user):
    onboarding = get_owner_onboarding(user)
    if onboarding is None or not onboarding.email_verified or onboarding.business_created_at:
        return False

    from pos.models import CompanyProfile

    return not CompanyProfile.objects.filter(is_active=True).exists()

def _normalize_access_level(value):
    normalized = (value or "").strip().lower()
    if normalized not in ACCESS_RANK:
        return ACCESS_NONE
    return normalized


def _custom_role_slug_from_value(value):
    value = (value or "").strip()
    if value.startswith(CUSTOM_ROLE_PREFIX):
        return value[len(CUSTOM_ROLE_PREFIX) :]
    return ""


def _all_role_groups():
    role_groups = list(ensure_role_groups().values())
    from pos.models import CustomRole

    custom_names = [role.group_name for role in CustomRole.objects.all()]
    if custom_names:
        role_groups.extend(Group.objects.filter(name__in=custom_names))
    return role_groups


def get_role_choices():
    choices = [
        (ADMIN_ROLE, "Admin"),
        (CASHIER_ROLE, "Cashier"),
    ]

    from pos.models import CustomRole

    for role in CustomRole.objects.order_by("name"):
        choices.append((f"{CUSTOM_ROLE_PREFIX}{role.slug}", role.name))
    return choices


def get_role_label(role_value):
    if role_value == ADMIN_ROLE:
        return "Admin"
    if role_value == CASHIER_ROLE:
        return "Cashier"

    role = get_custom_role_by_identifier(role_value)
    if role:
        return role.name
    return "Unassigned"


def get_custom_role_by_identifier(role_value):
    slug = _custom_role_slug_from_value(role_value)
    if not slug:
        return None

    from pos.models import CustomRole

    return CustomRole.objects.filter(slug=slug).first()


def get_custom_role(user):
    if not getattr(user, "is_authenticated", False):
        return None

    role_group_names = set(user.groups.values_list("name", flat=True))
    if not role_group_names:
        return None

    role_slugs = []
    for group_name in role_group_names:
        if group_name.startswith(CUSTOM_ROLE_GROUP_PREFIX):
            role_slugs.append(group_name[len(CUSTOM_ROLE_GROUP_PREFIX) :])

    if not role_slugs:
        return None

    from pos.models import CustomRole

    return CustomRole.objects.filter(slug__in=role_slugs).order_by("name").first()


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

    role = get_custom_role(user)
    if role:
        return role.name
    return "Unassigned"


def get_user_role_value(user):
    if is_admin(user):
        return ADMIN_ROLE
    if is_cashier(user):
        return CASHIER_ROLE

    role = get_custom_role(user)
    if role:
        return f"{CUSTOM_ROLE_PREFIX}{role.slug}"
    return ""


def describe_permissions(permissions):
    manage_labels = []
    view_labels = []
    for module in MODULE_DEFINITIONS:
        level = _normalize_access_level((permissions or {}).get(module["key"]))
        if level == ACCESS_MANAGE:
            manage_labels.append(module["label"])
        elif level == ACCESS_VIEW:
            view_labels.append(module["label"])

    parts = []
    if manage_labels:
        parts.append("Manage: " + ", ".join(manage_labels))
    if view_labels:
        parts.append("View: " + ", ".join(view_labels))
    return " | ".join(parts) or "No access configured"


def get_user_role_summary(user):
    if is_admin(user):
        return "Full system access"
    if is_cashier(user):
        return "Manage POS only"

    role = get_custom_role(user)
    if role:
        return describe_permissions(role.permissions)
    return "No area access assigned"


def get_user_module_level(user, module_key):
    if module_key not in MODULE_LOOKUP:
        return ACCESS_NONE

    if is_admin(user):
        return ACCESS_MANAGE

    if is_cashier(user):
        return ACCESS_MANAGE if module_key == "pos" else ACCESS_NONE

    role = get_custom_role(user)
    if not role:
        return ACCESS_NONE

    return _normalize_access_level(role.permissions.get(module_key))


def user_can_access_module(user, module_key, minimum=ACCESS_VIEW):
    # A module outside the licence, or switched off by the owner, is closed to everyone, admins included.
    if not access_module_enabled(module_key):
        return False
    current_level = get_user_module_level(user, module_key)
    return ACCESS_RANK[current_level] >= ACCESS_RANK[_normalize_access_level(minimum)]


def get_navigation_access(user):
    return {
        module["key"]: user_can_access_module(user, module["key"], ACCESS_VIEW)
        for module in MODULE_DEFINITIONS
    }


def get_navigation_manage_access(user):
    return {
        module["key"]: user_can_access_module(user, module["key"], ACCESS_MANAGE)
        for module in MODULE_DEFINITIONS
    }


def default_home_url(user):
    if user_requires_business_setup(user):
        return reverse("setup_admin")

    if workspace_requires_billing(user):
        return reverse("billing_page")

    if is_cashier(user):
        return reverse("pos_screen")

    for module in MODULE_DEFINITIONS:
        if user_can_access_module(user, module["key"], ACCESS_VIEW):
            return reverse(module["url_name"])
    return reverse("login")


def assign_role(user, role):
    available_roles = {value for value, _ in get_role_choices()}
    if role not in available_roles:
        raise ValueError("Selected role is no longer available.")

    user.groups.remove(*_all_role_groups())
    groups = ensure_role_groups()

    update_fields = []
    if role == ADMIN_ROLE:
        user.groups.add(groups[ADMIN_ROLE])
        user.is_staff = True
        update_fields.append("is_staff")
    elif role == CASHIER_ROLE:
        user.groups.add(groups[CASHIER_ROLE])
        user.is_staff = False
        user.is_superuser = False
        update_fields.extend(["is_staff", "is_superuser"])
    else:
        custom_role = get_custom_role_by_identifier(role)
        if custom_role is None:
            raise ValueError("Selected custom role could not be found.")
        custom_role.ensure_group()
        custom_group = Group.objects.get(name=custom_role.group_name)
        user.groups.add(custom_group)
        user.is_staff = False
        user.is_superuser = False
        update_fields.extend(["is_staff", "is_superuser"])

    if update_fields:
        user.save(update_fields=sorted(set(update_fields)))
    return user


def _client_ip(request):
    # X-Forwarded-For is only believed behind a proxy we were told to trust.
    from pos.services.throttling import client_ip

    return client_ip(request, default=None)


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


def workspace_is_locked(user=None):
    """True when the trial is over and no licence key or subscription is in force.

    Every entry point (page decorators below, the DRF API, anything added later) must ask
    this one function, so there is exactly one place that decides access.
    """
    return workspace_requires_billing(user)


def workspace_lock_message(user=None):
    onboarding = workspace_owner_onboarding(user)
    if onboarding is not None and onboarding.clock_rolled_back:
        return "This computer's date and time look wrong. Correct them to continue, or enter a licence key."
    if user is None or is_admin(user):
        return "The free trial has ended. Enter a licence key or choose a plan to keep working."
    return "This workspace is locked until the owner activates it."


def _wants_json(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("accept", "")


def workspace_gate_response(request):
    """The single gate in front of every protected view. None means: carry on."""
    if is_first_user_setup_required():
        return redirect("signup")

    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), reverse("login"))

    if user_requires_business_setup(request.user):
        return redirect("setup_admin")

    if workspace_is_locked(request.user):
        message = workspace_lock_message(request.user)
        if _wants_json(request):
            # The POS screen talks JSON; a redirect to an HTML page would just look like a broken scan.
            return JsonResponse(
                {"status": "error", "message": message, "redirect": reverse("billing_page")},
                status=402,
            )
        messages.warning(request, message)
        return redirect("billing_page")

    return None


def module_disabled_response(request, module_key):
    """Friendly refusal for a feature module that is off: JSON 403 for the till's ajax, otherwise a redirect."""
    message = module_disabled_message(module_key)
    if _wants_json(request):
        return JsonResponse({"status": "error", "message": message, "module": module_key}, status=403)
    messages.info(request, message)
    return redirect(default_home_url(request.user))


def _closed_access_module(module_keys):
    """The feature module to blame when every one of these access areas is switched off, else None."""
    closed = None
    for module_key in module_keys:
        if access_module_enabled(module_key):
            return None
        closed = closed or module_for_access_key(module_key)
    return closed


def feature_required(feature_key, ceiling_only=False):
    """Gate for a sub-feature that is not a whole access area (returns, CSV import, hold sale...).

    Put it UNDER the access decorator, so the workspace gate and the role check run first.
    ceiling_only=True refuses only when the edition leaves the module out: for pages that
    already explain themselves while the owner's own switch is off (the expiry report).
    """
    is_open = module_within_ceiling if ceiling_only else module_enabled

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not is_open(feature_key):
                return module_disabled_response(request, feature_key)
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


def user_can_access_sale(user, sale):
    if is_admin(user):
        return True
    if is_cashier(user):
        return sale.sold_by_id == getattr(user, "id", None)
    return user_can_access_module(user, "pos", ACCESS_VIEW)


def role_required(*allowed_roles):
    normalized_roles = {role.lower() for role in allowed_roles}

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            blocked = workspace_gate_response(request)
            if blocked is not None:
                return blocked

            if is_admin(request.user) and ADMIN_ROLE in normalized_roles:
                return view_func(request, *args, **kwargs)

            if CASHIER_ROLE in normalized_roles and is_cashier(request.user):
                return view_func(request, *args, **kwargs)

            messages.error(request, "Your account does not have access to this area.")
            return redirect(default_home_url(request.user))

        return wrapped

    return decorator


def module_access_required(module_key, minimum=ACCESS_VIEW, write_level=ACCESS_MANAGE, write_methods=None):
    minimum = _normalize_access_level(minimum)
    write_level = _normalize_access_level(write_level)
    write_methods = tuple((write_methods or ("POST", "PUT", "PATCH", "DELETE")))

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            blocked = workspace_gate_response(request)
            if blocked is not None:
                return blocked

            closed = _closed_access_module((module_key,))
            if closed is not None:
                return module_disabled_response(request, closed.key)

            required_level = write_level if request.method.upper() in write_methods else minimum
            if user_can_access_module(request.user, module_key, required_level):
                return view_func(request, *args, **kwargs)

            if request.method.upper() in write_methods and user_can_access_module(request.user, module_key, minimum):
                messages.error(request, "Your role can open this area, but it cannot create, edit, or post changes here.")
            elif is_cashier(request.user):
                messages.error(request, "Cashier accounts can only access POS and their own receipts.")
            else:
                messages.error(request, "Your account does not have access to this area.")
            return redirect(default_home_url(request.user))

        return wrapped

    return decorator


def any_module_access_required(module_keys, minimum=ACCESS_VIEW, write_level=ACCESS_MANAGE, write_methods=None):
    minimum = _normalize_access_level(minimum)
    write_level = _normalize_access_level(write_level)
    write_methods = tuple((write_methods or ("POST", "PUT", "PATCH", "DELETE")))
    module_keys = tuple(module_keys)

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            blocked = workspace_gate_response(request)
            if blocked is not None:
                return blocked

            closed = _closed_access_module(module_keys)
            if closed is not None:
                return module_disabled_response(request, closed.key)

            required_level = write_level if request.method.upper() in write_methods else minimum
            if any(user_can_access_module(request.user, module_key, required_level) for module_key in module_keys):
                return view_func(request, *args, **kwargs)

            if request.method.upper() in write_methods and any(
                user_can_access_module(request.user, module_key, minimum) for module_key in module_keys
            ):
                messages.error(request, "Your role can open this area, but it cannot create, edit, or post changes here.")
            elif is_cashier(request.user):
                messages.error(request, "Cashier accounts can only access POS and their own receipts.")
            else:
                messages.error(request, "Your account does not have access to this area.")
            return redirect(default_home_url(request.user))

        return wrapped

    return decorator


admin_required = role_required(ADMIN_ROLE)
pos_access_required = role_required(ADMIN_ROLE, CASHIER_ROLE)
