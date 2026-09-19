"""Billing page, licence activation, checkout, portal redirect and the Lemon Squeezy webhook."""
import json
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from accounts.services.audit import record_audit
from ..models import OwnerOnboarding, trial_days
from ..services.access import (
    default_home_url,
    ensure_role_groups,
    is_admin,
    is_first_user_setup_required,
    user_requires_business_setup,
    workspace_is_locked,
)
from ..services.billing import (
    LEMON_SQUEEZY_SIGNATURE_HEADER,
    begin_lemon_squeezy_checkout,
    process_lemon_squeezy_webhook,
    user_can_manage_billing,
    verify_lemon_squeezy_signature,
    workspace_owner_onboarding,
)
from ..services.licensing import (
    LicenseError,
    activate_license,
    get_active_license,
    is_desktop_mode,
)
from ._shared import (
    LANDING_BRAND_NAME,
    LANDING_CHECKOUT_PROVIDER,
    _landing_plan_lookup,
    _landing_pricing_plans,
    safe_decimal,
)

logger = logging.getLogger(__name__)


def _billing_status_tone(onboarding, subscription):
    if onboarding is not None and onboarding.license_is_active:
        return "healthy"
    if subscription is not None and subscription.grants_access:
        return subscription.status_tone
    if onboarding is not None and onboarding.trial_is_active:
        return "advance"
    if subscription is not None:
        return subscription.status_tone
    if onboarding is not None and onboarding.requires_payment_setup:
        return "payable"
    return "clear"


def _billing_page_context(request, onboarding, *, can_manage=False):
    subscription = onboarding.current_subscription if onboarding is not None else None
    current_plan_code = (
        subscription.plan_code
        if subscription is not None
        else (onboarding.selected_plan if onboarding is not None else OwnerOnboarding.PLAN_STARTER)
    )
    current_plan = _landing_plan_lookup(current_plan_code) or _landing_plan_lookup(OwnerOnboarding.PLAN_STARTER)
    portal_url = (
        subscription.customer_portal_url
        if subscription is not None and subscription.customer_portal_url
        else getattr(settings, "LEMON_SQUEEZY_CUSTOMER_PORTAL_URL", "")
    )
    current_price = safe_decimal(current_plan["price"] if current_plan else "0")
    desktop = is_desktop_mode()
    plan_cards = []
    # A desktop install is bought once with a licence key; it has no online checkout.
    for plan in [] if desktop else _landing_pricing_plans():
        plan_price = safe_decimal(plan["price"])
        is_current = plan["code"] == current_plan_code
        if is_current:
            cta_label = "Current Plan" if onboarding and onboarding.billing_is_active else "Activate This Plan"
        elif plan_price > current_price:
            cta_label = f"Upgrade to {plan['name']}"
        elif plan_price < current_price:
            cta_label = f"Switch to {plan['name']}"
        else:
            cta_label = f"Choose {plan['name']}"
        plan_cards.append(
            {
                **plan,
                "cta_url": reverse("billing_checkout", args=[plan["code"]]),
                "cta_label": cta_label,
                "is_current": is_current,
                "checkout_configured": bool(plan.get("checkout_link")),
            }
        )

    return {
        "billing_onboarding": onboarding,
        "billing_subscription": subscription,
        "billing_current_plan": current_plan,
        "billing_status_label": onboarding.billing_status_label if onboarding is not None else "Setup not started",
        "billing_status_tone": _billing_status_tone(onboarding, subscription),
        "billing_trial_days_remaining": onboarding.trial_days_remaining if onboarding is not None else 0,
        "billing_portal_url": (portal_url or "").strip(),
        "billing_can_manage": can_manage,
        "billing_plan_cards": plan_cards,
        "billing_checkout_enabled": any(card["checkout_configured"] for card in plan_cards),
        "billing_requires_action": bool(onboarding and onboarding.requires_payment_setup),
        "billing_provider_name": LANDING_CHECKOUT_PROVIDER,
        "billing_is_desktop": desktop,
        "billing_license": onboarding.active_license if onboarding is not None else get_active_license(),
        "billing_trial_length_days": onboarding.trial_length_days if onboarding is not None else trial_days(),
        "billing_clock_rolled_back": bool(onboarding and onboarding.clock_rolled_back),
        "billing_can_activate_license": bool(can_manage or is_admin(request.user)),
    }


