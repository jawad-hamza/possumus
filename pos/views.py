from django.forms import modelform_factory, modelformset_factory
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from .models import Category, Product, PurchaseEditLog, PurchaseItem, Sale, SaleItem , Customer ,Supplier , Purchase,LedgerEntry , CompanyProfile
from django.db.models import Sum,Count , F , Q , Value ,  DecimalField, ExpressionWrapper
from .forms import CustomerForm , ProductForm, SupplierForm , BrandForm , CategoryForm
from datetime import datetime
from django.utils import timezone
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.db.models.functions import Coalesce
import datetime
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

# Create your views here.

def test (request):
    return render(request, "pos/test_template.html")


def scan_barcode(request, sku):
    """
    Look up product by SKU. If found, add to cart
    and return JSON status. Otherwise return error.
    """
    cart = request.session.get("cart", {})

    try:
        product = Product.objects.get(sku=sku, is_active=True)
    except Product.DoesNotExist:
        return JsonResponse({"status": "notfound"})

    pid = str(product.id)

    if pid in cart:
        cart[pid]["quantity"] += 1
    else:
        cart[pid] = {
            "name": product.name,
            "price": str(product.price),
            "quantity": 1,
        }

    request.session["cart"] = cart
    return JsonResponse({"status": "ok"})

def home (request):
    return render(request, "pos/pos_screen.html")

def product_list(request):
    # base queryset
    qs = Product.objects.select_related("category", "supplier").order_by("name")

    # search
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(name__icontains=q)

    # pagination
    paginator = Paginator(qs, 20)  # 20 per page
    page_num = request.GET.get("page")
    products = paginator.get_page(page_num)

    return render(request, "pos/product_list.html", {
        "products": products,
        "search_query": q,
    })
# ----------------------------------
# ADD NEW PRODUCT
# ----------------------------------
def product_add(request):
    form = ProductForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Product '{product.name}' added successfully!")
            return redirect("product_view", pk=product.id)
        else:
            messages.error(request, "Please fix the errors below.")

    return render(request, "pos/product_form.html", {
        "form": form,
        "title": "Add Product"
    })

    form = ProductForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            return redirect("product_list")

    return render(
        request,
        "pos/product_form.html",
        {"form": form, "title": "Add Product"}
    )
# ----------------------------------
# VIEW SINGLE PRODUCT
# ----------------------------------
def product_view(request, pk):
    product = get_object_or_404(Product, id=pk)

    total_sold = (
        SaleItem.objects.filter(product=product)
        .aggregate(total=Sum("quantity"))["total"]
        or 0
    )

    total_purchased = (
        PurchaseItem.objects.filter(product=product)
        .aggregate(total=Sum("quantity"))["total"]
        or 0
    )

    recent_sales = (
        SaleItem.objects.filter(product=product)
        .order_by("-sale__created_at")[:10]
    )

    recent_purchases = (
        PurchaseItem.objects.filter(product=product)
        .order_by("-purchase__created_at")[:10]
    )

    return render(request, "pos/product_view.html", {
        "product": product,
        "total_sold": total_sold,
        "total_purchased": total_purchased,
        "recent_sales": recent_sales,
        "recent_purchases": recent_purchases,
    })

    product = get_object_or_404(Product, id=pk)

    total_sold = (
        SaleItem.objects.filter(product=product)
        .aggregate(total=Sum("quantity"))["total"]
        or 0
    )

    total_purchased = (
        PurchaseItem.objects.filter(product=product)
        .aggregate(total=Sum("quantity"))["total"]
        or 0
    )

    recent_sales = (
        SaleItem.objects.filter(product=product)
        .order_by("-sale__created_at")[:10]
    )

    recent_purchases = (
        PurchaseItem.objects.filter(product=product)
        .order_by("-purchase__created_at")[:10]
    )

    return render(request, "pos/product_view.html", {
        "product": product,
        "total_sold": total_sold,
        "total_purchased": total_purchased,
        "recent_sales": recent_sales,
        "recent_purchases": recent_purchases,
    })

