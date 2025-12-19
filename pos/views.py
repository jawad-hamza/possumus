from django.shortcuts import render
from django.http import HttpResponse
from .models import Product

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
