from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver

from accounts.models import PartyLedgerEntry, Voucher, VoucherLine
from accounts.services.books_lock import assert_books_open, assert_voucher_open, normalize_voucher_date
from accounts.services.current_actor import get_current_actor


# Settlements are posted explicitly by accounts.services.settlements (and by the
# admin's save_model); there is deliberately no post_save receiver for them.


@receiver(pre_save, sender=Voucher, dispatch_uid="accounts.voucher.guard_save")
def guard_voucher_save(sender, instance, raw=False, **kwargs):
    if raw:
        return
    instance.date = normalize_voucher_date(instance.date)
    if instance.pk:
        assert_voucher_open(instance, action="rewrite an entry")
    assert_books_open(instance.date, action="post an entry")
    if instance._state.adding and instance.created_by_id is None:
        instance.created_by = get_current_actor()


@receiver(pre_delete, sender=Voucher, dispatch_uid="accounts.voucher.guard_delete")
def guard_voucher_delete(sender, instance, **kwargs):
    assert_books_open(instance.date, action="delete an entry")


def _guard_voucher_child(sender, instance, raw=False, **kwargs):
    if raw:
        return
    voucher = instance.voucher
    assert_books_open(voucher.date, action="change the lines of an entry")


for _child in (VoucherLine, PartyLedgerEntry):
    pre_save.connect(_guard_voucher_child, sender=_child, dispatch_uid=f"accounts.{_child.__name__}.guard_save")
    pre_delete.connect(_guard_voucher_child, sender=_child, dispatch_uid=f"accounts.{_child.__name__}.guard_delete")