# ----------------------------------
# EDIT PRODUCT
# ----------------------------------

    product = get_object_or_404(Product, id=pk)
    form = ProductForm(request.POST or None, instance=product)

    if request.method == "POST":
        if form.is_valid():
            form.save()
            return redirect("product_view", pk=product.id)

    return render(
        request,
        "pos/product_form.html",
        {"form": form, "title": "Edit Product"}
    )

def product_edit(request, pk):
    product = get_object_or_404(Product, id=pk)
    form = ProductForm(request.POST or None, instance=product)

    if request.method == "POST":
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Product '{product.name}' updated!")
            return redirect("product_view", pk=product.id)
        else:
            messages.error(request, "Please fix the errors below.")

    return render(request, "pos/product_form.html", {
        "form": form,
        "title": "Edit Product"
    })

# ----------------------------------
# DELETE PRODUCT
# ----------------------------------
def product_delete(request, pk):
    product = get_object_or_404(Product, id=pk)

    if request.method == "POST":
        product_name = product.name
        product.delete()
        messages.success(request, f"Product '{product_name}' deleted successfully!")
        return redirect("product_list")

    return render(request, "pos/product_confirm_delete.html", {
        "product": product
    })

    product = get_object_or_404(Product, id=pk)

    if request.method == "POST":
        product.delete()
        return redirect("product_list")

    return render(
        request,
        "pos/product_confirm_delete.html",
        {"product": product}
    )    
    return render(request, "pos/home.html")

def dashboard(request):
    today = timezone.localdate()

    # 1. Total Sales Today
    total_sales = Sale.objects.filter(
        created_at__date=today
    ).aggregate(total=Sum("total_amount"))["total"] or 0

    # 2. Total Bills (transactions) Today
    total_bills = Sale.objects.filter(created_at__date=today).count()

    # 3. Average Sale Value Today
    avg_sale = 0
    if total_bills > 0:
        avg_sale = total_sales / total_bills

    # 4. Low Stock Count
    low_stock_products = Product.objects.filter(stock__lte=F("low_stock_threshold"),is_active=True)
    low_stock_count = low_stock_products.count()

    # 5. Top 10 Low Stock Items
    top_low_stock = low_stock_products.order_by("stock")[:10]

    # 6. Best-Selling Products (all time)
    best_selling = (
        SaleItem.objects.values("product__id", "product__name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")[:10]
    )

    # 7. Best-Selling Products (this month)
    month_start = today.replace(day=1)
    best_selling_month = (
        SaleItem.objects.filter(sale__created_at__date__gte=month_start)
        .values("product__id", "product__name")
        .annotate(total_qty=Sum("quantity"))
        .order_by("-total_qty")[:10]
    )

    # 8. Top 10 Customers by Spend
    top_customers = (
        Sale.objects.values("customer__id", "customer__name")
        .annotate(total_spent=Sum("total_amount"))
        .order_by("-total_spent")[:10]
    )

    context = {
        "total_sales": total_sales,
        "total_bills": total_bills,
        "avg_sale": avg_sale,
        "low_stock_count": low_stock_count,
        "top_low_stock": top_low_stock,
        "best_selling": best_selling,
        "best_selling_month": best_selling_month,
        "top_customers": top_customers,
    }
    return render(request, "pos/dashboard.html", context)

def safe_decimal(value):
    try:
        return Decimal(value)
    except (TypeError, InvalidOperation):
        return Decimal("0")


