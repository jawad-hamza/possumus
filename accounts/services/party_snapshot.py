from decimal import Decimal

from pos.models import Supplier
from accounts.services.party_overview import (
    get_customer_overview,
    get_supplier_overview,
)
from accounts.services.party_matcher import normalize_phone


def _find_supplier_for_customer(customer):
    """
    Try to find matching supplier by phone.
    Returns Supplier or None.
    """
    phone = normalize_phone(getattr(customer, "phone", None))
    if not phone:
        return None

    for supplier in Supplier.objects.all():
        if normalize_phone(getattr(supplier, "phone", None)) == phone:
            return supplier

    return None


def get_party_snapshot(*, customer=None, supplier=None) -> dict:
    """
    Unified snapshot for a real-world person/business.

    Matching rule (EASY MODE):
    - explicit supplier passed → use it
    - else try phone-based match
    """

    customer_data = None
    supplier_data = None

    receivable = Decimal("0")
    payable = Decimal("0")

    name = None

    # --- CUSTOMER ---
    if customer:
        customer_data = get_customer_overview(customer)
        receivable = customer_data["receivable"]
        name = customer_data["name"]

        # try auto-match supplier if not provided
        if supplier is None:
            supplier = _find_supplier_for_customer(customer)

    # --- SUPPLIER ---
    if supplier:
        supplier_data = get_supplier_overview(supplier)
        payable = supplier_data["payable"]
        name = name or supplier_data["name"]

    net_exposure = receivable - payable

    return {
        "name": name,
        "customer": customer_data,
        "supplier": supplier_data,
        "net_exposure": net_exposure,
    }