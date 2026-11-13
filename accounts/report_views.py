"""
Accounting reports: statements, ledgers, aging, day book, cash flow.

Every report follows the same shape so the screens feel like one tool: parse
the filters leniently, build the data in a service, then either render the
page or stream the same rows as CSV when ?export=csv is present.
"""

import datetime

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from accounts.forms import BooksLockForm, PartyOpeningBalanceForm
from accounts.models import Account
from accounts.services import customer_statement
from accounts.services.aging import BUCKETS, PAYABLE, RECEIVABLE, get_aging
from accounts.services.audit import record_audit
from accounts.services.balance_sheet import get_balance_sheet
from accounts.services.books_lock import get_books_locked_until, set_books_lock
from accounts.services.cash_flow import get_cash_flow
from accounts.services.day_book import get_day_book
from accounts.services.expense_by_category import get_expense_by_category
from accounts.services.ledgers import get_account_ledger, get_party_ledger, ledger_rows
from accounts.services.party_opening import get_party_opening, set_party_opening_balance
from accounts.services.profit_loss import get_profit_and_loss
from accounts.services.report_utils import csv_response, parse_report_date, parse_report_range, plain_amount
from accounts.services.trial_balance import get_trial_balance
from pos.models import Customer, Supplier
from pos.services.access import is_admin, module_access_required

LEDGER_PAGE_SIZE = 50
DAY_BOOK_PAGE_SIZE = 25
PARTY_MODELS = {"customer": Customer, "supplier": Supplier}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def report_links():
    """One list drives the report switcher on every report page and the cards on the overview."""
    return [
        {"name": "trial_balance", "label": "Trial balance", "icon": "bi-shield-check", "url": reverse("trial_balance"),
         "description": "Confirms every debit is matched by a credit."},
        {"name": "profit_loss", "label": "Profit and loss", "icon": "bi-graph-up-arrow", "url": reverse("profit_loss"),
         "description": "Income earned against expenses for a period."},
        {"name": "balance_sheet", "label": "Balance sheet", "icon": "bi-bank", "url": reverse("balance_sheet"),
         "description": "What the business owns, owes and is worth."},
        {"name": "day_book", "label": "Day book", "icon": "bi-journal-text", "url": reverse("day_book"),
         "description": "Every entry posted on a day, in order."},
        {"name": "cash_flow", "label": "Cash flow", "icon": "bi-cash-stack", "url": reverse("cash_flow"),
         "description": "Where cash and bank money came from and went."},
        {"name": "receivables_aging", "label": "Receivables aging", "icon": "bi-people", "url": reverse("receivables_aging"),
         "description": "Who owes you, and for how long."},
        {"name": "payables_aging", "label": "Payables aging", "icon": "bi-truck", "url": reverse("payables_aging"),
         "description": "What you owe suppliers, and for how long."},
        {"name": "expense_by_category", "label": "Expenses by type", "icon": "bi-pie-chart", "url": reverse("expense_by_category"),
         "description": "Which kinds of expense cost the most."},
    ]


def _iso(value):
    return value.isoformat() if value else ""


def _query_without(request, *names):
    params = request.GET.copy()
    for name in names:
        params.pop(name, None)
    return params.urlencode()


def _wants_csv(request):
    return request.GET.get("export") == "csv"


def _range_presets(today):
    month_start = today.replace(day=1)
    last_month_end = month_start - datetime.timedelta(days=1)
    yesterday = today - datetime.timedelta(days=1)
    return [
        ("Today", today, today),
        ("Yesterday", yesterday, yesterday),
        ("This month", month_start, today),
        ("Last month", last_month_end.replace(day=1), last_month_end),
        ("This year", today.replace(month=1, day=1), today),
    ]


def _as_of_presets(today):
    month_start = today.replace(day=1)
    return [
        ("Today", today),
        ("End of last month", month_start - datetime.timedelta(days=1)),
        ("End of last year", today.replace(month=1, day=1) - datetime.timedelta(days=1)),
    ]