def pos_screen(request):
    cart = request.session.get("cart", {})

    # ✅ Category filter (simple GET)
    selected_category = request.GET.get("category")  # string or None

    # =========================
    # HANDLE POST ACTIONS
    # =========================
    if request.method == "POST":
        action = request.POST.get("action")
        product_id = request.POST.get("product_id")

        # =========================
        # ADD / INCREASE
        # =========================
        if action == "add" and product_id:
            product = get_object_or_404(Product, id=product_id)

            current_qty = cart.get(product_id, {}).get("quantity", 0)

            # ✅ BLOCK add if no stock OR qty exceeds stock
            if product.stock <= 0 or current_qty >= product.stock:
                # messages.error(request, f"Not enough stock for {product.name}")  # optional
                return redirect("pos_screen")

            if product_id in cart:
                cart[product_id]["quantity"] += 1
            else:
                cart[product_id] = {
                    "name": product.name,
                    "price": str(product.price),
                    "quantity": 1,
                }

            request.session["cart"] = cart
            return redirect("pos_screen")

        # =========================
        # DECREASE
        # =========================
        if action == "decrease" and product_id:
            if product_id in cart:
                cart[product_id]["quantity"] -= 1
                if cart[product_id]["quantity"] <= 0:
                    cart.pop(product_id, None)
                request.session["cart"] = cart
            return redirect("pos_screen")

        # =========================
        # REMOVE ALL
        # =========================
        if action == "remove_all" and product_id:
            if product_id in cart:
                cart.pop(product_id, None)
                request.session["cart"] = cart
            return redirect("pos_screen")

        # =========================
        # CHECKOUT
        # =========================
        if action == "checkout":
            if not cart:
                return redirect("pos_screen")

            # ✅ SERVER-SIDE STOCK VALIDATION (must-have)
            # Ensure every cart qty <= current product stock
            for pid, item in cart.items():
                product = get_object_or_404(Product, id=pid)
                if item["quantity"] > product.stock:
                    # messages.error(request, f"Not enough stock for {product.name}")  # optional
                    return redirect("pos_screen")

            discount_rate = safe_decimal(request.POST.get("discount_rate"))
            tax_rate = safe_decimal(request.POST.get("tax_rate"))

            is_credit = request.POST.get("is_credit") == "on"
            customer_id = request.POST.get("customer_id") or None

            # ✅ Credit sale only allowed if registered customer selected
            if is_credit and not customer_id:
                # messages.error(request, "Select a registered customer for loan sale.")  # optional
                return redirect("pos_screen")

            sale = Sale.objects.create(
                customer_id=customer_id,
                is_credit=is_credit,
                discount_rate=discount_rate,
                tax_rate=tax_rate
            )

            # Create SaleItem objects (SaleItem.clean will also protect)
            for pid, item in cart.items():
                SaleItem.objects.create(
                    sale=sale,
                    product_id=pid,
                    price=Decimal(item["price"]),
                    quantity=item["quantity"],
                )

            sale.update_total()

            request.session["cart"] = {}
            return redirect("invoice", sale_id=sale.id)

    # =========================
    # GET REQUEST (DISPLAY POS)
    # =========================
    products_qs = Product.objects.filter(is_active=True)

    if selected_category:
        products_qs = products_qs.filter(category_id=selected_category)

    products = products_qs.order_by("name")
    categories = Category.objects.filter(is_active=True).order_by("name")
    customers = Customer.objects.filter(is_active=True).order_by("name")

    # Build a stock_map for template checks
    stock_map = {str(p.id): p.stock for p in Product.objects.only("id", "stock")}

    # cart total
    cart_total = sum(
        Decimal(item["price"]) * item["quantity"]
        for item in cart.values()
    )

    # ✅ Determine if checkout should be disabled (front-end safety)
    checkout_disabled = False
    for pid, item in cart.items():
        available = stock_map.get(str(pid), 0)
        if item["quantity"] > available or available <= 0:
            checkout_disabled = True
            break

    return render(
        request,
        "pos/pos_screen.html",
        {
            "products": products,
            "cart": cart,
            "categories": categories,
            "customers": customers,
            "cart_total": cart_total,
            "selected_category": str(selected_category) if selected_category else "",
            "stock_map": stock_map,
            "checkout_disabled": checkout_disabled,
        }
    )

