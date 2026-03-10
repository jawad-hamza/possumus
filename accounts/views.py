from decimal import Decimal

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.forms import (
    CapitalEntryForm,
    ExpenseCategoryForm,
    ExpenseForm,
    ExpensePaymentForm,
    MoneyTransferForm,
)
from accounts.models import (
    Account,
    CapitalEntry,
    Expense,
    ExpenseCategory,
    ExpensePayment,
    MoneyTransfer,
    PartyLedgerEntry,
    Voucher,
    VoucherLine,
)
from accounts.services.audit import record_audit
from accounts.services.balance_sheet import get_balance_sheet
from accounts.services.cash_bank import bank_account_queryset, total_bank_balance
from accounts.services.expense_alerts import expense_initial_for_category, monthly_bill_alerts
from accounts.services.expense_posting import post_expense
from accounts.services.money_desk import post_capital_entry, post_expense_payment, post_money_transfer
from accounts.services.profit_loss import get_profit_and_loss
from accounts.services.trial_balance import get_trial_balance
from pos.services.access import module_access_required


ZERO = Decimal("0.00")

DEFAULT_EXPENSE_CONFIG = {
    "Rent": {"expense_pattern": ExpenseCategory.MONTHLY_FIXED, "alert_days_before": 7},
    "Electricity": {"expense_pattern": ExpenseCategory.MONTHLY_VARIABLE, "alert_days_before": 7},
    "Wages": {"expense_pattern": ExpenseCategory.MONTHLY_FIXED, "alert_days_before": 7},
    "Misc": {"expense_pattern": ExpenseCategory.CUSTOM, "alert_days_before": 7},
}
DEFAULT_EXPENSE_TYPES = tuple(DEFAULT_EXPENSE_CONFIG.keys())


def _ensure_default_expense_categories():
    categories = []
    for name, defaults in DEFAULT_EXPENSE_CONFIG.items():
        category, _ = ExpenseCategory.objects.get_or_create(
            name=name,
            defaults={"is_active": True, **defaults},
        )
        if not category.is_active:
            category.is_active = True
            category.save(update_fields=["is_active"])
        categories.append(category)
    return categories


def _sum_account(title: str):
    try:
        acc = Account.objects.get(title=title, is_active=True)
    except Account.DoesNotExist:
        return ZERO

    sums = VoucherLine.objects.filter(account=acc).aggregate(
        debit=Sum("debit"),
        credit=Sum("credit"),
    )
    debit = sums["debit"] or ZERO
    credit = sums["credit"] or ZERO

    if acc.type in (Account.ASSET, Account.EXPENSE):
        return debit - credit
    return credit - debit


def _period_label(start, end):
    if start and end:
        return f"{start} to {end}"
    if start:
        return f"From {start} onward"
    if end:
        return f"Up to {end}"
    return "All recorded transactions"


def _account_balance(account: Account):
    sums = VoucherLine.objects.filter(account=account).aggregate(
        debit=Sum("debit"),
        credit=Sum("credit"),
    )
    debit = sums["debit"] or ZERO
    credit = sums["credit"] or ZERO

    if account.type in (Account.ASSET, Account.EXPENSE):
        return debit - credit
    return credit - debit


def _bank_ledger_balances():
    ledgers = []
    for bank in bank_account_queryset():
        balance = _account_balance(bank.account) if bank.account_id else ZERO
        ledgers.append(
            {
                "bank": bank,
                "label": bank.display_name,
                "balance": balance,
            }
        )

    try:
        legacy_bank = Account.objects.get(title="Bank", is_active=True)
    except Account.DoesNotExist:
        legacy_balance = ZERO
    else:
        legacy_balance = _account_balance(legacy_bank)

    if legacy_balance != ZERO or not ledgers:
        ledgers.append({"bank": None, "label": "Legacy Bank", "balance": legacy_balance})

    return ledgers


