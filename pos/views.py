import json
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas

from accounts.forms import BankAccountForm
from accounts.models import Account, AuditLog, BackupSnapshot, BankAccount, Expense
from accounts.services.audit import record_audit
from accounts.services.backup_restore import (
    backup_runtime_summary,
    create_backup_snapshot,
    database_vendor_label,
    get_backup_file_path,
    infer_backup_vendor,
    restore_backup_snapshot,
    upload_backup_snapshot,
)
from accounts.services.cash_bank import bank_account_queryset
from accounts.services.expense_alerts import monthly_bill_alerts
from accounts.services.party_badges import get_customer_badge, get_supplier_badge
from accounts.services.pos_posting import post_purchase_from_pos, post_sale_from_pos
from .forms import (
    BrandForm,
    CategoryForm,
    AdminTimeZoneForm,
    CompanyProfileForm,
    CustomRoleForm,
    CustomerForm,
    LoginForm,
    OwnerBusinessSetupForm,
    OwnerSignupForm,
    ProductForm,
    PurchaseForm,
    PurchaseItemFormSet,
    QuickBrandForm,
    QuickCategoryForm,
    QuickProductForm,
    StockAdjustmentForm,
    SupplierForm,
    SystemSettingsForm,
    TeamUserForm,
)
from .models import (
    Brand,
    Category,
    BillingSubscription,
    CompanyProfile,
    CustomRole,
    Customer,
    OwnerOnboarding,
    Product,
    UserPreference,
    Purchase,
    PurchaseEditLog,
    PurchaseItem,
    Sale,
    SaleItem,
    StockAdjustment,
    Supplier,
    SystemSettings,
    UserSessionLog,
    quantize_money,
)
from .services.access import (
    ACCESS_MANAGE,
    ACCESS_VIEW,
    ADMIN_ROLE,
    MODULE_DEFINITIONS,
    admin_required,
    assign_role,
    any_module_access_required,
    close_user_session,
    default_home_url,
    describe_permissions,
    get_user_role_value,
    ensure_role_groups,
    get_owner_onboarding,
    get_role_choices,
    get_role_label,
    get_user_role_label,
    get_user_role_summary,
    is_first_user_setup_required,
    module_access_required,
    open_user_session,
    user_requires_business_setup,
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
from .services.stock_control import create_stock_adjustment
from .services.user_timezones import (
    get_effective_time_zone,
    get_session_time_zone,
    get_user_time_zone_preference,
    normalize_time_zone_name,
)
from .services.billing import (
    LEMON_SQUEEZY_SIGNATURE_HEADER,
    begin_lemon_squeezy_checkout,
    get_lemon_squeezy_plan_url,
    google_sign_in_setup_message,
    process_lemon_squeezy_webhook,
    user_can_manage_billing,
    verify_lemon_squeezy_signature,
    workspace_owner_onboarding,
    workspace_requires_billing,
)
from .services.google_auth import (
    build_google_authorization_url,
    clear_google_oauth_session,
    exchange_google_authorization_code,
    get_google_oauth_session,
    google_sign_in_enabled,
    verify_google_identity_token,
)
from .services.business_rules import (
    category_tax_rate_for_product,
    expired_products,
    get_active_company_profile,
    get_system_settings,
    near_expiry_products,
    payment_method_totals_for_sale,
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


def _remember_browser_time_zone(request, raw_time_zone):
    time_zone_name = normalize_time_zone_name(raw_time_zone, "")
    if not time_zone_name:
        return ""
    request.session["browser_time_zone"] = time_zone_name
    request.session.modified = True
    return time_zone_name


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
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params)}"


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

def _cart_state(cart, payment_method=Sale.CASH):
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
                    "cash_category_tax_rate": Decimal("0"),
                    "bank_category_tax_rate": Decimal("0"),
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
        cash_category_tax_rate = category_tax_rate_for_product(product, Sale.CASH)
        bank_category_tax_rate = category_tax_rate_for_product(product, Sale.BANK)
        category_tax_rate = bank_category_tax_rate if payment_method == Sale.BANK else cash_category_tax_rate

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
                "cash_category_tax_rate": cash_category_tax_rate,
                "bank_category_tax_rate": bank_category_tax_rate,
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
    params = {}
    if start:
        params["from"] = start.isoformat() if hasattr(start, "isoformat") else str(start)
    if end:
        params["to"] = end.isoformat() if hasattr(end, "isoformat") else str(end)
    base_url = reverse(route_name)
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params)}"


def _sum_expense_due(expenses):
    total_due = Decimal("0")
    for expense in expenses:
        amount = Decimal(getattr(expense, "amount", 0) or 0)
        paid_amount = Decimal(getattr(expense, "paid_amount", 0) or 0)
        total_due += max(amount - paid_amount, Decimal("0"))
    return quantize_money(total_due)


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

    return {
        "labels": labels,
        "net_sales": net_sales,
        "gross_profit": gross_profit,
    }


def _session_duration(log, now=None):
    end_time = log.logout_at or now or timezone.now()
    if end_time < log.login_at:
        return timedelta(0)
    return end_time - log.login_at


