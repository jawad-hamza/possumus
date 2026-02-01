"""
POS ACCOUNTING ENTRY POINT
=========================

This module is the ONLY place where POS transactions
(Sale / Purchase) are posted into accounting.

Rules:
- Supports cash, credit, and partial payments
- Automatically creates AR / AP
- Updates party sub-ledger
- ONE voucher per POS transaction

DO NOT:
- Create AccountingEvent for POS
- Post POS vouchers anywhere else

If you need to change POS accounting logic,
change it HERE.
"""

from decimal import Decimal, InvalidOperation
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from accounts.models import (
    Voucher, VoucherLine, PartyLedgerEntry, Account
)


# -------------------------
# Helpers
# -------------------------

def _acc(title: str) -> Account:
    """
    Resolve an active account by title.
    Fail loudly if accounting setup is broken.
    """
    try:
        return Account.objects.get(title=title, is_active=True)
    except Account.DoesNotExist:
        raise ValueError(f"Required account not found or inactive: {title}")


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError("Invalid monetary value")


# -------------------------
# SALE POSTING (POS)
# -------------------------

@transaction.atomic
def post_sale_from_pos(
    *,
    sale_id: int,
    customer,
    total_amount,
    paid_amount,
    payment_method: str,   # "cash" | "bank"
):
        # SAFETY: POS sale must always have an ID
    assert sale_id is not None, "POS sale_id is required"
    """
    POS Sale Posting Rules:
    - Always post FULL sale amount to Sales
    - Paid portion → Cash/Bank
    - Unpaid portion → Accounts Receivable + Party Ledger
    """

    total = _to_decimal(total_amount)
    paid = _to_decimal(paid_amount or 0)

    if total <= 0:
        raise ValueError("Sale total must be greater than zero")

    if paid < 0:
        raise ValueError("Paid amount cannot be negative")

    if paid > total:
        raise ValueError("Paid amount cannot exceed sale total")

    credit = total - paid

    voucher = Voucher.objects.create(
        voucher_type=Voucher.SALE,
        ref_model="Sale",
        ref_id=sale_id,
    )

    # 1️⃣ Cash / Bank received
    if paid > 0:
        cash_account = (
            _acc("Cash") if payment_method == "cash"
            else _acc("Bank")
        )
        VoucherLine.objects.create(
            voucher=voucher,
            account=cash_account,
            debit=paid
        )

    # 2️⃣ Credit portion → Accounts Receivable
    if credit > 0:
        ar = _acc("Accounts Receivable")

        VoucherLine.objects.create(
            voucher=voucher,
            account=ar,
            debit=credit
        )

        PartyLedgerEntry.objects.create(
            voucher=voucher,
            content_type=ContentType.objects.get_for_model(customer),
            object_id=customer.id,
            debit=credit
        )

    # 3️⃣ Sales income (FULL amount always)
    VoucherLine.objects.create(
        voucher=voucher,
        account=_acc("Sales"),
        credit=total
    )

    return voucher


# -------------------------
# PURCHASE POSTING (POS)
# -------------------------

@transaction.atomic
def post_purchase_from_pos(
    *,
    purchase_id: int,
    supplier,
    total_amount,
    paid_amount,
    payment_method: str,   # "cash" | "bank"
):
        # SAFETY: POS purchase must always have an ID
    assert purchase_id is not None, "POS purchase_id is required"
    """
    POS Purchase Posting Rules:
    - Always post FULL purchase amount to Inventory/Expense
    - Paid portion → Cash/Bank
    - Unpaid portion → Accounts Payable + Party Ledger
    """

    total = _to_decimal(total_amount)
    paid = _to_decimal(paid_amount or 0)

    if total <= 0:
        raise ValueError("Purchase total must be greater than zero")

    if paid < 0:
        raise ValueError("Paid amount cannot be negative")

    if paid > total:
        raise ValueError("Paid amount cannot exceed purchase total")

    credit = total - paid

    voucher = Voucher.objects.create(
        voucher_type=Voucher.PURCHASE,
        ref_model="Purchase",
        ref_id=purchase_id,
    )

    # 1️⃣ Inventory / Expense (FULL amount)
    VoucherLine.objects.create(
        voucher=voucher,
        account=_acc("Inventory"),  # or Expense account
        debit=total
    )

    # 2️⃣ Cash / Bank paid
    if paid > 0:
        cash_account = (
            _acc("Cash") if payment_method == "cash"
            else _acc("Bank")
        )
        VoucherLine.objects.create(
            voucher=voucher,
            account=cash_account,
            credit=paid
        )

    # 3️⃣ Credit portion → Accounts Payable
    if credit > 0:
        ap = _acc("Accounts Payable")

        VoucherLine.objects.create(
            voucher=voucher,
            account=ap,
            credit=credit
        )

        PartyLedgerEntry.objects.create(
            voucher=voucher,
            content_type=ContentType.objects.get_for_model(supplier),
            object_id=supplier.id,
            credit=credit
        )

    return voucher