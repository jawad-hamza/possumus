from urllib import request
from django.contrib import admin
from .models import Product,Sale,SaleItem, Customer, Supplier , Purchase, PurchaseItem, Category, LedgerEntry, CompanyProfile
from django.contrib.contenttypes.models import ContentType
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
    


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        'party_name',
        'party_type',
        'entry_type',
        'amount',
        'reference',
        'created_at',
    )

    list_filter = (
        'entry_type',
        'reference',
        'content_type',
        'created_at',
    )

    search_fields = (
        'note',
        'reference',
        'object_id',
    )

    date_hierarchy = 'created_at'

    ordering = ('-created_at',)

    readonly_fields = ('created_at',)

    fieldsets = (
        ("Party", {
            "fields": ("content_type", "object_id")
        }),
        ("Transaction", {
            "fields": ("entry_type", "amount", "reference", "note")
        }),
        ("Meta", {
            "fields": ("created_at",)
        }),
    )

    # -----------------------------
    # Custom display helpers
    # -----------------------------
    def party_name(self, obj):
        return str(obj.party)
    party_name.short_description = "Party"

    def party_type(self, obj):
        return obj.content_type.model.capitalize()
    party_type.short_description = "Type"
    
    
@admin.register(CompanyProfile)
class CompanyProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "phone", "email")

    fieldsets = (
        ("Company Info", {
            "fields": ("name", "logo", "address", "phone", "email")
        }),
        ("Invoice Settings", {
            "fields": ("invoice_footer", "show_qr", "qr_note")
        }),
        ("Status", {
            "fields": ("is_active",)
        }),
    )
    