def _format_duration(delta):
    total_seconds = max(int(delta.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _user_wage_summary(user):
    staff_expenses = Expense.objects.filter(employee=user).select_related("category")
    booked = quantize_money(sum((Decimal(expense.amount or 0) for expense in staff_expenses), Decimal("0")))
    paid = quantize_money(sum((Decimal(expense.paid_amount or 0) for expense in staff_expenses), Decimal("0")))
    due = quantize_money(sum((Decimal(expense.balance_due or 0) for expense in staff_expenses), Decimal("0")))
    return {
        "booked": booked,
        "paid": paid,
        "due": due,
        "count": staff_expenses.count(),
        "expenses": staff_expenses.order_by("-date", "-id"),
    }


def _employee_report_row(user, now=None):
    sales_queryset = (
        Sale.objects.filter(sold_by=user)
        .select_related("customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")
    )
    _, sales_totals = _sales_summary(sales_queryset)
    logs = list(UserSessionLog.objects.filter(user=user).order_by("-login_at"))
    total_duration = sum((_session_duration(log, now=now) for log in logs), timedelta(0))
    wage_summary = _user_wage_summary(user)
    return {
        "user": user,
        "role_label": get_user_role_label(user),
        "sales_totals": sales_totals,
        "sales_count": sales_totals["invoices"],
        "total_time": total_duration,
        "total_time_display": _format_duration(total_duration),
        "active_sessions": sum(1 for log in logs if log.logout_at is None),
        "wages_booked": wage_summary["booked"],
        "wages_paid": wage_summary["paid"],
        "wages_due": wage_summary["due"],
        "wage_expense_count": wage_summary["count"],
    }

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

def products_by_category(request, category_id):
    category = get_object_or_404(Category, pk=category_id, is_active=True)
    products = (
        Product.objects.filter(category=category, is_active=True)
        .select_related("brand", "category", "supplier")
        .order_by("name")
    )
    payload = [
        {
            "id": product.id,
            "name": product.name,
            "sku": product.sku,
            "barcode": product.barcode,
            "price": float(product.price or 0),
            "stock": product.stock,
            "location": product.location,
            "brand": product.brand.name if product.brand_id else "",
            "category": category.name,
            "supplier": product.supplier.name if product.supplier_id else "",
        }
        for product in products
    ]
    return JsonResponse({"category": category.name, "products": payload})


LANDING_BRAND_NAME = getattr(settings, "PUBLIC_BRAND_NAME", "Vendora")
LANDING_PUBLIC_DOMAIN = getattr(settings, "PUBLIC_APP_DOMAIN", "https://vendora.app")
LANDING_SUPPORT_EMAIL = getattr(settings, "DEFAULT_FROM_EMAIL", "hello@vendora.app")
LANDING_CHECKOUT_PROVIDER = getattr(settings, "LEMON_SQUEEZY_PROVIDER_NAME", "Lemon Squeezy")


def _landing_pricing_plans():
    return [
        {
            "code": OwnerOnboarding.PLAN_STARTER,
            "name": "Starter",
            "badge": "Lean retail",
            "price": "10",
            "description": "A clean starting package for a single counter or owner-operated shop.",
            "features": [
                "1 store workspace",
                "POS, products, purchases, and reports",
                "Up to 3 staff logins",
                "3 free days to set up and run the workspace",
            ],
            "featured": False,
            "cta_label": "Get Started Free",
            "checkout_link": get_lemon_squeezy_plan_url(OwnerOnboarding.PLAN_STARTER),
        },
        {
            "code": OwnerOnboarding.PLAN_GROWTH,
            "name": "Growth",
            "badge": "Most popular",
            "price": "24",
            "description": "For growing stores that need tighter controls, deeper reporting, and more staff capacity.",
            "features": [
                "Everything in Starter",
                "Advanced GST, backups, and audit trail",
                "Up to 10 staff logins",
                "Priority onboarding help",
            ],
            "featured": True,
            "cta_label": "Get Started Free",
            "checkout_link": get_lemon_squeezy_plan_url(OwnerOnboarding.PLAN_GROWTH),
        },
        {
            "code": OwnerOnboarding.PLAN_SCALE,
            "name": "Scale",
            "badge": "Operations ready",
            "price": "59",
            "description": "For larger retail teams that want broader operational control and room for custom rollout needs.",
            "features": [
                "Everything in Growth",
                "Higher-volume team and workflow support",
                "Operational rollout assistance",
                "Best fit for serious branch growth",
            ],
            "featured": False,
            "cta_label": "Get Started Free",
            "checkout_link": get_lemon_squeezy_plan_url(OwnerOnboarding.PLAN_SCALE),
        },
    ]



def _landing_plan_lookup(plan_code):
    plan_code = (plan_code or "").strip().lower()
    for plan in _landing_pricing_plans():
        if plan["code"] == plan_code:
            return plan
    return None



def _selected_plan_code(plan_code):
    plan = _landing_plan_lookup(plan_code)
    return plan["code"] if plan else OwnerOnboarding.PLAN_STARTER



def _google_sign_in_url(*, plan_code="", next_url=""):
    params = {}
    if plan_code:
        params["plan"] = _selected_plan_code(plan_code)
    if next_url:
        params["next"] = next_url
    base_url = reverse("google_auth_start")
    return f"{base_url}?{urlencode(params)}" if params else base_url



def _signup_url_for_plan(plan_code):
    return f"{reverse('signup')}?{urlencode({'plan': _selected_plan_code(plan_code)})}"



def _public_signup_is_open():
    return not get_user_model().objects.exists()



def _public_signup_lock_message():
    if OwnerOnboarding.objects.filter(email_verified=False, business_created_at__isnull=True).exists():
        return "A registration is already in progress for this workspace. Verify that email address and then sign in to continue."
    return "This Vendora workspace already has an owner account. Sign in with the existing owner email or a team login."



def _owner_verification_url(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(reverse("verify_signup_email", args=[uid, token]))



def _send_owner_verification_email(request, onboarding):
    verification_url = _owner_verification_url(request, onboarding.user)
    subject = f"{LANDING_BRAND_NAME} email verification"
    body = (
        f"Welcome to {LANDING_BRAND_NAME}.\n\n"
        f"Verify your email to unlock your 3 free days on the {onboarding.plan_label} package and create the business workspace.\n\n"
        "You can purchase or upgrade plans later after the workspace is live.\n\n"
        f"Verification link:\n{verification_url}\n\n"
        "If you did not request this account, you can ignore this email."
    )
    send_mail(subject, body, LANDING_SUPPORT_EMAIL, [onboarding.user.email], fail_silently=False)
    return verification_url



def _activate_owner_trial_if_needed(user):
    onboarding = get_owner_onboarding(user)
    if onboarding and onboarding.email_verified:
        onboarding.start_trial()
    return onboarding



def _bool_claim(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}



def _google_safe_next_url(request, next_url):
    next_url = (next_url or "").strip()
    if not next_url:
        return ""
    return (
        next_url
        if url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
        else ""
    )



def _google_failure_redirect():
    return "signup" if _public_signup_is_open() else "login"



def _find_user_by_email_or_username(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    user_model = get_user_model()
    return (
        user_model.objects.filter(Q(email__iexact=identifier) | Q(username__iexact=identifier))
        .order_by("id")
        .first()
    )



def _update_user_from_google_claims(user, claims):
    email = (claims.get("email") or "").strip().lower()
    given_name = (claims.get("given_name") or "").strip()
    family_name = (claims.get("family_name") or "").strip()
    update_fields = []

    if email and not user.email:
        user.email = email
        update_fields.append("email")
    if given_name and not user.first_name:
        user.first_name = given_name
        update_fields.append("first_name")
    if family_name and not user.last_name:
        user.last_name = family_name
        update_fields.append("last_name")

    if update_fields:
        user.save(update_fields=update_fields)



def _resolve_google_user(request, claims, plan_code):
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise ValueError("Google did not return an email address for this account.")
    if not _bool_claim(claims.get("email_verified")):
        raise ValueError("Google did not confirm that email address. Use a verified Google account instead.")

    user = _find_user_by_email_or_username(email)
    now = timezone.now()

    if user is None:
        if not _public_signup_is_open():
            raise ValueError("This Google email is not registered for the current Vendora workspace.")

        user_model = get_user_model()
        with transaction.atomic():
            user = user_model(
                username=email,
                email=email,
                first_name=(claims.get("given_name") or "").strip(),
                last_name=(claims.get("family_name") or "").strip(),
                is_active=True,
                is_staff=False,
            )
            user.set_unusable_password()
            user.save()
            onboarding = OwnerOnboarding.objects.create(
                user=user,
                selected_plan=_selected_plan_code(plan_code),
            )
            onboarding.mark_email_verified(now=now)
            onboarding.start_trial(now=now)
            record_audit(
                action="owner_google_signup_created",
                description=f"Owner account '{email}' created through Google sign-in.",
                actor=user,
                target=user,
                metadata={"plan": onboarding.plan_label},
            )
        return user, onboarding, True

    onboarding = getattr(user, "owner_onboarding", None)
    if not user.is_active and onboarding is None:
        raise ValueError("This Google email belongs to a disabled account. Ask an admin to re-enable it first.")

    _update_user_from_google_claims(user, claims)
    if onboarding is not None:
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])
        if not onboarding.email_verified:
            onboarding.mark_email_verified(now=now)
        onboarding.start_trial(now=now)

    return user, onboarding, False



def _sign_in_user_and_redirect(request, user, *, next_url=""):
    login(request, user)
    open_user_session(request, user)
    onboarding = _activate_owner_trial_if_needed(user)
    welcome_name = user.get_full_name() or user.email or user.username
    home_url = default_home_url(user)
    messages.success(request, f"Welcome back, {welcome_name}.")
    if onboarding and onboarding.email_verified and not onboarding.business_created_at:
        messages.info(request, "Create the business workspace to finish onboarding.")
    safe_next_url = _google_safe_next_url(request, next_url)
    if home_url != reverse("setup_admin") and safe_next_url:
        return redirect(safe_next_url)
    return redirect(home_url)



def _landing_context():
    pricing_plans = []
    for plan in _landing_pricing_plans():
        pricing_plans.append(
            {
                **plan,
                "cta_url": reverse("landing_plan_checkout", kwargs={"plan_code": plan["code"]}),
            }
        )
    return {
        "brand_name": LANDING_BRAND_NAME,
        "public_domain": LANDING_PUBLIC_DOMAIN,
        "support_email": LANDING_SUPPORT_EMAIL,
        "checkout_provider": LANDING_CHECKOUT_PROVIDER,
        "pricing_plans": pricing_plans,
    }



def _billing_status_tone(onboarding, subscription):
    if onboarding is not None and onboarding.trial_is_active:
        return "advance"
    if subscription is not None:
        return subscription.status_tone
    if onboarding is not None and onboarding.requires_payment_setup:
        return "payable"
    return "clear"



def _billing_page_context(request, onboarding, *, can_manage=False):
    subscription = onboarding.current_subscription if onboarding is not None else None
    current_plan_code = (
        subscription.plan_code
        if subscription is not None
        else (onboarding.selected_plan if onboarding is not None else OwnerOnboarding.PLAN_STARTER)
    )
    current_plan = _landing_plan_lookup(current_plan_code) or _landing_plan_lookup(OwnerOnboarding.PLAN_STARTER)
    portal_url = (
        subscription.customer_portal_url
        if subscription is not None and subscription.customer_portal_url
        else getattr(settings, "LEMON_SQUEEZY_CUSTOMER_PORTAL_URL", "")
    )
    current_price = safe_decimal(current_plan["price"] if current_plan else "0")
    plan_cards = []
    for plan in _landing_pricing_plans():
        plan_price = safe_decimal(plan["price"])
        is_current = plan["code"] == current_plan_code
        if is_current:
            cta_label = "Current Plan" if onboarding and onboarding.billing_is_active else "Activate This Plan"
        elif plan_price > current_price:
            cta_label = f"Upgrade to {plan['name']}"
        elif plan_price < current_price:
            cta_label = f"Switch to {plan['name']}"
        else:
            cta_label = f"Choose {plan['name']}"
        plan_cards.append(
            {
                **plan,
                "cta_url": reverse("billing_checkout", args=[plan["code"]]),
                "cta_label": cta_label,
                "is_current": is_current,
                "checkout_configured": bool(plan.get("checkout_link")),
            }
        )

    return {
        "billing_onboarding": onboarding,
        "billing_subscription": subscription,
        "billing_current_plan": current_plan,
        "billing_status_label": onboarding.billing_status_label if onboarding is not None else "Setup not started",
        "billing_status_tone": _billing_status_tone(onboarding, subscription),
        "billing_trial_days_remaining": onboarding.trial_days_remaining if onboarding is not None else 0,
        "billing_portal_url": (portal_url or "").strip(),
        "billing_can_manage": can_manage,
        "billing_plan_cards": plan_cards,
        "billing_checkout_enabled": any(card["checkout_configured"] for card in plan_cards),
        "billing_requires_action": bool(onboarding and onboarding.requires_payment_setup),
        "billing_provider_name": LANDING_CHECKOUT_PROVIDER,
    }

def home(request):
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    return render(
        request,
        "pos/landing.html",
        {
            **_landing_context(),
            "trial_cta_url": reverse("start_trial"),
            "login_url": reverse("login"),
            "signup_url": "#pricing",
        },
    )



def landing_plan_checkout(request, plan_code):
    plan = _landing_plan_lookup(plan_code)
    if plan is None:
        raise Http404("Unknown plan.")

    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    if _public_signup_is_open():
        messages.info(
            request,
            f"{plan['name']} selected. Create your owner account and verify your email to unlock 3 free days.",
        )
    return redirect(_signup_url_for_plan(plan["code"]))



def start_trial(request):
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    return redirect(_signup_url_for_plan(OwnerOnboarding.PLAN_STARTER))



def signup_view(request):
    ensure_role_groups()
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    selected_plan = _selected_plan_code(request.POST.get("selected_plan") or request.GET.get("plan"))
    selected_plan_data = _landing_plan_lookup(selected_plan)
    form = OwnerSignupForm(request.POST or None, initial={"selected_plan": selected_plan})
    form.fields["selected_plan"].initial = selected_plan
    signup_locked = not _public_signup_is_open()
    signup_lock_message = _public_signup_lock_message() if signup_locked else ""

    if request.method == "POST":
        if signup_locked:
            messages.info(request, signup_lock_message)
        elif form.is_valid():
            user = None
            try:
                with transaction.atomic():
                    user = form.save()
                    onboarding = OwnerOnboarding.objects.create(
                        user=user,
                        selected_plan=form.cleaned_data["selected_plan"],
                    )
                    onboarding.mark_verification_sent()
                verification_url = _send_owner_verification_email(request, onboarding)
            except Exception:
                if user is not None and user.pk:
                    user.delete()
                messages.error(request, "We could not send the verification email. Check the mail settings and try again.")
            else:
                return render(
                    request,
                    "pos/signup_email_sent.html",
                    {
                        "brand_name": LANDING_BRAND_NAME,
                        "email": onboarding.user.email,
                        "plan": _landing_plan_lookup(onboarding.selected_plan),
                        "login_url": reverse("login"),
                        "resend_email": onboarding.user.email,
                        "debug_verification_url": verification_url if settings.DEBUG else "",
                    },
                )
        else:
            messages.error(request, "Please review the owner account details below.")

    return render(
        request,
        "pos/signup.html",
        {
            "brand_name": LANDING_BRAND_NAME,
            "form": form,
            "plan": selected_plan_data,
            "login_url": reverse("login"),
            "google_sign_in_url": _google_sign_in_url(plan_code=selected_plan),
            "google_sign_in_enabled": google_sign_in_enabled(),
            "google_sign_in_setup_message": google_sign_in_setup_message(),
            "signup_locked": signup_locked,
            "signup_lock_message": signup_lock_message,
        },
    )


@require_POST
def resend_verification_email(request):
    email = (request.POST.get("email") or "").strip().lower()
    if not email:
        messages.error(request, "Enter the owner email address first.")
        return redirect("login")

    user = get_user_model().objects.filter(email__iexact=email).first()
    onboarding = getattr(user, "owner_onboarding", None) if user is not None else None
    if user is None or onboarding is None or onboarding.email_verified:
        messages.error(request, "No pending email verification was found for that address.")
        return redirect("login")

    onboarding.mark_verification_sent()
    verification_url = _send_owner_verification_email(request, onboarding)
    messages.success(request, f"A fresh verification email was sent to {user.email}.")
    return render(
        request,
        "pos/signup_email_sent.html",
        {
            "brand_name": LANDING_BRAND_NAME,
            "email": onboarding.user.email,
            "plan": _landing_plan_lookup(onboarding.selected_plan),
            "login_url": reverse("login"),
            "resend_email": onboarding.user.email,
            "debug_verification_url": verification_url if settings.DEBUG else "",
        },
    )



def verify_signup_email(request, uidb64, token):
    user_model = get_user_model()
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = user_model.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, user_model.DoesNotExist):
        user = None

    onboarding = getattr(user, "owner_onboarding", None) if user is not None else None
    if user is None or onboarding is None:
        messages.error(request, "That verification link is invalid.")
        return redirect("login")

    if onboarding.email_verified:
        messages.info(request, "Your email is already verified. Sign in to continue.")
        if request.user.is_authenticated and request.user.pk == user.pk:
            return redirect(default_home_url(user))
        return redirect("login")

    if not default_token_generator.check_token(user, token):
        messages.error(request, "That verification link is no longer valid. Use the resend option to get a fresh email.")
        return redirect("login")

    with transaction.atomic():
        user.is_active = True
        user.save(update_fields=["is_active"])
        onboarding.mark_email_verified()
        onboarding.start_trial()

    login(request, user)
    open_user_session(request, user)
    messages.success(request, "Email verified. Finish creating your business workspace.")
    return redirect("setup_admin")



def google_auth_start(request):
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    _remember_browser_time_zone(request, request.GET.get("browser_time_zone"))
    if not google_sign_in_enabled():
        messages.info(
            request,
            google_sign_in_setup_message(),
        )
        return redirect(_google_failure_redirect())

    plan_code = _selected_plan_code(request.GET.get("plan")) if _public_signup_is_open() else ""
    next_url = _google_safe_next_url(request, request.GET.get("next"))
    return redirect(build_google_authorization_url(request, plan_code=plan_code, next_url=next_url))



def google_auth_callback(request):
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    if not google_sign_in_enabled():
        messages.info(
            request,
            google_sign_in_setup_message(),
        )
        return redirect(_google_failure_redirect())

    if request.GET.get("error"):
        clear_google_oauth_session(request)
        messages.error(request, "Google sign-in was cancelled before completion.")
        return redirect(_google_failure_redirect())

    oauth_session = get_google_oauth_session(request)
    state = (request.GET.get("state") or "").strip()
    code = (request.GET.get("code") or "").strip()
    if not oauth_session.get("state") or state != oauth_session.get("state"):
        clear_google_oauth_session(request)
        messages.error(request, "Google sign-in expired or could not be verified. Please try again.")
        return redirect(_google_failure_redirect())
    if not code:
        clear_google_oauth_session(request)
        messages.error(request, "Google sign-in did not return an authorization code.")
        return redirect(_google_failure_redirect())

    try:
        token_payload = exchange_google_authorization_code(request, code)
        claims = verify_google_identity_token(
            token_payload.get("id_token"),
            expected_nonce=oauth_session.get("nonce", ""),
        )
        user, onboarding, created_owner = _resolve_google_user(
            request,
            claims,
            oauth_session.get("plan_code", ""),
        )
    except ValueError as exc:
        clear_google_oauth_session(request)
        messages.error(request, str(exc))
        return redirect(_google_failure_redirect())

    clear_google_oauth_session(request)
    if created_owner:
        messages.success(request, "Google account verified. Finish creating the business workspace to continue.")
    elif onboarding and onboarding.email_verified and not onboarding.business_created_at:
        messages.success(request, "Google account verified. Continue with business setup.")
    return _sign_in_user_and_redirect(request, user, next_url=oauth_session.get("next_url", ""))



def setup_admin(request):
    ensure_role_groups()
    if not request.user.is_authenticated:
        messages.info(request, "Sign in with your registered owner account to create the business workspace.")
        return redirect("login")

    onboarding = get_owner_onboarding(request.user)
    if onboarding is None:
        if _public_signup_is_open():
            return redirect("signup")
        messages.error(request, "Only the registered owner account can create the business workspace.")
        return redirect(default_home_url(request.user))

    if not onboarding.email_verified:
        messages.warning(request, "Verify your email before creating the business workspace.")
        return redirect("login")

    if onboarding.business_created_at or CompanyProfile.objects.filter(is_active=True).exists():
        return redirect(default_home_url(request.user))

    company_profile = CompanyProfile(is_active=True, email=request.user.email or "")
    form = OwnerBusinessSetupForm(request.POST or None, instance=company_profile)
    if request.method == "POST":
        _remember_browser_time_zone(request, request.POST.get("browser_time_zone"))
        if form.is_valid():
            with transaction.atomic():
                company = form.save(commit=False)
                company.is_active = True
                company.save()
                assign_role(request.user, ADMIN_ROLE)
                _activate_owner_trial_if_needed(request.user)
                onboarding.mark_business_created()
                record_audit(
                    action="business_workspace_created",
                    description=f"Business workspace '{company.name}' created during owner onboarding.",
                    actor=request.user,
                    target=company,
                    metadata={"plan": onboarding.plan_label},
                )
            messages.success(request, "Business workspace created. Your owner account now has full control.")
            return redirect(default_home_url(request.user))
        messages.error(request, "Please review the business details below.")

    return render(
        request,
        "pos/setup_admin.html",
        {
            "form": form,
            "onboarding": onboarding,
            "brand_name": LANDING_BRAND_NAME,
            "google_sign_in_enabled": getattr(settings, "GOOGLE_SIGN_IN_ENABLED", False),
        },
    )



def billing_page(request):
    ensure_role_groups()
    if is_first_user_setup_required():
        return redirect("signup")
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?{urlencode({'next': request.get_full_path()})}")
    if user_requires_business_setup(request.user):
        return redirect("setup_admin")

    onboarding = workspace_owner_onboarding(request.user)
    can_manage_billing = user_can_manage_billing(request.user, onboarding)
    billing_context = _billing_page_context(request, onboarding, can_manage=can_manage_billing)

    if workspace_requires_billing(request.user):
        if can_manage_billing:
            messages.warning(request, "The 3 free days have ended. Choose a paid package to reopen the workspace.")
        else:
            messages.warning(request, "This workspace is locked until the owner updates billing.")

    if onboarding is None:
        messages.info(request, "Billing becomes available after owner onboarding and business setup are complete.")

    return render(
        request,
        "pos/billing.html",
        {
            "page_title": "Billing",
            "page_subtitle": "Manage the workspace package, the 3 free days, and the subscription lifecycle.",
            "page_icon": "bi-credit-card-2-front",
            "page_icon_accent": "warning",
            "brand_name": LANDING_BRAND_NAME,
            **billing_context,
        },
    )



def billing_checkout(request, plan_code):
    ensure_role_groups()
    if is_first_user_setup_required():
        return redirect("signup")
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?{urlencode({'next': reverse('billing_page')})}")
    if user_requires_business_setup(request.user):
        return redirect("setup_admin")

    onboarding = workspace_owner_onboarding(request.user)
    if onboarding is None:
        messages.error(request, "Billing is not available until the owner workspace is created.")
        return redirect(default_home_url(request.user))
    if not user_can_manage_billing(request.user, onboarding):
        messages.error(request, "Only the owner or an admin can manage workspace billing.")
        return redirect("billing_page")

    plan = _landing_plan_lookup(plan_code)
    if plan is None:
        raise Http404("Unknown billing package.")

    try:
        subscription, checkout_url = begin_lemon_squeezy_checkout(onboarding, plan["code"])
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("billing_page")

    record_audit(
        action="billing_checkout_started",
        description=f"{plan['name']} checkout opened from the billing centre.",
        actor=request.user,
        target=subscription,
        metadata={"plan": plan["name"], "provider": LANDING_CHECKOUT_PROVIDER},
    )
    messages.info(request, f"{plan['name']} checkout opened in {LANDING_CHECKOUT_PROVIDER}. Complete payment there to activate the workspace.")
    return redirect(checkout_url)



def billing_portal_redirect(request):
    ensure_role_groups()
    if is_first_user_setup_required():
        return redirect("signup")
    if not request.user.is_authenticated:
        return redirect(f"{reverse('login')}?{urlencode({'next': reverse('billing_page')})}")

    onboarding = workspace_owner_onboarding(request.user)
    if onboarding is None:
        messages.error(request, "Billing portal is not available yet for this workspace.")
        return redirect(default_home_url(request.user))
    if not user_can_manage_billing(request.user, onboarding):
        messages.error(request, "Only the owner or an admin can open the billing portal.")
        return redirect("billing_page")

    subscription = onboarding.current_subscription
    portal_url = (
        subscription.customer_portal_url
        if subscription is not None and subscription.customer_portal_url
        else getattr(settings, "LEMON_SQUEEZY_CUSTOMER_PORTAL_URL", "")
    )
    portal_url = (portal_url or "").strip()
    if not portal_url:
        messages.info(request, "Customer billing portal URL is not configured yet.")
        return redirect("billing_page")
    return redirect(portal_url)



@csrf_exempt
def lemonsqueezy_webhook(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "method_not_allowed"}, status=405)

    raw_body = request.body or b""
    signature = (request.headers.get(LEMON_SQUEEZY_SIGNATURE_HEADER) or request.META.get("HTTP_X_SIGNATURE") or "").strip()
    if not getattr(settings, "LEMON_SQUEEZY_WEBHOOK_SECRET", "").strip():
        return JsonResponse({"ok": False, "error": "webhook_not_configured"}, status=503)
    if not verify_lemon_squeezy_signature(raw_body, signature):
        return JsonResponse({"ok": False, "error": "invalid_signature"}, status=400)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    webhook_event, created, subscription = process_lemon_squeezy_webhook(payload, raw_body=raw_body, signature=signature)
    if created and subscription is not None:
        record_audit(
            action="billing_subscription_synced",
            description=f"Billing updated through {LANDING_CHECKOUT_PROVIDER} webhook ({subscription.status_label}).",
            target=subscription,
            metadata={
                "plan": subscription.plan_label,
                "status": subscription.status,
                "provider_event": webhook_event.event_name,
            },
        )

    return JsonResponse({
        "ok": True,
        "duplicate": not created,
        "event": webhook_event.event_name,
        "subscription": subscription.provider_subscription_id if subscription is not None else "",
    })

