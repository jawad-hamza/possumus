from django.db import transaction
from accounts.models import AccountingEvent, Account, Voucher, VoucherLine
from accounts.services.party_ledger import post_party_ledger
from pos.models import Purchase, Sale


def get_account(title):
    try:
        return Account.objects.get(title=title, is_active=True)
    except Account.DoesNotExist:
        raise RuntimeError(f"Required account missing: {title}")


@transaction.atomic
def process_event(event: AccountingEvent):
    if event.processed:
        return

    if event.event_type == AccountingEvent.SALE:
        _process_sale(event)

    elif event.event_type == AccountingEvent.PURCHASE:
        _process_purchase(event)

    else :
        return

    # mark event as processed
    event.processed = True
    event.save(update_fields=["processed"])


def _process_sale(event: AccountingEvent):
    if Voucher.objects.filter(
        ref_model=event.ref_model,
        ref_id=event.ref_id,
        voucher_type=Voucher.SALE,
    ).exists():
        event.processed = True
        event.save(update_fields=["processed"])
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

    # ---- Party Ledger (ONLY credit sale) ----
    if event.is_credit:
        sale = Sale.objects.get(id=event.ref_id)

        post_party_ledger(
            voucher=voucher,
            party=sale.customer,
            debit=event.amount,
            credit=0
        )

    event.processed = True
    event.save(update_fields=["processed"])

def _process_purchase(event: AccountingEvent):
    if Voucher.objects.filter(
        ref_model=event.ref_model,
        ref_id=event.ref_id,
        voucher_type=Voucher.PURCHASE,
    ).exists():
        event.processed = True
        event.save(update_fields=["processed"])
        return

    purchase_acc = get_account("Purchase")
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
        VoucherLine.objects.create(voucher=voucher, account=purchase_acc, debit=event.amount)
        VoucherLine.objects.create(voucher=voucher, account=ap, credit=event.amount)
    else:
        VoucherLine.objects.create(voucher=voucher, account=purchase_acc, debit=event.amount)
        VoucherLine.objects.create(voucher=voucher, account=cash, credit=event.amount)

    voucher.clean()

    # ---- Party Ledger (ONLY credit purchase) ----
    if event.is_credit:
        purchase = Purchase.objects.get(id=event.ref_id)

        post_party_ledger(
            voucher=voucher,
            party=purchase.supplier,
            debit=0,
            credit=event.amount
        )

    event.processed = True
    event.save(update_fields=["processed"])