def _money_summary():
    cash_balance = _sum_account("Cash")
    bank_balance = total_bank_balance()
    expense_payable_balance = _sum_account("Expense Payable")
    return {
        "cash_balance": cash_balance,
        "bank_balance": bank_balance,
        "expense_payable_balance": expense_payable_balance,
        "available_money": cash_balance + bank_balance,
        "bank_ledgers": _bank_ledger_balances(),
    }


def _money_movement_rows():
    rows = []
    running_balances = {}

    lines = (
        VoucherLine.objects.filter(
            Q(account__title="Cash") | Q(account__title="Bank") | Q(account__bank_ledger__isnull=False),
            account__is_active=True,
        )
        .select_related("voucher", "account", "account__bank_ledger")
        .order_by("voucher__date", "voucher__id", "id")
    )

    for line in lines:
        source_title = line.account.bank_ledger.display_name if getattr(line.account, "bank_ledger", None) else line.account.title
        running_balances.setdefault(source_title, ZERO)
        money_in = line.debit or ZERO
        money_out = line.credit or ZERO
        effect = money_in - money_out
        running_balances[source_title] += effect
        total_after = sum(running_balances.values(), ZERO)

        rows.append(
            {
                "date": line.voucher.date,
                "voucher": line.voucher,
                "source": source_title,
                "narration": line.voucher.narration or "-",
                "money_in": money_in,
                "money_out": money_out,
                "effect": effect,
                "source_balance": running_balances[source_title],
                "available_money_after": total_after,
            }
        )

    return rows


def accounts_home(request):
    tb = get_trial_balance()
    pl = get_profit_and_loss()
    bs = get_balance_sheet()
    money = _money_summary()

    ar_balance = _sum_account("Accounts Receivable")
    ap_balance = _sum_account("Accounts Payable")
    inventory_balance = _sum_account("Inventory")
    net_profit = pl["net_profit"]
    net_worth = bs["total_assets"] - bs["total_liabilities"]
    recent_vouchers = Voucher.objects.all().order_by("-date", "-id")[:10]

    context = {
        "trial_balance": tb,
        "profit_loss": pl,
        "balance_sheet": bs,
        "cash_balance": money["cash_balance"],
        "bank_balance": money["bank_balance"],
        "bank_ledgers": money["bank_ledgers"],
        "ar_balance": ar_balance,
        "ap_balance": ap_balance,
        "expense_payable_balance": money["expense_payable_balance"],
        "inventory_balance": inventory_balance,
        "available_money": money["available_money"],
        "net_profit": net_profit,
        "net_worth": net_worth,
        "books_balanced": tb["balanced"] and bs["balanced"],
        "recent_vouchers": recent_vouchers,
        "money_desk_url": reverse("money_desk"),
        "money_breakdown_url": reverse("money_breakdown"),
        "report_cards": [
            {
                "title": "Internal Check",
                "summary": "Trial Balance",
                "description": "Confirms every debit is matched by a credit.",
                "url": reverse("trial_balance"),
                "cta": "Open check",
                "icon": "bi-shield-check",
                "accent": "primary",
            },
            {
                "title": "Money Earned",
                "summary": "Profit & Loss",
                "description": "Shows sales income against business expenses.",
                "url": reverse("profit_loss"),
                "cta": "View profit",
                "icon": "bi-graph-up-arrow",
                "accent": "success",
            },
            {
                "title": "What You Own vs Owe",
                "summary": "Balance Sheet",
                "description": "Summarizes assets, liabilities, and business worth.",
                "url": reverse("balance_sheet"),
                "cta": "View position",
                "icon": "bi-bank",
                "accent": "info",
            },
            {
                "title": "Money Desk",
                "summary": "Funding & Transfers",
                "description": "Introduce opening cash, move money between cash and bank, and pay old expense bills.",
                "url": reverse("money_desk"),
                "cta": "Open money desk",
                "icon": "bi-arrow-left-right",
                "accent": "warning",
            },
            {
                "title": "Operating Expenses",
                "summary": "Expense Log",
                "description": "Track bills like rent, electricity, gas, and see what is still unpaid.",
                "url": reverse("expense_list"),
                "cta": "Open expenses",
                "icon": "bi-receipt-cutoff",
                "accent": "warning",
            },
        ],
        "glossary": [
            {"term": "Money available now", "detail": "Cash in hand plus bank balance you can use.", "icon": "bi-wallet2"},
            {"term": "Customers owe you", "detail": "Open credit sales still waiting to be collected.", "icon": "bi-people"},
            {"term": "You owe suppliers", "detail": "Unpaid purchase balances still to be settled.", "icon": "bi-truck"},
            {"term": "Operating bills due", "detail": "Rent, utilities, and other running costs not paid yet.", "icon": "bi-lightning-charge"},
            {"term": "Stock value", "detail": "Inventory valued at current cost price, not sale price.", "icon": "bi-box-seam"},
        ],
    }
    return render(request, "accounts/dashboard.html", context)