def login_view(request):
    ensure_role_groups()
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    next_url = request.GET.get("next") or request.POST.get("next") or ""
    form = LoginForm(request, data=request.POST or None)

    if request.method == "POST":
        _remember_browser_time_zone(request, request.POST.get("browser_time_zone"))
        if form.is_valid():
            user = form.get_user()
            return _sign_in_user_and_redirect(request, user, next_url=next_url)
        messages.error(request, "Check the email or login ID and password and try again.")

    return render(
        request,
        "pos/login.html",
        {
            "form": form,
            "next_url": next_url,
            "signup_url": reverse("signup") if _public_signup_is_open() else "",
            "show_public_signup": _public_signup_is_open(),
            "google_sign_in_url": _google_sign_in_url(next_url=next_url),
            "google_sign_in_enabled": google_sign_in_enabled(),
            "google_sign_in_setup_message": google_sign_in_setup_message(),
        },
    )


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        close_user_session(request)
        logout(request)
    return redirect("login")


@require_POST
def set_client_time_zone(request):
    time_zone_name = _remember_browser_time_zone(
        request,
        request.POST.get("time_zone") or request.POST.get("browser_time_zone"),
    )
    if not time_zone_name:
        return JsonResponse({"ok": False, "error": "invalid_time_zone"}, status=400)
    return JsonResponse({"ok": True, "time_zone": time_zone_name})