def _report_context(request, active, *, mode, start=None, end=None, as_of=None, all_time=False, allow_all_time=True):
    """Everything the shared report header needs: switcher, filter values, presets, export link."""
    today = timezone.localdate()
    keep = _query_without(request, "start", "end", "as_of", "all", "page", "export")
    prefix = f"{keep}&" if keep else ""

    presets = []
    if mode == "range":
        for label, preset_start, preset_end in _range_presets(today):
            presets.append({
                "label": label,
                "query": f"{prefix}start={_iso(preset_start)}&end={_iso(preset_end)}",
                "active": not all_time and start == preset_start and end == preset_end,
            })
        if allow_all_time:
            presets.append({"label": "All time", "query": f"{prefix}all=1", "active": all_time})
    elif mode == "as_of":
        for label, preset_date in _as_of_presets(today):
            presets.append({
                "label": label,
                "query": f"{prefix}as_of={_iso(preset_date)}",
                "active": as_of == preset_date,
            })

    export_base = _query_without(request, "page", "export")
    return {
        "report_links": report_links(),
        "active_report": active,
        "filter_mode": mode,
        "filter_start": _iso(start),
        "filter_end": _iso(end),
        "filter_as_of": _iso(as_of),
        "filter_presets": presets,
        "filter_keep": [(key, value) for key, value in request.GET.items() if key not in ("start", "end", "as_of", "all", "page", "export")],
        "export_query": f"{export_base}&export=csv" if export_base else "export=csv",
        "books_locked_until": get_books_locked_until(),
    }


def _requested_range(request):
    """(start, end, all_time). Bad dates quietly fall back to the report's default range."""
    if request.GET.get("all"):
        return None, None, True
    start, end = parse_report_range(request.GET.get("start"), request.GET.get("end"))
    return start, end, False


def _paginate(request, object_list, per_page, param="page"):
    page = Paginator(object_list, per_page).get_page(request.GET.get(param))
    page.param = param
    page.base_query = _query_without(request, param, "export")
    return page


def _period_label(start, end):
    if start and end:
        if start == end:
            return f"{start:%d %b %Y}"
        return f"{start:%d %b %Y} to {end:%d %b %Y}"
    if start:
        return f"From {start:%d %b %Y} onward"
    if end:
        return f"Up to {end:%d %b %Y}"
    return "All recorded transactions"


def _get_party(model, pk):
    party_model = PARTY_MODELS.get((model or "").lower())
    if party_model is None:
        raise Http404("Unknown party type")
    return get_object_or_404(party_model, pk=pk)


def _safe_next(request, fallback):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return fallback


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

def trial_balance_view(request):
    as_of = parse_report_date(request.GET.get("as_of"))
    data = get_trial_balance(as_of_date=as_of)

    if _wants_csv(request):
        rows = [
            [row["account"], row["type"].title(), "" if row["is_active"] else "inactive",
             plain_amount(row["debit"]), plain_amount(row["credit"]),
             plain_amount(row["balance_debit"]), plain_amount(row["balance_credit"])]
            for row in data["rows"]
        ]
        rows.append(["Total", "", "", plain_amount(data["total_debit"]), plain_amount(data["total_credit"]), "", ""])
        return csv_response("trial-balance", ["Account", "Type", "Status", "Debit", "Credit", "Balance Dr", "Balance Cr"], rows)

    context = {
        **data,
        **_report_context(request, "trial_balance", mode="as_of", as_of=as_of),
        "difference": data["total_debit"] - data["total_credit"],
        "as_of_label": f"{as_of:%d %b %Y}" if as_of else "today (everything posted)",
        "page_summary": "A check on the books. If the two totals ever differ, an entry was recorded wrongly: note the date and contact support.",
    }
    return render(request, "accounts/trial_balance.html", context)


def profit_loss_view(request):
    start, end, all_time = _requested_range(request)
    data = get_profit_and_loss(start_date=start, end_date=end)

    if _wants_csv(request):
        rows = [["Income", row["account"], plain_amount(row["amount"])] for row in data["income"]]
        rows.append(["Income", "Total income", plain_amount(data["total_income"])])
        rows += [["Expense", row["account"], plain_amount(row["amount"])] for row in data["expenses"]]
        rows.append(["Expense", "Total expense", plain_amount(data["total_expense"])])
        rows.append(["Result", "Net profit", plain_amount(data["net_profit"])])
        return csv_response("profit-and-loss", ["Section", "Account", "Amount"], rows)

    context = {
        **data,
        **_report_context(request, "profit_loss", mode="range", start=start, end=end, all_time=all_time or not (start or end)),
        "period_label": _period_label(start, end),
        "status_label": "Net profit" if data["net_profit"] >= 0 else "Net loss",
    }
    return render(request, "accounts/profit_loss.html", context)


