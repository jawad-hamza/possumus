from django.contrib import admin

from .models import PriceList, PriceListItem, Scheme


class PriceListItemInline(admin.TabularInline):
    model = PriceListItem
    extra = 0
    raw_id_fields = ("product",)


@admin.register(PriceList)
class PriceListAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active")
    inlines = [PriceListItemInline]


@admin.register(Scheme)
class SchemeAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "start_date", "end_date", "scope", "reward")
    list_filter = ("is_active", "reward", "scope")
