"""Sales orders: booked at the shop, keyed in at the office, turned into invoices.

An order moves no stock and no money. Turning it into an invoice makes an ordinary sale
through ``pos.services.sale_recording.record_sale``, so the till's rules all apply: price
override and line discount as Settings allow, the minimum margin, the credit limit (refuse or
warn), invoice numbering and the ledger posting. When the shelf cannot cover a line, the
invoice takes what there is and the rest stays on the order line as its short quantity.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.services.audit import record_audit
from accounts.services.party_balance import get_party_balance
from pos.models import PACK_PIECE, Product, Sale, format_qty, quantize_money, quantize_qty
from pos.services.business_rules import category_tax_rate_for_product, get_system_settings
from pos.services.cart_pricing import tracks_stock, validate_cart_line
from pos.services.currency import format_money
from pos.services.field_sales import credit_standing, default_order_booker_for_route, field_sales_enabled
from pos.services.sale_recording import SaleRefused, record_sale
from pricing.services.invoicing import apply_order_terms
from pricing.services.rates import pricing_terms

from ..models import SalesOrder, SalesOrderItem
from .common import ZERO, DistributionError


# --------------------------------------------------------------------- the customer box
def customer_terms(customer):
    """Balance, credit limit and what is left of it, plus the route and order booker an order takes."""
    balance = quantize_money(get_party_balance(customer))
    standing = credit_standing(customer, balance)
    route = customer.route if customer.route_id and customer.route.is_active else None
    booker = customer.order_booker if customer.order_booker_id and customer.order_booker.is_active else None
    if booker is None and route is not None:
        booker = default_order_booker_for_route(route)
    return {
        "id": customer.id,
        "name": customer.name,
        "balance": str(balance),
        "balance_text": format_money(abs(balance)),
        "owes": balance > 0,
        "advance": balance < 0,
        "has_limit": standing["has_limit"],
        "limit_text": format_money(standing["limit"]) if standing["has_limit"] else "",
        "left_text": format_money(standing["left"]) if standing["has_limit"] else "",
        "over_text": format_money(standing["over"]) if standing["is_over"] else "",
        "is_over": standing["is_over"],
        "route_id": str(route.id) if route else "",
        "order_booker_id": str(booker.id) if booker else "",
        "city": customer.city,
        # Trade price list rates (pricing app): the order form fills its lines from these.
        **pricing_terms(customer),
    }


# ---------------------------------------------------------------------- saving an order
def _check_line_price(line):
    product, level = line["product"], line["unit_level"]
    try:
        validate_cart_line(product, line["rate"], line["discount_percent"], level)
    except ValidationError as exc:
        return f"{product.display_name}: {' '.join(exc.messages)}"
    return ""


@transaction.atomic
def save_order(*, order=None, customer, lines, user, route=None, order_booker=None, order_date=None, delivery_date=None, notes=""):
    """Create an order, or replace the lines of one still booked. Raises DistributionError."""
    if customer is None:
        raise DistributionError("Choose the customer this order is for.")
    if not lines:
        raise DistributionError("Add at least one product to the order.")
    problems = [problem for problem in (_check_line_price(line) for line in lines) if problem]
    if problems:
        raise DistributionError(" ".join(problems))

    creating = order is None
    if creating:
        order = SalesOrder(created_by=user)
    else:
        order = SalesOrder.objects.select_for_update().get(pk=order.pk)
        if order.status != SalesOrder.BOOKED:
            raise DistributionError(f"{order.display_number} is {order.get_status_display().lower()} and can no longer be changed.")

    if not field_sales_enabled():
        route, order_booker = order.route if not creating else None, order.order_booker if not creating else None
    order.customer = customer
    order.route = route
    order.order_booker = order_booker
    order.order_date = order_date or timezone.localdate()
    order.delivery_date = delivery_date
    order.notes = (notes or "").strip()[:255]
    order.save()

    if not creating:
        order.items.all().delete()
    SalesOrderItem.objects.bulk_create(
        [
            SalesOrderItem(
                order=order,
                product=line["product"],
                quantity=line["quantity"],
                unit_level=line["unit_level"],
                unit_factor=line["unit_factor"],
                price=line["rate"],
                discount_percent=line["discount_percent"],
            )
            for line in lines
        ]
    )
    record_audit(
        action="sales_order_booked" if creating else "sales_order_changed",
        description=f"Order {order.display_number} for {customer.name} {'booked' if creating else 'changed'}.",
        actor=user,
        target=order,
        metadata={"lines": len(lines), "total": str(order.total_amount)},
    )
    return order


@transaction.atomic
def cancel_order(order, *, reason, user):
    reason = (reason or "").strip()
    if not reason:
        raise DistributionError("Say why the order is cancelled.")
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != SalesOrder.BOOKED:
        raise DistributionError(f"{order.display_number} is {order.get_status_display().lower()}; only a booked order can be cancelled.")
    order.status = SalesOrder.CANCELLED
    order.cancel_reason = reason[:150]
    order.cancelled_at = timezone.now()
    order.save(update_fields=["status", "cancel_reason", "cancelled_at"])
    record_audit(
        action="sales_order_cancelled",
        description=f"Order {order.display_number} for {order.customer.name} cancelled.",
        actor=user,
        target=order,
        metadata={"reason": order.cancel_reason, "total": str(order.total_amount)},
    )
    return order


# ------------------------------------------------------------------ order -> invoice
@dataclass
class Shortage:
    product: object
    unit_label: str
    ordered: Decimal
    invoiced: Decimal

    @property
    def short(self):
        return quantize_qty(self.ordered - self.invoiced)

    def sentence(self):
        return (
            f"{self.product.display_name}: ordered {format_qty(self.ordered)} {self.unit_label}, "
            f"invoiced {format_qty(self.invoiced)}, short {format_qty(self.short)}"
        )


@dataclass
class Conversion:
    order: object
    sale: object = None
    error: str = ""
    shortages: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self):
        return self.sale is not None


def _available_units(product, stock, level, factor):
    """How many of the line's unit the shelf can still cover."""
    if stock is None:
        return None
    if stock <= 0:
        return ZERO
    if level == PACK_PIECE and product.allows_fractional_quantity:
        return quantize_qty(stock)
    return Decimal(stock // factor)


def invoice_order(order, *, user, credit=True):
    """Turn one booked order into an invoice. Never raises for a business refusal: the
    returned Conversion carries the reason, and nothing of a refused order is saved."""
    result = Conversion(order=order)
    system_settings = get_system_settings()
    try:
        with transaction.atomic():
            order = SalesOrder.objects.select_for_update().select_related("customer", "order_booker").get(pk=order.pk)
            result.order = order
            if order.status != SalesOrder.BOOKED:
                raise SaleRefused(f"It is {order.get_status_display().lower()}, not waiting to be invoiced.")
            customer = order.customer
            if not customer.is_active:
                raise SaleRefused(f"{customer.name} is no longer an active customer.")
            if credit and not system_settings.allow_credit_sales:
                raise SaleRefused("Credit sales are switched off in Settings. Invoice it as cash, or switch credit sales on.")

            items = list(order.items.select_related("product__category"))
            locked = Product.objects.select_for_update().select_related("category").in_bulk(
                {item.product_id for item in items}
            )
            # Two lines of one product share the same shelf.
            stock_left = {
                pk: (Decimal(product.stock) if tracks_stock(product) else None) for pk, product in locked.items()
            }
            rows, planned = [], []
            for item in items:
                product = locked[item.product_id]
                if product.resolve_pack_level(item.unit_level) != item.unit_level or product.pack_factor(item.unit_level) != Decimal(item.unit_factor):
                    raise SaleRefused(
                        f"The packing of {product.display_name} changed after this order was booked. "
                        "Open the order, check that line and save it again."
                    )
                ordered = Decimal(item.quantity)
                if not product.is_active:
                    can = ZERO
                else:
                    can = _available_units(product, stock_left[product.pk], item.unit_level, Decimal(item.unit_factor))
                quantity = ordered if can is None else min(ordered, can)
                if quantity > 0 and stock_left[product.pk] is not None:
                    stock_left[product.pk] -= quantity * Decimal(item.unit_factor)
                planned.append((item, quantity))
                if quantity < ordered:
                    result.shortages.append(Shortage(product, item.unit_label, ordered, quantity))
                if quantity <= 0:
                    continue
                rows.append(
                    {
                        "product_id": product.pk,
                        "quantity": quantity,
                        "unit_level": item.unit_level,
                        "unit_price": Decimal(item.price),
                        "original_price": product.pack_price(item.unit_level),
                        "discount_percent": Decimal(item.discount_percent or 0),
                        "cash_category_tax_rate": category_tax_rate_for_product(product, Sale.CASH),
                        "bank_category_tax_rate": category_tax_rate_for_product(product, Sale.BANK),
                    }
                )
            if not rows:
                raise SaleRefused("Nothing on it is in stock, so no invoice was made.")
            # The customer's price list rates and the schemes, on what is actually invoiced.
            rows = apply_order_terms(
                rows, customer=customer, locked=locked, stock_left=stock_left, warnings=result.warnings
            )

            booker = order.order_booker if field_sales_enabled() and order.order_booker_id and order.order_booker.is_active else None
            sale, adjusted = record_sale(
                user=user,
                system_settings=system_settings,
                rows=rows,
                locked_products=locked,
                customer=customer,
                is_credit=credit,
                payment_method=Sale.CASH,
                bank_account=None,
                discount_rate=Decimal("0"),
                tax_rate=Decimal("0"),
                order_booker=booker,
                warn=result.warnings.append,
            )
            if adjusted:
                result.warnings.append("Rates or discounts that Settings no longer allow were set back to the price list.")
            for item, quantity in planned:
                item.invoiced_quantity = quantize_qty(quantity)
                item.save(update_fields=["invoiced_quantity"])
            order.status = SalesOrder.INVOICED
            order.sale = sale
            order.invoiced_at = timezone.now()
            order.save(update_fields=["status", "sale", "invoiced_at"])
            record_audit(
                action="sales_order_invoiced",
                description=f"Order {order.display_number} invoiced as {sale.display_number}.",
                actor=user,
                target=order,
                metadata={
                    "sale_id": sale.pk,
                    "total": str(sale.total_amount),
                    "credit": credit,
                    "short_lines": len(result.shortages),
                },
            )
            result.sale = sale
    except SaleRefused as exc:
        result.error = str(exc)
        result.shortages = []
    except ValidationError as exc:
        result.error = " ".join(exc.messages)
        result.shortages = []
    except (ValueError, DistributionError) as exc:
        result.error = str(exc)
        result.shortages = []
    return result


def invoice_orders(orders, *, user, credit=True):
    """Each order in its own savepoint: one refusal never stops the others."""
    return [invoice_order(order, user=user, credit=credit) for order in orders]


def conversion_messages(results):
    """(level, sentence) pairs for the screen after converting one or more orders."""
    done = [result for result in results if result.ok]
    refused = [result for result in results if not result.ok]
    lines = []
    if done:
        total = quantize_money(sum((Decimal(result.sale.total_amount) for result in done), ZERO))
        numbers = ", ".join(result.sale.display_number for result in done[:10])
        more = f" and {len(done) - 10} more" if len(done) > 10 else ""
        lines.append(("success", f"{len(done)} invoice{'s' if len(done) != 1 else ''} made ({numbers}{more}), {format_money(total)} in all."))
    for result in done:
        if result.shortages:
            lines.append(
                (
                    "warning",
                    f"{result.order.display_number} ({result.order.customer.name}) was short: "
                    + "; ".join(shortage.sentence() for shortage in result.shortages)
                    + ".",
                )
            )
        for warning in result.warnings:
            lines.append(("warning", f"{result.order.display_number}: {warning}"))
    for result in refused:
        lines.append(("error", f"{result.order.display_number} ({result.order.customer.name}) not invoiced. {result.error}"))
    return lines