def billing_page(request):
    ensure_role_groups()
    if is_first_user_setup_required():
        return redirect("signup")
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")
    if user_requires_business_setup(request.user):
        return redirect("setup_admin")

    onboarding = workspace_owner_onboarding(request.user)
    can_manage_billing = user_can_manage_billing(request.user, onboarding)
    billing_context = _billing_page_context(request, onboarding, can_manage=can_manage_billing)

    # The lock itself is explained by the card on the page (and by the gate's own message),
    # so no second flash message is stacked on top here.
    if onboarding is None and not is_desktop_mode():
        messages.info(request, "Billing becomes available after owner onboarding and business setup are complete.")

    return render(
        request,
        "pos/billing.html",
        {
            "page_title": "Billing",
            "page_subtitle": "Free trial, licence key and plan in one place.",
            "page_icon": "bi-credit-card-2-front",
            "page_icon_accent": "warning",
            "brand_name": LANDING_BRAND_NAME,
            **billing_context,
        },
    )


def billing_checkout(request, plan_code):
    ensure_role_groups()
    if is_first_user_setup_required():
        return redirect("signup")
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?{urlencode({'next': reverse('billing_page')})}")
    if user_requires_business_setup(request.user):
        return redirect("setup_admin")

    onboarding = workspace_owner_onboarding(request.user)
    if onboarding is None:
        messages.error(request, "Billing is not available until the owner workspace is created.")
        return redirect(default_home_url(request.user))
    if not user_can_manage_billing(request.user, onboarding):
        messages.error(request, "Only the owner or an admin can manage workspace billing.")
        return redirect("billing_page")

    plan = _landing_plan_lookup(plan_code)
    if plan is None:
        raise Http404("Unknown billing package.")

    if is_desktop_mode():
        messages.info(request, "This install is activated with a licence key, not an online plan.")
        return redirect("billing_page")

    # Starting a checkout writes subscription state, so it only happens on POST. A GET
    # (link prefetch, a crawler, a pasted URL) lands on the billing page unchanged.
    if request.method != "POST":
        return redirect(f"{reverse('billing_page')}#plans")

    try:
        subscription, checkout_url = begin_lemon_squeezy_checkout(onboarding, plan["code"])
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("billing_page")

    record_audit(
        action="billing_checkout_started",
        description=f"{plan['name']} checkout opened from the billing centre.",
        actor=request.user,
        target=subscription,
        metadata={"plan": plan["name"], "provider": LANDING_CHECKOUT_PROVIDER},
    )
    messages.info(request, f"{plan['name']} checkout opened in {LANDING_CHECKOUT_PROVIDER}. Complete payment there to activate the workspace.")
    return redirect(checkout_url)


def billing_portal_redirect(request):
    ensure_role_groups()
    if is_first_user_setup_required():
        return redirect("signup")
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?{urlencode({'next': reverse('billing_page')})}")

    onboarding = workspace_owner_onboarding(request.user)
    if onboarding is None:
        messages.error(request, "Billing portal is not available yet for this workspace.")
        return redirect(default_home_url(request.user))
    if not user_can_manage_billing(request.user, onboarding):
        messages.error(request, "Only the owner or an admin can open the billing portal.")
        return redirect("billing_page")

    subscription = onboarding.current_subscription
    portal_url = (
        subscription.customer_portal_url
        if subscription is not None and subscription.customer_portal_url
        else getattr(settings, "LEMON_SQUEEZY_CUSTOMER_PORTAL_URL", "")
    )
    portal_url = (portal_url or "").strip()
    if not portal_url:
        messages.info(request, "Customer billing portal URL is not configured yet.")
        return redirect("billing_page")
    return redirect(portal_url)


