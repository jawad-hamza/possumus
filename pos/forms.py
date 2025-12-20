from django import forms
from .models import Customer, Product


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "phone", "email"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
        }

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "sku",
            "category",
            "supplier",
            "price",
            "stock",
            "is_active",
        ]