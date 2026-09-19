"""
Who is acting right now.

Vouchers are created many layers below the view, by services whose signatures
belong to other apps. Instead of threading a user through all of them, the
request middleware records the signed-in user here and Voucher picks it up
when no explicit actor was given.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_current_actor = ContextVar("accounts_current_actor", default=None)


def get_current_actor():
    actor = _current_actor.get()
    if actor is None or not getattr(actor, "is_authenticated", False):
        return None
    return actor


def set_current_actor(actor):
    return _current_actor.set(actor)


def reset_current_actor(token):
    _current_actor.reset(token)


@contextmanager
def acting_as(actor):
    """Attribute vouchers posted inside the block to `actor` (None keeps the current one)."""
    if actor is None:
        yield
        return
    token = _current_actor.set(actor)
    try:
        yield
    finally:
        _current_actor.reset(token)
