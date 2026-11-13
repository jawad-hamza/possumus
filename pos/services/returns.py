from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import Account, PartyLedgerEntry, Voucher, VoucherLine
from accounts.services.audit import record_audit
from accounts.services.cash_bank import resolve_money_account
from accounts.services.party_balance import get_party_balance
from pos.models import (
    PACK_LEVELS,
    PACK_PIECE,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Sale,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
    format_qty,
    normalize_pack_level,
    quantize_money,
    quantize_qty,
)
from pos.services.discounts import returned_net


ZERO = Decimal("0.00")


def _ensure_account(title: str, account_type: str) -> Account:
    account, _ = Account.objects.get_or_create(title=title, defaults={"type": account_type})
    if account.type != account_type:
        raise ValueError(f"Account '{title}' exists with the wrong type.")
    if not account.is_active:
        account.is_active = True
        account.save(update_fields=["is_active"])
    return account


def returned_base_quantity(item) -> Decimal:
    """What has already come back on this line, in the product's base unit."""
    return sum((row.base_quantity for row in item.return_items.all()), ZERO)


def _sale_item_returnable(item: SaleItem) -> Decimal:
    """Still returnable on this sale line, in the product's base unit."""
    return max(item.base_quantity - returned_base_quantity(item), ZERO)


def _purchase_item_returnable(item: PurchaseItem) -> Decimal:
    """Still returnable on this purchase line, in the product's base unit."""
    return max(item.base_quantity - returned_base_quantity(item), ZERO)


def return_unit_choices(item):
    """The units goods on this line can come back in: the line's own unit and every smaller one."""
    line_level = normalize_pack_level(item.unit_level)
    line_index = PACK_LEVELS.index(line_level)
    choices = []
    for level in item.product.pack_levels:
        if PACK_LEVELS.index(level["code"]) < line_index:
            continue
        # The line's own unit keeps the size it had when the line was written.
        factor = item.unit_factor if level["code"] == line_level else level["factor"]
        choices.append({"code": level["code"], "label": level["label"], "factor": Decimal(factor)})
    if not any(choice["code"] == line_level for choice in choices):
        # The product's packing changed since: the line's own unit still stands.
        choices.insert(0, {"code": line_level, "label": item.unit_label, "factor": Decimal(item.unit_factor)})
    return choices


def _return_unit(item, raw_level):
    """(level, factor) for a returned quantity: the line's own unit unless another was chosen."""
    level = normalize_pack_level(raw_level, default=item.unit_level) if raw_level else normalize_pack_level(item.unit_level)
    for choice in return_unit_choices(item):
        if choice["code"] == level:
            return level, choice["factor"]
    raise ValueError(f"{item.product.display_name} cannot be returned in that unit.")


def _chosen_unit(units, item_id):
    if not units:
        return None
    return units.get(item_id) or units.get(str(item_id))


def _return_quantity(product, raw_quantity, level=PACK_PIECE) -> Decimal:
    try:
        quantity = quantize_qty(raw_quantity or 0)
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ValueError(f"Enter a valid return quantity for {product.display_name}.") from exc
    if quantity > ZERO:
        quantity_error = product.validate_quantity(quantity, level)
        if quantity_error:
            raise ValueError(quantity_error)
    return quantity


def return_row(item):
    """What the return screens show for one sale or purchase line.

    ``returned_qty`` and ``returnable_qty`` are in the product's base unit. For a plain product
    that is the unit of the line itself; ``packed`` marks a line whose figures need pack wording.
    """
    product = item.product
    returned = returned_base_quantity(item)
    returnable = max(item.base_quantity - returned, ZERO)
    choices = []
    for choice in return_unit_choices(item):
        fractional = choice["code"] == PACK_PIECE and product.allows_fractional_quantity
        maximum = returnable / choice["factor"] if fractional else returnable // choice["factor"]
        choices.append({**choice, "max": format_qty(maximum), "step": "0.001" if fractional else "1"})
    packed = len(choices) > 1 or normalize_pack_level(item.unit_level) != PACK_PIECE
    return {
        "item": item,
        "returned_qty": returned,
        "returnable_qty": returnable,
        "packed": packed,
        "quantity_display": item.quantity_display,
        "returned_display": product.format_quantity(returned),
        "returnable_display": returnable_text(item, returnable),
        "unit_choices": choices,
        # The screen starts in the line's own unit, or the largest one something can still come back in.
        "unit": next((choice for choice in choices if Decimal(choice["max"]) > ZERO), choices[0]),
    }