@admin_required
def settings_page(request):
    ensure_role_groups()
    user_model = get_user_model()
    settings_obj = get_system_settings()
    company_profile = get_active_company_profile(create=True)
    try:
        time_zone_preference = request.user.preference
    except UserPreference.DoesNotExist:
        time_zone_preference = UserPreference(user=request.user)
    detected_time_zone = get_session_time_zone(request)
    effective_time_zone = getattr(
        request,
        "effective_time_zone",
        get_effective_time_zone(request.user, session_time_zone=detected_time_zone),
    )

    team_form = TeamUserForm(prefix="team")
    role_form = CustomRoleForm(prefix="role")
    branding_form = CompanyProfileForm(prefix="branding", instance=company_profile)
    system_form = SystemSettingsForm(prefix="business", instance=settings_obj)
    time_zone_form = AdminTimeZoneForm(
        prefix="timezone",
        instance=time_zone_preference,
        detected_time_zone=detected_time_zone,
        effective_time_zone=effective_time_zone,
    )
    bank_form = BankAccountForm(prefix="bank")
    backup_runtime = backup_runtime_summary()
    audit_page_size = 10

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "create_user":
            team_form = TeamUserForm(request.POST, prefix="team")
            if team_form.is_valid():
                member = team_form.save()
                role_value = team_form.cleaned_data["role"]
                role_label = get_role_label(role_value)
                assign_role(member, role_value)
                record_audit(
                    action="team_user_created",
                    description=f"Team member '{member.get_full_name() or member.username}' created from Settings.",
                    actor=request.user,
                    target=member,
                    metadata={"role": role_label},
                )
                messages.success(request, f"Team member '{member.get_full_name() or member.username}' created.")
                return redirect("settings_page")
            messages.error(request, "Please review the team member details below.")

        elif action == "create_role":
            role_form = CustomRoleForm(request.POST, prefix="role")
            if role_form.is_valid():
                role = role_form.save()
                record_audit(
                    action="custom_role_created",
                    description=f"Custom role '{role.name}' created from Settings.",
                    actor=request.user,
                    target=role,
                    metadata={"permissions": role.permissions},
                )
                messages.success(request, f"Custom role '{role.name}' is ready to assign.")
                return redirect("settings_page")
            messages.error(request, "Please review the custom role details below.")

        elif action == "save_time_zone":
            _remember_browser_time_zone(request, request.POST.get("browser_time_zone"))
            time_zone_form = AdminTimeZoneForm(
                request.POST,
                prefix="timezone",
                instance=time_zone_preference,
                detected_time_zone=get_session_time_zone(request),
                effective_time_zone=effective_time_zone,
            )
            if time_zone_form.is_valid():
                preference = time_zone_form.save(commit=False)
                preference.user = request.user
                preference.save()
                saved_time_zone = time_zone_form.cleaned_data["time_zone"]
                effective_label = saved_time_zone or get_session_time_zone(request) or settings.TIME_ZONE
                record_audit(
                    action="admin_time_zone_updated",
                    description=(
                        f"Time zone preference updated for {request.user.get_full_name() or request.user.username}."
                    ),
                    actor=request.user,
                    target=request.user,
                    metadata={
                        "time_zone": saved_time_zone or "system_detected",
                        "effective_time_zone": effective_label,
                    },
                )
                if saved_time_zone:
                    messages.success(request, f"Time zone saved as {saved_time_zone}.")
                else:
                    messages.success(request, f"Time zone will follow the detected system zone ({effective_label}).")
                return redirect("settings_page")
            messages.error(request, "Please review the time zone setting below.")

        elif action == "change_user_role":
            member = get_object_or_404(user_model, pk=request.POST.get("user_id"))
            selected_role = (request.POST.get("role") or "").strip()
            available_roles = {value for value, _ in get_role_choices()}

            if member.id == request.user.id:
                messages.error(request, "You cannot change the role of the account you are currently using.")
            elif selected_role not in available_roles:
                messages.error(request, "The selected role is no longer available.")
            else:
                assign_role(member, selected_role)
                role_label = get_role_label(selected_role)
                UserSessionLog.objects.filter(user=member, logout_at__isnull=True).update(role_name=role_label)
                record_audit(
                    action="team_user_role_changed",
                    description=f"Role changed for {member.get_full_name() or member.username} to {role_label}.",
                    actor=request.user,
                    target=member,
                    metadata={"role": role_label},
                )
                messages.success(request, f"Role updated for {member.get_full_name() or member.username}.")
                return redirect("settings_page")

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
                bank_form = BankAccountForm(prefix="bank")
            else:
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

        elif action == "create_backup":
            label = (request.POST.get("backup_label") or "").strip()
            notes = (request.POST.get("backup_notes") or "").strip()
            try:
                snapshot = create_backup_snapshot(label=label, notes=notes, actor=request.user)
                messages.success(request, f"Backup '{snapshot.label}' created.")
            except ValueError as exc:
                messages.error(request, str(exc))

        elif action == "upload_backup":
            label = (request.POST.get("upload_backup_label") or "").strip()
            notes = (request.POST.get("upload_backup_notes") or "").strip()
            uploaded_file = request.FILES.get("backup_upload_file")
            try:
                snapshot = upload_backup_snapshot(uploaded_file=uploaded_file, label=label, notes=notes, actor=request.user)
                messages.success(request, f"Backup '{snapshot.label}' uploaded.")
            except ValueError as exc:
                messages.error(request, str(exc))

        elif action == "restore_backup":
            snapshot = get_object_or_404(BackupSnapshot, pk=request.POST.get("snapshot_id"))
            try:
                restore_backup_snapshot(snapshot=snapshot, actor=request.user)
                messages.success(request, f"Backup '{snapshot.label}' restored. Refresh any open pages to load restored data.")
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
                status_copy = "enabled" if member.is_active else "disabled"
                record_audit(
                    action="team_user_toggled",
                    description=f"{member.get_full_name() or member.username} was {status_copy} from Settings.",
                    actor=request.user,
                    target=member,
                    metadata={"is_active": member.is_active},
                )
                messages.success(request, f"{member.get_full_name() or member.username} {status_copy}.")

    detected_time_zone = get_session_time_zone(request)
    saved_time_zone_preference = get_user_time_zone_preference(request.user)
    effective_time_zone = saved_time_zone_preference or detected_time_zone or getattr(
        request,
        "effective_time_zone",
        settings.TIME_ZONE,
    )
    raw_users = (
        user_model.objects.order_by("username")
        .annotate(
            sales_count=Count("sales_made", distinct=True),
            sales_value=Coalesce(Sum("sales_made__total_amount"), Decimal("0")),
        )
    )
    team_users = []
    for member in raw_users:
        role_value = get_user_role_value(member)
        role_label = get_user_role_label(member)
        team_users.append(
            {
                "user": member,
                "role_label": role_label,
                "role_value": role_value,
                "role_variant": "cashier" if role_value == "cashier" else ("custom" if role_value.startswith("custom:") else "admin"),
                "access_summary": get_user_role_summary(member),
                "sales_count": member.sales_count,
                "sales_value": member.sales_value,
            }
        )

    custom_roles = []
    for role in CustomRole.objects.order_by("name"):
        permission_items = []
        for module in MODULE_DEFINITIONS:
            level = (role.permissions or {}).get(module["key"], "none")
            if level == "none":
                continue
            permission_items.append(
                {
                    "label": module["label"],
                    "level": "Manage" if level == ACCESS_MANAGE else "View",
                    "tone": "healthy" if level == ACCESS_MANAGE else "clear",
                }
            )
        custom_roles.append(
            {
                "role": role,
                "permissions_summary": describe_permissions(role.permissions),
                "permission_items": permission_items,
                "member_count": user_model.objects.filter(groups__name=role.group_name).distinct().count(),
            }
        )

    session_logs = UserSessionLog.objects.select_related("user").order_by("-login_at")[:20]
    categories = Category.objects.order_by("name")
    bank_accounts = BankAccount.objects.select_related("account").order_by("name", "account_number")
    backup_snapshots = list(BackupSnapshot.objects.select_related("created_by").order_by("-created_at")[:10])
    for snapshot in backup_snapshots:
        snapshot.vendor = infer_backup_vendor(snapshot.file_name)
        snapshot.vendor_label = database_vendor_label(snapshot.vendor)
        snapshot.can_restore_here = snapshot.vendor == backup_runtime["vendor"]

    workspace_billing_onboarding = workspace_owner_onboarding(request.user)
    billing_context = _billing_page_context(
        request,
        workspace_billing_onboarding,
        can_manage=user_can_manage_billing(request.user, workspace_billing_onboarding),
    )

    audit_log_queryset = AuditLog.objects.select_related("actor").order_by("-created_at", "-id")
    audit_event_count = audit_log_queryset.count()
    audit_mode = "all" if (request.GET.get("audit_mode") or "").lower() == "all" else "page"
    audit_page_obj = None
    if audit_mode == "all":
        audit_logs = list(audit_log_queryset)
    else:
        audit_page_obj = Paginator(audit_log_queryset, audit_page_size).get_page(request.GET.get("audit_page") or 1)
        audit_logs = list(audit_page_obj.object_list)

    context = {
        "page_title": "Settings",
        "page_subtitle": "Control branding, GST, POS rules, bank ledgers, backups, and staff access from one place.",
        "page_icon": "bi-sliders",
        "page_icon_accent": "slate",
        "team_form": team_form,
        "role_form": role_form,
        "branding_form": branding_form,
        "system_form": system_form,
        "time_zone_form": time_zone_form,
        "bank_form": bank_form,
        "role_choices": get_role_choices(),
        "team_users": team_users,
        "custom_roles": custom_roles,
        "session_logs": session_logs,
        "categories": categories,
        "bank_accounts": bank_accounts,
        "backup_runtime": backup_runtime,
        "backup_snapshots": backup_snapshots,
        "audit_logs": audit_logs,
        "audit_mode": audit_mode,
        "audit_page_obj": audit_page_obj,
        "audit_page_size": audit_page_size,
        "settings_obj": settings_obj,
        "total_team_users": len(team_users),
        "cashier_count": sum(1 for row in team_users if row["role_value"] == "cashier"),
        "custom_user_count": sum(1 for row in team_users if row["role_value"].startswith("custom:")),
        "custom_role_count": len(custom_roles),
        "active_sessions_count": UserSessionLog.objects.filter(logout_at__isnull=True).count(),
        "taxable_category_count": categories.filter(tax_status=Category.TAXABLE).count(),
        "gst_enabled_category_count": categories.exclude(tax_status=Category.EXEMPT).count(),
        "bank_ledger_count": bank_accounts.count(),
        "active_bank_count": bank_accounts.filter(is_active=True).count(),
        "backup_count": BackupSnapshot.objects.count(),
        "audit_event_count": audit_event_count,
        "effective_time_zone": effective_time_zone,
        "saved_time_zone_preference": saved_time_zone_preference,
        "session_detected_time_zone": detected_time_zone,
        "uses_system_detected_time_zone": not bool(saved_time_zone_preference),
        **billing_context,
    }
    return render(request, "pos/settings.html", context)
