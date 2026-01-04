from decimal import Decimal

def normalize_balance(amount):
    """
    Returns (abs_amount, Dr/Cr)
    """
    if amount is None:
        return Decimal("0"), "Dr"

    if amount < 0:
        return abs(amount), "Cr"
    return amount, "Dr"
