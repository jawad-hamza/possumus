"""Parked POS carts.

A cashier can hold the sale in front of them, serve the next customer, and pick
the first one back up. Held carts live in the cashier's own session, so they are
private to that login and disappear on logout.
"""
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from django.utils import timezone

from pos.models import quantize_money
from pos.services.cart_pricing import cart_quantity, line_total, normalize_cart_item


SESSION_KEY = "held_carts"
MAX_HELD_CARTS = 20
LABEL_MAX_LENGTH = 40
CONTEXT_KEYS = ("customer_id", "credit", "payment_method", "bank_account_id", "discount_rate", "tax_rate")


def _stored(session):
    entries = session.get(SESSION_KEY) or []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("id") and entry.get("cart")]


def _save(session, entries):
    session[SESSION_KEY] = entries
    session.modified = True


def _cart_total(cart):
    total = Decimal("0")
    for item in cart.values():
        total += line_total(cart_quantity(item["quantity"]), item["price"], item["discount_percent"])
    return quantize_money(total)


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def list_held_carts(session):
    """Held carts for display, oldest first so the longest-waiting customer is on top."""
    rows = []
    for entry in _stored(session):
        rows.append(
            {
                "id": entry["id"],
                "label": entry.get("label") or "Held sale",
                "held_at": _parse_time(entry.get("held_at")),
                "line_count": len(entry["cart"]),
                "total": Decimal(entry.get("total") or "0"),
            }
        )
    return rows


def hold_cart(session, cart, *, label="", context=None):
    """Park the cart. Raises ValueError when there is nothing to park or the list is full."""
    cleaned = {
        str(product_id): normalize_cart_item(item)
        for product_id, item in (cart or {}).items()
        if cart_quantity(item.get("quantity", 0)) > 0
    }
    if not cleaned:
        raise ValueError("Add at least one item before holding the sale.")

    entries = _stored(session)
    if len(entries) >= MAX_HELD_CARTS:
        raise ValueError(f"Only {MAX_HELD_CARTS} sales can be held at once. Resume or discard one first.")

    now = timezone.now()
    label = (label or "").strip()[:LABEL_MAX_LENGTH]
    if not label:
        label = f"Held sale {timezone.localtime(now).strftime('%H:%M')}"

    entry = {
        "id": uuid4().hex[:12],
        "label": label,
        "held_at": now.isoformat(),
        "cart": cleaned,
        "total": str(_cart_total(cleaned)),
        "context": {key: (context or {}).get(key) or "" for key in CONTEXT_KEYS},
    }
    entries.append(entry)
    _save(session, entries)
    return entry


def pop_held_cart(session, held_id):
    """Remove and return the held cart, or None when it is no longer there."""
    entries = _stored(session)
    for index, entry in enumerate(entries):
        if entry["id"] == held_id:
            entries.pop(index)
            _save(session, entries)
            return entry
    return None