@admin_required
def download_backup_snapshot(request, snapshot_id):
    snapshot = get_object_or_404(BackupSnapshot, pk=snapshot_id)
    try:
        backup_path = get_backup_file_path(snapshot)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("settings_page")

    record_audit(
        action="backup_downloaded",
        description=f"Backup snapshot '{snapshot.label}' was downloaded.",
        actor=request.user,
        target=snapshot,
        metadata={"file_name": snapshot.file_name},
    )
    return FileResponse(backup_path.open("rb"), as_attachment=True, filename=snapshot.file_name)

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


@module_access_required("products")
def product_list(request):
    query = request.GET.get("q", "").strip()
    selected_brand = _clean_choice_id(request.GET.get("brand"))
    selected_category = _clean_choice_id(request.GET.get("category"))
    selected_supplier = _clean_choice_id(request.GET.get("supplier"))
    selected_status = request.GET.get("status", "").strip()
    selected_sort = request.GET.get("sort", "name_asc").strip()
    settings_obj = get_system_settings()
    today = timezone.localdate()
    expiry_alert_end = today + timedelta(days=int(settings_obj.expiry_alert_days or 30))

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

    if settings_obj.enable_product_expiry:
        for product in products.object_list:
            expiry_date = product.expiry_date
            if not expiry_date:
                product.expiry_status_tone = "unknown"
                product.expiry_status_label = "Not set"
            elif expiry_date < today:
                product.expiry_status_tone = "below"
                product.expiry_status_label = f"Expired {(today - expiry_date).days}d ago"
            elif expiry_date <= expiry_alert_end:
                product.expiry_status_tone = "advance"
                product.expiry_status_label = f"{(expiry_date - today).days}d left"
            else:
                product.expiry_status_tone = "healthy"
                product.expiry_status_label = "OK"

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
        "expiry_tracking_enabled": settings_obj.enable_product_expiry,
    }
    return render(request, "pos/product_list.html", context)