def balance_sheet_view(request):
    as_of = parse_report_date(request.GET.get("as_of"))
    data = get_balance_sheet(as_of_date=as_of)

    if _wants_csv(request):
        rows = []
        for section, key, total in (("Assets", "assets", "total_assets"), ("Liabilities", "liabilities", "total_liabilities"), ("Equity", "equity", "total_equity")):
            rows += [[section, row["account"], plain_amount(row["balance"])] for row in data[key]]
            rows.append([section, f"Total {section.lower()}", plain_amount(data[total])])
        return csv_response("balance-sheet", ["Section", "Account", "Balance"], rows)

    context = {
        **data,
        **_report_context(request, "balance_sheet", mode="as_of", as_of=as_of),
        "as_of_label": f"{as_of:%d %b %Y}" if as_of else "Today",
        "net_position": data["total_assets"] - data["total_liabilities"],
    }
    return render(request, "accounts/balance_sheet.html", context)


# ---------------------------------------------------------------------------
# Ledgers
# ---------------------------------------------------------------------------

def _ledger_csv(filename, data):
    header = ["Date", "Voucher", "Type", "Narration", "Debit", "Credit", "Balance", "Dr/Cr"]
    rows = [["", "Opening balance", "", "", "", "", plain_amount(data["opening_balance"]), data["opening_side"]]]
    for row in ledger_rows(data["entries"].iterator(chunk_size=500), data["opening"]):
        rows.append([
            row["date"].isoformat(), row["voucher"].id, row["voucher"].get_voucher_type_display(), row["narration"] or "",
            plain_amount(row["debit"]), plain_amount(row["credit"]), plain_amount(row["balance"]), row["drcr"],
        ])
    rows.append(["", "Closing balance", "", "", plain_amount(data["total_debit"]), plain_amount(data["total_credit"]),
                 plain_amount(data["closing_balance"]), data["closing_side"]])
    return csv_response(filename, header, rows)


def account_ledger(request, account_id):
    account = get_object_or_404(Account, id=account_id)
    start, end, all_time = _requested_range(request)
    data = get_account_ledger(account, start, end)

    if _wants_csv(request):
        return _ledger_csv(f"ledger-{account.id}", data)

    page = _paginate(request, data["entries"], LEDGER_PAGE_SIZE)
    context = {
        **{key: value for key, value in data.items() if key != "entries"},
        **_report_context(request, "account_ledger", mode="range", start=start, end=end, all_time=all_time or not (start or end)),
        "account": account,
        "accounts": Account.objects.order_by("title"),
        "ledger": ledger_rows(page.object_list, data["opening"]),
        "page_obj": page,
        "period_label": _period_label(start, end),
    }
    return render(request, "accounts/account_ledger.html", context)


def party_statement(request, model, pk):
    party = _get_party(model, pk)
    kind = model.lower()
    start, end, all_time = _requested_range(request)
    data = get_party_ledger(party, start, end)

    if _wants_csv(request):
        if kind == "customer":
            # The customer statement has its own layout: quantity beside the money, signed balance.
            return customer_statement.statement_csv(party, data, _period_label(start, end), f"customer-{party.pk}-statement")
        return _ledger_csv(f"{kind}-{party.pk}-statement", data)

    page = _paginate(request, data["entries"], LEDGER_PAGE_SIZE)
    opening = get_party_opening(party)
    context = {
        **{key: value for key, value in data.items() if key != "entries"},
        **_report_context(request, "party_statement", mode="range", start=start, end=end, all_time=all_time or not (start or end)),
        "party": party,
        "party_kind": kind,
        "party_detail_url": reverse(f"{kind}_detail", args=[party.pk]),
        "rows": ledger_rows(page.object_list, data["opening"]),
        "page_obj": page,
        "period_label": _period_label(start, end),
        "party_opening": opening,
        "opening_form": PartyOpeningBalanceForm(party_kind=kind, initial=opening or {}),
        "opening_action": reverse("party_opening_balance", args=[kind, party.pk]),
        "opening_next": request.get_full_path(),
    }
    if kind == "customer":
        context.update(
            customer_statement.statement_context(party, data, customer_statement.statement_rows(page.object_list, data["opening"]))
        )
    return render(request, "accounts/party_ledger.html", context)


