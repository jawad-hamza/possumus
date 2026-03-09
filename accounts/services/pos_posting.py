"""
POS accounting entry point.

Sales and purchases are synchronized here so the same sale/purchase can be
re-posted after edits without leaving duplicate vouchers behind.
"""

from decimal import Decimal, InvalidOperation

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from accounts.models import Account, PartyLedgerEntry, Voucher, VoucherLine
from accounts.services.cash_bank import resolve_money_account
from pos.models import PurchaseItem, SaleItem


def _acc(title: str) -> Account:
    try:
        return Account.objects.get(title=title, is_active=True)
    except Account.DoesNotExist as exc:
        raise ValueError(f"Required account not found or inactive: {title}") from exc


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid monetary value") from exc


def _money_label(payment_method: str, bank_account=None) -> str:
    if payment_method == "cash":
        return "Cash"
    if bank_account is not None:
        return bank_account.display_name
    return "Bank"


def _sync_voucher(*, voucher_type: str, ref_model: str, ref_id: int, narration: str, date) -> Voucher:
    voucher, created = Voucher.objects.get_or_create(
        voucher_type=voucher_type,
        ref_model=ref_model,
        ref_id=ref_id,
        defaults={
            "narration": narration,
            "date": date,
        },
    )

    if not created:
        voucher.narration = narration
        voucher.date = date
        voucher.save(update_fields=["narration", "date"])
        voucher.lines.all().delete()
        voucher.party_entries.all().delete()

    return voucher


def _sale_cost_total(sale_id: int) -> Decimal:
    total = Decimal("0")
    for item in SaleItem.objects.filter(sale_id=sale_id).select_related("product"):
        total += Decimal(item.cost_price) * Decimal(item.quantity)
    return total


def _purchase_inventory_total(purchase_id: int) -> Decimal:
    total = Decimal("0")
    for item in PurchaseItem.objects.filter(purchase_id=purchase_id).select_related("product"):
        total += Decimal(item.cost_price) * Decimal(item.quantity)
    return total


@transaction.atomic
def post_sale_from_pos(
    *,
    sale_id: int,
    customer,
    total_amount,
    paid_amount,
    payment_method: str,
    bank_account=None,
    sale_date=None,
    tax_amount=0,
):
    assert sale_id is not None, "POS sale_id is required"

    total = _to_decimal(total_amount)
    paid = _to_decimal(paid_amount or 0)
    tax = _to_decimal(tax_amount or 0)

    if total <= 0:
        raise ValueError("Sale total must be greater than zero")
    if paid < 0:
        raise ValueError("Paid amount cannot be negative")
    if paid > total:
        raise ValueError("Paid amount cannot exceed sale total")
    if tax < 0:
        raise ValueError("Tax amount cannot be negative")
    if tax > total:
        raise ValueError("Tax amount cannot exceed sale total")

    credit = total - paid
    net_sales = total - tax
    if credit > 0 and customer is None:
        raise ValueError("Credit sale requires a customer")

    inventory_cost = _sale_cost_total(sale_id)
    money_account = resolve_money_account(payment_method, bank_account)
    voucher = _sync_voucher(
        voucher_type=Voucher.SALE,
        ref_model="Sale",
        ref_id=sale_id,
        narration=f"Sale #{sale_id} via {_money_label(payment_method, bank_account)}",
        date=sale_date,
    )

    if paid > 0:
        VoucherLine.objects.create(
            voucher=voucher,
            account=money_account,
            debit=paid,
        )

    if credit > 0:
        VoucherLine.objects.create(
            voucher=voucher,
            account=_acc("Accounts Receivable"),
            debit=credit,
        )
        PartyLedgerEntry.objects.create(
            voucher=voucher,
            content_type=ContentType.objects.get_for_model(customer),
            object_id=customer.id,
            debit=credit,
        )

    VoucherLine.objects.create(
        voucher=voucher,
        account=_acc("Sales"),
        credit=net_sales,
    )

    if tax > 0:
        VoucherLine.objects.create(
            voucher=voucher,
            account=_acc("Sales Tax Payable"),
            credit=tax,
        )

    if inventory_cost > 0:
        VoucherLine.objects.create(
            voucher=voucher,
            account=_acc("Cost of Goods Sold"),
            debit=inventory_cost,
        )
        VoucherLine.objects.create(
            voucher=voucher,
            account=_acc("Inventory"),
            credit=inventory_cost,
        )

    voucher.clean()
    return voucher


@transaction.atomic
def post_purchase_from_pos(
    *,
    purchase_id: int,
    supplier,
    total_amount,
    paid_amount,
    payment_method: str,
    bank_account=None,
    purchase_date=None,
):
    assert purchase_id is not None, "POS purchase_id is required"

    total = _to_decimal(total_amount)
    paid = _to_decimal(paid_amount or 0)

    if total <= 0:
        raise ValueError("Purchase total must be greater than zero")
    if paid < 0:
        raise ValueError("Paid amount cannot be negative")
    if paid > total:
        raise ValueError("Paid amount cannot exceed purchase total")

    credit = total - paid
    if credit > 0 and supplier is None:
        raise ValueError("Credit purchase requires a supplier")

    inventory_total = _purchase_inventory_total(purchase_id) or total
    money_account = resolve_money_account(payment_method, bank_account)
    voucher = _sync_voucher(
        voucher_type=Voucher.PURCHASE,
        ref_model="Purchase",
        ref_id=purchase_id,
        narration=f"Purchase #{purchase_id} via {_money_label(payment_method, bank_account)}",
        date=purchase_date,
    )

    VoucherLine.objects.create(
        voucher=voucher,
        account=_acc("Inventory"),
        debit=inventory_total,
    )

    if paid > 0:
        VoucherLine.objects.create(
            voucher=voucher,
            account=money_account,
            credit=paid,
        )

    if credit > 0:
        VoucherLine.objects.create(
            voucher=voucher,
            account=_acc("Accounts Payable"),
            credit=credit,
        )
        PartyLedgerEntry.objects.create(
            voucher=voucher,
            content_type=ContentType.objects.get_for_model(supplier),
            object_id=supplier.id,
            credit=credit,
        )

    voucher.clean()
    return voucher
