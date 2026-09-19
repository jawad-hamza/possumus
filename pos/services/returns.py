from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.models import Account, PartyLedgerEntry, Voucher, VoucherLine
from accounts.services.audit import record_audit
from accounts.services.cash_bank import resolve_money_account
from accounts.services.party_balance import get_party_balance
from pos.models import (
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Sale,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
    format_qty,
    quantize_money,
    quantize_qty,
)


ZERO = Decimal("0.00")


def _ensure_account(title: str, account_type: str) -> Account:
    account, _ = Account.objects.get_or_create(title=title, defaults={"type": account_type})
    if account.type != account_type:
        raise ValueError(f"Account '{title}' exists with the wrong type.")
    if not account.is_active:
        account.is_active = True
        account.save(update_fields=["is_active"])
    return account


def _sale_item_returnable(item: SaleItem) -> Decimal:
    returned = item.return_items.aggregate(total=Sum("quantity"))["total"] or ZERO
    return max(item.quantity - returned, ZERO)


def _purchase_item_returnable(item: PurchaseItem) -> Decimal:
    returned = item.return_items.aggregate(total=Sum("quantity"))["total"] or ZERO
    return max(item.quantity - returned, ZERO)


def _return_quantity(product, raw_quantity) -> Decimal:
    try:
        quantity = quantize_qty(raw_quantity or 0)
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ValueError(f"Enter a valid return quantity for {product.display_name}.") from exc
    if quantity > ZERO:
        quantity_error = product.validate_quantity(quantity)
        if quantity_error:
            raise ValueError(quantity_error)
    return quantity


@transaction.atomic
def create_sale_return(*, sale, quantities, refund_method, reason, notes="", bank_account=None, actor=None):
    sale_obj = (
        Sale.objects.select_related("customer")
        .prefetch_related("items__product__category", "items__return_items")
        .select_for_update(of=("self",))
        .get(pk=getattr(sale, "pk", sale))
    )

    if not (reason or "").strip():
        raise ValueError("Reason is required for sale return.")
    if refund_method == SaleReturn.RECEIVABLE and sale_obj.customer is None:
        raise ValueError("A customer is required to reduce receivable.")
    if refund_method != SaleReturn.RECEIVABLE and sale_obj.is_credit and sale_obj.customer is not None:
        # Paying cash back on an unpaid credit sale would leave the receivable open.
        outstanding = get_party_balance(sale_obj.customer)
        if outstanding > ZERO:
            raise ValueError(
                f"{sale_obj.customer.name} still owes {outstanding:.2f} on credit. "
                "Reduce the receivable instead of refunding money."
            )

    sale_return = SaleReturn.objects.create(
        sale=sale_obj,
        customer=sale_obj.customer,
        refund_method=refund_method,
        bank_account=bank_account,
        reason=reason.strip(),
        notes=notes,
        created_by=actor,
    )

    discount_factor = Decimal("1") - (Decimal(sale_obj.discount_rate or 0) / Decimal("100"))
    total_net = ZERO
    total_tax = ZERO
    total_cost = ZERO

    items_map = {item.id: item for item in sale_obj.items.all()}
    for item_id, qty in quantities.items():
        item = items_map.get(int(item_id))
        if item is None:
            if quantize_qty(qty or 0) <= ZERO:
                continue
            raise ValueError("One of the sale items could not be found.")
        quantity = _return_quantity(item.product, qty)
        if quantity <= ZERO:
            continue
        returnable = _sale_item_returnable(item)
        if quantity > returnable:
            raise ValueError(f"Only {format_qty(returnable)} {item.product.unit} of {item.product.display_name} can be returned.")

        line_amount = quantize_money(item.line_total * Decimal(quantity) / Decimal(item.quantity))
        net_amount = quantize_money(line_amount * discount_factor)
        if sale_obj.tax_mode == Sale.TAX_MODE_MANUAL:
            tax_amount = quantize_money(net_amount * (Decimal(sale_obj.tax_rate or 0) / Decimal("100")))
        else:
            rate = Decimal(item.tax_rate_for_payment_method(sale_obj.payment_method))
            tax_amount = quantize_money(net_amount * (rate / Decimal("100")))
        cost_amount = quantize_money(Decimal(item.cost_price or 0) * Decimal(quantity))

        SaleReturnItem.objects.create(
            sale_return=sale_return,
            sale_item=item,
            product=item.product,
            quantity=quantity,
            unit_price=item.discounted_unit_price,
            line_amount=line_amount,
            net_amount=net_amount,
            tax_amount=tax_amount,
            cost_amount=cost_amount,
        )

        item.product.apply_stock_change(quantity)

        total_net += net_amount
        total_tax += tax_amount
        total_cost += cost_amount

    if not sale_return.items.exists():
        raise ValueError("Select at least one return quantity.")

    total_amount = quantize_money(total_net + total_tax)
    sale_return.net_amount = quantize_money(total_net)
    sale_return.tax_amount = quantize_money(total_tax)
    sale_return.total_amount = total_amount
    sale_return.cost_amount = quantize_money(total_cost)

    voucher = Voucher.objects.create(
        voucher_type=Voucher.JOURNAL,
        date=timezone.localdate(),
        narration=f"Sale return #{sale_return.id} for sale {sale_obj.display_number}",
        ref_model="SaleReturn",
        ref_id=sale_return.id,
    )
    VoucherLine.objects.create(voucher=voucher, account=_ensure_account("Sales Returns", Account.INCOME), debit=sale_return.net_amount)
    if sale_return.tax_amount > ZERO:
        VoucherLine.objects.create(voucher=voucher, account=_ensure_account("Sales Tax Payable", Account.LIABILITY), debit=sale_return.tax_amount)

    if refund_method == SaleReturn.RECEIVABLE:
        VoucherLine.objects.create(voucher=voucher, account=_ensure_account("Accounts Receivable", Account.ASSET), credit=total_amount)
        PartyLedgerEntry.objects.create(
            voucher=voucher,
            content_type=ContentType.objects.get_for_model(sale_obj.customer),
            object_id=sale_obj.customer.id,
            credit=total_amount,
        )
    else:
        money_account = resolve_money_account(refund_method, bank_account)
        VoucherLine.objects.create(voucher=voucher, account=money_account, credit=total_amount)

    if sale_return.cost_amount > ZERO:
        VoucherLine.objects.create(voucher=voucher, account=_ensure_account("Inventory", Account.ASSET), debit=sale_return.cost_amount)
        VoucherLine.objects.create(voucher=voucher, account=_ensure_account("Cost of Goods Sold", Account.EXPENSE), credit=sale_return.cost_amount)

    voucher.clean()
    sale_return.voucher = voucher
    sale_return.save(update_fields=["net_amount", "tax_amount", "total_amount", "cost_amount", "voucher"])
    record_audit(
        action="sale_return_created",
        description=f"Sale return #{sale_return.id} recorded for sale {sale_obj.display_number}.",
        actor=actor,
        target=sale_return,
        metadata={"total_amount": str(sale_return.total_amount)},
    )
    return sale_return


