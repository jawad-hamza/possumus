from django import forms
from .models import Category, Customer, Product , Supplier , Brand


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "phone", "email"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
        }

class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "phone", "email" , "address"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "brand",
            "sku",
            "category",
            "supplier",
            "price",
            "stock",
            "low_stock_threshold",
            "location",
            "is_active",
        ]
        labels = {
            'location': 'Storage Location',
        }
        help_texts = {
            'location': 'Example: Warehouse A / Rack 3',
        }

class BrandForm(forms.ModelForm):
    class Meta:
        model = Brand
        fields = [
            "name",
            "is_active",
        ]
        
class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = [
            "name",
            "is_active",
        ]