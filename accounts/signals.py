from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import LoanTransactions
from accounts.services.loan_accounting import post_loan_transaction


@receiver(post_save, sender=LoanTransactions)
def create_voucher_for_loan_transaction(sender, instance, created, **kwargs):
    if not created:
        return

    if instance.voucher_id:
        return

    voucher = post_loan_transaction(instance)
    instance.voucher = voucher
    instance.save(update_fields=["voucher"])
