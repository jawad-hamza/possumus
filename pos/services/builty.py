"""Builty (bilty): the transporter's freight bill, added to the cost of the goods it brought.

A distributor pays the transporter when the goods arrive. That freight is part of what the
goods cost, so it is shared over the purchase lines it covers and raises their cost.

The rule, step by step (the builty screens and tests say the same):

1. Counting. Each line of the ticked purchases is counted for the split:
   - Per box (the default): boxes when the product has boxes, otherwise cartons when it has
     cartons, otherwise its own unit (pieces, kg, dozen...).
   - Per carton: cartons, otherwise boxes, otherwise its own unit.
   - Per piece: its own unit (the base unit stock is counted in).
   - By purchase value: the line's amount.
   A level the product does not have (or the Cartons and boxes module being off) falls to the
   next one, and the screen says what each line was "counted as".

2. Splitting. Each line's share is amount x its count / all counts, cut to whole paisas; the
   paisas left over go one each to the lines with the largest remainder (the first line wins
   a tie). The shares always add up to the builty amount exactly.

3. Cost (per product, so two lines of one product are taken together). With S the stock on
   the shelf now and Q the quantity those lines received (less anything sent back to the
   supplier), min(S, Q) of the received goods are taken to be still on the shelf:
   - share x min(S, Q) / received goes into stock value, and the product's weighted-average
     cost rises: new cost = (S x cost + that part) / S, kept at six places per piece;
   - the rest (goods already sold, adjusted out or returned) goes to Cost of Goods Sold.

4. The voucher: Dr Inventory (the stock part), Dr Cost of Goods Sold (the sold part),
   Cr Cash, a bank ledger, or Accounts Payable with the transporter's party ledger.

5. A builty is never edited. It can be reversed once: the money side is undone in full; from
   stock comes back the freight it added per piece times the pieces on the shelf today (never
   more than it added, never more than the stock is worth) and the average cost falls by that;
   the rest is taken back off Cost of Goods Sold.
"""

import math
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.models import PartyLedgerEntry, Voucher, VoucherLine
from accounts.services.audit import record_audit
from accounts.services.books_lock import assert_books_open, normalize_voucher_date
from accounts.services.cash_bank import resolve_money_account
from accounts.services.chart_of_accounts import get_core_account
from pos.models import (
    PACK_BOX,
    PACK_CARTON,
    PACK_PIECE,
    PACK_PLURALS,
    Builty,
    BuiltyAllocation,
    Product,
    Purchase,
    PurchaseItem,
    format_qty,
    quantize_money,
    quantize_price,
    quantize_qty,
)

ZERO = Decimal("0.00")
VALUE = BuiltyAllocation.COUNTED_AS_VALUE

# For each basis, the levels to count a line in, best first. The piece is always there.
COUNT_ORDER = {
    Builty.PER_BOX: (PACK_BOX, PACK_CARTON, PACK_PIECE),
    Builty.PER_CARTON: (PACK_CARTON, PACK_BOX, PACK_PIECE),
    Builty.PER_PIECE: (PACK_PIECE,),
}
BASIS_HINTS = {
    Builty.PER_BOX: "Each line counts its boxes; a product without boxes counts its cartons, and one without either counts pieces.",
    Builty.PER_CARTON: "Each line counts its cartons; a product without cartons counts its boxes, and one without either counts pieces.",
    Builty.PER_PIECE: "Each line counts the pieces (or kg, dozen...) it brought.",
    Builty.BY_VALUE: "Each line takes freight in proportion to its amount on the bill.",
}


# ------------------------------------------------------------------------- counting

def counted_level(product, basis):
    """The unit a line of this product is counted in for the basis: "box", "carton" or "piece"."""
    available = product.pack_level_codes
    for level in COUNT_ORDER.get(basis, (PACK_PIECE,)):
        if level in available:
            return level
    return PACK_PIECE


def line_count(item, basis):
    """(counted as, exact weight, units as shown) for one purchase line."""
    if basis == Builty.BY_VALUE:
        value = item.line_total
        return VALUE, Fraction(value), value
    level = counted_level(item.product, basis)
    factor = Decimal(item.product.pack_factor(level))
    base = Decimal(item.base_quantity)
    return level, Fraction(base) / Fraction(factor), quantize_qty(base / factor)


