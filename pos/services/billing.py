import hashlib
import hmac
import json
from datetime import timezone as dt_timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
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
    onboarding = workspace_owner_onboarding(user)
    return bool(onboarding and onboarding.requires_payment_setup)


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
    return hmac.compare_digest(expected, signature)


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

    plan_code = normalize_plan_code(custom_data.get("plan_code") or onboarding.selected_plan)
    subscription, _ = BillingSubscription.objects.get_or_create(
        owner_onboarding=onboarding,
        defaults={
            "provider": BillingSubscription.PROVIDER_LEMON_SQUEEZY,
            "plan_code": plan_code,
        },
    )

    metadata = dict(subscription.metadata or {})
    metadata["custom_data"] = custom_data
    metadata["product_name"] = (attributes.get("product_name") or "").strip()
    metadata["variant_name"] = (attributes.get("variant_name") or "").strip()

    subscription.plan_code = plan_code
    subscription.status = _status_from_payload(attributes, event_name, subscription.status)
    subscription.provider_customer_id = str(attributes.get("customer_id") or subscription.provider_customer_id or "").strip()
    subscription.provider_order_id = str(attributes.get("order_id") or subscription.provider_order_id or "").strip()
    subscription.provider_subscription_id = str(data.get("id") or attributes.get("subscription_id") or subscription.provider_subscription_id or "").strip()
    subscription.provider_product_id = str(attributes.get("product_id") or subscription.provider_product_id or "").strip()
    subscription.provider_variant_id = str(attributes.get("variant_id") or subscription.provider_variant_id or "").strip()
    subscription.product_name = (attributes.get("product_name") or subscription.product_name or "").strip()
    subscription.variant_name = (attributes.get("variant_name") or subscription.variant_name or "").strip()
    subscription.customer_portal_url = (urls.get("customer_portal") or getattr(settings, "LEMON_SQUEEZY_CUSTOMER_PORTAL_URL", "") or subscription.customer_portal_url or "").strip()
    subscription.renews_at = _parse_provider_datetime(attributes.get("renews_at")) or subscription.renews_at
    subscription.ends_at = _parse_provider_datetime(attributes.get("ends_at")) or subscription.ends_at
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

    subscription.save()

    if onboarding.selected_plan != plan_code:
        onboarding.selected_plan = plan_code
        onboarding.save(update_fields=["selected_plan"])
    onboarding.billing_subscription = subscription
    onboarding.sync_billing_state()
    return subscription


def process_lemon_squeezy_webhook(payload, *, raw_body=b"", signature=""):
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
    if not created:
        return webhook_event, False, None

    onboarding = _resolve_onboarding_from_payload(payload)
    subscription = None
    if onboarding is not None and _subscription_payload_is_supported(payload, event_name):
        subscription = _sync_subscription_from_payload(onboarding, payload, event_name)

    if webhook_event.event_name != event_name or webhook_event.signature != signature or webhook_event.payload != (payload or {}):
        webhook_event.provider_event_id = provider_event_id
        webhook_event.event_name = event_name
        webhook_event.signature = signature
        webhook_event.payload = payload or {}
        webhook_event.save(update_fields=["provider_event_id", "event_name", "signature", "payload"])

    return webhook_event, True, subscription