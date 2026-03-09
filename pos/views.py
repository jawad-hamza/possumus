from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas

from accounts.forms import BankAccountForm
from accounts.models import Account, AuditLog, BackupSnapshot, BankAccount, Expense
from accounts.services.audit import record_audit
from accounts.services.backup_restore import create_backup_snapshot, restore_backup_snapshot
from accounts.services.cash_bank import bank_account_queryset
from accounts.services.party_badges import get_customer_badge, get_supplier_badge
from accounts.services.pos_posting import post_purchase_from_pos, post_sale_from_pos
from .forms import (
    BrandForm,
    CategoryForm,
    CompanyProfileForm,
    CustomerForm,
    FirstAdminSetupForm,
    LoginForm,
    ProductForm,
    PurchaseForm,
    PurchaseItemFormSet,
    PurchaseReturnForm,
    QuickBrandForm,
    QuickCategoryForm,
    QuickProductForm,
    SaleReturnForm,
    StockAdjustmentForm,
    SupplierForm,
    SystemSettingsForm,
    TeamUserForm,
)
from .models import (
    Brand,
    Category,
    CompanyProfile,
    Customer,
    Product,
    Purchase,
    PurchaseEditLog,
    PurchaseItem,
    PurchaseReturn,
    Sale,
    SaleItem,
    SaleReturn,
    StockAdjustment,
    Supplier,
    SystemSettings,
    UserSessionLog,
    quantize_money,
)
from .services.access import (
    ADMIN_ROLE,
    admin_required,
    assign_role,
    close_user_session,
    default_home_url,
    ensure_role_groups,
    is_first_user_setup_required,
    open_user_session,
    pos_access_required,
    user_can_access_sale,
)
from .services.cart_pricing import (
    effective_unit_price,
    line_total as cart_line_total,
    normalize_cart_item,
    seed_cart_item,
    to_decimal,
    validate_cart_line,
)
from .services.navigation import (
    build_customer_add_url,
    build_supplier_add_url,
    get_safe_next_url,
    pop_customer_for_pos,
    remember_customer_for_pos,
)
from .services.purchase_balances import annotate_purchase_balances
from .services.returns import create_purchase_return, create_sale_return
from .services.stock_control import create_stock_adjustment
from .services.business_rules import (
    category_tax_rate_for_product,
    get_active_company_profile,
    get_system_settings,
    sale_tax_amount,
    sale_tax_label,
    sale_totals_from_rows,
)

def test(request):
    return render(request, "pos/test_template.html")


def safe_decimal(value):
    try:
        return Decimal(value)
    except (TypeError, InvalidOperation):
        return Decimal("0")


def _normalized_rate(value):
    rate = safe_decimal(value)
    if rate < 0:
        return Decimal("0")
    if rate > 100:
        return Decimal("100")
    return quantize_money(rate)


def _parse_report_date(value, default):
    if not value:
        return default

    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    return default

def _build_pos_url(
    *,
    category=None,
    customer_id=None,
    credit=False,
    payment_method=None,
    bank_account_id=None,
    discount_rate=None,
    tax_rate=None,
):
    params = {}
    if category:
        params["category"] = category
    if customer_id:
        params["customer_id"] = customer_id
    if credit:
        params["credit"] = "1"
    if payment_method and payment_method != Sale.CASH:
        params["payment_method"] = payment_method
    if bank_account_id and payment_method == Sale.BANK:
        params["bank_account_id"] = bank_account_id
    if discount_rate and safe_decimal(discount_rate) > 0:
        params["discount_rate"] = str(_normalized_rate(discount_rate))
    if tax_rate and safe_decimal(tax_rate) > 0:
        params["tax_rate"] = str(_normalized_rate(tax_rate))

    base_url = reverse("pos_screen")
    return f"{base_url}?{urlencode(params)}" if params else base_url



def _normalize_cart(cart):
    normalized = {}
    for product_id, item in (cart or {}).items():
        fixed = normalize_cart_item(item)
        if fixed["quantity"] > 0:
            normalized[str(product_id)] = fixed
    return normalized


def _save_cart(request, cart):
    request.session["cart"] = _normalize_cart(cart)
    request.session.modified = True


def _add_product_to_cart(cart, product):
    product_id = str(product.id)

    if product_id in cart:
        line = normalize_cart_item(cart[product_id])
        if line["quantity"] >= product.stock:
            return False
        line["quantity"] += 1
        cart[product_id] = line
        return True

    cart[product_id] = seed_cart_item(product)
    return True

def _cart_state(cart):
    cart = _normalize_cart(cart)
    product_ids = [int(product_id) for product_id in cart.keys()]
    products = Product.objects.in_bulk(product_ids)

    rows = []
    cart_total = Decimal("0")
    checkout_disabled = False
    stock_map = {}

    for product_id, item in cart.items():
        product = products.get(int(product_id))
        if product is None or not product.is_active:
            checkout_disabled = True
            rows.append(
                {
                    "product_id": product_id,
                    "name": item.get("name", "Unavailable Product"),
                    "quantity": int(item["quantity"]),
                    "unit_price": to_decimal(item["price"]),
                    "original_price": to_decimal(item["original_price"], to_decimal(item["price"])),
                    "discount_percent": to_decimal(item["discount_percent"]),
                    "effective_unit_price": Decimal("0"),
                    "line_total": Decimal("0"),
                    "line_discount_amount": Decimal("0"),
                    "minimum_price": Decimal("0"),
                    "category_tax_rate": Decimal("0"),
                    "stock": 0,
                    "has_custom_price": False,
                    "has_line_discount": False,
                    "pricing_error": "This product is no longer available.",
                    "stock_error": "This product is no longer available.",
                }
            )
            continue

        quantity = int(item["quantity"])
        unit_price = to_decimal(item["price"])
        original_price = to_decimal(item["original_price"], unit_price)
        discount_percent = to_decimal(item["discount_percent"])
        final_unit_price = effective_unit_price(unit_price, discount_percent)
        minimum_price = product.minimum_sale_price_for_cost(product.cost_price)
        line_total = cart_line_total(quantity, unit_price, discount_percent)
        gross_line_total = quantize_money(unit_price * quantity)
        line_discount_amount = quantize_money(gross_line_total - line_total)
        category_tax_rate = category_tax_rate_for_product(product)

        pricing_error = ""
        stock_error = ""
        try:
            validation = validate_cart_line(product, unit_price, discount_percent)
            final_unit_price = validation["final_unit_price"]
            minimum_price = validation["minimum_price"]
        except ValidationError as exc:
            pricing_error = " ".join(exc.messages)
            checkout_disabled = True

        if quantity > product.stock:
            stock_error = f"Only {product.stock} in stock."
            checkout_disabled = True
        if product.stock <= 0:
            stock_error = "Out of stock."
            checkout_disabled = True

        cart_total += line_total
        stock_map[product_id] = product.stock
        rows.append(
            {
                "product_id": product_id,
                "product": product,
                "name": product.name,
                "quantity": quantity,
                "unit_price": unit_price,
                "original_price": original_price,
                "discount_percent": discount_percent,
                "effective_unit_price": final_unit_price,
                "line_total": line_total,
                "line_discount_amount": line_discount_amount,
                "minimum_price": minimum_price,
                "category_tax_rate": category_tax_rate,
                "stock": product.stock,
                "has_custom_price": unit_price != original_price,
                "has_line_discount": discount_percent > 0,
                "pricing_error": pricing_error,
                "stock_error": stock_error,
            }
        )

    return {
        "cart": cart,
        "rows": rows,
        "cart_total": quantize_money(cart_total),
        "checkout_disabled": checkout_disabled,
        "stock_map": stock_map,
    }


def _sale_report_fields(sale):
    items = list(sale.items.all())
    subtotal = sum((item.line_total for item in items), Decimal("0"))

    discount_amount = quantize_money(subtotal * (_normalized_rate(sale.discount_rate) / Decimal("100")))
    tax_amount = sale_tax_amount(sale, subtotal=subtotal, items=items)
    balance_due = quantize_money(max(Decimal(sale.total_amount or 0) - Decimal(sale.paid_amount or 0), Decimal("0")))

    sale.report_subtotal = quantize_money(subtotal)
    sale.report_discount_amount = discount_amount
    sale.report_net_sales = quantize_money(Decimal(sale.total_amount or 0) - tax_amount)
    sale.report_tax_amount = tax_amount
    sale.report_tax_label = sale_tax_label(sale, get_system_settings().tax_label)
    sale.report_balance_due = balance_due
    return sale


def _sale_profit_fields(sale):
    sale = _sale_report_fields(sale)
    cost_total = Decimal("0")
    for item in sale.items.all():
        cost_total += item.line_cost

    sale.report_cost_total = quantize_money(cost_total)
    sale.report_gross_profit = quantize_money(sale.report_net_sales - sale.report_cost_total)
    return sale


def _sales_summary(sales_queryset):
    rows = []
    totals = {
        "subtotal": Decimal("0"),
        "discount": Decimal("0"),
        "net_sales": Decimal("0"),
        "tax": Decimal("0"),
        "total": Decimal("0"),
        "cost": Decimal("0"),
        "gross_profit": Decimal("0"),
        "paid": Decimal("0"),
        "due": Decimal("0"),
        "invoices": 0,
    }

    for sale in sales_queryset:
        row = _sale_profit_fields(sale)
        totals["subtotal"] += row.report_subtotal
        totals["discount"] += row.report_discount_amount
        totals["net_sales"] += row.report_net_sales
        totals["tax"] += row.report_tax_amount
        totals["total"] += Decimal(row.total_amount or 0)
        totals["cost"] += row.report_cost_total
        totals["gross_profit"] += row.report_gross_profit
        totals["paid"] += Decimal(row.paid_amount or 0)
        totals["due"] += row.report_balance_due
        totals["invoices"] += 1
        rows.append(row)

    for key in ("subtotal", "discount", "net_sales", "tax", "total", "cost", "gross_profit", "paid", "due"):
        totals[key] = quantize_money(totals[key])

    totals["avg_invoice"] = (
        quantize_money(totals["net_sales"] / totals["invoices"])
        if totals["invoices"]
        else Decimal("0")
    )
    return rows, totals