def money_breakdown(request):
    money = _money_summary()
    movement_rows = list(reversed(_money_movement_rows()))

    context = {
        **money,
        "movement_rows": movement_rows[:50],
        "movement_count": len(movement_rows),
        "cash_movements": sum(1 for row in movement_rows if row["source"] == "Cash"),
        "bank_movements": sum(1 for row in movement_rows if row["source"] != "Cash"),
        "money_desk_url": reverse("money_desk"),
    }
    return render(request, "accounts/money_breakdown.html", context)


def expense_list(request):
    default_expense_types = _ensure_default_expense_categories()
    today = timezone.localdate()
    month_start = today.replace(day=1)

    selected_category_param = request.GET.get("category", "").strip()
    selected_type_param = request.GET.get("type", "").strip() or selected_category_param
    selected_expense_param = request.GET.get("expense", "").strip()

    selected_category = ExpenseCategory.objects.filter(pk=selected_category_param).first() if selected_category_param.isdigit() else None
    selected_type = ExpenseCategory.objects.filter(pk=selected_type_param).first() if selected_type_param.isdigit() else None

    initial_expense = expense_initial_for_category(selected_category, today=today) if selected_category else {}

    selected_expense = None
    expense_payment_initial = {}
    if selected_expense_param.isdigit():
        selected_expense = (
            Expense.objects.select_related("category", "category__account", "voucher", "employee")
            .filter(pk=selected_expense_param)
            .first()
        )
        if selected_expense and selected_expense.balance_due > 0:
            expense_payment_initial["expense"] = selected_expense.id
            expense_payment_initial["amount"] = selected_expense.balance_due

    if request.method == "POST":
        action = request.POST.get("action")
        posted_type_id = request.POST.get("type_id", "").strip()
        bound_type = ExpenseCategory.objects.filter(pk=posted_type_id).first() if posted_type_id.isdigit() else selected_type

        category_form = ExpenseCategoryForm(prefix="type", instance=bound_type)
        expense_form = ExpenseForm(prefix="expense", initial=initial_expense)
        expense_payment_form = ExpensePaymentForm(prefix="payment", initial=expense_payment_initial)

        if action == "add_category":
            category_form = ExpenseCategoryForm(request.POST, prefix="type", instance=bound_type)
            if category_form.is_valid():
                category = category_form.save()
                record_audit(
                    action="expense_category_updated" if bound_type else "expense_category_created",
                    description=f"Expense type '{category.name}' saved from Expenses page.",
                    actor=request.user,
                    target=category,
                )
                messages.success(request, f"Expense type '{category.name}' saved.")
                return redirect(f"{reverse('expense_list')}?category={category.id}&type={category.id}")

            requested_name = (request.POST.get("type-name") or "").strip()
            existing_category = ExpenseCategory.objects.filter(name__iexact=requested_name).first() if requested_name and not bound_type else None
            if existing_category:
                if not existing_category.is_active:
                    existing_category.is_active = True
                    existing_category.save(update_fields=["is_active"])
                messages.success(request, f"Expense type '{existing_category.name}' is already available.")
                return redirect(f"{reverse('expense_list')}?category={existing_category.id}&type={existing_category.id}")
            messages.error(request, "Please review the expense type details.")
        elif action == "pay_expense":
            expense_payment_form = ExpensePaymentForm(request.POST, prefix="payment")
            if expense_payment_form.is_valid():
                try:
                    with transaction.atomic():
                        payment = expense_payment_form.save()
                        post_expense_payment(payment.id)
                        record_audit(
                            action="expense_paid",
                            description=f"Expense '{payment.expense.display_title}' paid from {payment.from_account_title}.",
                            actor=request.user,
                            target=payment,
                            metadata={"amount": str(payment.amount)},
                        )
                    messages.success(request, f"Expense payment for '{payment.expense.display_title}' recorded successfully.")
                    return redirect("expense_list")
                except ValueError as exc:
                    expense_payment_form.add_error(None, str(exc))
            messages.error(request, "Please review the expense payment details.")
        else:
            expense_form = ExpenseForm(request.POST, prefix="expense")
            if expense_form.is_valid():
                try:
                    with transaction.atomic():
                        expense = expense_form.save()
                        post_expense(expense.id)
                        record_audit(
                            action="expense_created",
                            description=f"Expense '{expense.display_title}' recorded for {expense.amount}.",
                            actor=request.user,
                            target=expense,
                            metadata={"employee_id": expense.employee_id or ""},
                        )
                    messages.success(request, f"Expense '{expense.display_title}' recorded successfully.")
                    return redirect("expense_list")
                except ValueError as exc:
                    expense_form.add_error(None, str(exc))
            messages.error(request, "Please review the expense form.")
    else:
        category_form = ExpenseCategoryForm(prefix="type", instance=selected_type)
        expense_form = ExpenseForm(prefix="expense", initial=initial_expense)
        expense_payment_form = ExpensePaymentForm(prefix="payment", initial=expense_payment_initial)

    expenses = Expense.objects.select_related("category", "category__account", "voucher", "employee").order_by("-date", "-id")
    open_expenses = expenses.filter(amount__gt=F("paid_amount")).order_by("date", "id")
    this_month = expenses.filter(date__gte=month_start)
    recent_expense_payments = ExpensePayment.objects.select_related("expense", "expense__category", "voucher").order_by("-date", "-id")
    expense_types = ExpenseCategory.objects.filter(is_active=True).order_by("name")
    expense_reminders = monthly_bill_alerts(today=today)
    for reminder in expense_reminders:
        reminder["record_url"] = f"{reverse('expense_list')}?category={reminder['category'].id}&type={reminder['category'].id}#record-expense"

    expense_alert_count = len(expense_reminders)
    expense_overdue_count = sum(1 for reminder in expense_reminders if reminder["status"] == "overdue")
    expense_due_soon_count = expense_alert_count - expense_overdue_count

    context = {
        "category_form": category_form,
        "expense_form": expense_form,
        "expense_payment_form": expense_payment_form,
        "expenses": expenses[:25],
        "open_expenses": open_expenses[:12],
        "recent_expense_payments": recent_expense_payments[:10],
        "selected_expense": selected_expense,
        "selected_expense_id": str(selected_expense.id) if selected_expense else "",
        "selected_category": str(selected_category.id) if selected_category else "",
        "selected_type": selected_type,
        "selected_type_id": str(selected_type.id) if selected_type else "",
        "expense_types": expense_types,
        "default_expense_types": default_expense_types,
        "custom_expense_types": expense_types.exclude(name__in=DEFAULT_EXPENSE_TYPES),
        "expense_reminders": expense_reminders,
        "expense_alert_count": expense_alert_count,
        "expense_overdue_count": expense_overdue_count,
        "expense_due_soon_count": expense_due_soon_count,
        "month_total": this_month.aggregate(total=Coalesce(Sum("amount"), ZERO))["total"],
        "month_paid": this_month.aggregate(total=Coalesce(Sum("paid_amount"), ZERO))["total"],
        "outstanding_total": sum((expense.balance_due for expense in expenses), ZERO),
        "expense_type_count": expense_types.count(),
        "expense_payable_balance": _sum_account("Expense Payable"),
        "open_expense_count": open_expenses.count(),
        "cleared_expense_count": max(expenses.count() - open_expenses.count(), 0),
        "money_desk_url": reverse("money_desk"),
        "money_breakdown_url": reverse("money_breakdown"),
    }
    return render(request, "accounts/expenses.html", context)


