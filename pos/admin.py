from django.contrib import admin

from .models import (
    Brand,
    Category,
    CompanyProfile,
    Customer,
    LedgerEntry,
    License,
    Product,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Sale,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
    StockAdjustment,
    Supplier,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Look, don't touch.

    Sales, purchases, returns and stock adjustments move stock and post vouchers when they
    are saved through the app. Editing or deleting them here would skip both, leaving the
    shelf count and the ledger disagreeing with the documents, so the admin only displays them.
    """

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SaleItemInline(ReadOnlyInline):
    model = SaleItem


class PurchaseItemInline(ReadOnlyInline):
    model = PurchaseItem


class SaleReturnItemInline(ReadOnlyInline):
    model = SaleReturnItem


class PurchaseReturnItemInline(ReadOnlyInline):
    model = PurchaseReturnItem


@admin.register(License)
class LicenseAdmin(ReadOnlyAdmin):
    """For support only: access is decided by re-verifying the signed key, never by these columns."""

    list_display = ("licensee", "plan", "issued_at", "expires_at", "max_users", "is_active", "activated_at")
    search_fields = ("licensee", "email", "license_id")
    exclude = ("key",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "brand", "sku", "barcode", "category", "supplier", "unit", "price", "stock", "track_stock", "low_stock_threshold", "location", "is_active")
    list_filter = ("brand", "supplier", "is_active")
    search_fields = ("name", "sku", "barcode")
    # Stock only changes through purchases, sales, returns and stock adjustments, which keep the ledger in step.
    readonly_fields = ("stock",)


@admin.register(Sale)
class SaleAdmin(ReadOnlyAdmin):
    list_display = ("id", "customer", "sold_by", "payment_method", "is_credit", "created_at", "total_amount", "paid_amount")
    list_filter = ("payment_method", "is_credit")
    date_hierarchy = "created_at"
    inlines = [SaleItemInline]


@admin.register(SaleItem)
class SaleItemAdmin(ReadOnlyAdmin):
    list_display = ("sale", "product", "quantity", "price", "discount_percent")
    search_fields = ("product__name", "product__sku")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email", "is_active")
    search_fields = ("name", "phone")
    list_filter = ("is_active",)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email", "address", "is_active")
    search_fields = ("name", "phone")
    list_filter = ("is_active",)


@admin.register(Purchase)
class PurchaseAdmin(ReadOnlyAdmin):
    list_display = ("id", "supplier", "payment_method", "created_at", "total_amount", "paid_amount")
    list_filter = ("supplier", "payment_method")
    date_hierarchy = "created_at"
    inlines = [PurchaseItemInline]


@admin.register(PurchaseItem)
class PurchaseItemAdmin(ReadOnlyAdmin):
    list_display = ("purchase", "product", "quantity", "cost_price", "sale_price")
    search_fields = ("product__name", "product__sku")


@admin.register(SaleReturn)
class SaleReturnAdmin(ReadOnlyAdmin):
    list_display = ("id", "sale", "customer", "refund_method", "total_amount", "reason", "created_by", "created_at")
    list_filter = ("refund_method",)
    date_hierarchy = "created_at"
    inlines = [SaleReturnItemInline]


@admin.register(SaleReturnItem)
class SaleReturnItemAdmin(ReadOnlyAdmin):
    list_display = ("sale_return", "product", "quantity", "unit_price", "line_amount")


@admin.register(PurchaseReturn)
class PurchaseReturnAdmin(ReadOnlyAdmin):
    list_display = ("id", "purchase", "supplier", "settlement_method", "total_amount", "reason", "created_by", "created_at")
    list_filter = ("settlement_method",)
    date_hierarchy = "created_at"
    inlines = [PurchaseReturnItemInline]


@admin.register(PurchaseReturnItem)
class PurchaseReturnItemAdmin(ReadOnlyAdmin):
    list_display = ("purchase_return", "product", "quantity", "line_amount")


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(ReadOnlyAdmin):
    list_display = ("id", "product", "adjustment_mode", "quantity_before", "quantity_delta", "quantity_after", "reason", "created_by", "created_at")
    list_filter = ("adjustment_mode",)
    search_fields = ("product__name", "product__sku", "reason")
    date_hierarchy = "created_at"


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "party_name",
        "party_type",
        "entry_type",
        "amount",
        "reference",
        "created_at",
    )
    list_filter = (
        "entry_type",
        "reference",
        "content_type",
        "created_at",
    )
    search_fields = (
        "note",
        "reference",
        "object_id",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)
    fieldsets = (
        ("Party", {"fields": ("content_type", "object_id")}),
        ("Transaction", {"fields": ("entry_type", "amount", "reference", "note")}),
        ("Meta", {"fields": ("created_at",)}),
    )

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
        ("Company Info", {"fields": ("name", "logo", "address", "phone", "email")}),
        ("Invoice Settings", {"fields": ("invoice_footer", "show_qr", "qr_note")}),
        ("Status", {"fields": ("is_active",)}),
    )


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    search_fields = ("name",)
    list_filter = ("is_active",)
