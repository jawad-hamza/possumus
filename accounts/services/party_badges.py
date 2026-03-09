from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from accounts.models import PartyLedgerEntry


def _get_raw_balance(party):
    ct = ContentType.objects.get_for_model(party)

    qs = PartyLedgerEntry.objects.filter(
        content_type=ct,
        object_id=party.id,
    )

    balance = Decimal("0.00")
    for entry in qs:
        balance += entry.debit - entry.credit

    return balance


def get_customer_badge(customer):
    balance = _get_raw_balance(customer)

    if balance > 0:
        return {"balance_type": "receivable", "amount": balance}
    if balance < 0:
        return {"balance_type": "advance", "amount": abs(balance)}
    return {"balance_type": "clear", "amount": Decimal("0.00")}


def get_supplier_badge(supplier):
    balance = _get_raw_balance(supplier)

    if balance < 0:
        return {"balance_type": "payable", "amount": abs(balance)}
    if balance > 0:
        return {"balance_type": "advance", "amount": balance}
    return {"balance_type": "clear", "amount": Decimal("0.00")}
