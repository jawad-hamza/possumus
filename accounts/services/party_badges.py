from decimal import Decimal

from accounts.services.party_balance import get_party_balance


def _get_raw_balance(party):
    return get_party_balance(party)


def get_customer_badge(customer):
    balance = _get_raw_balance(customer)

    if balance > 0:
        return {"balance_type": "receivable", "amount": balance}
    if balance < 0:
        return {"balance_type": "advance", "amount": abs(balance)}
    return {"balance_type": "clear", "amount": Decimal("0.00")}


def get_supplier_badge(supplier):
    balance = _get_raw_balance(supplier)

    if balance < 0:
        return {"balance_type": "payable", "amount": abs(balance)}
    if balance > 0:
        return {"balance_type": "advance", "amount": balance}
    return {"balance_type": "clear", "amount": Decimal("0.00")}
