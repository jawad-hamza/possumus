from django.contrib import admin

from .models import DeliveryMan, LoadForm, Recovery, SalesOrder, StockReturn, Vehicle


@admin.register(DeliveryMan)
class DeliveryManAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "is_active")


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("name", "number", "is_active")


@admin.register(SalesOrder)
class SalesOrderAdmin(admin.ModelAdmin):
    list_display = ("display_number", "customer", "order_date", "status")
    list_filter = ("status",)
    readonly_fields = ("sale",)


@admin.register(LoadForm)
class LoadFormAdmin(admin.ModelAdmin):
    list_display = ("display_number", "load_date", "route", "delivery_man", "status")
    list_filter = ("status",)


@admin.register(StockReturn)
class StockReturnAdmin(admin.ModelAdmin):
    list_display = ("display_number", "kind", "return_date", "total_amount")
    list_filter = ("kind",)


@admin.register(Recovery)
class RecoveryAdmin(admin.ModelAdmin):
    list_display = ("customer", "amount", "payment_method", "collected_on", "collector_name")