def party_opening_balance(request, model, pk):
    party = _get_party(model, pk)
    kind = model.lower()
    statement_url = reverse("party_statement", args=[kind, party.pk])
    opening = get_party_opening(party)

    if request.method == "POST":
        form = PartyOpeningBalanceForm(request.POST, party_kind=kind)
        if form.is_valid():
            try:
                with transaction.atomic():
                    voucher = set_party_opening_balance(
                        party=party,
                        amount=form.cleaned_data["amount"],
                        direction=form.cleaned_data["direction"],
                        date=form.cleaned_data["date"],
                        actor=request.user,
                    )
                    record_audit(
                        action="party_opening_balance_set" if voucher else "party_opening_balance_removed",
                        description=f"Opening balance for {kind} '{party}' {'set to ' + str(form.cleaned_data['amount']) if voucher else 'removed'}.",
                        actor=request.user,
                        target=party,
                        metadata={
                            "amount": str(form.cleaned_data["amount"]),
                            "direction": form.cleaned_data["direction"],
                            "voucher_id": voucher.id if voucher else "",
                        },
                    )
                messages.success(request, f"Opening balance for {party} {'saved' if voucher else 'removed'}.")
                return redirect(_safe_next(request, statement_url))
            except ValueError as exc:
                form.add_error(None, str(exc))
        messages.error(request, "Please review the opening balance details.")
    else:
        form = PartyOpeningBalanceForm(party_kind=kind, initial=opening or {})

    return render(
        request,
        "accounts/party_opening.html",
        {
            "party": party,
            "party_kind": kind,
            "party_opening": opening,
            "opening_form": form,
            "opening_action": reverse("party_opening_balance", args=[kind, party.pk]),
            "opening_next": _safe_next(request, statement_url),
            "statement_url": statement_url,
        },
    )


# ---------------------------------------------------------------------------
# Aging
# ---------------------------------------------------------------------------

def _aging_view(request, kind):
    as_of = parse_report_date(request.GET.get("as_of"))
    data = get_aging(kind, as_of_date=as_of)
    party_kind = "customer" if kind == RECEIVABLE else "supplier"
    name = "receivables_aging" if kind == RECEIVABLE else "payables_aging"

    if _wants_csv(request):
        header = ["Party", "Phone", "Oldest unpaid", *[label for _key, label, _low, _high in BUCKETS], "Outstanding", "Advance"]
        rows = [
            [row["party"], row["phone"], _iso(row["oldest_open"]),
             *[plain_amount(row[key]) for key, *_rest in BUCKETS],
             plain_amount(row["outstanding"]), plain_amount(row["advance"])]
            for row in data["rows"]
        ]
        totals = data["totals"]
        rows.append(["Total", "", "", *[plain_amount(totals[key]) for key, *_rest in BUCKETS],
                     plain_amount(totals["outstanding"]), plain_amount(totals["advance"])])
        return csv_response(name.replace("_", "-"), header, rows)

    for row in data["rows"]:
        row["amounts"] = [row[key] for key, *_rest in BUCKETS]
        row["statement_url"] = reverse("party_statement", args=[party_kind, row["party_id"]]) if row["party_exists"] else ""

    context = {
        **data,
        **_report_context(request, name, mode="as_of", as_of=as_of),
        "is_receivable": kind == RECEIVABLE,
        "party_kind": party_kind,
        "as_of_label": f"{data['as_of_date']:%d %b %Y}",
    }
    return render(request, "accounts/aging.html", context)


def receivables_aging(request):
    return _aging_view(request, RECEIVABLE)


def payables_aging(request):
    return _aging_view(request, PAYABLE)


# ---------------------------------------------------------------------------
# Day book, cash flow, expenses by type
# ---------------------------------------------------------------------------

def day_book(request):
    start, end, all_time = _requested_range(request)
    data = get_day_book(start, end, voucher_type=request.GET.get("type", ""), all_time=all_time)

    if _wants_csv(request):
        rows = []
        for voucher in data["vouchers"].iterator(chunk_size=200):
            for line in voucher.lines.all():
                rows.append([
                    voucher.date.isoformat(), voucher.id, voucher.get_voucher_type_display(), voucher.narration or "",
                    line.account.title, plain_amount(line.debit), plain_amount(line.credit),
                    str(voucher.created_by) if voucher.created_by_id else "",
                ])
        rows.append(["", "", "", "Total", "", plain_amount(data["total_debit"]), plain_amount(data["total_credit"]), ""])
        return csv_response("day-book", ["Date", "Voucher", "Type", "Narration", "Account", "Debit", "Credit", "Posted by"], rows)

    page = _paginate(request, data["vouchers"], DAY_BOOK_PAGE_SIZE)
    context = {
        **{key: value for key, value in data.items() if key != "vouchers"},
        **_report_context(request, "day_book", mode="range", start=data["start_date"], end=data["end_date"], all_time=all_time),
        "page_obj": page,
        "period_label": _period_label(data["start_date"], data["end_date"]),
    }
    return render(request, "accounts/day_book.html", context)


