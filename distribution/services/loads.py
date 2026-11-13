"""Load forms: the invoices that go out on one vehicle, the printed load sheet, and the
day-end settlement of what came back and the cash handed in.

Every posting goes through the services the rest of the app uses: goods that came back are
sale returns against their invoice (``create_sale_return``), money collected on the run is a
customer receipt (see ``recovery.record_recovery``). The only voucher written here is the
cash difference when the load is closed:

* short (less cash handed in than expected): Dr Cash Short and Over, Cr Cash;
* over (more handed in): Dr Cash, Cr Cash Short and Over.

The cash the delivery man should hand in is what the Cash ledger already holds for the run:
the cash bills on the load, less cash refunded for goods that came back on those bills, plus
the cash recoveries he brought in.
"""
from collections import OrderedDict
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Account, Voucher, VoucherLine
from accounts.services.audit import record_audit
from accounts.services.cash_bank import ensure_cash_account
from pos.models import PACK_BOX, PACK_CARTON, PACK_PIECE, Sale, SaleItem, SaleReturn, format_qty, quantize_money, quantize_qty
from pos.services.modules import module_enabled
from pos.services.reporting import range_q
from pos.services.returns import create_sale_return

from ..models import LoadForm, LoadFormInvoice, LoadFormReturn, Recovery
from .common import ZERO, DistributionError, ensure_account, pack_columns, packing_text

CASH_SHORT_OVER = "Cash Short and Over"


def _locked(load):
    return LoadForm.objects.select_for_update().get(pk=getattr(load, "pk", load))


# ------------------------------------------------------------------------ the invoices
def loadable_sales(*, load_date=None, route=None, order_booker=None, from_orders=False):
    """Invoices that can still go on a load: made for a named customer and not on any load yet."""
    sales = Sale.objects.filter(customer__isnull=False, load_entry__isnull=True)
    if load_date is not None:
        sales = sales.filter(range_q(load_date, load_date))
    if route is not None:
        sales = sales.filter(Q(sales_order__route=route) | Q(sales_order__isnull=True, customer__route=route))
    if order_booker is not None:
        sales = sales.filter(order_booker=order_booker)
    if from_orders:
        sales = sales.filter(sales_order__isnull=False)
    return sales.select_related("customer", "customer__route", "order_booker").order_by("created_at", "id")


@transaction.atomic
def create_load(*, load_date, route=None, delivery_man=None, vehicle=None, notes="", user=None):
    load = LoadForm.objects.create(
        load_date=load_date or timezone.localdate(),
        route=route,
        delivery_man=delivery_man,
        vehicle=vehicle,
        notes=(notes or "").strip()[:255],
        created_by=user,
    )
    record_audit(
        action="load_form_created",
        description=f"Load {load.display_number} opened for {load.load_date:%d %b %Y}.",
        actor=user,
        target=load,
    )
    return load


@transaction.atomic
def update_load(load, *, load_date, route=None, delivery_man=None, vehicle=None, notes="", user=None):
    load = _locked(load)
    if load.is_settled:
        raise DistributionError(f"{load.display_number} is settled and can no longer be changed.")
    load.load_date = load_date or load.load_date
    load.route = route
    load.delivery_man = delivery_man
    load.vehicle = vehicle
    load.notes = (notes or "").strip()[:255]
    load.save(update_fields=["load_date", "route", "delivery_man", "vehicle", "notes"])
    return load


@transaction.atomic
def add_invoices(load, sale_ids, *, user=None):
    """Put invoices on a load that is still being loaded. Returns how many were added."""
    load = _locked(load)
    if not load.is_open:
        raise DistributionError(f"{load.display_number} has left; invoices can only be added while it is being loaded.")
    ids = sorted({int(value) for value in sale_ids if str(value).isdigit()})
    if not ids:
        raise DistributionError("Tick the invoices to put on this load.")
    sales = Sale.objects.select_for_update(of=("self",)).filter(pk__in=ids).select_related("customer")
    found = {sale.pk: sale for sale in sales}
    problems = []
    taken = dict(
        LoadFormInvoice.objects.filter(sale_id__in=ids).select_related("load_form").values_list("sale_id", "load_form_id")
    )
    added = []
    for pk in ids:
        sale = found.get(pk)
        if sale is None:
            problems.append(f"Invoice #{pk} no longer exists.")
        elif sale.customer_id is None:
            problems.append(f"{sale.display_number} has no customer, so there is nowhere to deliver it.")
        elif pk in taken:
            where = "this load" if taken[pk] == load.pk else f"load LF-{taken[pk]:06d}"
            problems.append(f"{sale.display_number} is already on {where}.")
        else:
            added.append(sale)
    if problems:
        raise DistributionError(" ".join(problems))
    LoadFormInvoice.objects.bulk_create([LoadFormInvoice(load_form=load, sale=sale) for sale in added])
    record_audit(
        action="load_form_invoices_added",
        description=f"{len(added)} invoice(s) put on load {load.display_number}.",
        actor=user,
        target=load,
        metadata={"sales": [sale.display_number for sale in added]},
    )
    return len(added)