def invoice(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id)
    items = sale.items.all()

    # calculations (same as before)
    subtotal = sum(item.price * item.quantity for item in items)

    discount_rate = sale.discount_rate or Decimal("0")
    tax_rate = sale.tax_rate or Decimal("0")

    discount_amount = subtotal * (discount_rate / Decimal("100"))
    after_discount = subtotal - discount_amount
    tax_amount = after_discount * (tax_rate / Decimal("100"))

    grand_total = sale.total_amount

    company = CompanyProfile.objects.filter(is_active=True).first()

    return render(
        request,
        "pos/invoice.html",
        {
            "sale": sale,
            "items": items,
            "subtotal": subtotal,
            "discount_rate": discount_rate,
            "discount_amount": discount_amount,
            "tax_rate": tax_rate,
            "tax_amount": tax_amount,
            "grand_total": grand_total,
            "company": company,
            "payments": [],  # later
        }
    )
def add_customer_from_pos(request):
    
    if request.method == "POST":
        form = CustomerForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("pos_screen")
    else:
        form = CustomerForm()

    return render(
        request,
        "pos/add_customer.html",
        {"form": form}
    )


def add_supplier_from_pos(request):
    
    if request.method == "POST":
        form = SupplierForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("pos_screen")
    else:
        form = SupplierForm()

    return render(
        request,
        "pos/add_supplier.html",
        {"form": form}
    )


    
    
    from django.db.models import Q, F, Sum, Value
    from django.db.models.functions import Coalesce
    import datetime

    start_date = request.GET.get("start_date")
    end_date = request.GET.get("end_date")
    search = request.GET.get("search", "")
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)

    # default to today if no date provided
    if not start_date and request.GET.get("date"):
        start_date = end_date = request.GET.get("date")

    if not start_date:
        start_date = end_date = today
    
    # Convert strings to date objects if needed
    if isinstance(start_date, str):
        start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
    if isinstance(end_date, str):
        end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()

    sales = Sale.objects.filter(
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )

    if search:
        sales = sales.filter(
            Q(id__icontains=search) |
            Q(customer__name__icontains=search)
        )

    sales = sales.annotate(
        discount_rate_safe=Coalesce("discount_rate", Value(0)),
        tax_rate_safe=Coalesce("tax_rate", Value(0)),
    ).annotate(
        discount_amount=Coalesce(
            F("total_amount") * F("discount_rate_safe") / Value(100),
            Value(0),
        ),
        tax_amount=Coalesce(
            (F("total_amount") - (F("total_amount") * F("discount_rate_safe") / Value(100))) * F("tax_rate_safe") / Value(100),
            Value(0),
        ),
        num_items=Coalesce(Sum("items__quantity"), Value(0)),
    ).annotate(
        final_total=F("total_amount") - F("discount_amount") + F("tax_amount")
    )

    totals = sales.aggregate(
        subtotal=Coalesce(Sum("total_amount"), Value(0)),
        discount=Coalesce(Sum("discount_amount"), Value(0)),
        tax=Coalesce(Sum("tax_amount"), Value(0)),
        total=Coalesce(Sum("final_total"), Value(0)),
    )

    # average needs safety
    total_bills = sales.count()
    if total_bills > 0:
        avg_order_value = totals["total"] / total_bills
    else:
        avg_order_value = 0

    # Net revenue is total after discount and tax
    net_revenue = totals["total"]

    context = {
        "sales": sales,
        "totals": totals,
        "start_date": start_date,
        "end_date": end_date,
        "selected_label": f"{start_date} → {end_date}",
        "search_term": search,
        "total_bills": total_bills,
        "total_sales": totals["total"],
        "avg_order_value": avg_order_value,
        "total_tax": totals["tax"],
        "total_discount": totals["discount"],
        "net_revenue": net_revenue,
        "today": today,
        "yesterday": yesterday,
    }
    return render(request, "pos/daily_sales_report.html", context)

