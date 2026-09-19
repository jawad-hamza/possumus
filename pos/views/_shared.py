"""Helpers used by more than one pos.views module."""
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings

from ..models import OwnerOnboarding, quantize_money, trial_days
from ..services.user_timezones import normalize_time_zone_name
from ..services.access import ACCESS_MANAGE, user_can_access_module
from ..services.billing import get_lemon_squeezy_plan_url


def safe_decimal(value):
    try:
        return Decimal(value)
    except (TypeError, InvalidOperation):
        return Decimal("0")


def _normalized_rate(value):
    rate = safe_decimal(value)
    if rate < 0:
        return Decimal("0")
    if rate > 100:
        return Decimal("100")
    return quantize_money(rate)


def _remember_browser_time_zone(request, raw_time_zone):
    time_zone_name = normalize_time_zone_name(raw_time_zone, "")
    if not time_zone_name:
        return ""
    request.session["browser_time_zone"] = time_zone_name
    request.session.modified = True
    return time_zone_name


def _parse_report_date(value, default):
    if not value:
        return default

    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return default


def _date_range_url(route_name, start, end):
    # The report views read start/end; pos.services.reporting owns the format.
    from ..services.reporting import date_range_url

    return date_range_url(route_name, start, end)


LANDING_BRAND_NAME = getattr(settings, "PUBLIC_BRAND_NAME", "ZehnMS")


LANDING_PUBLIC_DOMAIN = getattr(settings, "PUBLIC_APP_DOMAIN", "https://example.com")


LANDING_SUPPORT_EMAIL = getattr(settings, "DEFAULT_FROM_EMAIL", "hello@example.com")


LANDING_CHECKOUT_PROVIDER = getattr(settings, "LEMON_SQUEEZY_PROVIDER_NAME", "Lemon Squeezy")


def _landing_pricing_plans():
    return [
        {
            "code": OwnerOnboarding.PLAN_STARTER,
            "name": "Starter",
            "badge": "Lean retail",
            "price": "10",
            "description": "A clean starting package for a single counter or owner-operated shop.",
            "features": [
                "1 store workspace",
                "POS, products, purchases, and reports",
                "Up to 3 staff logins",
                f"{trial_days()} free days to set up and run the workspace",
            ],
            "featured": False,
            "cta_label": "Get Started Free",
            "checkout_link": get_lemon_squeezy_plan_url(OwnerOnboarding.PLAN_STARTER),
        },
        {
            "code": OwnerOnboarding.PLAN_GROWTH,
            "name": "Growth",
            "badge": "Most popular",
            "price": "24",
            "description": "For growing stores that need tighter controls, deeper reporting, and more staff capacity.",
            "features": [
                "Everything in Starter",
                "Advanced tax settings, backups, and audit trail",
                "Up to 10 staff logins",
                "Priority onboarding help",
            ],
            "featured": True,
            "cta_label": "Get Started Free",
            "checkout_link": get_lemon_squeezy_plan_url(OwnerOnboarding.PLAN_GROWTH),
        },
        {
            "code": OwnerOnboarding.PLAN_SCALE,
            "name": "Scale",
            "badge": "Operations ready",
            "price": "59",
            "description": "For larger retail teams that want broader operational control and room for custom rollout needs.",
            "features": [
                "Everything in Growth",
                "Higher-volume team and workflow support",
                "Operational rollout assistance",
                "Best fit for serious branch growth",
            ],
            "featured": False,
            "cta_label": "Get Started Free",
            "checkout_link": get_lemon_squeezy_plan_url(OwnerOnboarding.PLAN_SCALE),
        },
    ]


def _landing_plan_lookup(plan_code):
    plan_code = (plan_code or "").strip().lower()
    for plan in _landing_pricing_plans():
        if plan["code"] == plan_code:
            return plan
    return None


def _party_opening_form(request, party_kind):
    """Optional "they already owe us / we already owe them" block on the add-party pages.

    Only offered to people who may post to the books, and every field is optional so a
    plain add (or a POST from the quick-add dialogs) is never blocked by it.
    """
    if not user_can_access_module(request.user, "accounts", ACCESS_MANAGE):
        return None
    from accounts.forms import PartyOpeningBalanceForm

    form = PartyOpeningBalanceForm(request.POST or None, party_kind=party_kind, prefix="opening")
    for field in form.fields.values():
        field.required = False
    form.fields["amount"].help_text = "Leave empty when nothing was owed before you started using the system."
    return form


def _save_party_opening(request, party, party_kind, opening_form):
    """Post the opening balance typed on the add-party page. Raises ValueError with a readable reason."""
    if opening_form is None:
        return None
    amount = opening_form.cleaned_data.get("amount")
    if not amount:
        return None
    from accounts.services.audit import record_audit
    from accounts.services.party_opening import THEY_OWE_US, WE_OWE_THEM, set_party_opening_balance

    direction = opening_form.cleaned_data.get("direction") or (THEY_OWE_US if party_kind == "customer" else WE_OWE_THEM)
    voucher = set_party_opening_balance(
        party=party,
        amount=amount,
        direction=direction,
        date=opening_form.cleaned_data.get("date"),
        actor=request.user,
    )
    record_audit(
        action="party_opening_balance_set",
        description=f"Opening balance for {party_kind} '{party}' set to {amount} while adding the {party_kind}.",
        actor=request.user,
        target=party,
        metadata={"amount": str(amount), "direction": direction, "voucher_id": voucher.id if voucher else ""},
    )
    return voucher