@module_access_required("products", minimum=ACCESS_MANAGE)
def product_add(request):
    form = ProductForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Product '{product.name}' added successfully.")
            return redirect("product_view", pk=product.id)
        messages.error(request, "Please fix the highlighted product fields.")

    return render(request, "pos/product_form.html", {"form": form, "title": "Add Product"})

@module_access_required("products")
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

@module_access_required("products", minimum=ACCESS_MANAGE)
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


@module_access_required("products", minimum=ACCESS_MANAGE)
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

@module_access_required("dashboard")
def dashboard(request):
    today = timezone.localdate()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)
    settings_obj = get_system_settings()

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
    near_expiry = near_expiry_products(settings_obj, today=today)
    expired = expired_products(settings_obj, today=today)
    near_expiry_count = near_expiry.count()
    expired_count = expired.count()
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
    expense_alert_count = len(monthly_bill_alerts(today=today))
    customer_badges = [get_customer_badge(customer) for customer in Customer.objects.filter(is_active=True).only("id", "name", "is_active")]
    supplier_badges = [get_supplier_badge(supplier) for supplier in Supplier.objects.filter(is_active=True).only("id", "name", "is_active")]
    customer_receivable_total = quantize_money(sum((badge["amount"] for badge in customer_badges if badge["balance_type"] == "receivable"), Decimal("0")))
    customer_receivable_count = sum(1 for badge in customer_badges if badge["balance_type"] == "receivable")
    supplier_payable_total = quantize_money(sum((badge["amount"] for badge in supplier_badges if badge["balance_type"] == "payable"), Decimal("0")))
    supplier_payable_count = sum(1 for badge in supplier_badges if badge["balance_type"] == "payable")
    today_report_url = _date_range_url("daily_sales_report", today, today)
    week_report_url = _date_range_url("daily_sales_report", week_start, today)
    month_report_url = _date_range_url("daily_sales_report", month_start, today)
    expiry_report_url = reverse("expiry_alert_report")
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
            "accent": "slate",
            "icon": "bi-truck",
        },
    ]

    if settings_obj.enable_product_expiry:
        dashboard_kpis.insert(
            5,
            {
                "title": "Expiry Alerts",
                "value": near_expiry_count,
                "meta": f"{expired_count} already expired",
                "url": expiry_report_url,
                "kind": "number",
                "accent": "warning",
                "icon": "bi-hourglass-split",
            },
        )

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
            "expense_alert_count": expense_alert_count,
            "today_collection_rate": today_collection_rate,
            "month_margin_percent": month_margin_percent,
            "dashboard_kpis": dashboard_kpis,
            "reports_center_url": reverse("reports_home"),
            "expiry_report_url": expiry_report_url,
            "near_expiry_count": near_expiry_count,
            "expired_count": expired_count,
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


