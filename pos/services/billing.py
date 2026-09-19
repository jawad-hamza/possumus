import hashlib
import hmac
import json
from datetime import timezone as dt_timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pos.models import BillingSubscription, BillingWebhookEvent, OwnerOnboarding

LEMON_SQUEEZY_SIGNATURE_HEADER = "X-Signature"
LEMON_SQUEEZY_SUBSCRIPTION_EVENTS = {
    "subscription_created",
    "subscription_updated",
    "subscription_cancelled",
    "subscription_resumed",
    "subscription_expired",
    "subscription_paused",
    "subscription_unpaused",
    "subscription_plan_changed",
}


def normalize_plan_code(plan_code):
    normalized = (plan_code or "").strip().lower()
    allowed = {choice for choice, _ in OwnerOnboarding.PLAN_CHOICES}
    return normalized if normalized in allowed else OwnerOnboarding.PLAN_STARTER


def lemon_squeezy_plan_urls():
    return {
        OwnerOnboarding.PLAN_STARTER: getattr(settings, "LEMON_SQUEEZY_STARTER_URL", "").strip(),
        OwnerOnboarding.PLAN_GROWTH: getattr(settings, "LEMON_SQUEEZY_GROWTH_URL", "").strip(),
        OwnerOnboarding.PLAN_SCALE: getattr(settings, "LEMON_SQUEEZY_SCALE_URL", "").strip(),
    }


def get_lemon_squeezy_plan_url(plan_code):
    return lemon_squeezy_plan_urls().get(normalize_plan_code(plan_code), "")


def lemon_squeezy_checkout_enabled():
    return bool(getattr(settings, "LEMON_SQUEEZY_ENABLED", False) and any(lemon_squeezy_plan_urls().values()))


def google_sign_in_setup_message():
    return "Google sign-in will turn on after the Google OAuth client ID, secret, and callback URL are connected."


def workspace_owner_onboarding(user=None):
    if getattr(user, "is_authenticated", False):
        onboarding = getattr(user, "owner_onboarding", None)
        if onboarding is not None and onboarding.email_verified:
            return onboarding

    queryset = OwnerOnboarding.objects.select_related("user", "billing_subscription")
    onboarding = queryset.filter(email_verified=True, business_created_at__isnull=False).order_by("-business_created_at", "-created_at").first()
    if onboarding is not None:
        return onboarding
    return queryset.filter(email_verified=True).order_by("-created_at").first()


def workspace_requires_billing(user=None):
    """THE access gate: locked unless the trial, a paid subscription, or a licence key is in force."""
    onboarding = workspace_owner_onboarding(user)
    if onboarding is None:
        return False
    onboarding.touch_last_seen()
    return bool(onboarding.requires_payment_setup)


def user_can_manage_billing(user, onboarding=None):
    if not getattr(user, "is_authenticated", False):
        return False
    onboarding = onboarding or workspace_owner_onboarding(user)
    if onboarding is None:
        return False
    return bool(user.is_staff or user.is_superuser or onboarding.user_id == getattr(user, "id", None))


def _merge_query_params(url, params):
    parts = urlsplit(url)
    merged = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        if value in (None, ""):
            continue
        merged[key] = value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), parts.fragment))


def build_lemon_squeezy_checkout_url(onboarding, plan_code):
    normalized_plan = normalize_plan_code(plan_code or onboarding.selected_plan)
    base_url = get_lemon_squeezy_plan_url(normalized_plan)
    if not base_url:
        return ""

    user = onboarding.user
    params = {
        "checkout[email]": (user.email or "").strip(),
        "checkout[name]": (user.get_full_name() or user.get_username()).strip(),
        "checkout[custom][owner_onboarding_id]": str(onboarding.id),
        "checkout[custom][plan_code]": normalized_plan,
        "checkout[custom][user_id]": str(onboarding.user_id),
    }
    success_url = getattr(settings, "LEMON_SQUEEZY_SUCCESS_URL", "").strip()
    cancel_url = getattr(settings, "LEMON_SQUEEZY_CANCEL_URL", "").strip()
    if success_url:
        params["checkout[success_url]"] = success_url
    if cancel_url:
        params["checkout[cancel_url]"] = cancel_url
    return _merge_query_params(base_url, params)