def _date_range_url(route_name, start, end):
    return f"{reverse(route_name)}?{urlencode({'start': start.isoformat(), 'end': end.isoformat()})}"


def _sum_expense_due(expenses):
    return quantize_money(sum((expense.balance_due for expense in expenses), Decimal("0")))


def _sales_trend(end_date, days=7):
    start_date = end_date - timedelta(days=days - 1)
    labels = []
    net_sales = []
    gross_profit = []

    for offset in range(days):
        current_day = start_date + timedelta(days=offset)
        daily_sales = (
            Sale.objects.filter(created_at__date=current_day)
            .prefetch_related("items", "items__product")
            .order_by("-created_at")
        )
        _, totals = _sales_summary(daily_sales)
        labels.append(current_day.strftime("%d %b"))
        net_sales.append(float(totals["net_sales"]))
        gross_profit.append(float(totals["gross_profit"]))

    return {"labels": labels, "net_sales": net_sales, "gross_profit": gross_profit}

@transaction.atomic
def scan_barcode(request, sku):
    code = (sku or "").strip()
    cart = request.session.get("cart", {})

    product = (
        Product.objects.select_for_update()
        .filter(Q(sku__iexact=code) | Q(barcode__iexact=code), is_active=True)
        .first()
    )
    if product is None:
        return JsonResponse({"status": "notfound"})

    if not _add_product_to_cart(cart, product):
        _save_cart(request, cart)
        return JsonResponse({"status": "out_of_stock", "product": product.name})

    _save_cart(request, cart)
    return JsonResponse({"status": "ok", "product_id": product.id})


def home(request):
    if is_first_user_setup_required():
        return redirect("setup_admin")
    if not request.user.is_authenticated:
        return redirect("login")
    return redirect(default_home_url(request.user))


