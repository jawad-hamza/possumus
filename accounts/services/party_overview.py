from decimal import Decimal
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.contrib.contenttypes.models import ContentType

from accounts.models import PartyLedgerEntry


# -------------------------------------------------
# INTERNAL HELPERS
# -------------------------------------------------

def _sum(qs, field):
    return qs.aggregate(
        total=Coalesce(Sum(field), Decimal("0"))
    )["total"]


# -------------------------------------------------
# CUSTOMER OVERVIEW
# -------------------------------------------------

def get_customer_overview(customer) -> dict:
    """
    Customer balances come from:
    - POS sales on credit
    - settlements (received payments)
    """

    ct = ContentType.objects.get_for_model(customer)

    qs = PartyLedgerEntry.objects.filter(
        content_type=ct,
        object_id=customer.id,
    )

    debit = _sum(qs, "debit")
    credit = _sum(qs, "credit")

    balance = debit - credit   # positive = receivable

    return {
        "type": "customer",
        "name": str(customer),
        "balance": balance,
        "receivable": balance if balance > 0 else Decimal("0"),
        "payable": Decimal("0"),
        "summary": {
            "total_sales_credit": debit,
            "total_received": credit,
        }
    }


# -------------------------------------------------
# SUPPLIER OVERVIEW
# -------------------------------------------------

def get_supplier_overview(supplier) -> dict:
    """
    Supplier balances come from:
    - purchases on credit
    - settlements (payments made)
    """

    ct = ContentType.objects.get_for_model(supplier)

    qs = PartyLedgerEntry.objects.filter(
        content_type=ct,
        object_id=supplier.id,
    )

    debit = _sum(qs, "debit")
    credit = _sum(qs, "credit")

    balance = credit - debit   # positive = payable

    return {
        "type": "supplier",
        "name": str(supplier),
        "balance": balance,
        "receivable": Decimal("0"),
        "payable": balance if balance > 0 else Decimal("0"),
        "summary": {
            "total_purchase_credit": credit,
            "total_paid": debit,
        }
    }