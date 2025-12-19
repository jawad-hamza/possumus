from urllib import request
from django.contrib import admin
from .models import Product,Sale,SaleItem
# Register your models here.

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "price", "stock", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "sku")
    
@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "total_amount")
    
@admin.register(SaleItem)
class SaleItemAdmin(admin.ModelAdmin):
    list_display = ("sale", "product", "quantity", "price")
    def get_actions(self, request):
        actions = super().get_actions(request)
        if "delete_selected" in actions:
            del actions["delete_selected"]
        return actions