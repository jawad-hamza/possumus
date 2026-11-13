"""Stock returns that do not name one invoice or purchase bill.

**Market return (from a customer).** The shop gives goods back. The customer is credited
the rate typed for each line (Dr Sales Returns, Cr Accounts Receivable and the customer's
ledger, or Cr Cash / Bank when the money is paid out). The goods themselves:

* good stock goes back on the shelf: stock goes up, Dr Inventory, Cr Cost of Goods Sold at
  average cost, exactly as a sale return does;
* damaged or expired stock is never put back into saleable stock. It is claimable from the
  company, so it is kept as an asset of its own: Dr Damaged Stock, Cr Cost of Goods Sold at
  average cost, with its quantity kept on the return line. The Damaged Stock and Claims
  report lists what is held per product.

**Return to supplier (company return).** Goods go back to the company. Good stock leaves the
shelf (Cr Inventory at average cost); damaged stock leaves the Damaged Stock held for claims
(Cr Damaged Stock at the cost it was taken in at). The supplier is charged the rate typed
(Dr Accounts Payable and the supplier's ledger, or Dr Cash / Bank when they pay it back).
When that rate differs from the stock value, the difference is an Inventory Adjustment Gain
or Loss, so the voucher always balances and stock value stays quantity x average cost.

No tax is worked out: a market return has no invoice to take a tax rate from.
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone

from accounts.models import Account, PartyLedgerEntry, Voucher, VoucherLine
from accounts.services.audit import record_audit
from accounts.services.cash_bank import resolve_money_account
from pos.models import Product, format_qty, quantize_money, quantize_qty

from ..models import StockReturn, StockReturnItem
from .common import ZERO, DistributionError, ensure_account

DAMAGED_STOCK = "Damaged Stock"
QTY_FIELD = DecimalField(max_digits=18, decimal_places=3)
MONEY_FIELD = DecimalField(max_digits=18, decimal_places=2)


def damaged_held(product_ids=None, *, exclude_return=None):
    """{product id: (base quantity, cost)} of damaged goods taken in and not yet sent back."""
    rows = StockReturnItem.objects.filter(condition=StockReturnItem.DAMAGED)
    if product_ids is not None:
        rows = rows.filter(product_id__in=product_ids)
    if exclude_return is not None:
        rows = rows.exclude(stock_return=exclude_return)
    base = ExpressionWrapper(F("quantity") * F("unit_factor"), output_field=QTY_FIELD)
    totals = rows.values("product_id").annotate(
        qty_in=Sum(base, filter=Q(stock_return__kind=StockReturn.FROM_CUSTOMER), output_field=QTY_FIELD),
        qty_out=Sum(base, filter=Q(stock_return__kind=StockReturn.TO_SUPPLIER), output_field=QTY_FIELD),
        cost_in=Sum("cost_amount", filter=Q(stock_return__kind=StockReturn.FROM_CUSTOMER), output_field=MONEY_FIELD),
        cost_out=Sum("cost_amount", filter=Q(stock_return__kind=StockReturn.TO_SUPPLIER), output_field=MONEY_FIELD),
    )
    held = {}
    for row in totals:
        qty = quantize_qty(Decimal(row["qty_in"] or 0) - Decimal(row["qty_out"] or 0))
        cost = quantize_money(Decimal(row["cost_in"] or 0) - Decimal(row["cost_out"] or 0))
        held[row["product_id"]] = (qty, cost)
    return held


def _party_ledger(voucher, party, *, debit=ZERO, credit=ZERO):
    PartyLedgerEntry.objects.create(
        voucher=voucher,
        content_type=ContentType.objects.get_for_model(party),
        object_id=party.pk,
        debit=debit,
        credit=credit,
    )


@transaction.atomic
def create_stock_return(
    *,
    kind,
    lines,
    reason,
    settlement=StockReturn.ON_ACCOUNT,
    customer=None,
    supplier=None,
    bank_account=None,
    return_date=None,
    notes="",
    load_form=None,
    user=None,
):
    """``lines`` are dicts: product, quantity, unit_level, unit_factor, rate, condition."""
    if kind not in (StockReturn.FROM_CUSTOMER, StockReturn.TO_SUPPLIER):
        raise DistributionError("Choose whether the goods come from a customer or go back to a supplier.")
    reason = (reason or "").strip()
    if not reason:
        raise DistributionError("Say why the goods are being returned.")
    if not lines:
        raise DistributionError("Add at least one product.")
    if settlement not in dict(StockReturn.SETTLEMENT_CHOICES):
        raise DistributionError("Choose how the return is settled.")
    party = customer if kind == StockReturn.FROM_CUSTOMER else supplier
    if kind == StockReturn.FROM_CUSTOMER:
        supplier = None
    else:
        customer = None
    if settlement == StockReturn.ON_ACCOUNT and party is None:
        who = "customer" if kind == StockReturn.FROM_CUSTOMER else "supplier"
        raise DistributionError(f"Choose the {who} whose account the return goes to, or settle it in cash.")
    if settlement != StockReturn.BANK:
        bank_account = None

    stock_return = StockReturn.objects.create(
        kind=kind,
        customer=customer,
        supplier=supplier,
        return_date=return_date or timezone.localdate(),
        settlement=settlement,
        bank_account=bank_account,
        reason=reason[:150],
        notes=(notes or "").strip()[:255],
        load_form=load_form,
        created_by=user,
    )

    product_ids = {line["product"].pk for line in lines}
    locked = Product.objects.select_for_update().in_bulk(product_ids)
    held = damaged_held(product_ids) if kind == StockReturn.TO_SUPPLIER else {}
    good_cost = ZERO
    damaged_cost = ZERO
    total = ZERO
    for line in lines:
        product = locked[line["product"].pk]
        level = line["unit_level"]
        factor = Decimal(line.get("unit_factor") or product.pack_factor(level))
        quantity = quantize_qty(line["quantity"])
        base = quantize_qty(quantity * factor)
        condition = line.get("condition") or StockReturnItem.SALEABLE
        if condition not in (StockReturnItem.SALEABLE, StockReturnItem.DAMAGED):
            condition = StockReturnItem.SALEABLE
        amount = quantize_money(Decimal(line["rate"]) * quantity)
        tracked = bool(product.track_stock)

        if kind == StockReturn.FROM_CUSTOMER:
            cost = quantize_money(Decimal(product.cost_price or 0) * base) if tracked else ZERO
            if condition == StockReturnItem.SALEABLE and tracked:
                product.apply_stock_change(base)
        elif condition == StockReturnItem.SALEABLE:
            cost = quantize_money(Decimal(product.cost_price or 0) * base) if tracked else ZERO
            if tracked:
                try:
                    product.apply_stock_change(-base)
                except ValidationError as exc:
                    raise DistributionError(
                        f"Only {product.stock_display} of {product.display_name} is in stock to send back."
                    ) from exc
        else:
            held_qty, held_cost = held.get(product.pk, (ZERO, ZERO))
            if base > held_qty:
                have = product.format_quantity(held_qty) if product.has_packaging else format_qty(held_qty)
                raise DistributionError(
                    f"Only {have} of damaged {product.display_name} is held for a claim."
                )
            # The damaged pool leaves at the cost it came in at, so it empties to exactly zero.
            cost = quantize_money(held_cost * base / held_qty) if held_qty else ZERO
            held[product.pk] = (quantize_qty(held_qty - base), quantize_money(held_cost - cost))

        StockReturnItem.objects.create(
            stock_return=stock_return,
            product=product,
            quantity=quantity,
            unit_level=level,
            unit_factor=factor,
            rate=quantize_money(line["rate"]),
            line_amount=amount,
            cost_amount=cost,
            condition=condition,
        )
        total += amount
        if condition == StockReturnItem.DAMAGED:
            damaged_cost += cost
        else:
            good_cost += cost

    total = quantize_money(total)
    cost_total = quantize_money(good_cost + damaged_cost)
    stock_return.total_amount = total
    stock_return.cost_amount = cost_total

    voucher = Voucher.objects.create(
        voucher_type=Voucher.JOURNAL,
        date=stock_return.return_date,
        narration=(
            f"Market return {stock_return.display_number} from {stock_return.party_name}"
            if kind == StockReturn.FROM_CUSTOMER
            else f"Return {stock_return.display_number} to {stock_return.party_name}"
        ),
        ref_model="StockReturn",
        ref_id=stock_return.pk,
        created_by=user,
    )
    inventory = ensure_account("Inventory", Account.ASSET)
    damaged_account = ensure_account(DAMAGED_STOCK, Account.ASSET) if damaged_cost > 0 else None

    def line(account, debit=ZERO, credit=ZERO):
        if debit > 0 or credit > 0:
            VoucherLine.objects.create(voucher=voucher, account=account, debit=debit, credit=credit)

    if kind == StockReturn.FROM_CUSTOMER:
        line(ensure_account("Sales Returns", Account.INCOME), debit=total)
        if settlement == StockReturn.ON_ACCOUNT:
            line(ensure_account("Accounts Receivable", Account.ASSET), credit=total)
            if total > 0:
                _party_ledger(voucher, customer, credit=total)
        else:
            line(resolve_money_account(settlement, bank_account), credit=total)
        line(inventory, debit=quantize_money(good_cost))
        if damaged_account is not None:
            line(damaged_account, debit=quantize_money(damaged_cost))
        line(ensure_account("Cost of Goods Sold", Account.EXPENSE), credit=cost_total)
    else:
        if settlement == StockReturn.ON_ACCOUNT:
            line(ensure_account("Accounts Payable", Account.LIABILITY), debit=total)
            if total > 0:
                _party_ledger(voucher, supplier, debit=total)
        else:
            line(resolve_money_account(settlement, bank_account), debit=total)
        line(inventory, credit=quantize_money(good_cost))
        if damaged_account is not None:
            line(damaged_account, credit=quantize_money(damaged_cost))
        difference = quantize_money(total - cost_total)
        if difference > 0:
            line(ensure_account("Inventory Adjustment Gain", Account.INCOME), credit=difference)
        elif difference < 0:
            line(ensure_account("Inventory Adjustment Loss", Account.EXPENSE), debit=-difference)

    if not voucher.lines.exists():
        voucher.delete()
        voucher = None
    else:
        voucher.clean()
    stock_return.voucher = voucher
    stock_return.save(update_fields=["total_amount", "cost_amount", "voucher"])
    record_audit(
        action="stock_return_created",
        description=f"{stock_return.get_kind_display()} {stock_return.display_number} recorded for {stock_return.party_name}.",
        actor=user,
        target=stock_return,
        metadata={
            "kind": kind,
            "total_amount": str(total),
            "cost_amount": str(cost_total),
            "damaged_cost": str(quantize_money(damaged_cost)),
        },
    )
    return stock_return
