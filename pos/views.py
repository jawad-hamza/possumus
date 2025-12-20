from django.shortcuts import render
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from .models import Product, Sale, SaleItem , Customer
from django.db.models import Sum
from .forms import CustomerForm
from datetime import datetime
from django.utils import timezone
from reportlab.lib.pagesizes import A4
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
    total_sales = Sale.objects.aggregate(
        total=Sum("total_amount")
    )["total"] or 0

    low_stock_products = Product.objects.filter(
        stock__lte=5,
        is_active=True
    )

    return render(
        request,
        "pos/dashboard.html",
        {
            "total_sales": total_sales,
            "low_stock_products": low_stock_products,
        }
    )
    


def pos_screen(request):
    cart = request.session.get("cart", {})

    # Always initialize safely
    action = None
    product_id = None

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
                    "price": float(product.price),
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
                    del cart[product_id]

                request.session["cart"] = cart
            return redirect("pos_screen")

        # =========================
        # REMOVE ALL
        # =========================
        if action == "remove_all" and product_id:
            if product_id in cart:
                del cart[product_id]
                request.session["cart"] = cart
            return redirect("pos_screen")

        # =========================
        # CHECKOUT
        # =========================
        if action == "checkout":
            if not cart:
                return redirect("pos_screen")

            customer_id = request.POST.get("customer_id")

            # ---------- GST / TAX (ROBUST) ----------
            custom_tax = (request.POST.get("custom_tax_rate") or "").strip()
            slab_tax = (request.POST.get("tax_rate") or "").strip()

            tax_raw = custom_tax if custom_tax else slab_tax

            try:
                tax_rate = Decimal(tax_raw)
            except (InvalidOperation, TypeError):
                tax_rate = Decimal("0")

            # Safety limits
            if tax_rate < 0:
                tax_rate = Decimal("0")
            if tax_rate > 100:
                tax_rate = Decimal("100")
            # ----------------------------------------

            # Create Sale
            if customer_id:
                sale = Sale.objects.create(
                    customer_id=customer_id,
                    tax_rate=tax_rate
                )
            else:
                sale = Sale.objects.create(
                    tax_rate=tax_rate
                )

            # Create SaleItems (stock deducted by model)
            for pid, item in cart.items():
                product = get_object_or_404(Product, id=pid)
                SaleItem.objects.create(
                    sale=sale,
                    product=product,
                    quantity=item["quantity"],
                    price=item["price"],
                )

            # Clear cart
            request.session["cart"] = {}

            # Redirect to invoice
            return redirect("invoice", sale_id=sale.id)

    # =========================
    # GET REQUEST (DISPLAY)
    # =========================

    products = Product.objects.filter(is_active=True)
    customers = Customer.objects.filter(is_active=True)

    # Cart totals (display only)
    cart_total = sum(
        Decimal(item["price"]) * item["quantity"]
        for item in cart.values()
    )

    tax_amount = Decimal("0")   # real tax shown on invoice
    discount = Decimal("0")
    grand_total = cart_total + tax_amount - discount

    return render(
        request,
        "pos/pos_screen.html",
        {
            "products": products,
            "cart": cart,
            "customers": customers,
            "cart_total": cart_total,
            "tax_amount": tax_amount,
            "discount": discount,
            "grand_total": grand_total,
        }
    )
     
def invoice_view(request, sale_id):
    sale = get_object_or_404(Sale, id=sale_id)
    items = sale.items.all()

    return render(
        request,
        "pos/invoice.html",
        {
            "sale": sale,
            "items": items,
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

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="invoice_{sale.id}.pdf"'

    p = canvas.Canvas(response, pagesize=A4)
    width, height = A4

    y = height - 50

    # Header
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, y, "INVOICE")
    y -= 30

    p.setFont("Helvetica", 10)
    p.drawString(50, y, f"Invoice #: {sale.id}")
    y -= 15
    p.drawString(50, y, f"Date: {sale.created_at.strftime('%d-%m-%Y %H:%M')}")
    y -= 15

    customer_name = sale.customer.name if sale.customer else "Walk-in Customer"
    p.drawString(50, y, f"Customer: {customer_name}")
    y -= 30

    # Table header
    p.setFont("Helvetica-Bold", 10)
    p.drawString(50, y, "Product")
    p.drawString(250, y, "Qty")
    p.drawString(300, y, "Price")
    p.drawString(370, y, "Total")
    y -= 15

    p.setFont("Helvetica", 10)

    for item in items:
        p.drawString(50, y, item.product.name)
        p.drawString(250, y, str(item.quantity))
        p.drawString(300, y, f"Rs {item.price}")
        p.drawString(370, y, f"Rs {item.price * item.quantity}")
        y -= 15

        if y < 100:
            p.showPage()
            y = height - 50

    # Grand total
    y -= 20
    p.setFont("Helvetica-Bold", 12)
    p.drawString(300, y, "Grand Total:")
    p.drawString(400, y, f"Rs {sale.total_amount}")

    p.showPage()
    p.save()

    return response