@module_access_required("reports")
def reports_home(request):
    today = timezone.localdate()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)
    settings_obj = get_system_settings()

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
    near_expiry = near_expiry_products(settings_obj, today=today)
    expired = expired_products(settings_obj, today=today)
    near_expiry_count = near_expiry.count()
    expired_count = expired.count()
    near_expiry_preview = list(near_expiry[:8])
    expired_preview = list(expired[:5])
    for product in near_expiry_preview:
        product.days_to_expiry = (product.expiry_date - today).days
    for product in expired_preview:
        product.days_overdue = (today - product.expiry_date).days

    month_purchase_total = Purchase.objects.filter(created_at__date__gte=month_start, created_at__date__lte=today).aggregate(
        total=Coalesce(Sum("total_amount"), Decimal("0"))
    )["total"]
    expense_due_total = _sum_expense_due(Expense.objects.all().only("amount", "paid_amount"))
    expense_alert_count = len(monthly_bill_alerts(today=today))
    month_best_selling = (
        SaleItem.objects.filter(sale__created_at__date__gte=month_start, sale__created_at__date__lte=today)
        .values("product__id", "product__name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")[:8]
    )
    employee_rows = [
        _employee_report_row(user, now=timezone.now())
        for user in get_user_model().objects.filter(is_active=True).order_by("first_name", "username")
    ]
    employee_rows.sort(key=lambda row: (row["sales_totals"]["net_sales"], row["sales_count"]), reverse=True)

    today_report_url = _date_range_url("daily_sales_report", today, today)
    week_report_url = _date_range_url("daily_sales_report", week_start, today)
    month_report_url = _date_range_url("daily_sales_report", month_start, today)
    expiry_report_url = reverse("expiry_alert_report")
    employee_report_url = reverse("employee_report_list")
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
        {
            "title": "Expiry Alerts",
            "value": near_expiry_count,
            "meta": "Expiry tracking is off" if not settings_obj.enable_product_expiry else f"{expired_count} expired items",
            "url": expiry_report_url,
            "kind": "number",
                "accent": "warning",
            "icon": "bi-hourglass-split",
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
            "eyebrow": "Expiry watch",
            "title": "Near Expiry Report",
            "description": "Catch stock that is about to expire so you can clear or rotate it before it becomes a loss.",
            "metric_label": "Items flagged",
            "metric_value": near_expiry_count + expired_count,
            "metric_kind": "number",
            "url": expiry_report_url,
            "cta": "Open expiry report",
            "icon": "bi-hourglass-split",
        },
        {
            "eyebrow": "Operating costs",
            "title": "Expense Register",
            "description": "Track rent, utilities, wages, and any other bills with paid and unpaid portions.",
            "metric_label": "Bills due",
            "metric_value": expense_due_total,
            "metric_kind": "money",
            "url": reverse("expense_list"),
            "cta": "Open expenses",
            "icon": "bi-receipt-cutoff",
        },
        {
            "eyebrow": "Team performance",
            "title": "Employee Report",
            "description": "See session logs, total time, wages, profit, and sales generated by each login.",
            "metric_label": "Active users",
            "metric_value": len(employee_rows),
            "metric_kind": "number",
            "url": employee_report_url,
            "cta": "Open employee reports",
            "icon": "bi-people-fill",
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
            "today_report_url": today_report_url,
            "week_report_url": week_report_url,
            "month_report_url": month_report_url,
            "expiry_report_url": expiry_report_url,
            "employee_report_url": employee_report_url,
            "top_low_stock": low_stock_products[:10],
            "near_expiry_preview": near_expiry_preview,
            "expired_preview": expired_preview,
            "best_selling_month": month_best_selling,
            "employee_rows": employee_rows[:8],
            "month_purchase_total": month_purchase_total,
            "expense_due_total": expense_due_total,
        },
    )
@module_access_required("reports")
def expiry_alert_report(request):
    settings_obj = get_system_settings()
    today = timezone.localdate()
    near_expiry_list = list(near_expiry_products(settings_obj, today=today))
    expired_list = list(expired_products(settings_obj, today=today))

    for product in near_expiry_list:
        product.days_to_expiry = (product.expiry_date - today).days
    for product in expired_list:
        product.days_overdue = (today - product.expiry_date).days

    return render(
        request,
        "pos/expiry_alert_report.html",
        {
            "today": today,
            "near_expiry_products": near_expiry_list,
            "expired_products": expired_list,
            "expiry_tracking_enabled": settings_obj.enable_product_expiry,
            "expiry_alert_days": settings_obj.expiry_alert_days,
        },
    )


@module_access_required("reports")
def employee_report_list(request):
    employee_rows = [
        _employee_report_row(user, now=timezone.now())
        for user in get_user_model().objects.filter(is_active=True).order_by("first_name", "username")
    ]
    employee_rows.sort(key=lambda row: (row["sales_totals"]["net_sales"], row["sales_count"]), reverse=True)
    return render(request, "pos/employee_report_list.html", {"employee_rows": employee_rows})


@module_access_required("reports")
def employee_report_detail(request, user_id):
    employee = get_object_or_404(get_user_model(), pk=user_id)
    now = timezone.now()
    metrics = _employee_report_row(employee, now=now)
    session_logs = list(UserSessionLog.objects.filter(user=employee).order_by("-login_at"))
    for log in session_logs:
        log.duration_display = _format_duration(_session_duration(log, now=now))

    wage_summary = _user_wage_summary(employee)
    recent_sales = list(
        Sale.objects.filter(sold_by=employee)
        .select_related("customer")
        .prefetch_related("items", "items__product")
        .order_by("-created_at")[:20]
    )
    recent_sales = [_sale_profit_fields(sale) for sale in recent_sales]

    return render(
        request,
        "pos/employee_report_detail.html",
        {
            "employee": employee,
            "metrics": metrics,
            "session_logs": session_logs,
            "wage_expenses": wage_summary["expenses"][:20],
            "recent_sales": recent_sales,
        },
    )


