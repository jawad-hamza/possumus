from django.contrib import admin

from .models import ReviewNote, ReviewSwitch


@admin.register(ReviewNote)
class ReviewNoteAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "author_name", "screen_label", "page_path", "resolved")
    list_filter = ("resolved",)
    search_fields = ("text", "author_name", "page_path", "screen_label")
    readonly_fields = ("created_at",)


@admin.register(ReviewSwitch)
class ReviewSwitchAdmin(admin.ModelAdmin):
    list_display = ("mode", "updated_at", "updated_by")