def begin_lemon_squeezy_checkout(onboarding, plan_code):
    normalized_plan = normalize_plan_code(plan_code or onboarding.selected_plan)
    checkout_url = build_lemon_squeezy_checkout_url(onboarding, normalized_plan)
    if not checkout_url:
        raise ValueError("Billing checkout is not configured for this package yet.")

    now = timezone.now()
    subscription, _ = BillingSubscription.objects.get_or_create(
        owner_onboarding=onboarding,
        defaults={
            "plan_code": normalized_plan,
            "provider": BillingSubscription.PROVIDER_LEMON_SQUEEZY,
            "status": BillingSubscription.STATUS_PENDING,
        },
    )
    metadata = dict(subscription.metadata or {})
    metadata["last_checkout_url"] = checkout_url
    metadata["last_checkout_plan"] = normalized_plan
    subscription.plan_code = normalized_plan
    subscription.checkout_url = checkout_url
    subscription.checkout_started_at = now
    subscription.last_event_name = "checkout_opened"
    subscription.last_event_at = now
    subscription.metadata = metadata
    if not subscription.grants_access:
        subscription.status = BillingSubscription.STATUS_PENDING
    subscription.save()
    onboarding.mark_checkout_started(plan_code=normalized_plan, now=now)
    return subscription, checkout_url


def verify_lemon_squeezy_signature(raw_body, signature):
    secret = getattr(settings, "LEMON_SQUEEZY_WEBHOOK_SECRET", "").strip()
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # Bytes, not str: compare_digest raises TypeError on a non-ASCII str, which would be a 500.
    return hmac.compare_digest(expected.encode("ascii"), str(signature).encode("utf-8", "replace"))