def login_view(request):
    ensure_role_groups()
    if is_first_user_setup_required():
        return redirect("setup_admin")
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    next_url = request.GET.get("next") or request.POST.get("next") or ""
    form = LoginForm(request, data=request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            open_user_session(request, user)
            messages.success(request, f"Welcome back, {user.get_full_name() or user.username}.")
            if next_url and url_has_allowed_host_and_scheme(
                url=next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect(default_home_url(user))
        messages.error(request, "Check the login ID and password and try again.")

    return render(
        request,
        "pos/login.html",
        {
            "form": form,
            "next_url": next_url,
        },
    )


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        close_user_session(request)
        logout(request)
    return redirect("login")


def setup_admin(request):
    if not is_first_user_setup_required():
        return redirect(default_home_url(request.user) if request.user.is_authenticated else "login")

    ensure_role_groups()
    form = FirstAdminSetupForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            user = form.save()
            assign_role(user, ADMIN_ROLE)
            login(request, user)
            open_user_session(request, user)
            messages.success(request, "Owner account created. You can now manage the full system.")
            return redirect("dashboard")
        messages.error(request, "Please review the owner account details below.")

    return render(request, "pos/setup_admin.html", {"form": form})


def settings_page(request):
    ensure_role_groups()
    user_model = get_user_model()
    settings_obj = get_system_settings()
    company_profile = get_active_company_profile(create=True)

    team_form = TeamUserForm(prefix="team")
    branding_form = CompanyProfileForm(prefix="branding", instance=company_profile)
    system_form = SystemSettingsForm(prefix="business", instance=settings_obj)
    bank_form = BankAccountForm(prefix="bank")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create_user":
            team_form = TeamUserForm(request.POST, prefix="team")
            if team_form.is_valid():
                member = team_form.save()
                role = team_form.cleaned_data["role"]
                assign_role(member, role)
                record_audit(
                    action="team_user_created",
                    description=f"Login '{member.username}' created as {role.title()}.",
                    actor=request.user,
                    target=member,
                )
                messages.success(request, f"{member.username} created as {role.title()}.")
                return redirect("settings_page")
            messages.error(request, "Please review the team member details below.")

        elif action == "save_branding":
            branding_form = CompanyProfileForm(request.POST, request.FILES, prefix="branding", instance=company_profile)
            if branding_form.is_valid():
                branding = branding_form.save(commit=False)
                branding.is_active = True
                branding.save()
                record_audit(
                    action="branding_updated",
                    description="Business branding and receipt identity were updated.",
                    actor=request.user,
                    target=branding,
                )
                messages.success(request, "Business branding and receipt details updated.")
                return redirect("settings_page")
            messages.error(request, "Please review the branding details below.")

        elif action == "save_rules":
            system_form = SystemSettingsForm(request.POST, prefix="business", instance=settings_obj)
            if system_form.is_valid():
                settings_obj = system_form.save()
                if settings_obj.gst_enabled:
                    Account.objects.get_or_create(title="Sales Tax Payable", defaults={"type": Account.LIABILITY})
                record_audit(
                    action="system_settings_updated",
                    description="GST and POS rules were updated from Settings.",
                    actor=request.user,
                    target=settings_obj,
                    metadata={
                        "gst_enabled": settings_obj.gst_enabled,
                        "tax_mode": settings_obj.tax_mode,
                        "tax_label": settings_obj.tax_label,
                    },
                )
                messages.success(request, "GST and POS rules updated.")
                return redirect("settings_page")
            messages.error(request, "Please review the settings below.")

        elif action == "create_bank":
            bank_form = BankAccountForm(request.POST, prefix="bank")
            if bank_form.is_valid():
                bank = bank_form.save()
                record_audit(
                    action="bank_ledger_created",
                    description=f"Bank ledger '{bank.display_name}' created.",
                    actor=request.user,
                    target=bank,
                )
                messages.success(request, f"Bank ledger '{bank.display_name}' created.")
                return redirect("settings_page")
            messages.error(request, "Please review the bank ledger details below.")

        elif action == "toggle_bank":
            bank = get_object_or_404(BankAccount, pk=request.POST.get("bank_id"))
            bank.is_active = not bank.is_active
            bank.save(update_fields=["is_active"])
            record_audit(
                action="bank_ledger_toggled",
                description=f"Bank ledger '{bank.display_name}' {'enabled' if bank.is_active else 'disabled'}.",
                actor=request.user,
                target=bank,
                metadata={"is_active": bank.is_active},
            )
            messages.success(request, f"{bank.display_name} {'enabled' if bank.is_active else 'disabled'}.")
            return redirect("settings_page")

        elif action == "create_backup":
            label = (request.POST.get("backup_label") or "").strip()
            notes = (request.POST.get("backup_notes") or "").strip()
            try:
                snapshot = create_backup_snapshot(label=label, notes=notes, actor=request.user)
                messages.success(request, f"Backup '{snapshot.label}' created.")
                return redirect("settings_page")
            except ValueError as exc:
                messages.error(request, str(exc))

        elif action == "restore_backup":
            snapshot = get_object_or_404(BackupSnapshot, pk=request.POST.get("snapshot_id"))
            try:
                restore_backup_snapshot(snapshot=snapshot, actor=request.user)
                messages.success(request, f"Backup '{snapshot.label}' restored. Refresh any open pages to load restored data.")
                return redirect("settings_page")
            except ValueError as exc:
                messages.error(request, str(exc))

        elif action == "toggle_user":
            member = get_object_or_404(user_model, pk=request.POST.get("user_id"))
            if member.id == request.user.id:
                messages.error(request, "You cannot disable the account you are currently using.")
            else:
                member.is_active = not member.is_active
                member.save(update_fields=["is_active"])
                if not member.is_active:
                    UserSessionLog.objects.filter(user=member, logout_at__isnull=True).update(logout_at=timezone.now())
                record_audit(
                    action="team_user_toggled",
                    description=f"Login '{member.username}' {'enabled' if member.is_active else 'disabled'}.",
                    actor=request.user,
                    target=member,
                    metadata={"is_active": member.is_active},
                )
                status_copy = "enabled" if member.is_active else "disabled"
                messages.success(request, f"{member.username} has been {status_copy}.")
            return redirect("settings_page")

    raw_users = user_model.objects.all().order_by("username").annotate(
        sales_count=Count("sales_made", distinct=True),
        sales_value=Coalesce(Sum("sales_made__total_amount"), Decimal("0")),
    )
    team_users = [
        {
            "user": member,
            "role_label": "Admin" if member.is_superuser or member.is_staff else ("Cashier" if member.groups.filter(name="Cashier").exists() else "Unassigned"),
            "sales_count": member.sales_count,
            "sales_value": member.sales_value,
        }
        for member in raw_users
    ]

    session_logs = UserSessionLog.objects.select_related("user").order_by("-login_at")[:20]
    categories = Category.objects.order_by("name")
    bank_accounts = BankAccount.objects.select_related("account").order_by("name", "account_number")
    backup_snapshots = BackupSnapshot.objects.select_related("created_by").order_by("-created_at")[:10]
    audit_logs = AuditLog.objects.select_related("actor").order_by("-created_at")[:25]

    context = {
        "page_title": "Settings",
        "page_subtitle": "Control branding, GST, POS rules, bank ledgers, backups, and staff access from one place.",
        "page_icon": "bi-sliders",
        "page_icon_accent": "slate",
        "team_form": team_form,
        "branding_form": branding_form,
        "system_form": system_form,
        "bank_form": bank_form,
        "team_users": team_users,
        "session_logs": session_logs,
        "categories": categories,
        "bank_accounts": bank_accounts,
        "backup_snapshots": backup_snapshots,
        "audit_logs": audit_logs,
        "settings_obj": settings_obj,
        "total_team_users": len(team_users),
        "cashier_count": sum(1 for row in team_users if row["role_label"] == "Cashier"),
        "active_sessions_count": UserSessionLog.objects.filter(logout_at__isnull=True).count(),
        "taxable_category_count": categories.filter(tax_status=Category.TAXABLE).count(),
        "gst_enabled_category_count": categories.exclude(tax_status=Category.EXEMPT).count(),
        "bank_ledger_count": bank_accounts.count(),
        "active_bank_count": bank_accounts.filter(is_active=True).count(),
        "backup_count": BackupSnapshot.objects.count(),
        "audit_event_count": AuditLog.objects.count(),
    }
    return render(request, "pos/settings.html", context)
PRODUCT_SORT_OPTIONS = [
    ("name_asc", "Name A-Z"),
    ("name_desc", "Name Z-A"),
    ("brand", "Brand"),
    ("category", "Category"),
    ("supplier", "Supplier"),
    ("location", "Location A-Z"),
    ("location_desc", "Location Z-A"),
    ("stock_low", "Lowest Stock"),
    ("stock_high", "Highest Stock"),
    ("price_low", "Lowest Sale Price"),
    ("price_high", "Highest Sale Price"),
    ("recent", "Newest First"),
]


def _clean_choice_id(value):
    value = (value or "").strip()
    return value if value.isdigit() else ""


def _filtered_querystring(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


def _purchase_setup_context(next_url):
    return {
        "supplier_add_url": f"{reverse('supplier_add')}?{urlencode({'next': next_url})}",
        "quick_brand_form": QuickBrandForm(prefix="quick_brand"),
        "quick_category_form": QuickCategoryForm(prefix="quick_category"),
        "quick_supplier_form": SupplierForm(prefix="quick_supplier"),
        "quick_product_form": QuickProductForm(prefix="quick_product"),
        "quick_create_brand_url": reverse("quick_create_brand"),
        "quick_create_category_url": reverse("quick_create_category"),
        "quick_create_supplier_url": reverse("quick_create_supplier"),
        "quick_create_product_url": reverse("quick_create_product"),
    }


def _json_form_errors(form):
    return {
        field: [entry["message"] for entry in entries]
        for field, entries in form.errors.get_json_data().items()
    }


def product_list(request):
    query = request.GET.get("q", "").strip()
    selected_brand = _clean_choice_id(request.GET.get("brand"))
    selected_category = _clean_choice_id(request.GET.get("category"))
    selected_supplier = _clean_choice_id(request.GET.get("supplier"))
    selected_status = request.GET.get("status", "").strip()
    selected_sort = request.GET.get("sort", "name_asc").strip()

    queryset = Product.objects.select_related("category", "supplier", "brand")

    if query:
        queryset = queryset.filter(
            Q(name__icontains=query)
            | Q(sku__icontains=query)
            | Q(barcode__icontains=query)
            | Q(category__name__icontains=query)
            | Q(brand__name__icontains=query)
            | Q(supplier__name__icontains=query)
            | Q(location__icontains=query)
        )

    if selected_brand:
        queryset = queryset.filter(brand_id=selected_brand)
    if selected_category:
        queryset = queryset.filter(category_id=selected_category)
    if selected_supplier:
        queryset = queryset.filter(supplier_id=selected_supplier)

    if selected_status == "active":
        queryset = queryset.filter(is_active=True)
    elif selected_status == "inactive":
        queryset = queryset.filter(is_active=False)
    elif selected_status == "low_stock":
        queryset = queryset.filter(is_active=True, stock__lte=F("low_stock_threshold"))
    else:
        selected_status = ""

    sort_map = {
        "name_asc": ("name",),
        "name_desc": ("-name",),
        "brand": ("brand__name", "name"),
        "category": ("category__name", "name"),
        "supplier": ("supplier__name", "name"),
        "location": ("location", "name"),
        "location_desc": ("-location", "name"),
        "stock_low": ("stock", "name"),
        "stock_high": ("-stock", "name"),
        "price_low": ("price", "name"),
        "price_high": ("-price", "name"),
        "recent": ("-id",),
    }
    if selected_sort not in sort_map:
        selected_sort = "name_asc"

    queryset = queryset.order_by(*sort_map[selected_sort])

    paginator = Paginator(queryset, 20)
    products = paginator.get_page(request.GET.get("page"))

    summary_queryset = queryset
    context = {
        "products": products,
        "search_query": query,
        "brands": Brand.objects.filter(is_active=True).order_by("name"),
        "categories": Category.objects.filter(is_active=True).order_by("name"),
        "suppliers": Supplier.objects.filter(is_active=True).order_by("name"),
        "selected_brand": selected_brand,
        "selected_category": selected_category,
        "selected_supplier": selected_supplier,
        "selected_status": selected_status,
        "selected_sort": selected_sort,
        "sort_options": PRODUCT_SORT_OPTIONS,
        "filter_querystring": _filtered_querystring(request),
        "summary_total": summary_queryset.count(),
        "summary_active": summary_queryset.filter(is_active=True).count(),
        "summary_low_stock": summary_queryset.filter(is_active=True, stock__lte=F("low_stock_threshold")).count(),
        "summary_inactive": summary_queryset.filter(is_active=False).count(),
    }
    return render(request, "pos/product_list.html", context)


def product_add(request):
    form = ProductForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Product '{product.name}' added successfully.")
            return redirect("product_view", pk=product.id)
        messages.error(request, "Please fix the highlighted product fields.")

    return render(request, "pos/product_form.html", {"form": form, "title": "Add Product"})


def product_view(request, pk):
    product = get_object_or_404(Product.objects.select_related("category", "supplier", "brand"), id=pk)
    total_sold = SaleItem.objects.filter(product=product).aggregate(total=Coalesce(Sum("quantity"), 0))["total"]
    total_purchased = PurchaseItem.objects.filter(product=product).aggregate(total=Coalesce(Sum("quantity"), 0))["total"]
    recent_sales = SaleItem.objects.filter(product=product).select_related("sale").order_by("-sale__created_at")[:10]
    recent_purchases = (
        PurchaseItem.objects.filter(product=product)
        .select_related("purchase", "purchase__supplier")
        .order_by("-purchase__created_at")[:10]
    )
    return render(
        request,
        "pos/product_view.html",
        {
            "product": product,
            "total_sold": total_sold,
            "total_purchased": total_purchased,
            "recent_sales": recent_sales,
            "recent_purchases": recent_purchases,
        },
    )


def product_edit(request, pk):
    product = get_object_or_404(Product, id=pk)
    form = ProductForm(request.POST or None, instance=product)

    if request.method == "POST":
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Product '{product.name}' updated.")
            return redirect("product_view", pk=product.id)
        messages.error(request, "Please fix the highlighted product fields.")

    return render(request, "pos/product_form.html", {"form": form, "title": "Edit Product"})


def product_delete(request, pk):
    product = get_object_or_404(Product, id=pk)

    if request.method == "POST":
        if SaleItem.objects.filter(product=product).exists() or PurchaseItem.objects.filter(product=product).exists():
            messages.error(request, "This product has transaction history and cannot be deleted.")
            return redirect("product_view", pk=product.id)

        name = product.name
        product.delete()
        messages.success(request, f"Product '{name}' deleted successfully.")
        return redirect("product_list")

    return render(request, "pos/product_confirm_delete.html", {"product": product})


def dashboard(request):
    today = timezone.localdate()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)

    today_sales_queryset = (
        Sale.objects.filter(created_at__date=today)
        .select_related("customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )
    week_sales_queryset = (
        Sale.objects.filter(created_at__date__gte=week_start, created_at__date__lte=today)
        .select_related("customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )
    month_sales_queryset = (
        Sale.objects.filter(created_at__date__gte=month_start, created_at__date__lte=today)
        .select_related("customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )
    _, today_sales = _sales_summary(today_sales_queryset)
    _, week_sales = _sales_summary(week_sales_queryset)
    _, month_sales = _sales_summary(month_sales_queryset)

    low_stock_products = (
        Product.objects.filter(stock__lte=F("low_stock_threshold"), is_active=True)
        .select_related("category")
        .order_by("stock", "name")
    )
    low_stock_count = low_stock_products.count()
    best_selling = (
        SaleItem.objects.values("product__id", "product__name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")[:10]
    )
    best_selling_month = (
        SaleItem.objects.filter(sale__created_at__date__gte=month_start)
        .values("product__id", "product__name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")[:10]
    )
    top_customers = (
        Sale.objects.exclude(customer__isnull=True)
        .values("customer__id", "customer__name")
        .annotate(total_spent=Coalesce(Sum("total_amount"), Decimal("0")), invoices=Count("id"))
        .order_by("-total_spent")[:10]
    )
    recent_sales = Sale.objects.select_related("customer").order_by("-created_at")[:8]
    recent_purchases = Purchase.objects.select_related("supplier").order_by("-created_at")[:8]

    month_purchase_total = Purchase.objects.filter(created_at__date__gte=month_start, created_at__date__lte=today).aggregate(
        total=Coalesce(Sum("total_amount"), Decimal("0"))
    )["total"]
    expense_due_total = _sum_expense_due(Expense.objects.all().only("amount", "paid_amount"))
    customer_badges = [get_customer_badge(customer) for customer in Customer.objects.filter(is_active=True).only("id", "name", "is_active")]
    supplier_badges = [get_supplier_badge(supplier) for supplier in Supplier.objects.filter(is_active=True).only("id", "name", "is_active")]
    customer_receivable_total = quantize_money(sum((badge["amount"] for badge in customer_badges if badge["balance_type"] == "receivable"), Decimal("0")))
    customer_receivable_count = sum(1 for badge in customer_badges if badge["balance_type"] == "receivable")
    supplier_payable_total = quantize_money(sum((badge["amount"] for badge in supplier_badges if badge["balance_type"] == "payable"), Decimal("0")))
    supplier_payable_count = sum(1 for badge in supplier_badges if badge["balance_type"] == "payable")
    today_report_url = _date_range_url("daily_sales_report", today, today)
    week_report_url = _date_range_url("daily_sales_report", week_start, today)
    month_report_url = _date_range_url("daily_sales_report", month_start, today)
    month_margin_percent = quantize_money(
        (month_sales["gross_profit"] / month_sales["net_sales"]) * Decimal("100")
        if month_sales["net_sales"]
        else Decimal("0")
    )
    today_collection_rate = quantize_money(
        (today_sales["paid"] / today_sales["total"]) * Decimal("100")
        if today_sales["total"]
        else Decimal("0")
    )
    dashboard_kpis = [
        {
            "title": "Today Net Sales",
            "value": today_sales["net_sales"],
            "meta": f"{today_sales['invoices']} invoices today",
            "url": today_report_url,
            "kind": "money",
            "accent": "primary",
            "icon": "bi-cash-stack",
        },
        {
            "title": "Today Gross Profit",
            "value": today_sales["gross_profit"],
            "meta": f"Collection rate {today_collection_rate:.2f}%",
            "url": today_report_url,
            "kind": "money",
            "accent": "success",
            "icon": "bi-graph-up-arrow",
        },
        {
            "title": "Last 7 Days Sales",
            "value": week_sales["net_sales"],
            "meta": f"Avg invoice Rs {week_sales['avg_invoice']:.2f}",
            "url": week_report_url,
            "kind": "money",
            "accent": "info",
            "icon": "bi-calendar-week",
        },
        {
            "title": "This Month Purchases",
            "value": month_purchase_total,
            "meta": "Inventory bought this month",
            "url": reverse("purchase_list"),
            "kind": "money",
            "accent": "warning",
            "icon": "bi-box-seam",
        },
        {
            "title": "Low Stock Alerts",
            "value": low_stock_count,
            "meta": "Products below reorder point",
            "url": reverse("low_stock_list"),
            "kind": "number",
            "accent": "danger",
            "icon": "bi-exclamation-triangle",
        },
        {
            "title": "Expense Bills Due",
            "value": expense_due_total,
            "meta": "Unpaid operating bills",
            "url": reverse("expense_list"),
            "kind": "money",
            "accent": "danger",
            "icon": "bi-receipt-cutoff",
        },
        {
            "title": "Customers Owe You",
            "value": customer_receivable_total,
            "meta": f"{customer_receivable_count} customer balances need collection",
            "url": reverse("customer_list"),
            "kind": "money",
            "accent": "danger",
            "icon": "bi-people",
        },
        {
            "title": "You Owe Suppliers",
            "value": supplier_payable_total,
            "meta": f"{supplier_payable_count} supplier balances need settlement",
            "url": reverse("supplier_list"),
            "kind": "money",
            "accent": "warning",
            "icon": "bi-truck",
        },
    ]

    return render(
        request,
        "pos/dashboard.html",
        {
            "today": today,
            "week_start": week_start,
            "month_start": month_start,
            "today_sales": today_sales,
            "week_sales": week_sales,
            "month_sales": month_sales,
            "month_purchase_total": month_purchase_total,
            "expense_due_total": expense_due_total,
            "today_collection_rate": today_collection_rate,
            "month_margin_percent": month_margin_percent,
            "dashboard_kpis": dashboard_kpis,
            "reports_center_url": reverse("reports_home"),
            "low_stock_count": low_stock_count,
            "top_low_stock": low_stock_products[:10],
            "best_selling": best_selling,
            "best_selling_month": best_selling_month,
            "top_customers": top_customers,
            "recent_sales": recent_sales,
            "recent_purchases": recent_purchases,
            "sales_trend": _sales_trend(today, days=7),
        },
    )


def reports_home(request):
    today = timezone.localdate()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)

    today_sales_queryset = (
        Sale.objects.filter(created_at__date=today)
        .select_related("customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )
    week_sales_queryset = (
        Sale.objects.filter(created_at__date__gte=week_start, created_at__date__lte=today)
        .select_related("customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )
    month_sales_queryset = (
        Sale.objects.filter(created_at__date__gte=month_start, created_at__date__lte=today)
        .select_related("customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )
    _, today_sales = _sales_summary(today_sales_queryset)
    _, week_sales = _sales_summary(week_sales_queryset)
    _, month_sales = _sales_summary(month_sales_queryset)

    low_stock_products = (
        Product.objects.filter(stock__lte=F("low_stock_threshold"), is_active=True)
        .select_related("category")
        .order_by("stock", "name")
    )
    low_stock_count = low_stock_products.count()
    month_purchase_total = Purchase.objects.filter(created_at__date__gte=month_start, created_at__date__lte=today).aggregate(
        total=Coalesce(Sum("total_amount"), Decimal("0"))
    )["total"]
    expense_due_total = _sum_expense_due(Expense.objects.all().only("amount", "paid_amount"))
    month_best_selling = (
        SaleItem.objects.filter(sale__created_at__date__gte=month_start, sale__created_at__date__lte=today)
        .values("product__id", "product__name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")[:8]
    )

    today_report_url = _date_range_url("daily_sales_report", today, today)
    week_report_url = _date_range_url("daily_sales_report", week_start, today)
    month_report_url = _date_range_url("daily_sales_report", month_start, today)
    report_kpis = [
        {
            "title": "Today Sales",
            "value": today_sales["net_sales"],
            "meta": f"{today_sales['invoices']} invoices",
            "url": today_report_url,
            "kind": "money",
            "accent": "primary",
            "icon": "bi-calendar-day",
        },
        {
            "title": "Last 7 Days",
            "value": week_sales["net_sales"],
            "meta": f"Gross profit Rs {week_sales['gross_profit']:.2f}",
            "url": week_report_url,
            "kind": "money",
            "accent": "info",
            "icon": "bi-graph-up",
        },
        {
            "title": "This Month",
            "value": month_sales["net_sales"],
            "meta": f"{month_sales['invoices']} invoices this month",
            "url": month_report_url,
            "kind": "money",
            "accent": "success",
            "icon": "bi-calendar-month",
        },
        {
            "title": "Low Stock",
            "value": low_stock_count,
            "meta": "Products needing restock",
            "url": reverse("low_stock_list"),
            "kind": "number",
            "accent": "danger",
            "icon": "bi-boxes",
        },
    ]
    report_cards = [
        {
            "eyebrow": "Sales by invoice",
            "title": "Daily Sales Report",
            "description": "See each invoice with discounts, tax, collected amount, due amount, cost, and gross profit.",
            "metric_label": "Today",
            "metric_value": today_sales["gross_profit"],
            "metric_kind": "money",
            "url": today_report_url,
            "cta": "Open daily report",
            "icon": "bi-receipt",
            "accent": "primary",
        },
        {
            "eyebrow": "Range performance",
            "title": "Last 7 Days Sales",
            "description": "Quick management view of the last week without changing filters manually.",
            "metric_label": "Net sales",
            "metric_value": week_sales["net_sales"],
            "metric_kind": "money",
            "url": week_report_url,
            "cta": "Open 7 day report",
            "icon": "bi-activity",
            "accent": "info",
        },
        {
            "eyebrow": "Inventory watch",
            "title": "Low Stock Report",
            "description": "Find products that are at or below their reorder threshold before you miss sales.",
            "metric_label": "Products",
            "metric_value": low_stock_count,
            "metric_kind": "number",
            "url": reverse("low_stock_list"),
            "cta": "Open low stock",
            "icon": "bi-exclamation-diamond",
            "accent": "danger",
        },
        {
            "eyebrow": "Operating costs",
            "title": "Expense Register",
            "description": "Track rent, utilities, transport, and any other bills with paid and unpaid portions.",
            "metric_label": "Bills due",
            "metric_value": expense_due_total,
            "metric_kind": "money",
            "url": reverse("expense_list"),
            "cta": "Open expenses",
            "icon": "bi-receipt-cutoff",
            "accent": "warning",
        },
        {
            "eyebrow": "Accounting summary",
            "title": "Profit & Loss",
            "description": "Business-language income and expense statement for the current books.",
            "metric_label": "Month gross profit",
            "metric_value": month_sales["gross_profit"],
            "metric_kind": "money",
            "url": reverse("profit_loss"),
            "cta": "Open P&L",
            "icon": "bi-pie-chart",
            "accent": "success",
        },
        {
            "eyebrow": "Financial position",
            "title": "Balance Sheet",
            "description": "Understand what the business owns and owes without digging through ledgers.",
            "metric_label": "Month purchases",
            "metric_value": month_purchase_total,
            "metric_kind": "money",
            "url": reverse("balance_sheet"),
            "cta": "Open balance sheet",
            "icon": "bi-bank",
            "accent": "slate",
        },
    ]

    return render(
        request,
        "pos/reports_home.html",
        {
            "today": today,
            "week_start": week_start,
            "month_start": month_start,
            "report_kpis": report_kpis,
            "report_cards": report_cards,
            "top_low_stock": low_stock_products[:8],
            "best_selling_month": month_best_selling,
            "today_report_url": today_report_url,
            "week_report_url": week_report_url,
            "month_report_url": month_report_url,
        },
    )


@transaction.atomic
def pos_screen(request):
    system_settings = get_system_settings()
    cart = _normalize_cart(request.session.get("cart", {}))
    session_customer_id, session_credit = pop_customer_for_pos(request)
    bank_accounts = list(bank_account_queryset())
    active_bank_accounts = {str(account.id): account for account in bank_accounts}

    selected_category = request.GET.get("category", "").strip()
    selected_customer_id = request.GET.get("customer_id", "").strip() or request.POST.get("customer_id", "").strip()
    selected_bank_account_id = request.GET.get("bank_account_id", "").strip() or request.POST.get("bank_account_id", "").strip()
    if not selected_customer_id and session_customer_id:
        selected_customer_id = str(session_customer_id)
    if selected_bank_account_id not in active_bank_accounts:
        selected_bank_account_id = ""

    default_payment_method = system_settings.default_sale_payment_method or Sale.CASH
    payment_method = request.GET.get("payment_method", request.POST.get("payment_method", default_payment_method))
    if payment_method not in {choice[0] for choice in Sale.PAYMENT_METHOD_CHOICES}:
        payment_method = default_payment_method if default_payment_method in {choice[0] for choice in Sale.PAYMENT_METHOD_CHOICES} else Sale.CASH

    discount_rate = request.GET.get("discount_rate", request.POST.get("discount_rate", "0"))
    tax_rate = request.GET.get("tax_rate", request.POST.get("tax_rate", "0")) if system_settings.allows_manual_tax else "0"
    prefill_credit_sale = (
        request.GET.get("credit") == "1"
        or request.POST.get("is_credit") == "on"
        or session_credit
    )
    if not system_settings.allow_credit_sales:
        prefill_credit_sale = False

    if request.method == "POST":
        action = request.POST.get("action")
        product_id = request.POST.get("product_id", "").strip()
        selected_customer_id = (request.POST.get("selected_customer_id") or request.POST.get("customer_id") or selected_customer_id).strip()
        selected_bank_account_id = (request.POST.get("preserve_bank_account_id") or request.POST.get("bank_account_id") or selected_bank_account_id).strip()
        if selected_bank_account_id not in active_bank_accounts:
            selected_bank_account_id = ""
        prefill_credit_sale = system_settings.allow_credit_sales and (
            request.POST.get("preserve_credit") == "1" or request.POST.get("is_credit") == "on"
        )
        payment_method = request.POST.get("preserve_payment_method", payment_method)
        discount_rate = request.POST.get("preserve_discount_rate", discount_rate)
        tax_rate = request.POST.get("preserve_tax_rate", tax_rate) if system_settings.allows_manual_tax else "0"
        selected_category = request.POST.get("selected_category", selected_category).strip()

        if action == "add" and product_id:
            product = get_object_or_404(Product, id=product_id, is_active=True)
            if not _add_product_to_cart(cart, product):
                messages.error(request, f"{product.name} has no more stock available.")
            _save_cart(request, cart)
            return redirect(
                _build_pos_url(
                    category=selected_category,
                    customer_id=selected_customer_id,
                    credit=prefill_credit_sale,
                    payment_method=payment_method,
                    bank_account_id=selected_bank_account_id,
                    discount_rate=discount_rate,
                    tax_rate=tax_rate,
                )
            )

        if action == "decrease" and product_id:
            if product_id in cart:
                cart[product_id]["quantity"] = max(int(cart[product_id]["quantity"]) - 1, 0)
                if int(cart[product_id]["quantity"]) <= 0:
                    cart.pop(product_id, None)
                _save_cart(request, cart)
            return redirect(
                _build_pos_url(
                    category=selected_category,
                    customer_id=selected_customer_id,
                    credit=prefill_credit_sale,
                    payment_method=payment_method,
                    bank_account_id=selected_bank_account_id,
                    discount_rate=discount_rate,
                    tax_rate=tax_rate,
                )
            )

        if action == "remove_all" and product_id:
            cart.pop(product_id, None)
            _save_cart(request, cart)
            return redirect(
                _build_pos_url(
                    category=selected_category,
                    customer_id=selected_customer_id,
                    credit=prefill_credit_sale,
                    payment_method=payment_method,
                    bank_account_id=selected_bank_account_id,
                    discount_rate=discount_rate,
                    tax_rate=tax_rate,
                )
            )

        if action == "update_line" and product_id:
            product = get_object_or_404(Product, id=product_id, is_active=True)
            if product_id not in cart:
                messages.error(request, "Cart line no longer exists.")
            else:
                updated_line = normalize_cart_item(cart[product_id])
                custom_price = request.POST.get("custom_price", updated_line["price"])
                line_discount = request.POST.get("line_discount", updated_line["discount_percent"])
                settings_messages = []

                if not system_settings.allow_price_override:
                    custom_price = updated_line.get("original_price") or updated_line["price"]
                    if safe_decimal(request.POST.get("custom_price")) != safe_decimal(custom_price):
                        settings_messages.append("Price override is disabled in Settings.")

                if not system_settings.allow_line_discount:
                    if safe_decimal(request.POST.get("line_discount")) != Decimal("0"):
                        settings_messages.append("Line discount is disabled in Settings.")
                    line_discount = "0"

                try:
                    validated = validate_cart_line(product, custom_price, line_discount)
                except ValidationError as exc:
                    messages.error(request, " ".join(exc.messages))
                else:
                    updated_line["price"] = str(validated["price"])
                    updated_line["original_price"] = str(to_decimal(updated_line.get("original_price"), product.price))
                    updated_line["discount_percent"] = str(validated["discount_percent"])
                    cart[product_id] = updated_line
                    _save_cart(request, cart)
                    if settings_messages:
                        messages.error(request, " ".join(settings_messages))
                    else:
                        messages.success(request, f"{product.name} pricing updated.")

            return redirect(
                _build_pos_url(
                    category=selected_category,
                    customer_id=selected_customer_id,
                    credit=prefill_credit_sale,
                    payment_method=payment_method,
                    bank_account_id=selected_bank_account_id,
                    discount_rate=discount_rate,
                    tax_rate=tax_rate,
                )
            )

        if action == "checkout":
            if not cart:
                messages.error(request, "Add at least one item before checkout.")
            else:
                product_ids = [int(pid) for pid in cart.keys()]
                locked_products = Product.objects.select_for_update().in_bulk(product_ids)
                cart_snapshot = _cart_state(cart)

                if cart_snapshot["checkout_disabled"]:
                    messages.error(request, "Review stock and pricing warnings before checkout.")
                else:
                    discount_rate_value = _normalized_rate(request.POST.get("discount_rate"))
                    tax_rate_value = _normalized_rate(request.POST.get("tax_rate")) if system_settings.allows_manual_tax else Decimal("0")
                    customer = (
                        Customer.objects.filter(id=selected_customer_id, is_active=True).first()
                        if selected_customer_id
                        else None
                    )
                    is_credit = request.POST.get("is_credit") == "on"
                    payment_method_value = request.POST.get("payment_method") or default_payment_method
                    if payment_method_value not in {choice[0] for choice in Sale.PAYMENT_METHOD_CHOICES}:
                        payment_method_value = default_payment_method
                    bank_account = active_bank_accounts.get(selected_bank_account_id) if payment_method_value == Sale.BANK else None

                    if is_credit and not system_settings.allow_credit_sales:
                        messages.error(request, "Credit sale is disabled in Settings.")
                    elif is_credit and customer is None:
                        messages.error(request, "Credit sale requires an active customer.")
                    else:
                        sale = Sale(
                            customer=customer,
                            sold_by=request.user,
                            is_credit=is_credit,
                            payment_method=payment_method_value,
                            bank_account=bank_account,
                            paid_amount=Decimal("0"),
                            discount_rate=discount_rate_value,
                            tax_rate=tax_rate_value,
                            tax_mode=Sale.TAX_MODE_CATEGORY if system_settings.uses_category_gst else Sale.TAX_MODE_MANUAL,
                            tax_label=system_settings.tax_label,
                        )
                        sale.full_clean()
                        sale.save()

                        settings_adjusted_lines = False
                        for row in cart_snapshot["rows"]:
                            product = locked_products.get(int(row["product_id"]))
                            if product is None or row["quantity"] > product.stock:
                                raise ValidationError(f"Stock changed for {row['name']}. Please review the cart.")

                            original_price = to_decimal(row["original_price"], product.price)
                            price = row["unit_price"] if system_settings.allow_price_override else original_price
                            discount_percent = row["discount_percent"] if system_settings.allow_line_discount else Decimal("0")
                            validated = validate_cart_line(product, price, discount_percent)

                            if (
                                (not system_settings.allow_price_override and row["unit_price"] != original_price)
                                or (not system_settings.allow_line_discount and row["discount_percent"] > 0)
                            ):
                                settings_adjusted_lines = True

                            SaleItem.objects.create(
                                sale=sale,
                                product=product,
                                quantity=row["quantity"],
                                original_price=original_price,
                                price=validated["price"],
                                discount_percent=validated["discount_percent"],
                                cost_price=product.cost_price,
                            )

                        sale.refresh_from_db()
                        sale.paid_amount = Decimal("0") if is_credit else Decimal(sale.total_amount or 0)
                        sale.full_clean()
                        sale.save(update_fields=["paid_amount", "bank_account"])

                        post_sale_from_pos(
                            sale_id=sale.id,
                            customer=sale.customer,
                            total_amount=sale.total_amount,
                            paid_amount=sale.paid_amount,
                            payment_method=sale.payment_method,
                            bank_account=sale.bank_account,
                            sale_date=sale.created_at.date(),
                            tax_amount=sale.tax_amount,
                        )

                        record_audit(
                            action="sale_completed",
                            description=f"Sale #{sale.id} completed through POS.",
                            actor=request.user,
                            target=sale,
                            metadata={"total_amount": str(sale.total_amount), "payment_method": sale.payment_method},
                        )
                        request.session["cart"] = {}
                        request.session.modified = True
                        if settings_adjusted_lines:
                            messages.warning(
                                request,
                                "Some saved cart price changes were removed because current Settings no longer allow them.",
                            )
                        messages.success(request, f"Sale #{sale.id} completed successfully.")
                        return redirect("invoice", sale_id=sale.id)

    _save_cart(request, cart)
    cart_state = _cart_state(cart)
    sale_preview = sale_totals_from_rows(cart_state["rows"], discount_rate, tax_rate, system_settings)

    products_queryset = Product.objects.filter(is_active=True)
    if selected_category:
        products_queryset = products_queryset.filter(category_id=selected_category)

    products = products_queryset.select_related("category").order_by("name")
    categories = Category.objects.filter(is_active=True).order_by("name")
    customers = Customer.objects.filter(is_active=True).order_by("name")

    return render(
        request,
        "pos/pos_screen.html",
        {
            "products": products,
            "categories": categories,
            "customers": customers,
            "bank_accounts": bank_accounts,
            "selected_category": str(selected_category) if selected_category else "",
            "cart": cart_state["cart"],
            "cart_rows": cart_state["rows"],
            "cart_total": cart_state["cart_total"],
            "stock_map": cart_state["stock_map"],
            "checkout_disabled": cart_state["checkout_disabled"],
            "selected_customer_id": str(selected_customer_id) if selected_customer_id else "",
            "selected_bank_account_id": selected_bank_account_id,
            "prefill_credit_sale": prefill_credit_sale,
            "payment_method_value": payment_method,
            "discount_rate_value": _normalized_rate(discount_rate),
            "tax_rate_value": _normalized_rate(tax_rate) if system_settings.allows_manual_tax else Decimal("0"),
            "tax_label_value": system_settings.tax_label,
            "manual_tax_enabled": system_settings.allows_manual_tax,
            "uses_category_gst": system_settings.uses_category_gst,
            "gst_enabled": system_settings.gst_enabled,
            "allow_credit_sales": system_settings.allow_credit_sales,
            "allow_price_override": system_settings.allow_price_override,
            "allow_line_discount": system_settings.allow_line_discount,
            "sale_preview": sale_preview,
            "pos_customer_add_url": build_customer_add_url(),
        },
    )


def invoice(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related("customer", "sold_by"), id=sale_id)
    if not user_can_access_sale(request.user, sale):
        messages.error(request, "Cashiers can only open receipts for their own sales.")
        return redirect("pos_screen")
    items = list(sale.items.select_related("product"))

    subtotal = sum((item.line_total for item in items), Decimal("0"))
    line_discount_total = sum((item.line_discount_amount for item in items), Decimal("0"))
    discount_amount = quantize_money(subtotal * (_normalized_rate(sale.discount_rate) / Decimal("100")))
    tax_amount = sale_tax_amount(sale, subtotal=subtotal, items=items)
    grand_total = Decimal(sale.total_amount or 0)
    settings_obj = get_system_settings()

    previous_balance = None
    new_balance = None
    if sale.customer and sale.is_credit:
        current_balance = Decimal(sale.customer.balance or 0)
        sale_impact = Decimal(sale.balance_due or 0)
        previous_balance = quantize_money(current_balance - sale_impact)
        new_balance = quantize_money(current_balance)

    company = get_active_company_profile()
    return render(
        request,
        "pos/invoice.html",
        {
            "sale": sale,
            "items": items,
            "subtotal": subtotal,
            "line_discount_total": line_discount_total,
            "discount_rate": sale.discount_rate or Decimal("0"),
            "discount_amount": discount_amount,
            "tax_rate": sale.tax_rate or Decimal("0"),
            "tax_amount": tax_amount,
            "tax_label_value": sale_tax_label(sale, settings_obj.tax_label),
            "grand_total": grand_total,
            "paid_amount": sale.paid_amount,
            "balance_due": sale.balance_due,
            "company": company,
            "previous_balance": previous_balance,
            "new_balance": new_balance,
            "thermal_width_mm": settings_obj.thermal_printer_width_mm,
        },
    )


def add_customer_from_pos(request):
    next_url = get_safe_next_url(request, "customer_list")
    return_to_pos = next_url.startswith(reverse("pos_screen"))

    form = CustomerForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            customer = form.save()
            messages.success(request, f"Customer '{customer.name}' added.")

            if return_to_pos:
                remember_customer_for_pos(request, customer.id, credit="credit=1" in next_url)
                separator = "&" if "?" in next_url else "?"
                return redirect(f"{next_url}{separator}customer_id={customer.id}")

            if request.POST.get("next") or request.GET.get("next"):
                return redirect(next_url)
            return redirect("customer_detail", id=customer.id)

        messages.error(request, "Please fix the customer details below.")

    return render(
        request,
        "pos/add_customer.html",
        {
            "form": form,
            "next_url": next_url,
            "return_to_pos": return_to_pos,
        },
    )


def add_supplier_from_pos(request):
    next_url = get_safe_next_url(request, "supplier_list")
    return_to_purchase = reverse("purchase_list") in next_url or reverse("purchase_add") in next_url or "/purchases/" in next_url

    form = SupplierForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            supplier = form.save()
            messages.success(request, f"Supplier '{supplier.name}' added.")

            if request.POST.get("next") or request.GET.get("next"):
                return redirect(next_url)
            if return_to_purchase:
                return redirect(next_url)
            return redirect("supplier_detail", id=supplier.id)

        messages.error(request, "Please fix the supplier details below.")

    return render(
        request,
        "pos/add_supplier.html",
        {
            "form": form,
            "next_url": next_url,
            "return_to_purchase": return_to_purchase,
        },
    )


def invoice_pdf(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related("customer", "sold_by"), id=sale_id)
    if not user_can_access_sale(request.user, sale):
        messages.error(request, "Cashiers can only open receipts for their own sales.")
        return redirect("pos_screen")
    items = list(sale.items.select_related("product"))

    subtotal = sum((item.line_total for item in items), Decimal("0"))
    line_discount_total = sum((item.line_discount_amount for item in items), Decimal("0"))
    discount_amount = quantize_money(subtotal * (_normalized_rate(sale.discount_rate) / Decimal("100")))
    tax_amount = sale_tax_amount(sale, subtotal=subtotal, items=items)
    tax_label_value = sale_tax_label(sale, get_system_settings().tax_label)
    grand_total = Decimal(sale.total_amount or 0)
    printer_width_mm = get_system_settings().thermal_printer_width_mm

    paper_width = printer_width_mm * mm
    left_margin = 4 * mm
    right_margin = paper_width - (4 * mm)
    line_height = 9
    total_lines = 16
    if sale.sold_by_id:
        total_lines += 1
    for item in items:
        total_lines += 2
        if item.discount_percent > 0 or item.price != item.original_price:
            total_lines += 1
    if line_discount_total > 0:
        total_lines += 1
    if sale.discount_rate > 0:
        total_lines += 1
    if tax_amount > 0:
        total_lines += 1
    if sale.is_credit and sale.customer:
        total_lines += 3

    paper_height = (total_lines * line_height) + (14 * mm)
    y = paper_height - (6 * mm)

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="receipt_{sale.id}.pdf"'
    pdf = canvas.Canvas(response, pagesize=(paper_width, paper_height))

    company = get_active_company_profile()
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(paper_width / 2, y, (company.name if company else "POS IMS")[:24])
    y -= line_height

    if company and company.address:
        pdf.setFont("Helvetica", 6.5)
        pdf.drawCentredString(paper_width / 2, y, company.address[:34])
        y -= line_height

    if company and company.tax_registration_number:
        pdf.setFont("Helvetica", 6.5)
        pdf.drawCentredString(paper_width / 2, y, company.tax_registration_number[:34])
        y -= line_height

    pdf.setFont("Helvetica", 7)
    pdf.drawCentredString(paper_width / 2, y, "Sales Receipt")
    y -= line_height
    pdf.line(left_margin, y, right_margin, y)
    y -= line_height

    pdf.setFont("Helvetica", 6.8)
    pdf.drawString(left_margin, y, f"Inv #{sale.id}")
    pdf.drawRightString(right_margin, y, sale.created_at.strftime("%d-%m-%y"))
    y -= line_height
    pdf.drawString(left_margin, y, sale.created_at.strftime("%I:%M %p"))
    pdf.drawRightString(right_margin, y, sale.get_payment_method_display())
    y -= line_height
    pdf.drawString(left_margin, y, f"Cust: {(sale.customer.name if sale.customer else 'Walk-in')[:20]}")
    y -= line_height
    if sale.sold_by_id:
        cashier_name = sale.sold_by.get_full_name() or sale.sold_by.username
        pdf.drawString(left_margin, y, f"Cashier: {cashier_name[:18]}")
        y -= line_height
    if sale.is_credit:
        pdf.drawString(left_margin, y, "Type: Credit sale")
        y -= line_height
    pdf.line(left_margin, y, right_margin, y)
    y -= line_height

    for item in items:
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawString(left_margin, y, item.product.name[:24])
        y -= line_height

        pdf.setFont("Helvetica", 6.8)
        pdf.drawString(left_margin, y, f"{item.quantity} x {item.discounted_unit_price:.2f}")
        pdf.drawRightString(right_margin, y, f"{item.line_total:.2f}")
        y -= line_height

        if item.discount_percent > 0 or item.price != item.original_price:
            notes = []
            if item.price != item.original_price:
                notes.append(f"Custom {item.price:.2f}")
            if item.discount_percent > 0:
                notes.append(f"Disc {item.discount_percent:.0f}%")
            pdf.setFont("Helvetica", 6.2)
            pdf.drawString(left_margin, y, " | ".join(notes)[:30])
            y -= line_height

    pdf.line(left_margin, y, right_margin, y)
    y -= line_height

    pdf.setFont("Helvetica", 6.8)
    pdf.drawString(left_margin, y, "Subtotal")
    pdf.drawRightString(right_margin, y, f"{subtotal:.2f}")
    y -= line_height

    if line_discount_total > 0:
        pdf.drawString(left_margin, y, "Line disc")
        pdf.drawRightString(right_margin, y, f"{line_discount_total:.2f}")
        y -= line_height

    if sale.discount_rate > 0:
        pdf.drawString(left_margin, y, f"Order disc {sale.discount_rate}%")
        pdf.drawRightString(right_margin, y, f"{discount_amount:.2f}")
        y -= line_height

    if tax_amount > 0:
        label = tax_label_value[:10]
        if sale.tax_mode == Sale.TAX_MODE_MANUAL and Decimal(sale.tax_rate or 0) > 0:
            label = f"{label} {sale.tax_rate}%"[:18]
        pdf.drawString(left_margin, y, label)
        pdf.drawRightString(right_margin, y, f"{tax_amount:.2f}")
        y -= line_height

    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.drawString(left_margin, y, "TOTAL")
    pdf.drawRightString(right_margin, y, f"{grand_total:.2f}")
    y -= line_height

    pdf.setFont("Helvetica", 6.8)
    pdf.drawString(left_margin, y, "Paid")
    pdf.drawRightString(right_margin, y, f"{sale.paid_amount:.2f}")
    y -= line_height
    pdf.drawString(left_margin, y, "Balance")
    pdf.drawRightString(right_margin, y, f"{sale.balance_due:.2f}")
    y -= line_height

    if sale.is_credit and sale.customer:
        pdf.line(left_margin, y, right_margin, y)
        y -= line_height
        current_balance = Decimal(sale.customer.balance or 0)
        previous_balance = quantize_money(current_balance - sale.balance_due)
        pdf.drawString(left_margin, y, "Prev balance")
        pdf.drawRightString(right_margin, y, f"{previous_balance:.2f}")
        y -= line_height
        pdf.drawString(left_margin, y, "Current balance")
        pdf.drawRightString(right_margin, y, f"{current_balance:.2f}")
        y -= line_height

    pdf.setFont("Helvetica", 6.2)
    pdf.drawCentredString(
        paper_width / 2,
        y,
        company.invoice_footer[:32] if company and company.invoice_footer else "Thank you for your business.",
    )

    pdf.showPage()
    pdf.save()
    return response


def products_by_category(request, category_id):
    products = Product.objects.filter(category_id=category_id, is_active=True).order_by("name")
    return render(request, "pos/partials/product_grid.html", {"products": products})


def daily_sales_report(request):
    today = timezone.localdate()
    start = _parse_report_date(request.GET.get("start"), today)
    end = _parse_report_date(request.GET.get("end"), start)
    if end < start:
        start, end = end, start

    sales = (
        Sale.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
        .select_related("customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )

    sales_rows, totals = _sales_summary(sales)
    collection_rate = quantize_money(
        (totals["paid"] / totals["total"]) * Decimal("100")
        if totals["total"]
        else Decimal("0")
    )
    gross_margin = quantize_money(
        (totals["gross_profit"] / totals["net_sales"]) * Decimal("100")
        if totals["net_sales"]
        else Decimal("0")
    )
    range_label = (
        start.strftime("%d %b %Y")
        if start == end
        else f"{start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}"
    )
    quick_ranges = [
        {"label": "Today", "url": _date_range_url("daily_sales_report", today, today)},
        {"label": "Last 7 Days", "url": _date_range_url("daily_sales_report", today - timedelta(days=6), today)},
        {"label": "This Month", "url": _date_range_url("daily_sales_report", today.replace(day=1), today)},
        {"label": "Reports Centre", "url": reverse("reports_home")},
    ]
    return render(
        request,
        "pos/daily_sales_report.html",
        {
            "sales": sales_rows,
            "totals": totals,
            "start": start,
            "end": end,
            "range_label": range_label,
            "collection_rate": collection_rate,
            "gross_margin": gross_margin,
            "quick_ranges": quick_ranges,
            "tax_label": get_system_settings().tax_label,        },
    )


def parties_home(request):
    recent_customers = Customer.objects.order_by("-created_at", "-id")[:5]
    recent_suppliers = Supplier.objects.order_by("-created_at", "-id")[:5]
    return render(
        request,
        "parties/parties_home.html",
        {
            "total_customers": Customer.objects.count(),
            "total_suppliers": Supplier.objects.count(),
            "recent_customers": recent_customers,
            "recent_suppliers": recent_suppliers,
        },
    )


def customer_list(request):
    search = request.GET.get("search", "").strip()
    queryset = Customer.objects.order_by("name")

    if search:
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(phone__icontains=search)
            | Q(email__icontains=search)
            | Q(address__icontains=search)
        )

    customers = []
    for customer in queryset:
        badge = get_customer_badge(customer)
        customer.balance_type = badge["balance_type"]
        customer.balance_amount = badge["amount"]
        customers.append(customer)

    return render(
        request,
        "parties/customer_list.html",
        {
            "customers": customers,
            "total_customers": queryset.count(),
            "search_term": search,
        },
    )


def supplier_list(request):
    search = request.GET.get("search", "").strip()
    queryset = Supplier.objects.order_by("name")

    if search:
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(phone__icontains=search)
            | Q(email__icontains=search)
            | Q(address__icontains=search)
        )

    suppliers = []
    for supplier in queryset:
        badge = get_supplier_badge(supplier)
        supplier.balance_type = badge["balance_type"]
        supplier.balance_amount = badge["amount"]
        suppliers.append(supplier)

    return render(
        request,
        "parties/supplier_list.html",
        {
            "suppliers": suppliers,
            "total_suppliers": queryset.count(),
            "search_term": search,
        },
    )


def customer_detail(request, id):
    customer = get_object_or_404(Customer, id=id)
    sales = customer.sales.order_by("-created_at")
    total_sales = sales.aggregate(total=Coalesce(Sum("total_amount"), Decimal("0")))["total"]
    badge = get_customer_badge(customer)

    return render(
        request,
        "parties/customer_detail.html",
        {
            "customer": customer,
            "sales": sales[:10],
            "total_sales": total_sales,
            "total_transactions": sales.count(),
            "badge": badge,
            "bank_accounts": bank_account_queryset(),
        },
    )


def supplier_detail(request, id):
    supplier = get_object_or_404(Supplier, id=id)
    purchases = supplier.purchases.order_by("-created_at")
    total_purchases = purchases.aggregate(total=Coalesce(Sum("total_amount"), Decimal("0")))["total"]
    badge = get_supplier_badge(supplier)

    return render(
        request,
        "parties/supplier_detail.html",
        {
            "supplier": supplier,
            "purchases": purchases[:10],
            "total_purchases": total_purchases,
            "total_transactions": purchases.count(),
            "bank_accounts": bank_account_queryset(),
            "badge": badge,
        },
    )


def party_statement_page(request):
    return render(request, "parties/party_statement.html")


def brand_add(request):
    form = BrandForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "Brand added successfully.")
            return redirect("product_list")
        messages.error(request, "Please fix the brand form.")

    return render(request, "pos/brand_form.html", {"form": form})


def category_add(request):
    form = CategoryForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            category = form.save()
            messages.success(request, f"Category '{category.name}' added successfully.")
            return redirect("settings_page")
        messages.error(request, "Please fix the category form.")

    return render(
        request,
        "pos/category_form.html",
        {
            "form": form,
            "title": "Add Category",
            "submit_label": "Save Category",
        },
    )


def category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk)
    form = CategoryForm(request.POST or None, instance=category)
    if request.method == "POST":
        if form.is_valid():
            category = form.save()
            messages.success(request, f"Category '{category.name}' updated.")
            return redirect("settings_page")
        messages.error(request, "Please fix the category form.")

    return render(
        request,
        "pos/category_form.html",
        {
            "form": form,
            "title": f"Edit {category.name}",
            "submit_label": "Update Category",
            "category": category,
        },
    )


@require_POST
def quick_create_brand(request):
    form = QuickBrandForm(request.POST, prefix="quick_brand")
    if form.is_valid():
        brand = form.save()
        return JsonResponse({"status": "ok", "brand": {"id": brand.id, "name": brand.name}})
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


@require_POST
def quick_create_category(request):
    form = QuickCategoryForm(request.POST, prefix="quick_category")
    if form.is_valid():
        category = form.save()
        return JsonResponse({"status": "ok", "category": {"id": category.id, "name": category.name}})
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


@require_POST
def quick_create_supplier(request):
    form = SupplierForm(request.POST, prefix="quick_supplier")
    if form.is_valid():
        supplier = form.save()
        return JsonResponse({"status": "ok", "supplier": {"id": supplier.id, "name": supplier.name}})
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


@require_POST
def quick_create_product(request):
    form = QuickProductForm(request.POST, prefix="quick_product")
    if form.is_valid():
        product = form.save()
        return JsonResponse(
            {
                "status": "ok",
                "product": {
                    "id": product.id,
                    "name": product.name,
                    "sku": product.sku,
                    "price": str(product.price),
                    "brand_id": product.brand_id,
                    "category_id": product.category_id,
                    "supplier_id": product.supplier_id,
                },
            }
        )
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


def _active_purchase_items(formset):
    items = []
    total = Decimal("0")
    for form in formset.forms:
        if not getattr(form, "cleaned_data", None):
            continue
        if form.cleaned_data.get("DELETE"):
            continue
        if not form.cleaned_data.get("product"):
            continue

        item = form.save(commit=False)
        items.append(item)
        total += item.line_total

    return items, quantize_money(total)


def purchase_list(request):
    purchases_queryset = Purchase.objects.select_related("supplier").order_by("-created_at", "-id")
    month_start = timezone.localdate().replace(day=1)

    this_month_total = purchases_queryset.filter(created_at__date__gte=month_start).aggregate(
        total=Coalesce(Sum("total_amount"), Decimal("0"))
    )["total"]
    purchases = annotate_purchase_balances(purchases_queryset)
    outstanding_total = quantize_money(sum((purchase.report_balance_due for purchase in purchases), Decimal("0")))

    return render(
        request,
        "pos/purchase_list.html",
        {
            "purchases": purchases,
            "this_month_total": this_month_total,
            "outstanding_total": outstanding_total,
        },
    )


@transaction.atomic
def purchase_add(request):
    purchase_form = PurchaseForm(request.POST or None)
    formset = PurchaseItemFormSet(
        request.POST or None,
        queryset=PurchaseItem.objects.none(),
        prefix="items",
    )

    if request.method == "POST":
        if purchase_form.is_valid() and formset.is_valid():
            items, calculated_total = _active_purchase_items(formset)
            supplier = purchase_form.cleaned_data["supplier"]
            paid_amount = purchase_form.cleaned_data["paid_amount"]

            if paid_amount > calculated_total:
                purchase_form.add_error("paid_amount", "Paid amount cannot exceed purchase total.")
            if calculated_total > paid_amount and supplier is None:
                purchase_form.add_error("supplier", "Supplier is required when any balance remains unpaid.")
            if not purchase_form.errors:
                purchase = purchase_form.save()
                for item in items:
                    item.purchase = purchase
                    item.save()

                purchase.refresh_from_db()
                post_purchase_from_pos(
                    purchase_id=purchase.id,
                    supplier=purchase.supplier,
                    total_amount=purchase.total_amount,
                    paid_amount=purchase.paid_amount,
                    payment_method=purchase.payment_method,
                    bank_account=purchase.bank_account,
                    purchase_date=purchase.created_at.date(),
                )
                messages.success(request, f"Purchase #{purchase.id} saved successfully.")
                record_audit(
                    action="purchase_created",
                    description=f"Purchase #{purchase.id} recorded.",
                    actor=request.user,
                    target=purchase,
                    metadata={"total_amount": str(purchase.total_amount), "payment_method": purchase.payment_method},
                )
                return redirect("purchase_detail", pk=purchase.id)
        messages.error(request, "Please review the purchase form and line items.")

    context = {
        "purchase_form": purchase_form,
        "formset": formset,
    }
    context.update(_purchase_setup_context(reverse("purchase_add")))
    return render(request, "pos/purchase_add.html", context)


def purchase_detail(request, pk):
    purchase = get_object_or_404(
        Purchase.objects.select_related("supplier").prefetch_related("items__product", "edit_logs"),
        pk=pk,
    )
    purchase = annotate_purchase_balances([purchase])[0]
    return render(request, "pos/purchase_detail.html", {"purchase": purchase})


@transaction.atomic
def purchase_edit(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related("supplier"), pk=pk)
    purchase_form = PurchaseForm(request.POST or None, instance=purchase)
    formset = PurchaseItemFormSet(
        request.POST or None,
        queryset=purchase.items.select_related("product").all(),
        prefix="items",
    )

    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()

        if purchase_form.is_valid() and formset.is_valid():
            items, calculated_total = _active_purchase_items(formset)
            supplier = purchase_form.cleaned_data["supplier"]
            paid_amount = purchase_form.cleaned_data["paid_amount"]

            if not reason:
                messages.error(request, "Reason for edit is required.")
            else:
                if paid_amount > calculated_total:
                    purchase_form.add_error("paid_amount", "Paid amount cannot exceed purchase total.")
                if calculated_total > paid_amount and supplier is None:
                    purchase_form.add_error("supplier", "Supplier is required when any balance remains unpaid.")

            if reason and not purchase_form.errors:
                old_total = purchase.total_amount
                purchase = purchase_form.save(commit=False)
                purchase.last_edited_by = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
                purchase.last_edited_at = timezone.now()
                purchase.edit_reason = reason
                purchase.save()

                for deleted in formset.deleted_objects:
                    deleted.delete()

                for item in items:
                    item.purchase = purchase
                    item.save()

                purchase.refresh_from_db()
                post_purchase_from_pos(
                    purchase_id=purchase.id,
                    supplier=purchase.supplier,
                    total_amount=purchase.total_amount,
                    paid_amount=purchase.paid_amount,
                    payment_method=purchase.payment_method,
                    bank_account=purchase.bank_account,
                    purchase_date=purchase.created_at.date(),
                )

                record_audit(
                    action="purchase_updated",
                    description=f"Purchase #{purchase.id} updated. Reason: {reason}",
                    actor=request.user,
                    target=purchase,
                    metadata={"old_total": str(old_total), "new_total": str(purchase.total_amount)},
                )
                PurchaseEditLog.objects.create(
                    purchase=purchase,
                    edited_by=purchase.last_edited_by,
                    reason=reason,
                    old_total=old_total,
                    new_total=purchase.total_amount,
                )

                messages.success(request, f"Purchase #{purchase.id} updated.")
                return redirect("purchase_detail", pk=purchase.id)

        messages.error(request, "Please review the purchase changes.")

    context = {
        "purchase": purchase,
        "purchase_form": purchase_form,
        "formset": formset,
    }
    context.update(_purchase_setup_context(reverse("purchase_edit", args=[purchase.id])))
    return render(request, "pos/purchase_edit.html", context)


def stock_adjustments(request):
    selected_product_id = (request.GET.get("product") or request.POST.get("product") or "").strip()
    initial = {}
    if selected_product_id.isdigit():
        initial["product"] = selected_product_id

    form = StockAdjustmentForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            adjustment = create_stock_adjustment(
                product=form.cleaned_data["product"],
                adjustment_mode=form.cleaned_data["adjustment_mode"],
                quantity_after=form.cleaned_data.get("quantity_after"),
                quantity_delta=form.cleaned_data.get("quantity_delta"),
                reason=form.cleaned_data["reason"],
                notes=form.cleaned_data.get("notes") or "",
                actor=request.user,
            )
            messages.success(request, f"Stock updated for {adjustment.product.name}.")
            next_url = get_safe_next_url(request, "stock_adjustments")
            if next_url != reverse("stock_adjustments"):
                return redirect(next_url)
            return redirect(f"{reverse('stock_adjustments')}?product={adjustment.product_id}")
        except ValueError as exc:
            form.add_error(None, str(exc))
            messages.error(request, "Please review the stock adjustment details.")

    adjustments = StockAdjustment.objects.select_related("product", "voucher", "created_by").order_by("-created_at", "-id")[:20]
    return render(
        request,
        "pos/stock_adjustments.html",
        {
            "form": form,
            "adjustments": adjustments,
            "selected_product_id": selected_product_id,
            "page_title": "Stock Adjustments",
        },
    )


def sale_return_view(request, sale_id):
    sale = get_object_or_404(
        Sale.objects.select_related("customer", "sold_by").prefetch_related("items__product__category", "items__return_items", "returns__items__product"),
        id=sale_id,
    )
    initial = {"refund_method": SaleReturn.RECEIVABLE if sale.is_credit and sale.customer else SaleReturn.CASH}
    form = SaleReturnForm(request.POST or None, initial=initial)

    item_rows = []
    for item in sale.items.all():
        returned_qty = sum(return_item.quantity for return_item in item.return_items.all())
        item_rows.append(
            {
                "item": item,
                "returned_qty": returned_qty,
                "returnable_qty": max(item.quantity - returned_qty, 0),
            }
        )

    if request.method == "POST" and form.is_valid():
        quantities = {row["item"].id: request.POST.get(f"qty_{row['item'].id}", "0") for row in item_rows}
        try:
            sale_return = create_sale_return(
                sale=sale,
                quantities=quantities,
                refund_method=form.cleaned_data["refund_method"],
                bank_account=form.cleaned_data.get("bank_account"),
                reason=form.cleaned_data["reason"],
                notes=form.cleaned_data.get("notes") or "",
                actor=request.user,
            )
            messages.success(request, f"Sale return #{sale_return.id} recorded.")
            return redirect("invoice", sale_id=sale.id)
        except ValueError as exc:
            form.add_error(None, str(exc))
            messages.error(request, "Please review the sale return details.")

    existing_returns = sale.returns.prefetch_related("items__product").order_by("-created_at", "-id")
    return render(
        request,
        "pos/sale_return.html",
        {
            "sale": sale,
            "form": form,
            "item_rows": item_rows,
            "existing_returns": existing_returns,
        },
    )


def purchase_return_view(request, pk):
    purchase = get_object_or_404(
        Purchase.objects.select_related("supplier").prefetch_related("items__product", "items__return_items", "returns__items__product"),
        pk=pk,
    )
    purchase = annotate_purchase_balances([purchase])[0]
    initial = {"settlement_method": PurchaseReturn.PAYABLE if purchase.supplier else PurchaseReturn.CASH}
    form = PurchaseReturnForm(request.POST or None, initial=initial)

    item_rows = []
    for item in purchase.items.all():
        returned_qty = sum(return_item.quantity for return_item in item.return_items.all())
        item_rows.append(
            {
                "item": item,
                "returned_qty": returned_qty,
                "returnable_qty": max(item.quantity - returned_qty, 0),
            }
        )

    if request.method == "POST" and form.is_valid():
        quantities = {row["item"].id: request.POST.get(f"qty_{row['item'].id}", "0") for row in item_rows}
        try:
            purchase_return = create_purchase_return(
                purchase=purchase,
                quantities=quantities,
                settlement_method=form.cleaned_data["settlement_method"],
                bank_account=form.cleaned_data.get("bank_account"),
                reason=form.cleaned_data["reason"],
                notes=form.cleaned_data.get("notes") or "",
                actor=request.user,
            )
            messages.success(request, f"Purchase return #{purchase_return.id} recorded.")
            return redirect("purchase_detail", pk=purchase.id)
        except ValueError as exc:
            form.add_error(None, str(exc))
            messages.error(request, "Please review the purchase return details.")

    existing_returns = purchase.returns.prefetch_related("items__product").order_by("-created_at", "-id")
    return render(
        request,
        "pos/purchase_return.html",
        {
            "purchase": purchase,
            "form": form,
            "item_rows": item_rows,
            "existing_returns": existing_returns,
        },
    )

def low_stock_list(request):
    products = (
        Product.objects.filter(stock__lte=F("low_stock_threshold"), is_active=True)
        .select_related("category")
        .order_by("stock", "name")
    )
    return render(request, "pos/low_stock_list.html", {"products": products})






















test = admin_required(test)
settings_page = admin_required(settings_page)
scan_barcode = pos_access_required(scan_barcode)
product_list = admin_required(product_list)
product_add = admin_required(product_add)
product_view = admin_required(product_view)
product_edit = admin_required(product_edit)
product_delete = admin_required(product_delete)
dashboard = admin_required(dashboard)
reports_home = admin_required(reports_home)
pos_screen = pos_access_required(pos_screen)
invoice = pos_access_required(invoice)
add_customer_from_pos = pos_access_required(add_customer_from_pos)
add_supplier_from_pos = admin_required(add_supplier_from_pos)
invoice_pdf = pos_access_required(invoice_pdf)
products_by_category = pos_access_required(products_by_category)
daily_sales_report = admin_required(daily_sales_report)
parties_home = admin_required(parties_home)
customer_list = admin_required(customer_list)
supplier_list = admin_required(supplier_list)
customer_detail = admin_required(customer_detail)
supplier_detail = admin_required(supplier_detail)
party_statement_page = admin_required(party_statement_page)
brand_add = admin_required(brand_add)
category_add = admin_required(category_add)
quick_create_brand = admin_required(quick_create_brand)
quick_create_category = admin_required(quick_create_category)
quick_create_supplier = admin_required(quick_create_supplier)
quick_create_product = admin_required(quick_create_product)
purchase_list = admin_required(purchase_list)
purchase_add = admin_required(purchase_add)
purchase_detail = admin_required(purchase_detail)
purchase_edit = admin_required(purchase_edit)
low_stock_list = admin_required(low_stock_list)
stock_adjustments = admin_required(stock_adjustments)
sale_return_view = admin_required(sale_return_view)
purchase_return_view = admin_required(purchase_return_view)
























