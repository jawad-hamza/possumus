"""
Books lock (period close).

Once books_locked_until is set, nothing dated on or before it may be posted,
rewritten or deleted. The checks are wired to model signals in
accounts/signals.py, so every posting service is covered, including the ones
that live in the pos app, without each of them having to remember to ask.

Known gap: QuerySet.update() and bulk_create() do not send signals. No posting
service uses them for vouchers; keep it that way.
"""

import datetime

from django.utils import timezone

from accounts.models import AccountingSettings, Voucher


def get_books_locked_until():
    return (
        AccountingSettings.objects.filter(pk=1)
        .values_list("books_locked_until", flat=True)
        .first()
    )


def normalize_voucher_date(value):
    """Vouchers carry a plain local date; callers sometimes hand over None or a datetime."""
    if value is None:
        return timezone.localdate()
    if isinstance(value, datetime.datetime):
        if timezone.is_aware(value):
            return timezone.localtime(value).date()
        return value.date()
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(f"Invalid voucher date: {value!r}") from exc
    return value


def is_date_locked(value, locked_until=None) -> bool:
    if locked_until is None:
        locked_until = get_books_locked_until()
    if locked_until is None or value is None:
        return False
    return normalize_voucher_date(value) <= locked_until


def assert_books_open(value, *, action="post an entry"):
    locked_until = get_books_locked_until()
    if locked_until is None:
        return
    entry_date = normalize_voucher_date(value)
    if entry_date <= locked_until:
        raise ValueError(
            f"The books are locked up to {locked_until:%Y-%m-%d}. "
            f"You cannot {action} dated {entry_date:%Y-%m-%d}. "
            "Use a later date, or ask an administrator to move the books lock from the Accounts page."
        )


def assert_voucher_open(voucher, *, action="change an entry"):
    """Refuse when the voucher, as stored, already sits inside the locked period."""
    if voucher is None or voucher.pk is None:
        return
    stored_date = Voucher.objects.filter(pk=voucher.pk).values_list("date", flat=True).first()
    if stored_date is not None:
        assert_books_open(stored_date, action=action)


def set_books_lock(locked_until, *, actor=None) -> AccountingSettings:
    """locked_until=None reopens the books."""
    if locked_until is not None:
        locked_until = normalize_voucher_date(locked_until)
        if locked_until > timezone.localdate():
            raise ValueError("The books lock date cannot be in the future.")
    config = AccountingSettings.load()
    config.books_locked_until = locked_until
    config.locked_by = actor if locked_until is not None else None
    config.save()
    return config