def _payload_hash(raw_body, payload):
    if raw_body:
        return hashlib.sha256(raw_body).hexdigest()
    normalized = json.dumps(payload or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _payload_event_name(payload):
    meta = payload.get("meta") or {}
    event_name = (meta.get("event_name") or "").strip().lower()
    if event_name:
        return event_name
    data = payload.get("data") or {}
    event_name = (data.get("type") or "").strip().lower()
    return event_name.replace(".", "_")


def _payload_event_id(payload):
    meta = payload.get("meta") or {}
    return str(meta.get("webhook_event_id") or meta.get("event_id") or "").strip()


def _payload_attributes(payload):
    return ((payload.get("data") or {}).get("attributes") or {})


def _payload_custom_data(payload):
    meta = payload.get("meta") or {}
    custom_data = meta.get("custom_data") or {}
    if isinstance(custom_data, dict):
        return custom_data
    return {}


def _payload_urls(attributes):
    urls = attributes.get("urls") or {}
    return urls if isinstance(urls, dict) else {}


def _parse_provider_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(str(value).strip())
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _status_from_payload(attributes, event_name, existing_status=""):
    raw_status = (attributes.get("status") or existing_status or "").strip().lower().replace("-", "_")
    allowed = {choice for choice, _ in BillingSubscription.STATUS_CHOICES}
    if raw_status in allowed:
        return raw_status

    event_status_map = {
        "subscription_created": BillingSubscription.STATUS_ACTIVE,
        "subscription_updated": BillingSubscription.STATUS_ACTIVE,
        "subscription_resumed": BillingSubscription.STATUS_ACTIVE,
        "subscription_unpaused": BillingSubscription.STATUS_ACTIVE,
        "subscription_cancelled": BillingSubscription.STATUS_CANCELLED,
        "subscription_expired": BillingSubscription.STATUS_EXPIRED,
        "subscription_paused": BillingSubscription.STATUS_PAUSED,
        "subscription_plan_changed": BillingSubscription.STATUS_ACTIVE,
    }
    return event_status_map.get(event_name, BillingSubscription.STATUS_PENDING)


def _resolve_onboarding_from_payload(payload):
    custom_data = _payload_custom_data(payload)
    onboarding_id = str(custom_data.get("owner_onboarding_id") or "").strip()
    if onboarding_id.isdigit():
        onboarding = OwnerOnboarding.objects.select_related("user", "billing_subscription").filter(pk=int(onboarding_id)).first()
        if onboarding is not None:
            return onboarding

    user_id = str(custom_data.get("user_id") or "").strip()
    if user_id.isdigit():
        onboarding = OwnerOnboarding.objects.select_related("user", "billing_subscription").filter(user_id=int(user_id)).order_by("-created_at").first()
        if onboarding is not None:
            return onboarding

    attributes = _payload_attributes(payload)
    email_candidates = [
        attributes.get("user_email"),
        attributes.get("customer_email"),
        custom_data.get("email"),
    ]
    for email in email_candidates:
        normalized = (email or "").strip().lower()
        if normalized:
            onboarding = OwnerOnboarding.objects.select_related("user", "billing_subscription").filter(user__email__iexact=normalized).order_by("-created_at").first()
            if onboarding is not None:
                return onboarding
    return None


def lemon_squeezy_variant_plan_map():
    """{variant or product id: plan code} from LEMON_SQUEEZY_VARIANT_PLAN_MAP (dict or JSON text)."""
    raw = getattr(settings, "LEMON_SQUEEZY_VARIANT_PLAN_MAP", None) or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if not isinstance(raw, dict):
        return {}
    allowed = {choice for choice, _ in OwnerOnboarding.PLAN_CHOICES}
    mapping = {}
    for provider_id, plan_code in raw.items():
        plan_code = str(plan_code or "").strip().lower()
        if plan_code in allowed:
            mapping[str(provider_id).strip()] = plan_code
    return mapping


def _plan_code_from_payload(attributes, custom_data, fallback_plan):
    mapping = lemon_squeezy_variant_plan_map()
    if mapping:
        # What was actually bought decides the plan. custom_data rides on the checkout URL,
        # so a buyer could pay for Starter while claiming Scale.
        for provider_id in (attributes.get("variant_id"), attributes.get("product_id")):
            plan_code = mapping.get(str(provider_id or "").strip())
            if plan_code:
                return plan_code
        return normalize_plan_code(fallback_plan)
    return normalize_plan_code(custom_data.get("plan_code") or fallback_plan)


def _attribute_datetime(attributes, key, current):
    """An explicit null from the provider clears the date (e.g. ends_at after a resume)."""
    if key not in attributes:
        return current
    return _parse_provider_datetime(attributes.get(key))


def _subscription_payload_is_supported(payload, event_name):
    data = payload.get("data") or {}
    data_type = (data.get("type") or "").strip().lower()
    return data_type == "subscriptions" or event_name in LEMON_SQUEEZY_SUBSCRIPTION_EVENTS


def _sync_subscription_from_payload(onboarding, payload, event_name):
    attributes = _payload_attributes(payload)
    custom_data = _payload_custom_data(payload)
    data = payload.get("data") or {}
    urls = _payload_urls(attributes)
    now = timezone.now()

    subscription = BillingSubscription.objects.select_for_update().filter(owner_onboarding=onboarding).first()
    plan_code = _plan_code_from_payload(
        attributes,
        custom_data,
        subscription.plan_code if subscription is not None else onboarding.selected_plan,
    )
    if subscription is None:
        subscription = BillingSubscription.objects.create(
            owner_onboarding=onboarding,
            provider=BillingSubscription.PROVIDER_LEMON_SQUEEZY,
            plan_code=plan_code,
        )

    incoming_status = _status_from_payload(attributes, event_name, subscription.status)
    incoming_subscription_id = str(data.get("id") or attributes.get("subscription_id") or "").strip()
    provider_updated_at = _parse_provider_datetime(attributes.get("updated_at"))

    # Webhooks can arrive late or out of order; an older snapshot must not undo a newer one.
    if (
        provider_updated_at is not None
        and subscription.provider_updated_at is not None
        and provider_updated_at < subscription.provider_updated_at
    ):
        return subscription, "stale"
    # The death of a previous subscription must not lock out its paid replacement.
    if (
        incoming_subscription_id
        and subscription.provider_subscription_id
        and incoming_subscription_id != subscription.provider_subscription_id
        and subscription.grants_access
        and incoming_status not in {BillingSubscription.STATUS_ACTIVE, BillingSubscription.STATUS_ON_TRIAL}
    ):
        return subscription, "other_subscription"

    metadata = dict(subscription.metadata or {})
    metadata["custom_data"] = custom_data
    metadata["product_name"] = (attributes.get("product_name") or "").strip()
    metadata["variant_name"] = (attributes.get("variant_name") or "").strip()

    subscription.plan_code = plan_code
    subscription.status = incoming_status
    if provider_updated_at is not None:
        subscription.provider_updated_at = provider_updated_at
    subscription.provider_customer_id = str(attributes.get("customer_id") or subscription.provider_customer_id or "").strip()
    subscription.provider_order_id = str(attributes.get("order_id") or subscription.provider_order_id or "").strip()
    subscription.provider_subscription_id = incoming_subscription_id or subscription.provider_subscription_id
    subscription.provider_product_id = str(attributes.get("product_id") or subscription.provider_product_id or "").strip()
    subscription.provider_variant_id = str(attributes.get("variant_id") or subscription.provider_variant_id or "").strip()
    subscription.product_name = (attributes.get("product_name") or subscription.product_name or "").strip()
    subscription.variant_name = (attributes.get("variant_name") or subscription.variant_name or "").strip()
    subscription.customer_portal_url = (urls.get("customer_portal") or getattr(settings, "LEMON_SQUEEZY_CUSTOMER_PORTAL_URL", "") or subscription.customer_portal_url or "").strip()
    subscription.renews_at = _attribute_datetime(attributes, "renews_at", subscription.renews_at)
    subscription.ends_at = _attribute_datetime(attributes, "ends_at", subscription.ends_at)
    subscription.last_event_name = event_name
    subscription.last_event_at = now
    subscription.metadata = metadata

    if subscription.checkout_started_at is None:
        subscription.checkout_started_at = now
    if subscription.status in {BillingSubscription.STATUS_ACTIVE, BillingSubscription.STATUS_ON_TRIAL, BillingSubscription.STATUS_PAST_DUE} and subscription.activated_at is None:
        subscription.activated_at = now
    if subscription.status == BillingSubscription.STATUS_CANCELLED and subscription.cancelled_at is None:
        subscription.cancelled_at = now
    if subscription.status == BillingSubscription.STATUS_PAUSED and subscription.paused_at is None:
        subscription.paused_at = now
    if subscription.status in {BillingSubscription.STATUS_ACTIVE, BillingSubscription.STATUS_ON_TRIAL, BillingSubscription.STATUS_PAST_DUE}:
        subscription.paused_at = None
    # The grace period runs from the FIRST past-due event; repeats of it must not restart the clock.
    if subscription.status == BillingSubscription.STATUS_PAST_DUE:
        if subscription.past_due_since is None:
            subscription.past_due_since = provider_updated_at or now
    else:
        subscription.past_due_since = None

    subscription.save()

    if onboarding.selected_plan != plan_code:
        onboarding.selected_plan = plan_code
        onboarding.save(update_fields=["selected_plan"])
    onboarding.billing_subscription = subscription
    onboarding.sync_billing_state()
    return subscription, ""


@transaction.atomic
def process_lemon_squeezy_webhook(payload, *, raw_body=b"", signature=""):
    """Record and apply one delivery as a unit.

    Atomic on purpose: if the sync fails, the BillingWebhookEvent row is rolled back too,
    so the provider's retry is processed instead of being dismissed as a duplicate.
    """
    signature = (signature or "")[:255]
    event_name = _payload_event_name(payload)
    event_hash = _payload_hash(raw_body, payload)
    provider_event_id = _payload_event_id(payload)
    webhook_event, created = BillingWebhookEvent.objects.get_or_create(
        event_hash=event_hash,
        defaults={
            "provider": BillingSubscription.PROVIDER_LEMON_SQUEEZY,
            "provider_event_id": provider_event_id,
            "event_name": event_name,
            "signature": signature,
            "payload": payload or {},
        },
    )
    webhook_event.skipped_reason = ""
    if not created:
        return webhook_event, False, None

    onboarding = _resolve_onboarding_from_payload(payload)
    subscription = None
    if onboarding is not None and _subscription_payload_is_supported(payload, event_name):
        subscription, webhook_event.skipped_reason = _sync_subscription_from_payload(onboarding, payload, event_name)
        if webhook_event.skipped_reason:
            subscription = None

    if webhook_event.event_name != event_name or webhook_event.signature != signature or webhook_event.payload != (payload or {}):
        webhook_event.provider_event_id = provider_event_id
        webhook_event.event_name = event_name
        webhook_event.signature = signature
        webhook_event.payload = payload or {}
        webhook_event.save(update_fields=["provider_event_id", "event_name", "signature", "payload"])

    return webhook_event, True, subscription