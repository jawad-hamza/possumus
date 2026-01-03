from django.utils import timezone
from django.db import transaction

from accounts.models import Account, Voucher, VoucherLine


def post_cash_sale(amount, ref_model=None, ref_id=None):
    """
    Manually posts a CASH SALE voucher.
    """

    cash = Account.objects.get(title="Cash")
    sales = Account.objects.get(title="Sales")

    with transaction.atomic():
        voucher = Voucher.objects.create(
            date=timezone.now().date(),
            voucher_type="sale",
            narration="Manual cash sale test",
            ref_model=ref_model,
            ref_id=ref_id,
        )

        VoucherLine.objects.create(
            voucher=voucher,
            account=cash,
            debit=amount,
            credit=0,
        )

        VoucherLine.objects.create(
            voucher=voucher,
            account=sales,
            debit=0,
            credit=amount,
        )

        voucher.clean()

    return voucher
