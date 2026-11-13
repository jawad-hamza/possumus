from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.forms import (
    BankAccountForm,
    BooksLockForm,
    CapitalEntryForm,
    ExpenseCategoryForm,
    ExpenseForm,
    ExpensePaymentForm,
    MoneyTransferForm,
)
from accounts.models import (
    Account,
    BankAccount,
    CapitalEntry,
    Expense,
    ExpenseCategory,
    ExpensePayment,
    MoneyTransfer,
    Voucher,
    VoucherLine,
)
from accounts.report_views import report_links
from accounts.services.audit import record_audit
from accounts.services.balance_sheet import get_balance_sheet
from accounts.services.bank_receipts import UPLOAD_FIELD as RECEIPT_FIELD, attach_receipt
from accounts.services.books_lock import get_books_locked_until
from accounts.services.cash_bank import total_bank_balance
from accounts.services.cash_flow import money_accounts
from accounts.services.expense_alerts import expense_initial_for_category, monthly_bill_alerts
from accounts.services.expense_posting import post_expense
from accounts.services.money_desk import post_capital_entry, post_expense_payment, post_money_transfer
from accounts.services.profit_loss import get_profit_and_loss
from accounts.services.report_utils import natural_balance, opening_sides, to_money
from accounts.services.trial_balance import get_trial_balance
from pos.services.access import ACCESS_MANAGE, admin_required, is_admin, module_access_required
from pos.services.quick_add import quick_add_context


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


def _account_balance(account: Account):
    """Same figure the balance sheet shows: opening balance plus every posting."""
    sums = VoucherLine.objects.filter(account=account).aggregate(
        debit=Sum("debit"),
        credit=Sum("credit"),
    )
    opening_debit, opening_credit = opening_sides(account)
    return natural_balance(
        account,
        to_money(sums["debit"]) + opening_debit,
        to_money(sums["credit"]) + opening_credit,
    )


def _sum_account(title: str):
    # An account that was switched off still carries its history.
    account = Account.objects.filter(title=title).first()
    return _account_balance(account) if account else ZERO


def _paginate(request, object_list, per_page, param="page"):
    page = Paginator(object_list, per_page).get_page(request.GET.get(param))
    params = request.GET.copy()
    params.pop(param, None)
    page.param = param
    page.base_query = params.urlencode()
    return page


def _bank_ledger_balances():
    ledgers = []
    for bank in BankAccount.objects.select_related("account").order_by("name", "account_number"):
        balance = _account_balance(bank.account) if bank.account_id else ZERO
        if not bank.is_active and balance == ZERO:
            continue  # a closed bank only matters while money still sits in it
        ledgers.append(
            {
                "bank": bank,
                "label": bank.display_name,
                "balance": balance,
            }
        )

    legacy_balance = _sum_account("Bank")

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
        VoucherLine.objects.filter(account__in=money_accounts())
        .select_related("voucher", "account", "account__bank_ledger")
        .order_by("voucher__date", "voucher__id", "id")
    )

    for account in money_accounts():
        opening_debit, opening_credit = opening_sides(account)
        if opening_debit or opening_credit:
            bank = getattr(account, "bank_ledger", None)
            running_balances[bank.display_name if bank else account.title] = opening_debit - opening_credit

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
    tax_collected = _sum_account("Sales Tax Payable")
    inventory_balance = _sum_account("Inventory")
    net_profit = pl["net_profit"]
    net_worth = bs["total_assets"] - bs["total_liabilities"]
    recent_vouchers = Voucher.objects.all().order_by("-date", "-id")[:10]
    books_locked_until = get_books_locked_until()

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
        "tax_collected": tax_collected,
        "inventory_balance": inventory_balance,
        "available_money": money["available_money"],
        "net_profit": net_profit,
        "net_worth": net_worth,
        "books_balanced": tb["balanced"] and bs["balanced"],
        "recent_vouchers": recent_vouchers,
        "money_desk_url": reverse("money_desk"),
        "money_breakdown_url": reverse("money_breakdown"),
        "report_links": report_links(),
        "books_locked_until": books_locked_until,
        "books_lock_form": BooksLockForm(initial={"books_locked_until": books_locked_until}),
        "can_lock_books": is_admin(request.user),
        "books_lock_url": reverse("books_lock_update"),
        "report_cards": [
            {
                "title": "Money desk",
                "summary": "Funding and transfers",
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
            {"term": "Tax collected", "detail": "Sales tax already collected from customers and currently sitting in Sales Tax Payable.", "icon": "bi-receipt"},
            {"term": "Stock value", "detail": "Inventory valued at current cost price, not sale price.", "icon": "bi-box-seam"},
        ],
    }
    return render(request, "accounts/dashboard.html", context)