def money_desk(request):
    selected_expense_id = request.GET.get("expense", "").strip()
    expense_initial = {}
    if selected_expense_id.isdigit():
        expense_initial["expense"] = selected_expense_id
        expense = Expense.objects.filter(pk=selected_expense_id).first()
        if expense and expense.balance_due > 0:
            expense_initial["amount"] = expense.balance_due

    if request.method == "POST":
        action = request.POST.get("action")
        capital_form = CapitalEntryForm(prefix="capital")
        transfer_form = MoneyTransferForm(prefix="transfer")
        expense_payment_form = ExpensePaymentForm(prefix="payment", initial=expense_initial)

        if action == "capital":
            capital_form = CapitalEntryForm(request.POST, prefix="capital")
            if capital_form.is_valid():
                try:
                    with transaction.atomic():
                        entry = capital_form.save()
                        post_capital_entry(entry.id)
                        record_audit(
                            action="capital_recorded",
                            description=f"Funds introduced into {entry.to_account_title}.",
                            actor=request.user,
                            target=entry,
                            metadata={"amount": str(entry.amount)},
                        )
                    messages.success(request, "Opening funds / owner capital recorded successfully.")
                    return redirect("money_desk")
                except ValueError as exc:
                    capital_form.add_error(None, str(exc))
            messages.error(request, "Please review the owner capital form.")
        elif action == "transfer":
            transfer_form = MoneyTransferForm(request.POST, prefix="transfer")
            if transfer_form.is_valid():
                try:
                    with transaction.atomic():
                        transfer = transfer_form.save()
                        post_money_transfer(transfer.id)
                        record_audit(
                            action="money_transfer_recorded",
                            description=f"Money moved from {transfer.from_account_title} to {transfer.to_account_title}.",
                            actor=request.user,
                            target=transfer,
                            metadata={"amount": str(transfer.amount)},
                        )
                    messages.success(request, "Money transfer recorded successfully.")
                    return redirect("money_desk")
                except ValueError as exc:
                    transfer_form.add_error(None, str(exc))
            messages.error(request, "Please review the transfer details.")
        elif action == "pay_expense":
            expense_payment_form = ExpensePaymentForm(request.POST, prefix="payment")
            if expense_payment_form.is_valid():
                try:
                    with transaction.atomic():
                        payment = expense_payment_form.save()
                        post_expense_payment(payment.id)
                        record_audit(
                            action="expense_paid",
                            description=f"Expense '{payment.expense.display_title}' paid from {payment.from_account_title} via Money Desk.",
                            actor=request.user,
                            target=payment,
                            metadata={"amount": str(payment.amount)},
                        )
                    messages.success(request, "Expense payment recorded successfully.")
                    return redirect("money_desk")
                except ValueError as exc:
                    expense_payment_form.add_error(None, str(exc))
            messages.error(request, "Please review the expense payment details.")
    else:
        capital_form = CapitalEntryForm(prefix="capital")
        transfer_form = MoneyTransferForm(prefix="transfer")
        expense_payment_form = ExpensePaymentForm(prefix="payment", initial=expense_initial)

    money = _money_summary()
    open_expenses = Expense.objects.select_related("category").filter(amount__gt=F("paid_amount")).order_by("date", "id")

    context = {
        "capital_form": capital_form,
        "transfer_form": transfer_form,
        "expense_payment_form": expense_payment_form,
        "open_expenses": open_expenses[:12],
        "recent_expense_payments": ExpensePayment.objects.select_related("expense", "voucher").order_by("-date", "-id")[:10],
        "recent_capital_entries": CapitalEntry.objects.select_related("voucher").order_by("-date", "-id")[:10],
        "recent_transfers": MoneyTransfer.objects.select_related("voucher").order_by("-date", "-id")[:10],
        "selected_expense_id": selected_expense_id,
        **money,
    }
    return render(request, "accounts/money_desk.html", context)


