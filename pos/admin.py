from urllib import request
from django.contrib import admin
from .models import Product,Sale,SaleItem, Customer, Supplier , Purchase, PurchaseItem, Category
# Register your models here.

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "sku","barcode", "category", "supplier", "price", "stock", "is_active")
    list_filter = ("supplier", "is_active")
    search_fields = ("name", "sku", "barcode")
    
@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "created_at", "total_amount")
    
@admin.register(SaleItem)
class SaleItemAdmin(admin.ModelAdmin):
    list_display = ("sale", "product", "quantity", "price")
    def get_actions(self, request):
        actions = super().get_actions(request)
        if "delete_selected" in actions:
            del actions["delete_selected"]
        return actions
    
    
@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email", "is_active")
    search_fields = ("name", "phone")
    list_filter = ("is_active",)
    
@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email", "address","is_active")
    search_fields = ("name", "phone")
    list_filter = ("is_active",)
    
@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("id", "supplier", "created_at", "total_amount")
    list_filter = ("supplier",)
    
@admin.register(PurchaseItem)
class PurchaseItemAdmin(admin.ModelAdmin):
    list_display = ("purchase", "product", "quantity", "cost_price")
    def get_actions(self, request):
        actions = super().get_actions(request)
        if "delete_selected" in actions:
            del actions["delete_selected"]
        return actions


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    


