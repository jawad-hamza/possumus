import threading
from decimal import Decimal, InvalidOperation

from django.core.signals import request_finished
from django.db.models.signals import post_save

from pos.models import SystemSettings, quantize_money

# A page (or a PDF receipt) formats dozens of amounts. The context processor
# primes the symbol once per request so formatting does not query SystemSettings
# for every amount; the value is dropped when the request ends or the settings
# row is saved.
_request_cache = threading.local()


def prime_currency_symbol(symbol):
    _request_cache.symbol = symbol or ""


def _forget_currency_symbol(**kwargs):
    _request_cache.__dict__.pop("symbol", None)


request_finished.connect(_forget_currency_symbol, dispatch_uid="pos.currency.request_finished")
post_save.connect(_forget_currency_symbol, sender=SystemSettings, dispatch_uid="pos.currency.settings_saved")


def currency_symbol():
    symbol = getattr(_request_cache, "symbol", None)
    if symbol is None:
        symbol = SystemSettings.load().currency_symbol or ""
    return symbol


def format_amount(value):
    """Thousands-separated 2dp number without the currency symbol."""
    try:
        number = Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except (InvalidOperation, ValueError, TypeError):
        number = Decimal("0")
    return f"{quantize_money(number):,.2f}"


def format_money(value):
    return f"{currency_symbol()} {format_amount(value)}".strip()