@transaction.atomic
def remove_invoice(load, sale_id, *, user=None):
    load = _locked(load)
    if not load.is_open:
        raise DistributionError(f"{load.display_number} has left; take it back to loading first.")
    entry = LoadFormInvoice.objects.filter(load_form=load, sale_id=sale_id).select_related("sale").first()
    if entry is None:
        raise DistributionError("That invoice is not on this load.")
    number = entry.sale.display_number
    entry.delete()
    record_audit(
        action="load_form_invoice_removed",
        description=f"{number} taken off load {load.display_number}.",
        actor=user,
        target=load,
    )
    return number


@transaction.atomic
def dispatch_load(load, *, user=None):
    load = _locked(load)
    if not load.is_open:
        raise DistributionError(f"{load.display_number} is already {load.get_status_display().lower()}.")
    if not load.invoices.exists():
        raise DistributionError("Put at least one invoice on the load before it leaves.")
    load.status = LoadForm.DISPATCHED
    load.dispatched_at = timezone.now()
    load.save(update_fields=["status", "dispatched_at"])
    record_audit(
        action="load_form_dispatched",
        description=f"Load {load.display_number} out for delivery.",
        actor=user,
        target=load,
    )
    return load


@transaction.atomic
def reopen_load(load, *, user=None):
    """Back to loading, while nothing has been settled against it yet."""
    load = _locked(load)
    if not load.is_dispatched:
        raise DistributionError(f"{load.display_number} is {load.get_status_display().lower()}.")
    if load.sale_returns.exists() or load.recoveries.exists():
        raise DistributionError("Returns or recoveries are already recorded on this load, so it stays out for delivery.")
    load.status = LoadForm.OPEN
    load.dispatched_at = None
    load.save(update_fields=["status", "dispatched_at"])
    record_audit(action="load_form_reopened", description=f"Load {load.display_number} back to loading.", actor=user, target=load)
    return load


# ----------------------------------------------------------------------- the load sheet
def load_sales(load):
    return [
        entry.sale
        for entry in load.invoices.select_related("sale__customer__route", "sale__order_booker").order_by("id")
    ]


def load_sheet(load):
    """The printed sheet: product-wise totals in cartons, boxes and loose pieces, and the invoices.

    Quantities are what went out on the van: the invoice lines less anything returned on those
    invoices before the load was settled (returns booked at this load's own settlement are the
    goods that came back, so they do not shrink the sheet).
    """
    sales = load_sales(load)
    own_returns = set(load.sale_returns.values_list("sale_return_id", flat=True))
    items = (
        SaleItem.objects.filter(sale__in=sales)
        .select_related("product", "sale")
        .prefetch_related("return_items__sale_return")
        .order_by("product__name", "product__variant_label", "id")
    )
    products = OrderedDict()
    for item in items:
        earlier = sum(
            (row.base_quantity for row in item.return_items.all() if row.sale_return_id not in own_returns), ZERO
        )
        base = quantize_qty(item.base_quantity - earlier)
        if base <= 0:
            continue
        share = base / item.base_quantity if item.base_quantity else ZERO
        amount = quantize_money(item.line_total * share * (Decimal("1") - Decimal(item.sale.discount_rate or 0) / Decimal("100")))
        row = products.setdefault(
            item.product_id,
            {"product": item.product, "base": ZERO, "free": ZERO, "amount": ZERO},
        )
        # Free goods of a scheme go out on the van too, in a column of their own.
        if item.is_free:
            row["free"] += base
        else:
            row["base"] += base
        row["amount"] += amount
    lines = []
    for row in products.values():
        product = row["product"]
        counts = pack_columns(product, row["base"])
        lines.append(
            {
                "product": product,
                "packing": packing_text(product),
                "cartons": counts[PACK_CARTON],
                "boxes": counts[PACK_BOX],
                "pieces": counts[PACK_PIECE],
                "unit": product.pack_label(PACK_PIECE),
                "base": quantize_qty(row["base"]),
                "in_packs": product.format_quantity(row["base"]) if product.has_packaging else f"{format_qty(row['base'])} {product.unit}",
                "free_base": quantize_qty(row["free"]),
                "free": (
                    (product.format_quantity(row["free"]) if product.has_packaging else f"{format_qty(row['free'])} {product.unit}")
                    if row["free"] > 0
                    else ""
                ),
                "retail_price": product.pack_retail_price(product.top_pack_level),
                "amount": quantize_money(row["amount"]),
            }
        )
    invoices = []
    for sale in sales:
        invoices.append(
            {
                "sale": sale,
                "customer": sale.customer,
                "route": sale.customer.route if sale.customer and sale.customer.route_id else None,
                "amount": quantize_money(sale.total_amount),
                "kind": "Credit" if sale.is_credit else ("Bank" if sale.payment_method == Sale.BANK else "Cash"),
            }
        )
    return {
        "lines": lines,
        "invoices": invoices,
        "show_boxes": any(line["boxes"] for line in lines),
        "show_free": any(line["free_base"] > 0 for line in lines),
        "show_mrp": module_enabled("price_lists") and any(line["retail_price"] is not None for line in lines),
        "total_cartons": sum((line["cartons"] for line in lines), ZERO),
        "total_boxes": sum((line["boxes"] for line in lines), ZERO),
        "total_pieces": sum((line["pieces"] for line in lines), ZERO),
        "total_amount": quantize_money(sum((line["amount"] for line in lines), ZERO)),
        "bill_value": quantize_money(sum((row["amount"] for row in invoices), ZERO)),
        "invoice_count": len(invoices),
    }