@pos_access_required
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

    valid_payment_methods = {choice[0] for choice in Sale.PAYMENT_METHOD_CHOICES}
    default_payment_method = system_settings.default_sale_payment_method or Sale.CASH
    payment_method = request.GET.get("payment_method", request.POST.get("payment_method", default_payment_method))
    if payment_method not in valid_payment_methods:
        payment_method = default_payment_method if default_payment_method in valid_payment_methods else Sale.CASH

    discount_rate = request.GET.get("discount_rate", request.POST.get("discount_rate", "0"))
    tax_rate = request.GET.get("tax_rate", request.POST.get("tax_rate", "0")) if system_settings.allows_manual_tax else "0"
    prefill_credit_sale = (
        request.GET.get("credit") == "1"
        or request.POST.get("is_credit") == "on"
        or session_credit
    )
    if not system_settings.allow_credit_sales:
        prefill_credit_sale = False

    def pos_redirect_url():
        return _build_pos_url(
            category=selected_category,
            customer_id=selected_customer_id,
            credit=prefill_credit_sale,
            payment_method=payment_method,
            bank_account_id=selected_bank_account_id,
            discount_rate=discount_rate,
            tax_rate=tax_rate,
        )

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
        payment_method = request.POST.get("preserve_payment_method", request.POST.get("payment_method", payment_method))
        if payment_method not in valid_payment_methods:
            payment_method = default_payment_method if default_payment_method in valid_payment_methods else Sale.CASH
        discount_rate = request.POST.get("preserve_discount_rate", request.POST.get("discount_rate", discount_rate))
        tax_rate = request.POST.get("preserve_tax_rate", request.POST.get("tax_rate", tax_rate)) if system_settings.allows_manual_tax else "0"
        selected_category = request.POST.get("selected_category", selected_category).strip()

        if action == "add" and product_id:
            product = get_object_or_404(Product, id=product_id, is_active=True)
            if not _add_product_to_cart(cart, product):
                messages.error(request, f"{product.name} has no more stock available.")
            _save_cart(request, cart)
            return redirect(pos_redirect_url())

        if action == "decrease" and product_id:
            if product_id in cart:
                cart[product_id]["quantity"] = max(int(cart[product_id]["quantity"]) - 1, 0)
                if int(cart[product_id]["quantity"]) <= 0:
                    cart.pop(product_id, None)
                _save_cart(request, cart)
            return redirect(pos_redirect_url())

        if action == "remove_all" and product_id:
            cart.pop(product_id, None)
            _save_cart(request, cart)
            return redirect(pos_redirect_url())

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

            return redirect(pos_redirect_url())

        if action == "checkout":
            if not cart:
                messages.error(request, "Add at least one item before checkout.")
            else:
                product_ids = [int(pid) for pid in cart.keys()]
                locked_products = Product.objects.select_for_update().in_bulk(product_ids)
                cart_snapshot = _cart_state(cart, payment_method=payment_method)

                if cart_snapshot["checkout_disabled"]:
                    messages.error(request, "Review the cart lines with stock or pricing issues before checkout.")
                else:
                    for row in cart_snapshot["rows"]:
                        product = locked_products.get(int(row["product_id"]))
                        if product is None or row["quantity"] > product.stock:
                            messages.error(request, f"Stock changed for {row['name']}. Please review the cart.")
                            return redirect(pos_redirect_url())

                    discount_rate_value = _normalized_rate(request.POST.get("discount_rate"))
                    tax_rate_value = _normalized_rate(request.POST.get("tax_rate")) if system_settings.allows_manual_tax else Decimal("0")
                    customer = (
                        Customer.objects.filter(id=selected_customer_id, is_active=True).first()
                        if selected_customer_id
                        else None
                    )
                    is_credit = request.POST.get("is_credit") == "on"
                    payment_method_value = request.POST.get("payment_method") or default_payment_method
                    if payment_method_value not in valid_payment_methods:
                        payment_method_value = default_payment_method if default_payment_method in valid_payment_methods else Sale.CASH
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
                            tax_rate=(
                                system_settings.gst_rate_for_payment_method(payment_method_value)
                                if system_settings.uses_payment_method_gst
                                else tax_rate_value
                            ),
                            tax_mode=Sale.TAX_MODE_CATEGORY if system_settings.uses_category_gst else Sale.TAX_MODE_MANUAL,
                            tax_label=system_settings.tax_label,
                            cash_tax_rate_snapshot=system_settings.gst_rate_for_payment_method(Sale.CASH),
                            bank_tax_rate_snapshot=system_settings.gst_rate_for_payment_method(Sale.BANK),
                        )
                        sale.full_clean()
                        sale.save()

                        settings_adjusted_lines = False
                        for row in cart_snapshot["rows"]:
                            product = locked_products.get(int(row["product_id"]))
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
                                cash_tax_rate_snapshot=row.get("cash_category_tax_rate", Decimal("0")),
                                bank_tax_rate_snapshot=row.get("bank_category_tax_rate", Decimal("0")),
                            )

                        sale.refresh_from_db()
                        sale.paid_amount = Decimal("0") if is_credit else Decimal(sale.total_amount or 0)
                        sale.bank_account = bank_account if payment_method_value == Sale.BANK and not is_credit else None
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
    cart_state = _cart_state(cart, payment_method=payment_method)
    sale_preview = sale_totals_from_rows(cart_state["rows"], discount_rate, tax_rate, system_settings, payment_method=payment_method)

    products_queryset = Product.objects.filter(is_active=True)
    if selected_category:
        products_queryset = products_queryset.filter(category_id=selected_category)

    products = products_queryset.select_related("category", "brand").order_by("name")
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
            "uses_payment_method_gst": system_settings.uses_payment_method_gst,
            "uses_category_gst": system_settings.uses_category_gst,
            "gst_enabled": system_settings.gst_enabled,
            "cash_gst_rate_value": system_settings.gst_rate_for_payment_method(Sale.CASH),
            "bank_gst_rate_value": system_settings.gst_rate_for_payment_method(Sale.BANK),
            "allow_credit_sales": system_settings.allow_credit_sales,
            "allow_price_override": system_settings.allow_price_override,
            "allow_line_discount": system_settings.allow_line_discount,
            "sale_preview": sale_preview,
            "pos_customer_add_url": build_customer_add_url(),
        },
    )
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
    payment_method_totals = payment_method_totals_for_sale(sale, subtotal=subtotal, items=items)
    show_payment_method_totals = bool(payment_method_totals)

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
            "payment_method_totals": payment_method_totals,
            "show_payment_method_totals": show_payment_method_totals,
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


def invoice(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related("customer", "sold_by", "bank_account"), id=sale_id)
    if not user_can_access_sale(request.user, sale):
        messages.error(request, "Cashiers can only open receipts for their own sales.")
        return redirect("pos_screen")

    items = list(sale.items.select_related("product", "product__category"))
    subtotal = sum((item.line_total for item in items), Decimal("0"))
    line_discount_total = sum((item.line_discount_amount for item in items), Decimal("0"))
    discount_amount = quantize_money(subtotal * (_normalized_rate(sale.discount_rate) / Decimal("100")))
    tax_amount = sale_tax_amount(sale, subtotal=subtotal, items=items)
    settings_obj = get_system_settings()
    tax_label_value = sale_tax_label(sale, settings_obj.tax_label)
    company = get_active_company_profile()
    payment_method_totals = payment_method_totals_for_sale(sale, subtotal=subtotal, items=items)
    customer_badge = get_customer_badge(sale.customer) if sale.customer_id else None
    previous_balance = None
    new_balance = None
    if customer_badge:
        new_balance = quantize_money(customer_badge["amount"])
        previous_balance = quantize_money(new_balance - Decimal(sale.balance_due))

    context = {
        "sale": sale,
        "items": items,
        "subtotal": subtotal,
        "line_discount_total": line_discount_total,
        "discount_amount": discount_amount,
        "tax_amount": tax_amount,
        "tax_label_value": tax_label_value,
        "grand_total": quantize_money(sale.total_amount or 0),
        "paid_amount": quantize_money(sale.paid_amount or 0),
        "balance_due": quantize_money(sale.balance_due),
        "company": company,
        "thermal_width_mm": settings_obj.thermal_printer_width_mm,
        "payment_method_totals": payment_method_totals,
        "show_payment_method_totals": bool(payment_method_totals),
        "previous_balance": previous_balance,
        "new_balance": new_balance,
    }
    return render(request, "pos/invoice.html", context)


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
    settings_obj = get_system_settings()
    tax_label_value = sale_tax_label(sale, settings_obj.tax_label)
    grand_total = Decimal(sale.total_amount or 0)
    printer_width_mm = settings_obj.thermal_printer_width_mm
    payment_method_totals = payment_method_totals_for_sale(sale, subtotal=subtotal, items=items)
    show_payment_method_totals = bool(payment_method_totals)
    cashier_name = sale.sold_by.get_full_name() or sale.sold_by.username if sale.sold_by_id else ""

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
    if show_payment_method_totals:
        total_lines += len(payment_method_totals) * 2
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

    if show_payment_method_totals:
        for option in payment_method_totals:
            pdf.drawString(left_margin, y, option["tax_label_display"][:24])
            pdf.drawRightString(right_margin, y, f"{option['tax_amount']:.2f}")
            y -= line_height
            pdf.drawString(left_margin, y, f"{option['label']} total")
            pdf.drawRightString(right_margin, y, f"{option['total_amount']:.2f}")
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
    footer_text = (company.invoice_footer if company and company.invoice_footer else "Thank you for your business.")[:40]
    pdf.drawCentredString(paper_width / 2, y, footer_text)
    pdf.showPage()
    pdf.save()
    return response


@module_access_required("reports")
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
            "tax_label": get_system_settings().tax_label,
        },
    )


@module_access_required("parties")
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


@module_access_required("parties")
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


@module_access_required("parties")
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


@module_access_required("parties")
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


@module_access_required("parties")
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


@module_access_required("parties")
def party_statement_page(request):
    return render(request, "parties/party_statement.html")


@module_access_required("products", minimum=ACCESS_MANAGE)
def brand_add(request):
    form = BrandForm(request.POST or None)
@module_access_required("products", minimum=ACCESS_MANAGE)
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


@module_access_required("products", minimum=ACCESS_MANAGE)
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
                    "location": product.location,
                    "expiry_date": product.expiry_date.isoformat() if product.expiry_date else "",
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

    return items, total


@module_access_required("purchases")
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


@module_access_required("purchases", minimum=ACCESS_MANAGE)
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


@module_access_required("purchases")
def purchase_detail(request, pk):
    purchase = get_object_or_404(
        Purchase.objects.select_related("supplier").prefetch_related("items__product", "edit_logs"),
        pk=pk,
    )
    purchase = annotate_purchase_balances([purchase])[0]
    return render(request, "pos/purchase_detail.html", {"purchase": purchase})


@module_access_required("purchases", minimum=ACCESS_MANAGE)
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


@module_access_required("stock_control", minimum=ACCESS_MANAGE)
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


@module_access_required("pos", minimum=ACCESS_MANAGE)
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


@module_access_required("reports")
def low_stock_list(request):
    products = (
        Product.objects.filter(stock__lte=F("low_stock_threshold"), is_active=True)
        .select_related("category")
        .order_by("stock", "name")
    )
    return render(request, "pos/low_stock_list.html", {"products": products})




















