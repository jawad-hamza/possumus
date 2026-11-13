"""Turning a finished cash or bank sale into a credit sale, fully or in part.

A shop may take the goods and not pay after all, or pay only part. The sale keeps its lines,
stock and invoice number; only how it was settled changes. The sale's voucher is posted again
through the same entry point the till uses, so cash or bank goes down by what was not received
and the customer's account goes up by it. Nothing here moves stock.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from accounts.services.audit import record_audit
from accounts.services.pos_posting import post_sale_from_pos
from pos.models import Customer, Sale, SystemSettings, quantize_money
from pos.services.field_sales import credit_limit_problem, credit_limit_refuses
from pos.services.currency import format_money


ZERO = Decimal("0")


class PaymentChangeRefused(ValueError):
    """The change cannot be made; the message is safe to show to the user."""


def can_change_to_credit(sale):
    """'' when a paid (or partly paid) sale can be put on credit, otherwise the reason it cannot."""
    if Decimal(sale.paid_amount or 0) <= ZERO:
        return "This sale is already fully on credit."
    if Decimal(sale.total_amount or 0) <= ZERO:
        return "A sale that adds up to zero has nothing to put on credit."
    if sale.returns.exists():
        return "Goods from this sale were already returned, so its payment can no longer be changed."
    load_entry = getattr(sale, "load_entry", None) if hasattr(Sale, "load_entry") else None
    try:
        load_form = load_entry.load_form if load_entry is not None else None
    except Exception:  # noqa: BLE001 - no distribution app, or no load
        load_form = None
    if load_form is not None and getattr(load_form, "is_settled", False):
        return f"This invoice was settled on load {load_form.display_number}, so its cash is already counted."
    return ""


def _amount(value):
    try:
        amount = Decimal(str(value if value not in (None, "") else 0))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PaymentChangeRefused("Enter the amount received as a number.") from exc
    return quantize_money(amount)


@transaction.atomic
def change_sale_to_credit(*, sale, received=ZERO, customer=None, reason, actor=None):
    """Put what was not received on the customer's account.

    ``received`` is what the shop really paid (0 for the whole bill on credit). ``customer`` is
    needed only for a walk-in sale. Returns the list of warnings (a credit limit passed on a
    "warn only" setting); raises PaymentChangeRefused with the reason otherwise.
    """
    sale = Sale.objects.select_for_update().select_related("customer", "bank_account").get(pk=sale.pk)
    problem = can_change_to_credit(sale)
    if problem:
        raise PaymentChangeRefused(problem)
    reason = (reason or "").strip()
    if not reason:
        raise PaymentChangeRefused("Say why the payment is changed, for example: the shop did not pay.")

    total = quantize_money(sale.total_amount or 0)
    old_paid = quantize_money(sale.paid_amount or 0)
    received = _amount(received)
    if received < ZERO:
        raise PaymentChangeRefused("The amount received cannot be negative.")
    if received >= old_paid:
        raise PaymentChangeRefused(
            f"The amount received must be less than the {format_money(old_paid)} recorded as paid now."
        )

    if sale.customer_id is None:
        if customer is None:
            raise PaymentChangeRefused("Choose the customer who owes this bill.")
        if not Customer.objects.filter(pk=customer.pk, is_active=True).exists():
            raise PaymentChangeRefused("Choose an active customer.")
        sale.customer = customer
    customer = sale.customer

    on_credit = quantize_money(total - received)
    added = quantize_money(old_paid - received)
    warnings = []
    over_limit = credit_limit_problem(customer, added, lock=True)
    if over_limit:
        if credit_limit_refuses(SystemSettings.load()):
            raise PaymentChangeRefused(over_limit)
        warnings.append(over_limit)

    sale.paid_amount = received
    sale.is_credit = True
    if received <= ZERO:
        # Nothing was received, so no cash drawer or bank ledger is involved any more.
        sale.bank_account = None
    sale.save(update_fields=["paid_amount", "is_credit", "bank_account", "customer"])

    post_sale_from_pos(
        sale_id=sale.id,
        customer=customer,
        total_amount=sale.total_amount,
        paid_amount=sale.paid_amount,
        payment_method=sale.payment_method,
        bank_account=sale.bank_account,
        sale_date=timezone.localtime(sale.created_at).date(),
        tax_amount=sale.tax_amount,
        actor=actor,
        allow_zero_total=total <= ZERO,
    )
    record_audit(
        action="sale_changed_to_credit",
        description=(
            f"Sale {sale.display_number} changed from paid {format_money(old_paid)} to paid "
            f"{format_money(received)}; {format_money(on_credit)} now on {customer.name}'s account. Reason: {reason}"
        ),
        actor=actor,
        target=sale,
        metadata={"old_paid": str(old_paid), "new_paid": str(received), "on_credit": str(on_credit), "reason": reason},
    )
    return warnings