# ------------------------------------------------------------------- day-end settlement
def _refund_method(sale):
    if sale.is_credit:
        return SaleReturn.RECEIVABLE
    return SaleReturn.BANK if sale.payment_method == Sale.BANK else SaleReturn.CASH


@transaction.atomic
def post_load_returns(load, entries, *, reason, user=None):
    """Goods that came back on the van, posted as sale returns against their invoices.

    ``entries`` is {sale id: {"quantities": {sale item id: qty}, "units": {sale item id: level}}}.
    A credit invoice is reduced on the customer's account; goods back on a cash bill are money
    the delivery man never collected, so they come off the cash he owes.
    """
    load = _locked(load)
    if not load.is_dispatched:
        raise DistributionError("Returns are recorded while the load is out for delivery.")
    reason = (reason or "").strip()
    if not reason:
        raise DistributionError("Say why the goods came back (for example: shop closed, refused, short of cash).")
    on_load = {entry.sale_id: entry.sale for entry in load.invoices.select_related("sale")}
    created = []
    for sale_id, entry in entries.items():
        quantities = {key: value for key, value in (entry.get("quantities") or {}).items() if quantize_qty(value or 0) > 0}
        if not quantities:
            continue
        sale = on_load.get(int(sale_id))
        if sale is None:
            raise DistributionError("One of those invoices is not on this load.")
        method = _refund_method(sale)
        try:
            sale_return = create_sale_return(
                sale=sale,
                quantities=quantities,
                units=entry.get("units") or {},
                refund_method=method,
                reason=reason,
                notes=f"Came back on load {load.display_number}",
                bank_account=sale.bank_account if method == SaleReturn.BANK else None,
                actor=user,
            )
        except ValueError as exc:
            raise DistributionError(f"{sale.display_number}: {exc}") from exc
        LoadFormReturn.objects.create(load_form=load, sale_return=sale_return)
        created.append(sale_return)
    if not created:
        raise DistributionError("Enter what came back on at least one invoice.")
    record_audit(
        action="load_form_returns_posted",
        description=f"{len(created)} return(s) posted on load {load.display_number}.",
        actor=user,
        target=load,
        metadata={"returns": [row.pk for row in created], "total": str(sum((row.total_amount for row in created), ZERO))},
    )
    return created