def invoice_pdf(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id)
    items = sale.items.all()

    # =========================
    # CALCULATIONS
    # =========================
    subtotal = sum(item.price * item.quantity for item in items)

    discount_rate = sale.discount_rate or Decimal("0")
    tax_rate = sale.tax_rate or Decimal("0")

    discount_amount = subtotal * (discount_rate / Decimal("100"))
    after_discount = subtotal - discount_amount
    tax_amount = after_discount * (tax_rate / Decimal("100"))

    grand_total = sale.total_amount

    previous_balance = Decimal("0")
    new_balance = None

    if sale.customer and sale.is_credit:
        previous_balance = getattr(sale.customer, "balance", Decimal("0"))
        new_balance = previous_balance + grand_total

    # =========================
    # THERMAL PAPER SETUP
    # =========================
    paper_width = 80 * mm
    line_height = 12

    BASE_LINES = 14
    ITEM_LINES = 2
    CREDIT_LINES = 5 if sale.is_credit else 0
    FOOTER_LINES = 3

    total_lines = (
        BASE_LINES +
        (len(items) * ITEM_LINES) +
        CREDIT_LINES +
        FOOTER_LINES
    )

    paper_height = (total_lines * line_height) + 20
    y = paper_height - line_height

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="receipt_{sale.id}.pdf"'

    p = canvas.Canvas(response, pagesize=(paper_width, paper_height))

    # =========================
    # HEADER
    # =========================
    p.setFont("Helvetica-Bold", 12)
    p.drawCentredString(paper_width / 2, y, "POS IMS")
    y -= line_height

    p.setFont("Helvetica", 9)
    p.drawCentredString(paper_width / 2, y, "Sales Receipt")
    y -= line_height * 2

    p.drawString(10, y, f"Invoice #: {sale.id}")
    y -= line_height

    p.drawString(10, y, sale.created_at.strftime("%d-%m-%Y %H:%M"))
    y -= line_height

    customer_name = sale.customer.name if sale.customer else "Walk-in Customer"
    p.drawString(10, y, f"Customer: {customer_name}")
    y -= line_height

    p.line(10, y, paper_width - 10, y)
    y -= line_height

    # =========================
    # ITEMS
    # =========================
    p.setFont("Helvetica", 9)

    for item in items:
        p.drawString(10, y, item.product.name[:32])
        y -= line_height

        p.drawRightString(
            paper_width - 10,
            y,
            f"{item.quantity} x {item.price} = {item.price * item.quantity}"
        )
        y -= line_height

    p.line(10, y, paper_width - 10, y)
    y -= line_height

    # =========================
    # TOTALS
    # =========================
    p.drawString(10, y, "Subtotal:")
    p.drawRightString(paper_width - 10, y, f"Rs {subtotal:.2f}")
    y -= line_height

    if discount_rate > 0:
        p.drawString(10, y, f"Discount ({discount_rate}%):")
        p.drawRightString(paper_width - 10, y, f"- Rs {discount_amount:.2f}")
        y -= line_height

    if tax_rate > 0:
        p.drawString(10, y, f"Tax ({tax_rate}%):")
        p.drawRightString(paper_width - 10, y, f"Rs {tax_amount:.2f}")
        y -= line_height

    p.setFont("Helvetica-Bold", 11)
    p.drawString(10, y, "TOTAL:")
    p.drawRightString(paper_width - 10, y, f"Rs {grand_total:.2f}")
    y -= line_height

    # =========================
    # CREDIT BALANCE
    # =========================
    if sale.is_credit and sale.customer:
        p.line(10, y, paper_width - 10, y)
        y -= line_height

        p.setFont("Helvetica", 9)
        p.drawString(10, y, "Previous Balance:")
        p.drawRightString(paper_width - 10, y, f"Rs {previous_balance:.2f}")
        y -= line_height

        p.drawString(10, y, "This Sale:")
        p.drawRightString(paper_width - 10, y, f"Rs {grand_total:.2f}")
        y -= line_height

        p.setFont("Helvetica-Bold", 10)
        p.drawString(10, y, "New Balance:")
        p.drawRightString(paper_width - 10, y, f"Rs {new_balance:.2f}")
        y -= line_height

    # =========================
    # FOOTER
    # =========================
    p.setFont("Helvetica", 8)
    p.drawCentredString(paper_width / 2, y, "Thank you for your purchase!")
    y -= line_height

    p.drawCentredString(paper_width / 2, y, "Powered by POS IMS")

    p.showPage()
    p.save()

    return response