def trial_balance_view(request):
    context = get_trial_balance()
    context["difference"] = context["total_debit"] - context["total_credit"]
    context["page_summary"] = "Use this as an internal control report. If totals do not match, some posting logic is broken."
    return render(request, "accounts/trial_balance.html", context)


def profit_loss_view(request):
    start = request.GET.get("start")
    end = request.GET.get("end")
    context = get_profit_and_loss(start_date=start or None, end_date=end or None)
    context["period_label"] = _period_label(start, end)
    context["status_label"] = "Net profit" if context["net_profit"] >= 0 else "Net loss"
    return render(request, "accounts/profit_loss.html", context)


def balance_sheet_view(request):
    as_of = request.GET.get("as_of")
    context = get_balance_sheet(as_of_date=as_of or None)
    context["as_of_label"] = as_of or "Today"
    context["net_position"] = context["total_assets"] - context["total_liabilities"]
    return render(request, "accounts/balance_sheet.html", context)


def account_ledger(request, account_id):
    account = get_object_or_404(Account, id=account_id)
    lines = VoucherLine.objects.filter(account=account).select_related("voucher").order_by("voucher__date", "voucher__id")

    running = ZERO
    total_debit = ZERO
    total_credit = ZERO
    ledger = []

    for line in lines:
        debit = line.debit or ZERO
        credit = line.credit or ZERO
        total_debit += debit
        total_credit += credit

        if debit > 0:
            running += debit
        else:
            running -= credit

        ledger.append(
            {
                "date": line.voucher.date,
                "voucher": line.voucher,
                "narration": line.voucher.narration,
                "debit": debit,
                "credit": credit,
                "balance": abs(running),
                "drcr": "Dr" if running >= 0 else "Cr",
            }
        )

    closing_balance = ledger[-1]["balance"] if ledger else ZERO
    closing_side = ledger[-1]["drcr"] if ledger else "Dr"

    return render(
        request,
        "accounts/account_ledger.html",
        {
            "account": account,
            "ledger": ledger,
            "total_debit": total_debit,
            "total_credit": total_credit,
            "closing_balance": closing_balance,
            "closing_side": closing_side,
        },
    )


