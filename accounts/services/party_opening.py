"""
Opening balances for customers and suppliers.

What a party owed (or was owed) before the shop started using the system is
posted against "Opening Balance Equity", together with the party sub-ledger
entry, so the control accounts and the party statements agree from day one.
One opening balance per party; saving again replaces it.
"""

from decimal import Decimal, InvalidOperation

from django.db import transaction

from accounts.models import PartyLedgerEntry, Voucher, VoucherLine
from accounts.services.books_lock import assert_books_open, normalize_voucher_date
from accounts.services.chart_of_accounts import get_core_account
from accounts.services.party_ledger import post_party_ledger

# The party owes us / we owe the party.
THEY_OWE_US = "receivable"
WE_OWE_THEM = "payable"
DIRECTION_CHOICES = (
    (THEY_OWE_US, "They owe us"),
    (WE_OWE_THEM, "We owe them"),
)

CUSTOMER = "customer"
SUPPLIER = "supplier"
REF_MODELS = {
    CUSTOMER: "CustomerOpeningBalance",
    SUPPLIER: "SupplierOpeningBalance",
}
CONTROL_ACCOUNTS = {
    CUSTOMER: "Accounts Receivable",
    SUPPLIER: "Accounts Payable",
}


def party_kind(party) -> str:
    kind = party.__class__.__name__.lower()
    if kind not in REF_MODELS:
        raise ValueError("Opening balances can only be recorded for a customer or a supplier.")
    return kind


def get_party_opening_voucher(party):
    return Voucher.objects.filter(
        voucher_type=Voucher.JOURNAL,
        ref_model=REF_MODELS[party_kind(party)],
        ref_id=party.pk,
    ).first()


def get_party_opening(party):
    """{'amount', 'direction', 'date', 'voucher'} for the recorded opening balance, or None."""
    voucher = get_party_opening_voucher(party)
    if voucher is None:
        return None
    entry = PartyLedgerEntry.objects.filter(voucher=voucher).first()
    if entry is None:
        return None
    if entry.debit > 0:
        return {"amount": entry.debit, "direction": THEY_OWE_US, "date": voucher.date, "voucher": voucher}
    return {"amount": entry.credit, "direction": WE_OWE_THEM, "date": voucher.date, "voucher": voucher}


@transaction.atomic
def set_party_opening_balance(*, party, amount, direction, date=None, actor=None):
    """
    Record (or replace) the opening balance. amount=0 removes it.
    Returns the voucher, or None when the opening balance was removed.
    """
    kind = party_kind(party)
    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid opening balance amount") from exc
    if amount < 0:
        raise ValueError("Opening balance cannot be negative. Pick the direction instead.")
    if direction not in dict(DIRECTION_CHOICES):
        raise ValueError("Choose whether they owe you or you owe them.")

    date = normalize_voucher_date(date)
    existing = get_party_opening_voucher(party)

    if amount == 0:
        if existing is not None:
            existing.delete()  # the delete guard refuses when the entry sits in a locked period
        return None

    assert_books_open(date, action="record an opening balance")
    narration = f"Opening balance - {party} ({dict(DIRECTION_CHOICES)[direction].lower()})"

    if existing is None:
        voucher = Voucher.objects.create(
            voucher_type=Voucher.JOURNAL,
            ref_model=REF_MODELS[kind],
            ref_id=party.pk,
            narration=narration[:255],
            date=date,
            created_by=actor,
        )
    else:
        voucher = existing
        voucher.narration = narration[:255]
        voucher.date = date
        voucher.save(update_fields=["narration", "date"])
        voucher.lines.all().delete()
        voucher.party_entries.all().delete()

    control = get_core_account(CONTROL_ACCOUNTS[kind])
    equity = get_core_account("Opening Balance Equity")

    # Both control accounts move with the party balance: a debit means the
    # party owes us more (or we owe them less), a credit the reverse.
    if direction == THEY_OWE_US:
        VoucherLine.objects.create(voucher=voucher, account=control, debit=amount)
        VoucherLine.objects.create(voucher=voucher, account=equity, credit=amount)
        post_party_ledger(voucher=voucher, party=party, debit=amount, credit=Decimal("0"))
    else:
        VoucherLine.objects.create(voucher=voucher, account=equity, debit=amount)
        VoucherLine.objects.create(voucher=voucher, account=control, credit=amount)
        post_party_ledger(voucher=voucher, party=party, debit=Decimal("0"), credit=amount)

    voucher.clean()
    return voucher
