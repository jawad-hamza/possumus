from django.shortcuts import render
from django.http import HttpResponse
from .models import Product
from django.db.models import Sum
from .models import Sale, Product

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