def returnable_text(item, returnable_base):
    """"3 Ctn 2 Box" for a packed product; "5 pcs" for a plain one."""
    product = item.product
    if product.has_packaging or normalize_pack_level(item.unit_level) != PACK_PIECE:
        return product.format_quantity(returnable_base)
    return f"{format_qty(returnable_base)} {product.unit}"


@transaction.atomic
def create_sale_return(*, sale, quantities, refund_method, reason, notes="", bank_account=None, actor=None, units=None):
    """``quantities`` is {sale item id: quantity}. ``units`` may name the unit each quantity is in
    ({sale item id: "carton" | "box" | "piece"}); without it a quantity is in the unit the line was sold in."""
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
        level, factor = _return_unit(item, _chosen_unit(units, item_id))
        quantity = _return_quantity(item.product, qty, level)
        if quantity <= ZERO:
            continue
        base_quantity = quantize_qty(quantity * factor)
        returnable = _sale_item_returnable(item)
        if base_quantity > returnable:
            raise ValueError(f"Only {returnable_text(item, returnable)} of {item.product.display_name} can be returned.")

        # The share of the line that came back is measured in base units, so any unit works.
        # Multiply before dividing: a whole-unit return then comes out to the exact paisa.
        line_base = Decimal(item.base_quantity)
        line_amount = quantize_money(item.line_total * Decimal(base_quantity) / line_base)
        net_amount = quantize_money(line_amount * discount_factor)
        # A discount typed as cash is shared out to the paisa: returning everything refunds exactly what was paid.
        net_amount = returned_net(sale_obj, item, line_amount, quantity >= returnable, net_amount)
        if sale_obj.tax_mode == Sale.TAX_MODE_MANUAL:
            tax_amount = quantize_money(net_amount * (Decimal(sale_obj.tax_rate or 0) / Decimal("100")))
        else:
            rate = Decimal(item.tax_rate_for_payment_method(sale_obj.payment_method))
            tax_amount = quantize_money(net_amount * (rate / Decimal("100")))
        cost_amount = quantize_money(
            Decimal(item.cost_price or 0) * Decimal(item.quantity) * Decimal(base_quantity) / line_base
        )

        SaleReturnItem.objects.create(
            sale_return=sale_return,
            sale_item=item,
            product=item.product,
            quantity=quantity,
            unit_level=level,
            unit_factor=factor,
            unit_price=quantize_money(item.discounted_unit_price * Decimal(factor) / Decimal(item.unit_factor or 1)),
            line_amount=line_amount,
            net_amount=net_amount,
            tax_amount=tax_amount,
            cost_amount=cost_amount,
        )

        item.product.apply_stock_change(base_quantity)

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
    # Free goods of a scheme come back at no money: only their cost returns to stock below.
    if sale_return.net_amount > ZERO:
        VoucherLine.objects.create(voucher=voucher, account=_ensure_account("Sales Returns", Account.INCOME), debit=sale_return.net_amount)
    if sale_return.tax_amount > ZERO:
        VoucherLine.objects.create(voucher=voucher, account=_ensure_account("Sales Tax Payable", Account.LIABILITY), debit=sale_return.tax_amount)

    if total_amount <= ZERO:
        pass
    elif refund_method == SaleReturn.RECEIVABLE:
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
def create_purchase_return(*, purchase, quantities, settlement_method, reason, notes="", bank_account=None, actor=None, units=None):
    """``quantities`` is {purchase item id: quantity}; ``units`` may name the unit each quantity is in."""
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
        level, factor = _return_unit(item, _chosen_unit(units, item_id))
        quantity = _return_quantity(item.product, qty, level)
        if quantity <= ZERO:
            continue
        base_quantity = quantize_qty(quantity * factor)
        returnable = _purchase_item_returnable(item)
        if base_quantity > returnable:
            raise ValueError(f"Only {returnable_text(item, returnable)} of {item.product.display_name} can be returned.")

        line_amount = quantize_money(
            Decimal(item.cost_price or 0) * Decimal(item.quantity) * Decimal(base_quantity) / Decimal(item.base_quantity)
        )
        PurchaseReturnItem.objects.create(
            purchase_return=purchase_return,
            purchase_item=item,
            product=item.product,
            quantity=quantity,
            unit_level=level,
            unit_factor=factor,
            line_amount=line_amount,
        )
        try:
            item.product.apply_stock_change(-base_quantity)
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