def money_breakdown(request):
    money = _money_summary()
    movement_rows = list(reversed(_money_movement_rows()))

    context = {
        **money,
        "movement_rows": _paginate(request, movement_rows, 50),
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
            expense_payment_form = ExpensePaymentForm(request.POST, request.FILES, prefix="payment")
            if expense_payment_form.is_valid():
                try:
                    with transaction.atomic():
                        payment = expense_payment_form.save()
                        post_expense_payment(payment.id, actor=request.user)
                        attach_receipt(payment, expense_payment_form.cleaned_data.get(RECEIPT_FIELD), actor=request.user, audit=False)
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
            expense_form = ExpenseForm(request.POST, request.FILES, prefix="expense")
            if expense_form.is_valid():
                try:
                    with transaction.atomic():
                        expense = expense_form.save()
                        post_expense(expense.id, actor=request.user)
                        attach_receipt(expense, expense_form.cleaned_data.get(RECEIPT_FIELD), actor=request.user, audit=False)
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
        "expenses": _paginate(request, expenses, 25, "page"),
        "open_expenses": _paginate(request, open_expenses, 12, "open_page"),
        "recent_expense_payments": _paginate(request, recent_expense_payments, 10, "pay_page"),
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
        "outstanding_total": open_expenses.aggregate(total=Coalesce(Sum(F("amount") - F("paid_amount")), ZERO))["total"],
        "expense_type_count": expense_types.count(),
        "expense_payable_balance": _sum_account("Expense Payable"),
        "open_expense_count": open_expenses.count(),
        "cleared_expense_count": max(expenses.count() - open_expenses.count(), 0),
        "money_desk_url": reverse("money_desk"),
        "money_breakdown_url": reverse("money_breakdown"),
        **quick_add_context(request, "expense_type", "bank"),
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
            capital_form = CapitalEntryForm(request.POST, request.FILES, prefix="capital")
            if capital_form.is_valid():
                try:
                    with transaction.atomic():
                        entry = capital_form.save()
                        post_capital_entry(entry.id, actor=request.user)
                        attach_receipt(entry, capital_form.cleaned_data.get(RECEIPT_FIELD), actor=request.user, audit=False)
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
            transfer_form = MoneyTransferForm(request.POST, request.FILES, prefix="transfer")
            if transfer_form.is_valid():
                try:
                    with transaction.atomic():
                        transfer = transfer_form.save()
                        post_money_transfer(transfer.id, actor=request.user)
                        attach_receipt(transfer, transfer_form.cleaned_data.get(RECEIPT_FIELD), actor=request.user, audit=False)
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
            expense_payment_form = ExpensePaymentForm(request.POST, request.FILES, prefix="payment")
            if expense_payment_form.is_valid():
                try:
                    with transaction.atomic():
                        payment = expense_payment_form.save()
                        post_expense_payment(payment.id, actor=request.user)
                        attach_receipt(payment, expense_payment_form.cleaned_data.get(RECEIPT_FIELD), actor=request.user, audit=False)
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
        "open_expenses": _paginate(request, open_expenses, 12, "open_page"),
        "recent_expense_payments": _paginate(request, ExpensePayment.objects.select_related("expense", "voucher").order_by("-date", "-id"), 10, "pay_page"),
        "recent_capital_entries": _paginate(request, CapitalEntry.objects.select_related("voucher").order_by("-date", "-id"), 10, "capital_page"),
        "recent_transfers": _paginate(request, MoneyTransfer.objects.select_related("voucher").order_by("-date", "-id"), 10, "transfer_page"),
        "selected_expense_id": selected_expense_id,
        **money,
    }
    return render(request, "accounts/money_desk.html", context)


def _json_errors(form):
    return {
        field: [entry["message"] for entry in entries]
        for field, entries in form.errors.get_json_data().items()
    }


@require_POST
def quick_create_expense_type(request):
    """The expense form's side panel: a new expense type, same checks as the Expense types form."""
    # Asking again for a type that already exists (in any letter case) hands that one back, switched on.
    requested_name = (request.POST.get("quick_expense_type-name") or "").strip()
    existing = ExpenseCategory.objects.filter(name__iexact=requested_name).first() if requested_name else None
    if existing is not None:
        if not existing.is_active:
            existing.is_active = True
            existing.save(update_fields=["is_active"])
        return JsonResponse({"status": "ok", "expense_type": {"id": existing.id, "name": existing.name}})
    form = ExpenseCategoryForm(request.POST, prefix="quick_expense_type")
    if form.is_valid():
        category = form.save()
        record_audit(
            action="expense_category_created",
            description=f"Expense type '{category.name}' saved from the expense form.",
            actor=request.user,
            target=category,
        )
        return JsonResponse({"status": "ok", "expense_type": {"id": category.id, "name": category.name}})
    return JsonResponse({"status": "error", "errors": _json_errors(form)}, status=400)


@require_POST
def quick_create_bank_account(request):
    """A new bank ledger from a payment form's side panel; the same rules as Settings > Bank ledgers."""
    form = BankAccountForm(request.POST, prefix="quick_bank")
    if form.is_valid():
        try:
            bank = form.save()
        except ValidationError as exc:
            for field, problems in exc.message_dict.items():
                form.add_error(field if field in form.fields else None, problems)
        else:
            record_audit(
                action="bank_ledger_created",
                description=f"Bank ledger '{bank.display_name}' created.",
                actor=request.user,
                target=bank,
            )
            return JsonResponse({"status": "ok", "bank": {"id": bank.id, "name": str(bank)}})
    return JsonResponse({"status": "error", "errors": _json_errors(form)}, status=400)


accounts_home = module_access_required("accounts", minimum="view")(accounts_home)
quick_create_expense_type = module_access_required("expenses", minimum=ACCESS_MANAGE)(quick_create_expense_type)
quick_create_bank_account = admin_required(quick_create_bank_account)
money_breakdown = module_access_required("accounts", minimum="view")(money_breakdown)
expense_list = module_access_required("expenses", minimum="view")(expense_list)
money_desk = module_access_required("accounts", minimum="view")(money_desk)