###################################################################################
def products_by_category(request, category_id):
    products = Product.objects.filter(
        category_id=category_id,
        is_active=True
    )
    return render(
        request,
        "pos/partials/product_grid.html",
        {"products": products}
    )
    
##########################################################################################
 

def daily_sales_report(request):
    
    from django.db.models import Q, F, Sum, Value, DecimalField
    from django.db.models.functions import Coalesce
    from django.utils import timezone
    import datetime

    search = request.GET.get("search", "")
    # Get current date in Pakistan timezone
    today = timezone.localtime(timezone.now()).date()
    yesterday = today - datetime.timedelta(days=1)
    
    # Handle range parameter from Quick Presets buttons
    range_param = request.GET.get("range")
    
    if range_param == "today":
        start_date = end_date = today
    elif range_param == "yesterday":
        start_date = end_date = yesterday
    elif range_param == "last_7":
        start_date = today - datetime.timedelta(days=6)
        end_date = today
    else:
        # Handle manual date inputs
        start_date = request.GET.get("start_date")
        end_date = request.GET.get("end_date")
        
        # Handle single date parameter
        if not start_date and not end_date and request.GET.get("date"):
            start_date = end_date = request.GET.get("date")
        
        # Default to today if no dates provided
        if not start_date:
            start_date = today
        if not end_date:
            end_date = today
        
        # Convert strings to date objects if needed
        if isinstance(start_date, str):
            try:
                start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
            except ValueError:
                try:
                    start_date = datetime.datetime.strptime(start_date, "%m/%d/%Y").date()
                except ValueError:
                    start_date = today
        
        if isinstance(end_date, str):
            try:
                end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError:
                try:
                    end_date = datetime.datetime.strptime(end_date, "%m/%d/%Y").date()
                except ValueError:
                    end_date = today

    # Convert dates to timezone-aware datetime objects for proper filtering
    # Start of day: 00:00:00 in local timezone
    start_datetime = timezone.make_aware(
        datetime.datetime.combine(start_date, datetime.time.min)
    )
    # End of day: 23:59:59.999999 in local timezone
    end_datetime = timezone.make_aware(
        datetime.datetime.combine(end_date, datetime.time.max)
    )

    sales = Sale.objects.filter(
        created_at__gte=start_datetime,
        created_at__lte=end_datetime,
    ).order_by('-created_at')

    if search:
        sales = sales.filter(
            Q(id__icontains=search) |
            Q(customer__name__icontains=search)
        )

    sales = sales.annotate(
        discount_rate_safe=Coalesce("discount_rate", Value(0, output_field=DecimalField())),
        tax_rate_safe=Coalesce("tax_rate", Value(0, output_field=DecimalField())),
    ).annotate(
        discount_amount=Coalesce(
            F("total_amount") * F("discount_rate_safe") / Value(100, output_field=DecimalField()),
            Value(0, output_field=DecimalField()),
            output_field=DecimalField()
        ),
        tax_amount=Coalesce(
            (F("total_amount") - (F("total_amount") * F("discount_rate_safe") / Value(100, output_field=DecimalField()))) * F("tax_rate_safe") / Value(100, output_field=DecimalField()),
            Value(0, output_field=DecimalField()),
            output_field=DecimalField()
        ),
        num_items=Coalesce(Sum("items__quantity"), Value(0)),
    ).annotate(
        final_total=F("total_amount") - F("discount_amount") + F("tax_amount")
    )

    totals = sales.aggregate(
        subtotal=Coalesce(Sum("total_amount"), Value(0, output_field=DecimalField())),
        discount=Coalesce(Sum("discount_amount"), Value(0, output_field=DecimalField())),
        tax=Coalesce(Sum("tax_amount"), Value(0, output_field=DecimalField())),
        total=Coalesce(Sum("final_total"), Value(0, output_field=DecimalField())),
    )

    # average needs safety
    total_bills = sales.count()
    if total_bills > 0:
        avg_order_value = totals["total"] / total_bills
    else:
        avg_order_value = 0

    # Net revenue is total after discount and tax
    net_revenue = totals["total"]

    context = {
        "sales": sales,
        "totals": totals,
        "start_date": start_date,
        "end_date": end_date,
        "selected_label": f"{start_date} → {end_date}",
        "search_term": search,
        "total_bills": total_bills,
        "total_sales": totals["total"],
        "avg_order_value": avg_order_value,
        "total_tax": totals["tax"],
        "total_discount": totals["discount"],
        "net_revenue": net_revenue,
        "today": today,
        "yesterday": yesterday,
    }
    return render(request, "pos/daily_sales_report.html", context)