def settlement_figures(load):
    """Every figure of the settlement screen and the settlement report for one load."""
    sales = load_sales(load)
    returns = list(
        SaleReturn.objects.filter(load_entry__load_form=load).select_related("sale")
    )
    returned_by_sale = {}
    for row in returns:
        returned_by_sale[row.sale_id] = returned_by_sale.get(row.sale_id, ZERO) + Decimal(row.total_amount)
    recoveries = list(load.recoveries.select_related("customer", "sale").order_by("id"))

    cash_bills = ZERO
    credit_bills = ZERO
    bank_bills = ZERO
    invoices = []
    for sale in sales:
        total = Decimal(sale.total_amount)
        back = returned_by_sale.get(sale.pk, ZERO)
        if sale.is_credit:
            credit_bills += total
            kind = "Credit"
        elif sale.payment_method == Sale.BANK:
            bank_bills += total
            kind = "Bank"
        else:
            cash_bills += Decimal(sale.paid_amount)
            kind = "Cash"
        invoices.append(
            {
                "sale": sale,
                "kind": kind,
                "total": quantize_money(total),
                "returned": quantize_money(back),
                "net": quantize_money(total - back),
            }
        )
    cash_refunds = sum(
        (Decimal(row.total_amount) for row in returns if row.refund_method == SaleReturn.CASH), ZERO
    )
    cash_recovered = sum((Decimal(row.amount) for row in recoveries if row.payment_method == Recovery.CASH), ZERO)
    bank_recovered = sum((Decimal(row.amount) for row in recoveries if row.payment_method == Recovery.BANK), ZERO)
    returns_total = sum((Decimal(row.total_amount) for row in returns), ZERO)
    expected = quantize_money(cash_bills - cash_refunds + cash_recovered)
    if load.is_settled:
        expected = quantize_money(load.cash_expected)
    bill_value = sum((Decimal(row["total"]) for row in invoices), ZERO)
    return {
        "invoices": invoices,
        "returns": returns,
        "recoveries": recoveries,
        "bill_value": quantize_money(bill_value),
        "cash_bills": quantize_money(cash_bills),
        "credit_bills": quantize_money(credit_bills),
        "bank_bills": quantize_money(bank_bills),
        "returns_total": quantize_money(returns_total),
        "cash_refunds": quantize_money(cash_refunds),
        "cash_recovered": quantize_money(cash_recovered),
        "bank_recovered": quantize_money(bank_recovered),
        "recovered": quantize_money(cash_recovered + bank_recovered),
        "cash_expected": expected,
        "delivered": quantize_money(bill_value - returns_total),
    }


@transaction.atomic
def close_load(load, *, cash_received, notes="", user=None):
    """Fix the cash handed in against the cash expected, post any difference and settle the load."""
    load = _locked(load)
    if not load.is_dispatched:
        raise DistributionError(f"{load.display_number} is {load.get_status_display().lower()}; only a load out for delivery can be settled.")
    if cash_received is None or Decimal(cash_received) < 0:
        raise DistributionError("Enter the cash the delivery man handed in (0 when he brought none).")
    received = quantize_money(cash_received)
    figures = settlement_figures(load)
    expected = figures["cash_expected"]
    variance = quantize_money(received - expected)

    voucher = None
    if variance != 0:
        cash = ensure_cash_account()
        short_over = ensure_account(CASH_SHORT_OVER, Account.EXPENSE)
        who = f" ({load.delivery_man.name})" if load.delivery_man_id else ""
        voucher = Voucher.objects.create(
            voucher_type=Voucher.JOURNAL,
            date=timezone.localdate(),
            narration=f"Cash {'short' if variance < 0 else 'over'} on load {load.display_number}{who}",
            ref_model="LoadForm",
            ref_id=load.pk,
            created_by=user,
        )
        amount = abs(variance)
        if variance < 0:
            VoucherLine.objects.create(voucher=voucher, account=short_over, debit=amount)
            VoucherLine.objects.create(voucher=voucher, account=cash, credit=amount)
        else:
            VoucherLine.objects.create(voucher=voucher, account=cash, debit=amount)
            VoucherLine.objects.create(voucher=voucher, account=short_over, credit=amount)
        voucher.clean()

    load.cash_expected = expected
    load.cash_received = received
    load.cash_variance = variance
    load.variance_voucher = voucher
    load.settlement_notes = (notes or "").strip()[:255]
    load.status = LoadForm.SETTLED
    load.settled_at = timezone.now()
    load.settled_by = user
    load.save(
        update_fields=[
            "cash_expected",
            "cash_received",
            "cash_variance",
            "variance_voucher",
            "settlement_notes",
            "status",
            "settled_at",
            "settled_by",
        ]
    )
    record_audit(
        action="load_form_settled",
        description=f"Load {load.display_number} settled: cash expected {expected}, handed in {received}.",
        actor=user,
        target=load,
        metadata={"expected": str(expected), "received": str(received), "variance": str(variance)},
    )
    return load