@transaction.atomic
def create_purchase_return(*, purchase, quantities, settlement_method, reason, notes="", bank_account=None, actor=None):
    purchase_obj = (
        Purchase.objects.select_related("supplier")
        .prefetch_related("items__product", "items__return_items")
        .select_for_update(of=("self",))
        .get(pk=getattr(purchase, "pk", purchase))
    )

    if not (reason or "").strip():
        raise ValueError("Reason is required for purchase return.")
    if settlement_method == PurchaseReturn.PAYABLE and purchase_obj.supplier is None:
        raise ValueError("A supplier is required to reduce payable.")

    purchase_return = PurchaseReturn.objects.create(
        purchase=purchase_obj,
        supplier=purchase_obj.supplier,
        settlement_method=settlement_method,
        bank_account=bank_account,
        reason=reason.strip(),
        notes=notes,
        created_by=actor,
    )

    total_amount = ZERO
    items_map = {item.id: item for item in purchase_obj.items.all()}
    for item_id, qty in quantities.items():
        item = items_map.get(int(item_id))
        if item is None:
            if quantize_qty(qty or 0) <= ZERO:
                continue
            raise ValueError("One of the purchase items could not be found.")
        quantity = _return_quantity(item.product, qty)
        if quantity <= ZERO:
            continue
        returnable = _purchase_item_returnable(item)
        if quantity > returnable:
            raise ValueError(f"Only {format_qty(returnable)} {item.product.unit} of {item.product.display_name} can be returned.")

        line_amount = quantize_money(Decimal(item.cost_price or 0) * Decimal(quantity))
        PurchaseReturnItem.objects.create(
            purchase_return=purchase_return,
            purchase_item=item,
            product=item.product,
            quantity=quantity,
            line_amount=line_amount,
        )
        try:
            item.product.apply_stock_change(-quantity)
        except ValidationError as exc:
            raise ValueError(f"Current stock of {item.product.display_name} is not enough for this return.") from exc
        total_amount += line_amount

    if not purchase_return.items.exists():
        raise ValueError("Select at least one return quantity.")

    purchase_return.total_amount = quantize_money(total_amount)
    voucher = Voucher.objects.create(
        voucher_type=Voucher.JOURNAL,
        date=timezone.localdate(),
        narration=f"Purchase return #{purchase_return.id} for purchase #{purchase_obj.id}",
        ref_model="PurchaseReturn",
        ref_id=purchase_return.id,
    )

    if settlement_method == PurchaseReturn.PAYABLE:
        VoucherLine.objects.create(voucher=voucher, account=_ensure_account("Accounts Payable", Account.LIABILITY), debit=purchase_return.total_amount)
        PartyLedgerEntry.objects.create(
            voucher=voucher,
            content_type=ContentType.objects.get_for_model(purchase_obj.supplier),
            object_id=purchase_obj.supplier.id,
            debit=purchase_return.total_amount,
        )
    else:
        money_account = resolve_money_account(settlement_method, bank_account)
        VoucherLine.objects.create(voucher=voucher, account=money_account, debit=purchase_return.total_amount)

    VoucherLine.objects.create(voucher=voucher, account=_ensure_account("Inventory", Account.ASSET), credit=purchase_return.total_amount)
    voucher.clean()
    purchase_return.voucher = voucher
    purchase_return.save(update_fields=["total_amount", "voucher"])
    record_audit(
        action="purchase_return_created",
        description=f"Purchase return #{purchase_return.id} recorded for purchase #{purchase_obj.id}.",
        actor=actor,
        target=purchase_return,
        metadata={"total_amount": str(purchase_return.total_amount)},
    )
    return purchase_return

