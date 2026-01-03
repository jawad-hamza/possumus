from django.contrib import admin
from .models import Account, Voucher, VoucherLine, Loans, LoanTransactions


class VoucherLineInline(admin.TabularInline):
    model = VoucherLine
    extra = 2


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "opening_balance", "is_active")
    list_filter = ("type", "is_active")
    search_fields = ("title",)


@admin.register(Voucher)
class VoucherAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "voucher_type", "total_debit", "total_credit", "ref_model", "ref_id")
    list_filter = ("voucher_type", "date")
    search_fields = ("narration", "ref_model")
    inlines = [VoucherLineInline]

class LoanTransactionInline(admin.TabularInline):
    model = LoanTransactions
    extra = 1
    readonly_fields = ("voucher", "created_at")


@admin.register(Loans)
class LoanAdmin(admin.ModelAdmin):
    list_display = ("id", "party", "loan_type", "start_date", "is_closed", "balance")
    list_filter = ("loan_type", "is_closed")


@admin.register(LoanTransactions)
class LoanTransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "loan", "date", "tx_type", "payment_method", "amount", "voucher")
    list_filter = ("tx_type", "payment_method", "date")
    search_fields = ("loan__party_name",)