"""Recovery: money brought in from customers against what they owe.

The money is an ordinary customer receipt (``accounts.services.settlements.create_receipt``:
Dr Cash or Bank, Cr Accounts Receivable and the customer's ledger). The Recovery row beside it
says who brought it in, on which route and with which load, for the recovery reports. A bank
recovery follows the shop's bank receipt rule like any other customer receipt.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.models import Settlement
from accounts.services.audit import record_audit
from accounts.services.bank_receipts import MISSING_MESSAGE, attach_receipt, check_upload, receipts_required
from accounts.services.settlements import create_receipt
from pos.models import quantize_money
from pos.services.currency import format_money

from ..models import Recovery
from .common import DistributionError


def collector_name_for(collected_by, *, collector_booker=None, delivery_man=None, typed_name=""):
    if collected_by == Recovery.BY_ORDER_BOOKER and collector_booker is not None:
        return collector_booker.name
    if collected_by == Recovery.BY_DELIVERY_MAN and delivery_man is not None:
        return delivery_man.name
    return (typed_name or "").strip()[:100]


@transaction.atomic
def record_recovery(
    *,
    customer,
    amount,
    payment_method=Recovery.CASH,
    bank_account=None,
    collected_on=None,
    collected_by=Recovery.BY_OFFICE,
    collector_booker=None,
    delivery_man=None,
    collector_name="",
    load_form=None,
    sale=None,
    receipt_file=None,
    user=None,
):
    if customer is None:
        raise DistributionError("Choose the customer who paid.")
    try:
        amount = quantize_money(Decimal(str(amount)))
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise DistributionError("Enter the amount collected.") from exc
    if amount <= 0:
        raise DistributionError("The amount collected must be more than zero.")
    if payment_method not in (Recovery.CASH, Recovery.BANK):
        raise DistributionError("Choose cash or bank.")
    if collected_by not in dict(Recovery.COLLECTOR_CHOICES):
        collected_by = Recovery.BY_OFFICE
    if collected_by == Recovery.BY_ORDER_BOOKER and collector_booker is None:
        raise DistributionError("Choose the order booker who collected it.")
    if collected_by == Recovery.BY_DELIVERY_MAN and delivery_man is None:
        raise DistributionError("Choose the delivery man who collected it.")

    checked = None
    if payment_method == Recovery.BANK:
        if receipt_file:
            try:
                checked = check_upload(receipt_file)
            except ValidationError as exc:
                raise DistributionError(" ".join(exc.messages)) from exc
        elif receipts_required():
            raise DistributionError(f"{customer.name}: {MISSING_MESSAGE}")
    else:
        bank_account = None

    collected_on = collected_on or timezone.localdate()
    voucher = create_receipt(
        party=customer,
        amount=amount,
        payment_method=payment_method,
        bank_account=bank_account,
        date=collected_on,
        actor=user,
    )
    settlement = Settlement.objects.get(voucher=voucher)
    name = collector_name_for(collected_by, collector_booker=collector_booker, delivery_man=delivery_man, typed_name=collector_name)
    note = f"Recovery{' by ' + name if name else ''}"
    if load_form is not None:
        note += f" on load {load_form.display_number}"
    settlement.notes = note[:255]
    settlement.save(update_fields=["notes"])
    if checked is not None:
        attach_receipt(settlement, checked, actor=user, audit=False)

    route = customer.route if customer.route_id else (load_form.route if load_form is not None else None)
    recovery = Recovery.objects.create(
        settlement=settlement,
        customer=customer,
        route=route,
        order_booker=customer.order_booker if customer.order_booker_id else None,
        collected_by=collected_by,
        collector_booker=collector_booker if collected_by == Recovery.BY_ORDER_BOOKER else None,
        delivery_man=delivery_man if collected_by == Recovery.BY_DELIVERY_MAN else None,
        collector_name=name,
        load_form=load_form,
        sale=sale,
        amount=amount,
        payment_method=payment_method,
        collected_on=collected_on,
        created_by=user,
    )
    record_audit(
        action="recovery_recorded",
        description=f"Recovery of {format_money(amount)} from {customer.name}{' by ' + name if name else ''}.",
        actor=user,
        target=recovery,
        metadata={
            "settlement_id": settlement.pk,
            "amount": str(amount),
            "payment_method": payment_method,
            "load_form": load_form.pk if load_form is not None else None,
        },
    )
    return recovery