def party_statement(request, model, pk):
    ct = get_object_or_404(ContentType, model=model)
    entries = PartyLedgerEntry.objects.filter(content_type=ct, object_id=pk).select_related("voucher").order_by("voucher__date", "voucher__id")

    running = ZERO
    total_debit = ZERO
    total_credit = ZERO
    rows = []
    party = entries.first().party if entries else None

    for entry in entries:
        debit = entry.debit or ZERO
        credit = entry.credit or ZERO
        total_debit += debit
        total_credit += credit
        running += debit
        running -= credit

        rows.append(
            {
                "date": entry.voucher.date,
                "voucher": entry.voucher,
                "debit": debit,
                "credit": credit,
                "balance": abs(running),
                "drcr": "Dr" if running >= 0 else "Cr",
            }
        )

    closing_balance = rows[-1]["balance"] if rows else ZERO
    closing_side = rows[-1]["drcr"] if rows else "Dr"

    return render(
        request,
        "accounts/party_ledger.html",
        {
            "party": party,
            "rows": rows,
            "total_debit": total_debit,
            "total_credit": total_credit,
            "closing_balance": closing_balance,
            "closing_side": closing_side,
        },
    )


accounts_home = module_access_required("accounts", minimum="view")(accounts_home)
money_breakdown = module_access_required("accounts", minimum="view")(money_breakdown)
expense_list = module_access_required("expenses", minimum="view")(expense_list)
money_desk = module_access_required("accounts", minimum="view")(money_desk)
trial_balance_view = module_access_required("accounts", minimum="view")(trial_balance_view)
profit_loss_view = module_access_required("accounts", minimum="view")(profit_loss_view)
balance_sheet_view = module_access_required("accounts", minimum="view")(balance_sheet_view)
account_ledger = module_access_required("accounts", minimum="view")(account_ledger)
party_statement = module_access_required("accounts", minimum="view")(party_statement)








