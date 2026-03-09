from django.db import transaction

from accounts.models import Account, AccountingEvent, Voucher, VoucherLine
from accounts.services.party_ledger import post_party_ledger
from accounts.services.pos_posting import post_purchase_from_pos, post_sale_from_pos
from pos.models import Purchase, Sale


def get_account(title):
    try:
        return Account.objects.get(title=title, is_active=True)
    except Account.DoesNotExist as exc:
        raise RuntimeError(f"Required account missing: {title}") from exc


@transaction.atomic
def process_event(event: AccountingEvent):
    if event.processed:
        return

    if event.ref_model == "Sale":
        sale = Sale.objects.get(id=event.ref_id)
        post_sale_from_pos(
            sale_id=sale.id,
            customer=sale.customer,
            total_amount=sale.total_amount,
            paid_amount=sale.paid_amount,
            payment_method=sale.payment_method,
            sale_date=sale.created_at.date(),
        )
        event.processed = True
        event.save(update_fields=["processed"])
        return

    if event.ref_model == "Purchase":
        purchase = Purchase.objects.get(id=event.ref_id)
        post_purchase_from_pos(
            purchase_id=purchase.id,
            supplier=purchase.supplier,
            total_amount=purchase.total_amount,
            paid_amount=purchase.paid_amount,
            payment_method=purchase.payment_method,
            purchase_date=purchase.created_at.date(),
        )
        event.processed = True
        event.save(update_fields=["processed"])
        return

    if event.event_type == AccountingEvent.SALE:
        _process_sale(event)
    elif event.event_type == AccountingEvent.PURCHASE:
        _process_purchase(event)
    else:
        return

    event.processed = True
    event.save(update_fields=["processed"])


def _process_sale(event: AccountingEvent):
    if Voucher.objects.filter(
        ref_model=event.ref_model,
        ref_id=event.ref_id,
        voucher_type=Voucher.SALE,
    ).exists():
        return

    cash = get_account("Cash")
    sales = get_account("Sales")
    ar = get_account("Accounts Receivable")

    voucher = Voucher.objects.create(
        voucher_type=Voucher.SALE,
        narration=f"Sale #{event.ref_id}",
        ref_model=event.ref_model,
        ref_id=event.ref_id,
        date=event.created_at.date(),
    )

    if event.is_credit:
        VoucherLine.objects.create(voucher=voucher, account=ar, debit=event.amount)
        VoucherLine.objects.create(voucher=voucher, account=sales, credit=event.amount)
    else:
        VoucherLine.objects.create(voucher=voucher, account=cash, debit=event.amount)
        VoucherLine.objects.create(voucher=voucher, account=sales, credit=event.amount)

    voucher.clean()

    if event.is_credit:
        sale = Sale.objects.get(id=event.ref_id)
        post_party_ledger(voucher=voucher, party=sale.customer, debit=event.amount, credit=0)



def _process_purchase(event: AccountingEvent):
    if Voucher.objects.filter(
        ref_model=event.ref_model,
        ref_id=event.ref_id,
        voucher_type=Voucher.PURCHASE,
    ).exists():
        return

    inventory = get_account("Inventory")
    cash = get_account("Cash")
    ap = get_account("Accounts Payable")

    voucher = Voucher.objects.create(
        voucher_type=Voucher.PURCHASE,
        narration=f"Purchase #{event.ref_id}",
        ref_model=event.ref_model,
        ref_id=event.ref_id,
        date=event.created_at.date(),
    )

    if event.is_credit:
        VoucherLine.objects.create(voucher=voucher, account=inventory, debit=event.amount)
        VoucherLine.objects.create(voucher=voucher, account=ap, credit=event.amount)
    else:
        VoucherLine.objects.create(voucher=voucher, account=inventory, debit=event.amount)
        VoucherLine.objects.create(voucher=voucher, account=cash, credit=event.amount)

    voucher.clean()

    if event.is_credit:
        purchase = Purchase.objects.get(id=event.ref_id)
        post_party_ledger(voucher=voucher, party=purchase.supplier, debit=0, credit=event.amount)