@csrf_exempt
def lemonsqueezy_webhook(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method_not_allowed"}, status=405)

    raw_body = request.body or b""
    signature = (request.headers.get(LEMON_SQUEEZY_SIGNATURE_HEADER) or request.META.get("HTTP_X_SIGNATURE") or "").strip()
    if not getattr(settings, "LEMON_SQUEEZY_WEBHOOK_SECRET", "").strip():
        return JsonResponse({"ok": False, "error": "webhook_not_configured"}, status=503)
    if not verify_lemon_squeezy_signature(raw_body, signature):
        return JsonResponse({"ok": False, "error": "invalid_signature"}, status=400)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    try:
        webhook_event, created, subscription = process_lemon_squeezy_webhook(payload, raw_body=raw_body, signature=signature)
    except Exception:
        # Nothing was saved (the processing is atomic), so a 500 makes the provider retry cleanly.
        logger.exception("Lemon Squeezy webhook could not be processed")
        return JsonResponse({"ok": False, "error": "processing_failed"}, status=500)

    if created and subscription is not None:
        record_audit(
            action="billing_subscription_synced",
            description=f"Billing updated through {LANDING_CHECKOUT_PROVIDER} webhook ({subscription.status_label}).",
            target=subscription,
            metadata={
                "plan": subscription.plan_label,
                "status": subscription.status,
                "provider_event": webhook_event.event_name,
            },
        )

    return JsonResponse({
        "ok": True,
        "duplicate": not created,
        "ignored": getattr(webhook_event, "skipped_reason", ""),
        "event": webhook_event.event_name,
        "subscription": subscription.provider_subscription_id if subscription is not None else "",
    })


def license_page(request):
    """Paste a licence key. Deliberately outside the workspace gate: it is the way back in."""
    ensure_role_groups()
    if is_first_user_setup_required():
        return redirect("signup")
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?{urlencode({'next': reverse('license_page')})}")
    if user_requires_business_setup(request.user):
        return redirect("setup_admin")

    onboarding = workspace_owner_onboarding(request.user)
    can_activate = bool(is_admin(request.user) or user_can_manage_billing(request.user, onboarding))
    typed_key = ""

    if request.method == "POST":
        if not can_activate:
            messages.error(request, "Only the owner or an admin can activate a licence.")
            return redirect("license_page")

        typed_key = (request.POST.get("license_key") or "").strip()
        try:
            license_obj = activate_license(typed_key, user=request.user)
        except LicenseError as exc:
            messages.error(request, str(exc))
        else:
            if onboarding is not None:
                onboarding.sync_billing_state()
            record_audit(
                action="license_activated",
                description=f"Licence key activated for {license_obj.licensee}.",
                actor=request.user,
                target=license_obj,
                metadata={
                    "plan": license_obj.plan,
                    "expires_at": license_obj.expires_at.isoformat() if license_obj.expires_at else None,
                    "max_users": license_obj.max_users,
                },
            )
            messages.success(request, f"Licence activated for {license_obj.licensee}. {license_obj.status_label}.")
            # Straight back to work, not back to a billing screen.
            return redirect(default_home_url(request.user))

    return render(
        request,
        "pos/license.html",
        {
            "page_title": "Licence",
            "page_subtitle": "Activate this install with the key you received.",
            "page_icon": "bi-key",
            "page_icon_accent": "warning",
            "brand_name": LANDING_BRAND_NAME,
            "typed_license_key": typed_key,
            "workspace_locked": workspace_is_locked(request.user),
            **_billing_page_context(request, onboarding, can_manage=can_activate),
        },
    )
