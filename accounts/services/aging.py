"""
Receivables and payables aging from the party sub-ledger.

Payments are matched to the oldest open charges first (FIFO), which is how a
shopkeeper thinks about "what is still unpaid and how old is it". Whatever a
party has paid beyond its charges is shown as an advance, not netted into the
buckets.
"""

from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from accounts.models import PartyLedgerEntry
from accounts.services.report_utils import ZERO, parse_report_date
from pos.models import Customer, Supplier

RECEIVABLE = "receivable"
PAYABLE = "payable"

BUCKETS = (
    ("current", "0-30 days", 0, 30),
    ("days_31_60", "31-60 days", 31, 60),
    ("days_61_90", "61-90 days", 61, 90),
    ("over_90", "Over 90 days", 91, None),
)
BUCKET_KEYS = tuple(bucket[0] for bucket in BUCKETS)


def _bucket_for(age_days: int) -> str:
    for key, _label, low, high in BUCKETS:
        if age_days >= low and (high is None or age_days <= high):
            return key
    return BUCKET_KEYS[0]


def _party_label(party, object_id, kind):
    if party is None:
        return f"Deleted {'customer' if kind == RECEIVABLE else 'supplier'} #{object_id}"
    return getattr(party, "name", None) or str(party)


def get_aging(kind: str, as_of_date=None):
    if kind not in (RECEIVABLE, PAYABLE):
        raise ValueError("Aging kind must be 'receivable' or 'payable'")

    as_of_date = parse_report_date(as_of_date) or timezone.localdate()
    model = Customer if kind == RECEIVABLE else Supplier
    # A customer is charged with debits; a supplier charges us with credits.
    charge_field, payment_field = ("debit", "credit") if kind == RECEIVABLE else ("credit", "debit")

    entries = (
        PartyLedgerEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(model),
            voucher__date__lte=as_of_date,
        )
        .values_list("object_id", "voucher__date", charge_field, payment_field)
        .order_by("voucher__date", "voucher_id", "id")
    )

    charges = defaultdict(list)
    payments = defaultdict(lambda: ZERO)
    for object_id, entry_date, charge, payment in entries:
        if charge:
            charges[object_id].append([entry_date, charge])
        if payment:
            payments[object_id] += payment

    party_ids = set(charges) | set(payments)
    parties = model.objects.in_bulk(party_ids)

    rows = []
    totals = {key: ZERO for key in BUCKET_KEYS}
    totals.update({"outstanding": ZERO, "advance": ZERO})

    for object_id in party_ids:
        unapplied = payments[object_id]
        buckets = {key: ZERO for key in BUCKET_KEYS}
        oldest_open = None
        for entry_date, amount in charges[object_id]:
            settled = min(amount, unapplied)
            unapplied -= settled
            remaining = amount - settled
            if remaining <= 0:
                continue
            if oldest_open is None:
                oldest_open = entry_date
            buckets[_bucket_for((as_of_date - entry_date).days)] += remaining

        outstanding = sum(buckets.values(), ZERO)
        if outstanding == 0 and unapplied == 0:
            continue

        party = parties.get(object_id)
        rows.append({
            "party_id": object_id,
            "party_exists": party is not None,
            "party": _party_label(party, object_id, kind),
            "phone": getattr(party, "phone", "") or "",
            "oldest_open": oldest_open,
            "oldest_days": (as_of_date - oldest_open).days if oldest_open else None,
            "outstanding": outstanding,
            "advance": unapplied,
            **buckets,
        })
        for key in BUCKET_KEYS:
            totals[key] += buckets[key]
        totals["outstanding"] += outstanding
        totals["advance"] += unapplied

    rows.sort(key=lambda row: (-row["outstanding"], row["party"].lower()))

    return {
        "kind": kind,
        "as_of_date": as_of_date,
        "rows": rows,
        "totals": totals,
        "buckets": [{"key": key, "label": label, "total": totals[key]} for key, label, _low, _high in BUCKETS],
        "party_count": sum(1 for row in rows if row["outstanding"] > 0),
    }