def counted_text(product, level, units):
    """"410 Box", "12 Ctn", "25 pcs" or "Rs value" for the screens."""
    if level == VALUE:
        return "By value"
    return f"{format_qty(units)} {product.pack_label(level)}"


# ------------------------------------------------------------------------ splitting

def split_paisa(total, weights):
    """Share ``total`` (money) in proportion to ``weights``, to the paisa, adding up exactly.

    Largest remainder: every share is cut down to whole paisas, then the paisas left over
    go one each to the shares that lost the most (the earlier one on a tie).
    """
    total_paisa = int((quantize_money(total) * 100).to_integral_value())
    weights = [Fraction(weight) for weight in weights]
    whole = sum(weights, Fraction(0))
    if not weights:
        return []
    if whole <= 0:
        raise ValueError("There is nothing to share the freight over.")
    exact = [Fraction(total_paisa) * weight / whole for weight in weights]
    paisas = [math.floor(value) for value in exact]
    left = total_paisa - sum(paisas)
    order = sorted(range(len(weights)), key=lambda index: (-(exact[index] - paisas[index]), index))
    for index in order[:left]:
        paisas[index] += 1
    return [quantize_money(Decimal(paisa) / 100) for paisa in paisas]


@dataclass
class PlannedLine:
    item: PurchaseItem
    counted_as: str
    weight: Fraction
    units: Decimal
    amount: Decimal = ZERO
    to_stock: Decimal = ZERO
    to_cogs: Decimal = ZERO
    stock_before: Decimal = Decimal("0")
    cost_before: Decimal = Decimal("0")
    cost_after: Decimal = Decimal("0")
    extra: dict = field(default_factory=dict)

    @property
    def counted_text(self):
        return counted_text(self.item.product, self.counted_as, self.units)


def purchase_lines(purchases):
    """The lines a builty over these purchases is shared across, in purchase then line order."""
    ids = [purchase.pk if hasattr(purchase, "pk") else int(purchase) for purchase in purchases]
    return list(
        PurchaseItem.objects.filter(purchase_id__in=ids)
        .select_related("product", "purchase")
        .prefetch_related("return_items")
        .order_by("purchase__created_at", "purchase_id", "id")
    )


def plan_split(items, amount, basis):
    """Who gets how much of the freight. Touches nothing."""
    if basis not in dict(Builty.BASIS_CHOICES):
        raise ValueError("Choose how the freight is shared: per box, per carton, per piece or by value.")
    rows = []
    for item in items:
        counted_as, weight, units = line_count(item, basis)
        rows.append(PlannedLine(item=item, counted_as=counted_as, weight=weight, units=units))
    if not rows:
        raise ValueError("The ticked purchases have no items to put the freight on.")
    for row, share in zip(rows, split_paisa(amount, [row.weight for row in rows])):
        row.amount = share
    return rows


def _returned(item):
    return sum((line.base_quantity for line in item.return_items.all()), Decimal("0"))


def plan_cost(rows, products):
    """Split every line's share into a stock part and a sold part; work out the new cost.

    ``products`` maps product id to the product row as it stands (locked when posting).
    Returns {product id: (stock part, new cost)}.
    """
    groups = OrderedDict()
    for row in rows:
        groups.setdefault(row.item.product_id, []).append(row)

    outcome = {}
    for product_id, group in groups.items():
        product = products[product_id]
        stock = max(Decimal(product.stock or 0), Decimal("0"))
        cost = Decimal(product.cost_price or 0)
        received = sum((Decimal(row.item.base_quantity) for row in group), Decimal("0"))
        kept = sum((max(Decimal(row.item.base_quantity) - _returned(row.item), Decimal("0")) for row in group), Decimal("0"))
        on_hand = min(stock, kept)
        share = sum((row.amount for row in group), ZERO)

        to_stock = ZERO
        if received > 0 and on_hand > 0 and share > 0:
            to_stock = quantize_money(share * on_hand / received)
        parts = split_paisa(to_stock, [row.amount for row in group]) if to_stock > 0 else [ZERO] * len(group)

        new_cost = cost
        if stock > 0 and to_stock > 0:
            new_cost = quantize_price((stock * cost + to_stock) / stock)
        for row, part in zip(group, parts):
            row.to_stock = part
            row.to_cogs = row.amount - part
            row.stock_before = stock
            row.cost_before = cost
            row.cost_after = new_cost
        outcome[product_id] = (to_stock, new_cost)
    return outcome


