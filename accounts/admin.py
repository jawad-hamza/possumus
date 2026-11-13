from django import forms
from django.contrib import admin

from .models import (
    Account,
    AccountingSettings,
    BankReceipt,
    LoanTransactions,
    Loans,
    PartyLedgerEntry,
    Settlement,
    Voucher,
    VoucherLine,
)
from .services.books_lock import assert_books_open
from .services.settlements import post_settlement


class ReadOnlyAdminMixin:
    """
    The ledger is written only by the posting services. Editing it by hand
    here would bypass balancing, the party sub-ledger and the books lock.
    """

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class VoucherLineInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = VoucherLine
    extra = 0
    can_delete = False


class PartyLedgerEntryInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = PartyLedgerEntry
    extra = 0
    can_delete = False
    fields = ("content_type", "object_id", "debit", "credit", "created_at")


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "opening_balance", "is_active")
    list_filter = ("type", "is_active")
    search_fields = ("title",)

    def has_delete_permission(self, request, obj=None):
        # Accounts with postings are PROTECTed anyway; deactivate instead of deleting.
        return False


@admin.register(Voucher)
class VoucherAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "date", "voucher_type", "total_debit", "total_credit", "ref_model", "ref_id", "created_by")
    list_filter = ("voucher_type", "date")
    search_fields = ("narration", "ref_model", "=ref_id")
    date_hierarchy = "date"
    inlines = [VoucherLineInline, PartyLedgerEntryInline]


@admin.register(VoucherLine)
class VoucherLineAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "voucher", "account", "debit", "credit")
    list_filter = ("account__type",)
    search_fields = ("account__title", "voucher__narration", "=voucher__id")
    list_select_related = ("voucher", "account")


@admin.register(PartyLedgerEntry)
class PartyLedgerEntryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("id", "party", "voucher", "debit", "credit", "created_at")
    list_filter = ("content_type", "created_at")
    search_fields = ("=object_id", "voucher__narration")
    list_select_related = ("voucher", "content_type")


@admin.register(AccountingSettings)
class AccountingSettingsAdmin(admin.ModelAdmin):
    list_display = ("__str__", "books_locked_until", "locked_by", "updated_at")
    readonly_fields = ("locked_by", "updated_at")

    def has_add_permission(self, request):
        return not AccountingSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.locked_by = request.user if obj.books_locked_until else None
        super().save_model(request, obj, form, change)


class LoanTransactionInline(admin.TabularInline):
    model = LoanTransactions
    extra = 1
    readonly_fields = ("voucher", "created_at")


@admin.register(Loans)
class LoanAdmin(admin.ModelAdmin):
    list_display = ("id", "party", "loan_type", "start_date", "is_closed", "balance")
    list_filter = ("loan_type", "is_closed")
    search_fields = ("notes", "=object_id")


@admin.register(LoanTransactions)
class LoanTransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "loan", "date", "tx_type", "payment_method", "amount", "voucher")
    list_filter = ("tx_type", "payment_method", "date")
    search_fields = ("note", "loan__notes", "=loan__id")
    readonly_fields = ("voucher",)


class SettlementAdminForm(forms.ModelForm):
    class Meta:
        model = Settlement
        fields = "__all__"

    def clean_date(self):
        value = self.cleaned_data.get("date")
        try:
            assert_books_open(value, action="record a settlement")
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return value


@admin.register(Settlement)
class SettlementAdmin(admin.ModelAdmin):
    form = SettlementAdminForm
    actions = None
    list_display = ("id", "date", "settlement_type", "party", "payment_method", "amount", "voucher")
    list_filter = ("settlement_type", "payment_method", "date")
    search_fields = ("=object_id", "notes")
    readonly_fields = ("voucher",)

    def has_change_permission(self, request, obj=None):
        # A posted settlement already moved money in the ledger; editing the
        # document afterwards would leave the voucher saying something else.
        if obj is not None and obj.voucher_id:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.voucher_id:
            return False
        return super().has_delete_permission(request, obj)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not obj.voucher_id:
            post_settlement(obj, actor=request.user)


@admin.register(BankReceipt)
class BankReceiptAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Every bank receipt ever attached. A replaced or removed one stays listed with removed_at set."""

    list_display = ("id", "payment", "original_name", "mime_type", "size_bytes", "uploaded_by", "uploaded_at", "removed_by", "removed_at")
    list_filter = ("content_type", "mime_type", "uploaded_at")
    search_fields = ("=object_id", "original_name", "=sha256")
    list_select_related = ("content_type", "uploaded_by", "removed_by")