#############################################PARTIES APP VIEW ##########################################

def parties_home(request):
    """Landing page for parties management"""
    total_customers = Customer.objects.count()
    total_suppliers = Supplier.objects.count()
    
    # Recent customers and suppliers
    recent_customers = Customer.objects.all().order_by('-id')[:5]
    recent_suppliers = Supplier.objects.all().order_by('-id')[:5]
    
    context = {
        'total_customers': total_customers,
        'total_suppliers': total_suppliers,
        'recent_customers': recent_customers,
        'recent_suppliers': recent_suppliers,
    }
    return render(request, 'parties/parties_home.html', context)


def customer_list(request):
    """List all customers with search functionality"""
    search = request.GET.get('search', '')
    
    customers = Customer.objects.all()
    
    if search:
        customers = customers.filter(
            Q(name__icontains=search) |
            Q(phone__icontains=search) |
            Q(email__icontains=search)
        )
    
    customers = customers.order_by('-id')
    
    context = {
        'customers': customers,
        'search_term': search,
        'total_customers': customers.count(),
    }
    return render(request, 'parties/customer_list.html', context)


def supplier_list(request):
    """List all suppliers with search functionality"""
    search = request.GET.get('search', '')
    
    suppliers = Supplier.objects.all()
    
    if search:
        suppliers = suppliers.filter(
            Q(name__icontains=search) |
            Q(phone__icontains=search) |
            Q(email__icontains=search)
        )
    
    suppliers = suppliers.order_by('-id')
    
    context = {
        'suppliers': suppliers,
        'search_term': search,
        'total_suppliers': suppliers.count(),
    }
    return render(request, 'parties/supplier_list.html', context)


def customer_detail(request, id):
    """Customer detail page with sales history"""
    customer = get_object_or_404(Customer, id=id)
    
    # Get customer's sales history
    sales = Sale.objects.filter(customer=customer).order_by('-created_at')
    
    # Calculate statistics
    total_sales = sales.aggregate(
        total=Coalesce(Sum('total_amount'), Value(0, output_field=DecimalField()))
    )['total']
    
    total_transactions = sales.count()
    
    context = {
        'customer': customer,
        'sales': sales,
        'total_sales': total_sales,
        'total_transactions': total_transactions,
    }
    return render(request, 'parties/customer_detail.html', context)


def supplier_detail(request, id):
    """Supplier detail page with purchase history"""
    supplier = get_object_or_404(Supplier, id=id)
    
    # Get supplier's purchase history if you have a Purchase model
    # Otherwise, you can show products supplied by this supplier
    products = Product.objects.filter(supplier=supplier).order_by('-id')
    
    context = {
        'supplier': supplier,
        'products': products,
        'total_products': products.count(),
    }
    return render(request, 'parties/supplier_detail.html', context)

###################################################################################################




def create_sale_ledger(sale):
    """
    Create ledger entry ONLY for credit sales
    """
    if not sale.is_credit:
        return

    if not sale.customer:
        return

    ct = ContentType.objects.get_for_model(sale.customer)

    LedgerEntry.objects.create(
        content_type=ct,
        object_id=sale.customer.id,
        entry_type=LedgerEntry.DEBIT,
        amount=sale.total_amount,
        reference="Sale",
        note=f"Sale #{sale.id} (Credit)"
    )

def create_purchase_ledger(purchase):
    if not purchase.supplier:
        return

    ct = ContentType.objects.get_for_model(purchase.supplier)

    LedgerEntry.objects.create(
        content_type=ct,
        object_id=purchase.supplier.id,
        entry_type=LedgerEntry.CREDIT,
        amount=purchase.total_amount,
        reference="Purchase",
        note=f"Purchase #{purchase.id}"
    )

