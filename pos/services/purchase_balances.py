from collections import defaultdict, deque
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Sum

from accounts.models import Settlement
from pos.models import Purchase, Supplier, quantize_money


ZERO = Decimal("0.00")


def _money(value):
    return quantize_money(Decimal(value or 0))


def _payment_status(total, paid, due):
    if total <= 0:
        return "draft"
    if due <= 0:
        return "paid"
    if paid > 0:
        return "partial"
    return "credit"


def outstanding_purchase_total():
    """What is still owed on all purchases, without loading every purchase.

    annotate_purchase_balances() hands each supplier's payments to the oldest bills first
    and carries any excess forward, so the amount left per supplier is simply
    unpaid-at-purchase minus payments made since, never below zero.
    """
    due_by_supplier = defaultdict(lambda: ZERO)
    for supplier_id, total, paid in Purchase.objects.values_list("supplier_id", "total_amount", "paid_amount").iterator():
        total = _money(total)
        due_by_supplier[supplier_id] += max(total - min(_money(paid), total), ZERO)

    supplier_type = ContentType.objects.get_for_model(Supplier)
    settled = dict(
        Settlement.objects.filter(settlement_type=Settlement.PAYMENT, content_type=supplier_type)
        .values("object_id")
        .annotate(total=Sum("amount"))
        .values_list("object_id", "total")
    )
    outstanding = ZERO
    for supplier_id, due in due_by_supplier.items():
        if supplier_id is not None:
            due = max(due - _money(settled.get(supplier_id)), ZERO)
        outstanding += due
    return quantize_money(outstanding)


def annotate_purchase_balances(purchases):
    rows = list(purchases)
    if not rows:
        return rows

    supplier_ids = sorted({purchase.supplier_id for purchase in rows if purchase.supplier_id})
    state_by_purchase = {}

    for purchase in rows:
        total = _money(purchase.total_amount)
        initial_paid = min(_money(purchase.paid_amount), total)
        due = quantize_money(max(total - initial_paid, ZERO))
        purchase.report_initial_paid = initial_paid
        purchase.report_settlement_paid = ZERO
        purchase.report_paid_amount = initial_paid
        purchase.report_balance_due = due
        purchase.report_payment_status = _payment_status(total, initial_paid, due)

    if not supplier_ids:
        return rows

    all_purchases = list(
        Purchase.objects.filter(supplier_id__in=supplier_ids)
        .only("id", "supplier_id", "total_amount", "paid_amount", "created_at")
        .order_by("created_at", "id")
    )
    supplier_type = ContentType.objects.get_for_model(Supplier)
    settlements = list(
        Settlement.objects.filter(
            settlement_type=Settlement.PAYMENT,
            content_type=supplier_type,
            object_id__in=supplier_ids,
        )
        .only("id", "object_id", "amount", "created_at")
        .order_by("created_at", "id")
    )

    purchases_by_supplier = defaultdict(list)
    settlements_by_supplier = defaultdict(list)

    for purchase in all_purchases:
        total = _money(purchase.total_amount)
        initial_paid = min(_money(purchase.paid_amount), total)
        state_by_purchase[purchase.id] = {
            "total": total,
            "initial_paid": initial_paid,
            "settlement_paid": ZERO,
            "due": quantize_money(max(total - initial_paid, ZERO)),
        }
        purchases_by_supplier[purchase.supplier_id].append(purchase)

    for settlement in settlements:
        settlements_by_supplier[settlement.object_id].append(settlement)

    for supplier_id in supplier_ids:
        open_purchases = deque()
        advance_pool = ZERO
        supplier_purchases = purchases_by_supplier[supplier_id]
        supplier_settlements = settlements_by_supplier[supplier_id]
        purchase_index = 0
        settlement_index = 0

        while purchase_index < len(supplier_purchases) or settlement_index < len(supplier_settlements):
            next_purchase = supplier_purchases[purchase_index] if purchase_index < len(supplier_purchases) else None
            next_settlement = supplier_settlements[settlement_index] if settlement_index < len(supplier_settlements) else None

            process_purchase = next_purchase is not None and (
                next_settlement is None or next_purchase.created_at <= next_settlement.created_at
            )

            if process_purchase:
                state = state_by_purchase[next_purchase.id]
                if advance_pool > ZERO and state["due"] > ZERO:
                    allocated = min(advance_pool, state["due"])
                    state["settlement_paid"] = quantize_money(state["settlement_paid"] + allocated)
                    state["due"] = quantize_money(state["due"] - allocated)
                    advance_pool = quantize_money(advance_pool - allocated)

                if state["due"] > ZERO:
                    open_purchases.append(next_purchase.id)
                purchase_index += 1
                continue

            remaining = _money(next_settlement.amount)
            while remaining > ZERO and open_purchases:
                purchase_id = open_purchases[0]
                state = state_by_purchase[purchase_id]
                allocated = min(remaining, state["due"])
                state["settlement_paid"] = quantize_money(state["settlement_paid"] + allocated)
                state["due"] = quantize_money(state["due"] - allocated)
                remaining = quantize_money(remaining - allocated)
                if state["due"] <= ZERO:
                    open_purchases.popleft()

            if remaining > ZERO:
                advance_pool = quantize_money(advance_pool + remaining)
            settlement_index += 1

    for purchase in rows:
        state = state_by_purchase.get(purchase.id)
        if state is None:
            continue
        purchase.report_initial_paid = state["initial_paid"]
        purchase.report_settlement_paid = state["settlement_paid"]
        purchase.report_paid_amount = quantize_money(state["initial_paid"] + state["settlement_paid"])
        purchase.report_balance_due = state["due"]
        purchase.report_payment_status = _payment_status(
            state["total"],
            purchase.report_paid_amount,
            purchase.report_balance_due,
        )

    return rows