def cash_flow(request):
    start, end, all_time = _requested_range(request)
    data = get_cash_flow(start, end, all_time=all_time)

    if _wants_csv(request):
        rows = [["Opening balance", "", "", plain_amount(data["opening_balance"])]]
        rows += [[row["label"], plain_amount(row["money_in"]), plain_amount(row["money_out"]), plain_amount(row["net"])] for row in data["rows"]]
        rows.append(["Total movement", plain_amount(data["total_in"]), plain_amount(data["total_out"]), plain_amount(data["net_change"])])
        rows.append(["Closing balance", "", "", plain_amount(data["closing_balance"])])
        return csv_response("cash-flow", ["Activity", "Money in", "Money out", "Net"], rows)

    context = {
        **data,
        **_report_context(request, "cash_flow", mode="range", start=data["start_date"], end=data["end_date"], all_time=all_time),
        "period_label": _period_label(data["start_date"], data["end_date"]),
    }
    return render(request, "accounts/cash_flow.html", context)


def expense_by_category_report(request):
    start, end, all_time = _requested_range(request)
    data = get_expense_by_category(start, end, all_time=all_time)

    if _wants_csv(request):
        rows = [
            [row["category"], row["bills"], plain_amount(row["amount"]), plain_amount(row["paid"]), plain_amount(row["due"]), str(row["share"])]
            for row in data["rows"]
        ]
        rows.append(["Total", data["total_bills"], plain_amount(data["total_amount"]), plain_amount(data["total_paid"]), plain_amount(data["total_due"]), ""])
        return csv_response("expenses-by-type", ["Expense type", "Bills", "Amount", "Paid", "Still due", "Share %"], rows)

    context = {
        **data,
        **_report_context(request, "expense_by_category", mode="range", start=data["start_date"], end=data["end_date"], all_time=all_time),
        "period_label": _period_label(data["start_date"], data["end_date"]),
        "expense_list_url": reverse("expense_list"),
    }
    return render(request, "accounts/expense_by_category.html", context)


# ---------------------------------------------------------------------------
# Books lock
# ---------------------------------------------------------------------------

def books_lock_update(request):
    home = reverse("accounts_home")
    if request.method != "POST":
        return redirect(home)
    if not is_admin(request.user):
        messages.error(request, "Only an administrator can lock or reopen the books.")
        return redirect(home)

    form = BooksLockForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect(f"{home}#books-lock")

    new_lock = form.cleaned_data["books_locked_until"]
    previous = get_books_locked_until()
    try:
        set_books_lock(new_lock, actor=request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(f"{home}#books-lock")

    record_audit(
        action="books_locked" if new_lock else "books_reopened",
        description=f"Books lock changed from {previous or 'open'} to {new_lock or 'open'}.",
        actor=request.user,
        metadata={"previous": _iso(previous), "current": _iso(new_lock)},
    )
    if new_lock:
        messages.success(request, f"Books locked up to {new_lock:%d %b %Y}. Entries dated on or before that day can no longer be changed.")
    else:
        messages.success(request, "Books reopened. Entries of any date can be posted again.")
    return redirect(f"{home}#books-lock")


_view = module_access_required("accounts", minimum="view")
trial_balance_view = _view(trial_balance_view)
profit_loss_view = _view(profit_loss_view)
balance_sheet_view = _view(balance_sheet_view)
account_ledger = _view(account_ledger)
party_statement = _view(party_statement)
party_opening_balance = _view(party_opening_balance)
receivables_aging = _view(receivables_aging)
payables_aging = _view(payables_aging)
day_book = _view(day_book)
cash_flow = _view(cash_flow)
expense_by_category_report = _view(expense_by_category_report)
books_lock_update = _view(books_lock_update)