##################################################################################

def brand_add(request):
    if request.method == "POST":
        form = BrandForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Brand added successfully.")
            return redirect("product_list")  # or brand list later
    else:
        form = BrandForm()

    return render(
        request,
        "pos/brand_form.html",
        {
            "form": form,
        }
    )
    
################################################################################

def category_add(request):
    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Category added successfully.")
            return redirect("product_list")  # or category list later
    else:
        form = CategoryForm()

    return render(
        request,
        "pos/category_form.html",
        {
            "form": form,
        }
    )
    

def purchase_list(request):
    purchases = Purchase.objects.select_related("supplier").order_by("-id")
    return render(request, "pos/purchase_list.html", {
        "purchases": purchases
    })


@transaction.atomic
def purchase_add(request):
    PurchaseForm = modelform_factory(Purchase, fields=["supplier"])
    PurchaseItemForm = modelform_factory(
        PurchaseItem,
        fields=["product", "quantity", "cost_price"]
    )

    if request.method == "POST":
        purchase_form = PurchaseForm(request.POST)
        item_form = PurchaseItemForm(request.POST)

        if purchase_form.is_valid() and item_form.is_valid():
            purchase = purchase_form.save()
            item = item_form.save(commit=False)
            item.purchase = purchase
            item.save()
            return redirect("purchase_list")
    else:
        purchase_form = PurchaseForm()
        item_form = PurchaseItemForm()

    return render(request, "pos/purchase_add.html", {
        "purchase_form": purchase_form,
        "item_form": item_form
    })


def purchase_detail(request, pk):
    purchase = get_object_or_404(Purchase, pk=pk)
    return render(request, "pos/purchase_detail.html", {
        "purchase": purchase
    })
    
    

@transaction.atomic
def purchase_edit(request, pk):
    purchase = get_object_or_404(Purchase, pk=pk)
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")

    PurchaseItemFormSet = modelformset_factory(
        PurchaseItem,
        fields=("product", "quantity", "cost_price"),
        extra=0,
        can_delete=True
    )

    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()
        if not reason:
            formset = PurchaseItemFormSet(
                request.POST,
                queryset=purchase.items.all()
            )
            return render(request, "pos/purchase_edit.html", {
                "purchase": purchase,
                "suppliers": suppliers,
                "formset": formset,
                "error": "Reason for edit is required.",
            })

        old_total = purchase.total_amount

        # Update purchase header
        purchase.supplier_id = request.POST.get("supplier") or None
        purchase.last_edited_by = request.user
        purchase.last_edited_at = timezone.now()
        purchase.edit_reason = reason
        purchase.save()

        formset = PurchaseItemFormSet(
            request.POST,
            queryset=purchase.items.all()
        )

        if formset.is_valid():
            # Handle deleted items
            for obj in formset.deleted_objects:
                obj.delete()

            # Save edited items
            items = formset.save(commit=False)
            for item in items:
                item.purchase = purchase
                item.save()  # triggers stock + total + accounting

            # Audit log
            PurchaseEditLog.objects.create(
                purchase=purchase,
                edited_by=request.user,
                reason=reason,
                old_total=old_total,
                new_total=purchase.total_amount,
            )

            return redirect("purchase_detail", pk=purchase.id)

        # If formset invalid
        return render(request, "pos/purchase_edit.html", {
            "purchase": purchase,
            "suppliers": suppliers,
            "formset": formset,
        })

    # GET request
    formset = PurchaseItemFormSet(queryset=purchase.items.all())

    return render(request, "pos/purchase_edit.html", {
        "purchase": purchase,
        "suppliers": suppliers,
        "formset": formset,
    })
    

def low_stock_list(request):
    products = Product.objects.filter(
        stock__lte=F("low_stock_threshold"),
        is_active=True
    ).order_by("stock", "name")

    return render(
        request,
        "pos/low_stock_list.html",
        {"products": products}
    )