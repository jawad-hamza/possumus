from decimal import Decimal
from django.contrib.contenttypes.models import ContentType

from accounts.models import PartyLedgerEntry


def get_party_statement(
    *,
    party,
    from_date=None,
    to_date=None,
):
    """
    Returns chronological ledger with:
    - date filtering (Option A)
    - opening balance
    - UI-ready balance labels (Option B)

    Balance rule:
    - debit  increases balance
    - credit decreases balance
    """

    ct = ContentType.objects.get_for_model(party)

    base_qs = PartyLedgerEntry.objects.filter(
        content_type=ct,
        object_id=party.id,
    ).select_related("voucher")

    # -----------------------------
    # Opening balance (before from_date)
    # -----------------------------
    opening_balance = Decimal("0")

    if from_date:
        prev_entries = base_qs.filter(voucher__date__lt=from_date)
        for e in prev_entries:
            opening_balance += (e.debit - e.credit)

    # -----------------------------
    # Main entries (date range)
    # -----------------------------
    entries = base_qs

    if from_date:
        entries = entries.filter(voucher__date__gte=from_date)
    if to_date:
        entries = entries.filter(voucher__date__lte=to_date)

    entries = entries.order_by("voucher__date", "id")

    rows = []
    running = opening_balance

    for e in entries:
        running += (e.debit - e.credit)

        rows.append({
            # core
            "date": e.voucher.date,
            "voucher_id": e.voucher.id,
            "voucher_type": e.voucher.voucher_type,
            "ref": f"{e.voucher.ref_model}#{e.voucher.ref_id}",
            "debit": e.debit,
            "credit": e.credit,

            # accounting
            "running_balance": running,

            # UI helpers (Option B)
            "balance_type": (
                "receivable" if running > 0
                else "advance" if running < 0
                else "clear"
            ),
            "display_balance": abs(running),
        })

    return {
        "party": str(party),
        "opening_balance": opening_balance,
        "closing_balance": running,
        "balance_type": (
            "receivable" if running > 0
            else "advance" if running < 0
            else "clear"
        ),
        "rows": rows,
    }
    
from decimal import Decimal


def get_unified_party_statement(
    *,
    customer=None,
    supplier=None,
    from_date=None,
    to_date=None,
):
    """
    Unified customer + supplier statement.
    UI-ready and date-aware.
    """

    rows = []

    if customer:
        data = get_party_statement(
            party=customer,
            from_date=from_date,
            to_date=to_date,
        )
        for r in data["rows"]:
            rows.append({**r, "role": "customer"})

    if supplier:
        data = get_party_statement(
            party=supplier,
            from_date=from_date,
            to_date=to_date,
        )
        for r in data["rows"]:
            rows.append({**r, "role": "supplier"})

    # sort unified timeline
    rows.sort(key=lambda x: (x["date"], x["voucher_id"]))

    # recompute unified running balance
    running = Decimal("0")
    final = []

    for r in rows:
        running += (r["debit"] - r["credit"])
        r["running_balance"] = running
        r["balance_type"] = (
            "receivable" if running > 0
            else "advance" if running < 0
            else "clear"
        )
        r["display_balance"] = abs(running)
        final.append(r)

    return {
        "closing_balance": running,
        "balance_type": (
            "receivable" if running > 0
            else "advance" if running < 0
            else "clear"
        ),
        "rows": final,
    }