# -------------------------------------------------------------------------- posting

def _money_label(payment_method, bank_account=None):
    if payment_method == Builty.CASH:
        return "Cash"
    if payment_method == Builty.BANK:
        return bank_account.display_name if bank_account is not None else "Bank"
    return "transporter's account"


def _credit_side(voucher, builty, amount, *, debit):
    """The money side of a builty voucher: cash, bank, or the transporter's payable."""
    side = "debit" if debit else "credit"
    if builty.payment_method == Builty.CREDIT:
        VoucherLine.objects.create(voucher=voucher, account=get_core_account("Accounts Payable"), **{side: amount})
        PartyLedgerEntry.objects.create(
            voucher=voucher,
            content_type=ContentType.objects.get_for_model(builty.transporter),
            object_id=builty.transporter_id,
            **{side: amount},
        )
    else:
        account = resolve_money_account(builty.payment_method, builty.bank_account)
        VoucherLine.objects.create(voucher=voucher, account=account, **{side: amount})


def _purchase_words(purchases):
    numbers = ", ".join(f"#{purchase.pk}" for purchase in purchases)
    return f"purchase{'s' if len(purchases) != 1 else ''} {numbers}"


@transaction.atomic
def post_builty(
    *,
    purchases,
    amount,
    basis=Builty.PER_BOX,
    payment_method=Builty.CASH,
    date=None,
    builty_number="",
    transporter=None,
    bank_account=None,
    notes="",
    actor=None,
):
    """Record a builty over these purchases, raise the goods' cost and post its voucher."""
    amount = quantize_money(Decimal(amount or 0))
    if amount <= 0:
        raise ValueError("Enter the freight amount on the builty.")
    if payment_method not in dict(Builty.PAYMENT_METHOD_CHOICES):
        raise ValueError("Choose how the builty was paid.")
    if payment_method == Builty.CREDIT and transporter is None:
        raise ValueError("Choose the transporter: an unpaid builty is owed to someone.")
    if payment_method != Builty.BANK:
        bank_account = None
    date = normalize_voucher_date(date)
    if date > timezone.localdate():
        raise ValueError("A builty cannot be dated in the future.")
    assert_books_open(date, action="post a builty")

    purchases = list(Purchase.objects.filter(pk__in=[getattr(p, "pk", p) for p in purchases]).order_by("created_at", "id"))
    if not purchases:
        raise ValueError("Tick at least one purchase this freight was paid on.")

    items = purchase_lines(purchases)
    rows = plan_split(items, amount, basis)
    product_ids = sorted({row.item.product_id for row in rows})
    products = {product.pk: product for product in Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk")}
    outcome = plan_cost(rows, products)

    stock_total = sum((row.to_stock for row in rows), ZERO)
    cogs_total = sum((row.to_cogs for row in rows), ZERO)

    builty = Builty.objects.create(
        date=date,
        builty_number=(builty_number or "").strip(),
        transporter=transporter,
        amount=amount,
        payment_method=payment_method,
        bank_account=bank_account,
        basis=basis,
        notes=notes or "",
        stock_amount=stock_total,
        cogs_amount=cogs_total,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    builty.purchases.set(purchases)
    BuiltyAllocation.objects.bulk_create(
        [
            BuiltyAllocation(
                builty=builty,
                purchase_item=row.item,
                product_id=row.item.product_id,
                counted_as=row.counted_as,
                units_counted=row.units,
                amount=row.amount,
                amount_to_stock=row.to_stock,
                amount_to_cogs=row.to_cogs,
                stock_at_posting=row.stock_before,
                cost_before=row.cost_before,
                cost_after=row.cost_after,
            )
            for row in rows
        ]
    )

    for product_id, (_, new_cost) in outcome.items():
        product = products[product_id]
        if Decimal(product.cost_price or 0) != new_cost:
            product.cost_price = new_cost
            product.save(update_fields=["cost_price"])

    voucher = Voucher.objects.create(
        voucher_type=Voucher.PURCHASE,
        date=date,
        narration=(
            f"{builty.display_number} freight on {_purchase_words(purchases)} "
            f"via {_money_label(payment_method, bank_account)}"
        )[:255],
        ref_model="Builty",
        ref_id=builty.pk,
        created_by=builty.created_by,
    )
    if stock_total > 0:
        VoucherLine.objects.create(voucher=voucher, account=get_core_account("Inventory"), debit=stock_total)
    if cogs_total > 0:
        VoucherLine.objects.create(voucher=voucher, account=get_core_account("Cost of Goods Sold"), debit=cogs_total)
    _credit_side(voucher, builty, amount, debit=False)
    voucher.clean()
    builty.voucher = voucher
    builty.save(update_fields=["voucher"])

    record_audit(
        action="builty_posted",
        description=(
            f"{builty.display_number} of {amount} recorded on {_purchase_words(purchases)}: "
            f"{stock_total} added to stock value, {cogs_total} to cost of goods sold."
        ),
        actor=builty.created_by,
        target=builty,
        metadata={
            "amount": str(amount),
            "basis": basis,
            "stock_amount": str(stock_total),
            "cogs_amount": str(cogs_total),
            "purchase_ids": [purchase.pk for purchase in purchases],
            "new_costs": {str(pid): str(new_cost) for pid, (_, new_cost) in outcome.items()},
        },
    )
    return builty


@transaction.atomic
def reverse_builty(builty, *, reason, actor=None):
    """Undo a builty with a reversing voucher dated today. See rule 5 at the top."""
    builty = Builty.objects.select_for_update().select_related("transporter", "bank_account").get(pk=getattr(builty, "pk", builty))
    if builty.is_reversed:
        raise ValueError(f"{builty.display_number} was already reversed.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Say why the builty is being reversed.")
    date = timezone.localdate()
    assert_books_open(date, action="reverse a builty")

    allocations = list(builty.allocations.select_related("product").order_by("id"))
    groups = OrderedDict()
    for allocation in allocations:
        groups.setdefault(allocation.product_id, []).append(allocation)
    products = {product.pk: product for product in Product.objects.select_for_update().filter(pk__in=list(groups)).order_by("pk")}

    stock_back = ZERO
    for product_id, group in groups.items():
        product = products[product_id]
        added = sum((Decimal(row.amount_to_stock) for row in group), ZERO)
        stock_then = Decimal(group[0].stock_at_posting or 0)
        stock_now = max(Decimal(product.stock or 0), Decimal("0"))
        cost_now = Decimal(product.cost_price or 0)

        back = ZERO
        if added > 0 and stock_then > 0 and stock_now > 0:
            per_piece = Fraction(added) / Fraction(stock_then)
            back = min(
                added,
                quantize_money(Decimal(per_piece.numerator) * stock_now / Decimal(per_piece.denominator)),
                quantize_money(stock_now * cost_now),
            )
            back = max(back, ZERO)
        parts = split_paisa(back, [row.amount_to_stock for row in group]) if back > 0 else [ZERO] * len(group)
        for row, part in zip(group, parts):
            row.reversed_to_stock = part
            row.reversed_to_cogs = Decimal(row.amount) - part
            row.save(update_fields=["reversed_to_stock", "reversed_to_cogs"])
        if back > 0:
            product.cost_price = max(quantize_price((stock_now * cost_now - back) / stock_now), Decimal("0"))
            product.save(update_fields=["cost_price"])
        stock_back += back

    cogs_back = Decimal(builty.amount) - stock_back
    voucher = Voucher.objects.create(
        voucher_type=Voucher.PURCHASE,
        date=date,
        narration=f"{builty.display_number} reversed: {reason}"[:255],
        ref_model="BuiltyReversal",
        ref_id=builty.pk,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    _credit_side(voucher, builty, Decimal(builty.amount), debit=True)
    if stock_back > 0:
        VoucherLine.objects.create(voucher=voucher, account=get_core_account("Inventory"), credit=stock_back)
    if cogs_back > 0:
        VoucherLine.objects.create(voucher=voucher, account=get_core_account("Cost of Goods Sold"), credit=cogs_back)
    voucher.clean()

    builty.reversed_at = timezone.now()
    builty.reversed_by = voucher.created_by
    builty.reversal_reason = reason[:255]
    builty.reversal_voucher = voucher
    builty.reversal_stock_amount = stock_back
    builty.reversal_cogs_amount = cogs_back
    builty.save(
        update_fields=[
            "reversed_at",
            "reversed_by",
            "reversal_reason",
            "reversal_voucher",
            "reversal_stock_amount",
            "reversal_cogs_amount",
        ]
    )
    record_audit(
        action="builty_reversed",
        description=(
            f"{builty.display_number} of {builty.amount} reversed: {stock_back} taken back out of stock value, "
            f"{cogs_back} off cost of goods sold. Reason: {reason}"
        ),
        actor=voucher.created_by,
        target=builty,
        metadata={"stock_amount": str(stock_back), "cogs_amount": str(cogs_back)},
    )
    return builty


# -------------------------------------------------------------------------- reading

def _active_allocations():
    return BuiltyAllocation.objects.filter(builty__reversed_at__isnull=True)


def freight_by_item(item_ids):
    """{purchase line id: freight on it from builties still in force}."""
    rows = _active_allocations().filter(purchase_item_id__in=list(item_ids)).values("purchase_item_id").annotate(total=Sum("amount"))
    return {row["purchase_item_id"]: quantize_money(row["total"] or 0) for row in rows}


def freight_by_purchase(purchase_ids):
    """{purchase id: freight on its lines from builties still in force}."""
    rows = (
        _active_allocations()
        .filter(purchase_item__purchase_id__in=list(purchase_ids))
        .values("purchase_item__purchase_id")
        .annotate(total=Sum("amount"))
    )
    return {row["purchase_item__purchase_id"]: quantize_money(row["total"] or 0) for row in rows}


def freight_by_builty(purchase_id):
    """{builty id: the part of it that landed on this purchase}, reversed builties included."""
    rows = (
        BuiltyAllocation.objects.filter(purchase_item__purchase_id=purchase_id)
        .values("builty_id")
        .annotate(total=Sum("amount"))
    )
    return {row["builty_id"]: quantize_money(row["total"] or 0) for row in rows}


def landed_costs(item, freight):
    """(landed cost per unit the line was bought in, landed cost per piece) with this freight on it."""
    quantity = Decimal(item.quantity or 0)
    base = Decimal(item.base_quantity or 0)
    per_unit = quantize_money(Decimal(item.cost_price) + (Decimal(freight) / quantity if quantity > 0 else 0))
    per_piece = quantize_price(item.base_unit_cost + (Decimal(freight) / base if base > 0 else 0))
    return per_unit, per_piece


def preview_line(item):
    """What builty.js needs about one purchase line to show the split live, for every basis."""
    product = item.product
    counts = {}
    for basis in (Builty.PER_BOX, Builty.PER_CARTON, Builty.PER_PIECE):
        level = counted_level(product, basis)
        counts[basis] = {
            "level": level,
            "label": product.pack_label(level),
            "plural": PACK_PLURALS[level] if level != PACK_PIECE else (product.unit or "pcs"),
            "factor": str(product.pack_factor(level)),
        }
    return {
        "item": item.pk,
        "purchase": item.purchase_id,
        "product": product.display_name,
        "quantity": str(item.quantity),
        "unitLabel": item.unit_label,
        "base": str(item.base_quantity),
        "cost": str(item.cost_price),
        "lineTotal": str(item.line_total),
        "counts": counts,
    }
