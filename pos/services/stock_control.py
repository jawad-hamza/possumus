from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounts.models import Account, Voucher, VoucherLine
from accounts.services.audit import record_audit
from pos.models import Product, StockAdjustment, quantize_money


ZERO = Decimal("0.00")


def _ensure_account(title: str, account_type: str) -> Account:
    account, _ = Account.objects.get_or_create(title=title, defaults={"type": account_type})
    if account.type != account_type:
        raise ValueError(f"Account '{title}' exists with the wrong type.")
    if not account.is_active:
        account.is_active = True
        account.save(update_fields=["is_active"])
    return account


@transaction.atomic
def create_stock_adjustment(*, product, adjustment_mode, reason, quantity_after=None, quantity_delta=None, notes="", actor=None):
    product_obj = Product.objects.select_for_update().get(pk=getattr(product, "pk", product))
    quantity_before = int(product_obj.stock or 0)

    if not (reason or "").strip():
        raise ValueError("Reason is required for stock adjustment.")

    if adjustment_mode == StockAdjustment.STOCK_TAKE:
        if quantity_after in (None, ""):
            raise ValueError("Counted stock is required for stock take.")
        quantity_after = int(quantity_after)
        if quantity_after < 0:
            raise ValueError("Counted stock cannot be negative.")
        quantity_delta = quantity_after - quantity_before
    else:
        if quantity_delta in (None, ""):
            raise ValueError("Adjustment quantity is required.")
        quantity_delta = int(quantity_delta)
        quantity_after = quantity_before + quantity_delta
        if quantity_after < 0:
            raise ValueError("Adjustment would reduce stock below zero.")

    adjustment = StockAdjustment.objects.create(
        product=product_obj,
        adjustment_mode=adjustment_mode,
        quantity_before=quantity_before,
        quantity_after=quantity_after,
        quantity_delta=quantity_delta,
        reason=reason.strip(),
        notes=notes,
        created_by=actor,
    )

    product_obj.stock = quantity_after
    product_obj.save(update_fields=["stock"])

    value = quantize_money(Decimal(abs(quantity_delta)) * Decimal(product_obj.cost_price or 0))
    if quantity_delta != 0 and value > ZERO:
        inventory_account = _ensure_account("Inventory", Account.ASSET)
        if quantity_delta > 0:
            offset_account = _ensure_account("Inventory Adjustment Gain", Account.INCOME)
        else:
            offset_account = _ensure_account("Inventory Adjustment Loss", Account.EXPENSE)

        voucher = Voucher.objects.create(
            voucher_type=Voucher.JOURNAL,
            date=timezone.localdate(),
            narration=f"Stock adjustment #{adjustment.id} - {product_obj.name}",
            ref_model="StockAdjustment",
            ref_id=adjustment.id,
        )
        if quantity_delta > 0:
            VoucherLine.objects.create(voucher=voucher, account=inventory_account, debit=value)
            VoucherLine.objects.create(voucher=voucher, account=offset_account, credit=value)
        else:
            VoucherLine.objects.create(voucher=voucher, account=offset_account, debit=value)
            VoucherLine.objects.create(voucher=voucher, account=inventory_account, credit=value)
        voucher.clean()
        adjustment.voucher = voucher
        adjustment.save(update_fields=["voucher"])

    record_audit(
        action="stock_adjusted",
        description=f"Stock for '{product_obj.name}' changed from {quantity_before} to {quantity_after}.",
        actor=actor,
        target=adjustment,
        metadata={"quantity_delta": quantity_delta, "adjustment_mode": adjustment_mode},
    )
    return adjustment
