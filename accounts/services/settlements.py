# accounts/services/settlements.py
# --------------------------------
# Settlement Engine (RECEIVE / PAY)
#
# ✅ RECEIPT  = money received from a CUSTOMER (reduces AR)
# ✅ PAYMENT  = money paid to a SUPPLIER (reduces AP)
#
# Posts:
# - Voucher
# - VoucherLine(s)
# - PartyLedgerEntry
# - attaches voucher to Settlement
#
# NOTE:
# AP control balance is negative (liability) in voucher balance logic (debit-credit),
# but party ledger stores payable as positive (credit-debit). Integrity runner handles this.

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from accounts.models import Account, Voucher, VoucherLine, Settlement
from accounts.services.party_ledger import post_party_ledger


# -------------------------
# Helpers
# -------------------------

def _acc(title: str) -> Account:
    try:
        return Account.objects.get(title=title, is_active=True)
    except Account.DoesNotExist:
        raise ValueError(f"Required account not found or inactive: {title}")


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError("Invalid amount")


def _money_account(payment_method: str) -> Account:
    """
    Settlement.payment_method is expected to be Settlement.CASH or Settlement.BANK
    """
    if payment_method == Settlement.CASH:
        return _acc("Cash")
    if payment_method == Settlement.BANK:
        return _acc("Bank")
    raise ValueError("Invalid payment method. Use CASH or BANK.")


# -------------------------
# Core Posting
# -------------------------

@transaction.atomic
def post_settlement(settlement: Settlement) -> Voucher:
    """
    Posts a Settlement into accounting + party ledger.

    RECEIPT:
        Cash/Bank DR
        Accounts Receivable CR
        PartyLedgerEntry CR  (customer paid)

    PAYMENT:
        Accounts Payable DR
        Cash/Bank CR
        PartyLedgerEntry DR  (supplier paid)
    """

    if settlement.party is None:
        raise ValueError("Settlement.party is required")

    amount = _to_decimal(settlement.amount)
    if amount <= 0:
        raise ValueError("Settlement amount must be positive")

    # Prevent double posting
    if getattr(settlement, "voucher_id", None):
        # already posted
        return settlement.voucher

    money = _money_account(settlement.payment_method)
    ar = _acc("Accounts Receivable")
    ap = _acc("Accounts Payable")

    # Voucher type based on settlement type
    if settlement.settlement_type == Settlement.RECEIPT:
        vtype = Voucher.RECEIPT
        narration = f"Receipt - {settlement.party}"
    elif settlement.settlement_type == Settlement.PAYMENT:
        vtype = Voucher.PAYMENT
        narration = f"Payment - {settlement.party}"
    else:
        raise ValueError("Invalid settlement type. Use RECEIPT or PAYMENT.")

    voucher = Voucher.objects.create(
        date=getattr(settlement, "date", None) or timezone.now().date(),
        voucher_type=vtype,
        narration=narration,
        ref_model="Settlement",
        ref_id=settlement.id,
    )

    # ------------------------
    # RECEIPT (Customer paid)
    # ------------------------
    if settlement.settlement_type == Settlement.RECEIPT:
        # Accounting
        VoucherLine.objects.create(voucher=voucher, account=money, debit=amount)
        VoucherLine.objects.create(voucher=voucher, account=ar, credit=amount)

        # Party Ledger (customer paying reduces receivable)
        post_party_ledger(
            voucher=voucher,
            party=settlement.party,
            debit=Decimal("0"),
            credit=amount
        )

    # ------------------------
    # PAYMENT (Supplier paid)
    # ------------------------
    else:
        # Accounting
        VoucherLine.objects.create(voucher=voucher, account=ap, debit=amount)
        VoucherLine.objects.create(voucher=voucher, account=money, credit=amount)

        # Party Ledger (paying supplier reduces payable)
        post_party_ledger(
            voucher=voucher,
            party=settlement.party,
            debit=amount,
            credit=Decimal("0")
        )

    # Optional if you have Voucher.clean() validations
    if hasattr(voucher, "clean"):
        voucher.clean()

    settlement.voucher = voucher
    settlement.save(update_fields=["voucher"])

    return voucher


# -------------------------
# Convenience Creators (UI will call these later)
# -------------------------

@transaction.atomic
def create_receipt(*, party, amount, payment_method, date=None) -> Voucher:
    """
    Easy entry point for: Receive money from customer.
    Creates Settlement row then posts it.
    """
    s = Settlement.objects.create(
        settlement_type=Settlement.RECEIPT,
        payment_method=payment_method,
        date=date or timezone.now().date(),
        party=party,
        amount=_to_decimal(amount),
    )
    return post_settlement(s)


@transaction.atomic
def create_payment(*, party, amount, payment_method, date=None) -> Voucher:
    """
    Easy entry point for: Pay money to supplier.
    Creates Settlement row then posts it.
    """
    s = Settlement.objects.create(
        settlement_type=Settlement.PAYMENT,
        payment_method=payment_method,
        date=date or timezone.now().date(),
        party=party,
        amount=_to_decimal(amount),
    )
    return post_settlement(s)


