from django.shortcuts import render
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from .models import Category, Product, Sale, SaleItem , Customer
from django.db.models import Sum,Count 
from .forms import CustomerForm
from datetime import datetime
from django.utils import timezone
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas
from decimal import Decimal, InvalidOperation



# Create your views here.
def home(request):
    return HttpResponse("Welcome to the POS System")
def product_list(request):
    products = Product.objects.filter(is_active=True)

    return render(
        request,
        "pos/product_list.html",
        {"products": products}
    )


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
    low_stock_products = Product.objects.filter(stock__lte=5, is_active=True)
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

            if product_id in cart:
                cart[product_id]["quantity"] += 1
            else:
                cart[product_id] = {
                    "name": product.name,
                    # store price as string to avoid float issues
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

            # Read discount & tax correctly
            discount_rate = safe_decimal(request.POST.get("discount_rate"))
            tax_rate = safe_decimal(request.POST.get("tax_rate"))
            customer_id = request.POST.get("customer_id") or None

            # create Sale once, with correct rates
            sale = Sale.objects.create(
                customer_id=customer_id,
                discount_rate=discount_rate,
                tax_rate=tax_rate
            )

            # create SaleItem objects
            for pid, item in cart.items():
                SaleItem.objects.create(
                    sale=sale,
                    product_id=pid,
                    price=Decimal(item["price"]),
                    quantity=item["quantity"],
                )

            # now that items are created, calculate total properly
            sale.update_total()

            # clear cart
            request.session["cart"] = {}

            # redirect to invoice
            return redirect("invoice", sale_id=sale.id)

    # =========================
    # GET REQUEST (DISPLAY POS)
    # =========================

    products = Product.objects.filter(is_active=True)
    customers = Customer.objects.filter(is_active=True)
    categories = Category.objects.filter(is_active=True)

    # calculate UI cart total for display
    cart_total = sum(
        Decimal(item["price"]) * item["quantity"]
        for item in cart.values()
    )

    return render(
        request,
        "pos/pos_screen.html",
        {
            "products": products,
            "cart": cart,
            "categories": categories,
            "customers": customers,
            "cart_total": cart_total,
        }
    )
def invoice(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id)
    items = sale.items.all()

    # Calculate subtotal (for display only)
    subtotal = Decimal("0")
    for item in items:
        subtotal += item.price * item.quantity

    discount_rate = sale.discount_rate or Decimal("0")
    tax_rate = sale.tax_rate or Decimal("0")

    discount_amount = subtotal * (discount_rate / Decimal("100"))
    after_discount = subtotal - discount_amount

    tax_amount = after_discount * (tax_rate / Decimal("100"))

    # ✅ FINAL TOTAL COMES FROM MODEL (SOURCE OF TRUTH)
    grand_total = sale.total_amount

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
    
def daily_sales_report(request):
    # Get date from query (?date=YYYY-MM-DD)
    date_str = request.GET.get("date")

    if date_str:
        selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    else:
        selected_date = timezone.localdate()

    sales = Sale.objects.filter(
        created_at__date=selected_date
    )

    total_sales = sales.aggregate(
        total=Sum("total_amount")
    )["total"] or 0

    total_bills = sales.count()

    return render(
        request,
        "pos/daily_sales_report.html",
        {
            "sales": sales,
            "total_sales": total_sales,
            "total_bills": total_bills,
            "selected_date": selected_date,
        }
    )
    
def invoice_pdf(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id)
    items = sale.items.all()

    # -------- Calculations (DISPLAY ONLY) --------
    subtotal = Decimal("0")
    for item in items:
        subtotal += item.price * item.quantity

    discount_rate = sale.discount_rate or Decimal("0")
    tax_rate = sale.tax_rate or Decimal("0")

    discount_amount = subtotal * (discount_rate / Decimal("100"))
    after_discount = subtotal - discount_amount
    tax_amount = after_discount * (tax_rate / Decimal("100"))

    grand_total = sale.total_amount  # source of truth
    # --------------------------------------------

    # -------- THERMAL PAPER SIZE --------
    paper_width = 80 * mm          # 80mm roll
    line_height = 12

    # Dynamic height (base + items)
    paper_height = 120 + (len(items) * 15)
    # -----------------------------------

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="receipt_{sale.id}.pdf"'

    p = canvas.Canvas(response, pagesize=(paper_width, paper_height))
    y = paper_height - 20

    # ---------- HEADER ----------
    p.setFont("Helvetica-Bold", 12)
    p.drawCentredString(paper_width / 2, y, "POS IMS")
    y -= line_height

    p.setFont("Helvetica", 9)
    p.drawCentredString(paper_width / 2, y, "Sales Receipt")
    y -= line_height * 2

    p.drawString(10, y, f"Bill #: {sale.id}")
    y -= line_height

    p.drawString(10, y, sale.created_at.strftime("%d-%m-%Y %H:%M"))
    y -= line_height

    customer_name = sale.customer.name if sale.customer else "Walk-in Customer"
    p.drawString(10, y, f"Customer: {customer_name}")
    y -= line_height

    p.line(10, y, paper_width - 10, y)
    y -= line_height

    # ---------- ITEMS ----------
    p.setFont("Helvetica", 9)

    for item in items:
        p.drawString(10, y, item.product.name)
        y -= line_height

        p.drawString(
            10,
            y,
            f"{item.quantity} x {item.price} = {item.price * item.quantity}"
        )
        y -= line_height

    p.line(10, y, paper_width - 10, y)
    y -= line_height

    # ---------- TOTALS ----------
    p.drawString(10, y, f"Subtotal: Rs {subtotal}")
    y -= line_height

    p.drawString(10, y, f"Discount ({discount_rate}%): -Rs {discount_amount}")
    y -= line_height

    p.drawString(10, y, f"Tax ({tax_rate}%): Rs {tax_amount}")
    y -= line_height

    p.setFont("Helvetica-Bold", 11)
    p.drawString(10, y, f"TOTAL: Rs {grand_total}")
    y -= line_height * 2

    # ---------- FOOTER ----------
    p.setFont("Helvetica", 8)
    p.drawCentredString(paper_width / 2, y, "Thank you for your purchase!")
    y -= line_height

    p.drawCentredString(paper_width / 2, y, "Powered by POS IMS")

    p.showPage()
    p.save()

    return response
    
    sale = get_object_or_404(Sale, id=sale_id)
    items = sale.items.all()

    # -------- Calculations (DISPLAY ONLY) --------
    subtotal = Decimal("0")
    for item in items:
        subtotal += item.price * item.quantity

    discount_rate = sale.discount_rate or Decimal("0")
    tax_rate = sale.tax_rate or Decimal("0")

    discount_amount = subtotal * (discount_rate / Decimal("100"))
    after_discount = subtotal - discount_amount
    tax_amount = after_discount * (tax_rate / Decimal("100"))

    grand_total = sale.total_amount  # ✅ SOURCE OF TRUTH
    # --------------------------------------------

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="invoice_{sale.id}.pdf"'

    p = canvas.Canvas(response, pagesize=A4)
    width, height = A4
    y = height - 50

    # ---------- HEADER ----------
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, y, "INVOICE")
    y -= 30

    p.setFont("Helvetica", 10)
    p.drawString(50, y, f"Invoice #: {sale.id}")
    y -= 15
    p.drawString(50, y, f"Date: {sale.created_at.strftime('%d-%m-%Y %H:%M')}")

    customer_name = sale.customer.name if sale.customer else "Walk-in Customer"
    y -= 15
    p.drawString(50, y, f"Customer: {customer_name}")
    y -= 30

    # ---------- TABLE HEADER ----------
    p.setFont("Helvetica-Bold", 10)
    p.drawString(50, y, "Product")
    p.drawString(250, y, "Qty")
    p.drawString(300, y, "Price")
    p.drawString(370, y, "Total")
    y -= 15

    p.setFont("Helvetica", 10)

    # ---------- ITEMS ----------
    for item in items:
        p.drawString(50, y, item.product.name)
        p.drawString(250, y, str(item.quantity))
        p.drawString(300, y, f"Rs {item.price}")
        p.drawString(370, y, f"Rs {item.price * item.quantity}")
        y -= 15

        if y < 120:
            p.showPage()
            y = height - 50

    # ---------- SUMMARY ----------
    y -= 20
    p.setFont("Helvetica", 10)
    p.drawString(300, y, "Subtotal:")
    p.drawString(400, y, f"Rs {subtotal}")

    y -= 15
    p.drawString(300, y, f"Discount ({discount_rate}%):")
    p.drawString(400, y, f"- Rs {discount_amount}")

    y -= 15
    p.drawString(300, y, f"Tax ({tax_rate}%):")
    p.drawString(400, y, f"Rs {tax_amount}")

    y -= 20
    p.setFont("Helvetica-Bold", 12)
    p.drawString(300, y, "Grand Total:")
    p.drawString(400, y, f"Rs {grand_total}")

    p.showPage()
    p.save()

    return response
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
