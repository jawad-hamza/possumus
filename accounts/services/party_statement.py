from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from accounts.models import PartyLedgerEntry, Voucher


CUSTOMER = "customer"
SUPPLIER = "supplier"
UNIFIED = "unified"


def get_party_statement(*, party, from_date=None, to_date=None):
    role = _party_role(party)
    ct = ContentType.objects.get_for_model(party)

    base_qs = PartyLedgerEntry.objects.filter(
        content_type=ct,
        object_id=party.id,
    ).select_related("voucher")

    opening_balance = Decimal("0")
    if from_date:
        for entry in base_qs.filter(voucher__date__lt=from_date):
            opening_balance += entry.debit - entry.credit

    entries = base_qs
    if from_date:
        entries = entries.filter(voucher__date__gte=from_date)
    if to_date:
        entries = entries.filter(voucher__date__lte=to_date)

    entries = list(entries.order_by("voucher__date", "id"))
    documents = _document_labels([entry.voucher for entry in entries])

    rows = []
    running = opening_balance
    for entry in entries:
        running += entry.debit - entry.credit
        rows.append(
            {
                "date": entry.voucher.date,
                "voucher_id": entry.voucher.id,
                "voucher_type": entry.voucher.voucher_type,
                "ref": f"{entry.voucher.ref_model}#{entry.voucher.ref_id}",
                "document": documents.get(entry.voucher.id, ""),
                "debit": entry.debit,
                "credit": entry.credit,
                "running_balance": running,
                "balance_type": _balance_type(running, role),
                "display_balance": abs(running),
            }
        )

    return {
        "party": str(party),
        "opening_balance": opening_balance,
        "closing_balance": running,
        "balance_type": _balance_type(running, role),
        "rows": rows,
    }



def get_unified_party_statement(*, customer=None, supplier=None, from_date=None, to_date=None):
    rows = []
    opening_balance = Decimal("0")
    party_labels = []

    if customer:
        customer_data = get_party_statement(
            party=customer,
            from_date=from_date,
            to_date=to_date,
        )
        opening_balance += customer_data["opening_balance"]
        party_labels.append(str(customer))
        for row in customer_data["rows"]:
            rows.append({**row, "role": CUSTOMER})

    if supplier:
        supplier_data = get_party_statement(
            party=supplier,
            from_date=from_date,
            to_date=to_date,
        )
        opening_balance += supplier_data["opening_balance"]
        party_labels.append(str(supplier))
        for row in supplier_data["rows"]:
            rows.append({**row, "role": SUPPLIER})

    rows.sort(key=lambda row: (row["date"], row["voucher_id"], row.get("role", "")))

    running = opening_balance
    final_rows = []
    for row in rows:
        running += row["debit"] - row["credit"]
        row["running_balance"] = running
        row["balance_type"] = _balance_type(running, UNIFIED)
        row["display_balance"] = abs(running)
        final_rows.append(row)

    return {
        "party": " / ".join(party_labels),
        "opening_balance": opening_balance,
        "closing_balance": running,
        "balance_type": _balance_type(running, UNIFIED),
        "rows": final_rows,
    }



def _document_labels(vouchers) -> dict:
    """voucher id -> the paper the shop recognises: "Invoice INV-00024", "Receipt 7", "Opening balance"."""
    from pos.models import Purchase, Sale

    sale_ids = {v.ref_id for v in vouchers if v.ref_model == "Sale" and v.ref_id}
    purchase_ids = {v.ref_id for v in vouchers if v.ref_model == "Purchase" and v.ref_id}
    invoices = {pk: number for pk, number in Sale.objects.filter(pk__in=sale_ids).values_list("pk", "invoice_number")}
    bills = {
        pk: number
        for pk, number in Purchase.objects.filter(pk__in=purchase_ids).values_list("pk", "supplier_invoice_number")
    }

    labels = {}
    for voucher in vouchers:
        model, ref = voucher.ref_model, voucher.ref_id
        if model == "Sale":
            label = f"Invoice {invoices.get(ref) or f'#{ref}'}"
        elif model == "Purchase":
            label = f"Purchase #{ref}" + (f" (bill {bills[ref]})" if bills.get(ref) else "")
        elif model == "SaleReturn":
            label = f"Sale return #{ref}"
        elif model == "PurchaseReturn":
            label = f"Purchase return #{ref}"
        elif model == "Settlement":
            label = f"{'Receipt' if voucher.voucher_type == Voucher.RECEIPT else 'Payment'} #{ref}"
        elif model in ("CustomerOpeningBalance", "SupplierOpeningBalance"):
            label = "Opening balance"
        else:
            label = voucher.get_voucher_type_display()
        labels[voucher.id] = label
    return labels


def _party_role(party) -> str:
    model_name = party.__class__.__name__.lower()
    if model_name == "supplier":
        return SUPPLIER
    return CUSTOMER



def _balance_type(balance: Decimal, role: str) -> str:
    if role == CUSTOMER:
        if balance > 0:
            return "receivable"
        if balance < 0:
            return "advance"
        return "clear"

    if role == SUPPLIER:
        if balance < 0:
            return "payable"
        if balance > 0:
            return "advance"
        return "clear"

    if balance > 0:
        return "receivable"
    if balance < 0:
        return "payable"
    return "clear"
