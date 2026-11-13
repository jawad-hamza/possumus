from django.conf import settings
from django.db import models


class ReviewNote(models.Model):
    """A comment pinned to one spot of one screen.

    The screen is the page path plus its (normalised) query string; the anchor says where on
    the screen: a CSS selector, the element's own words and the click point inside it, so the
    dot can find its element again after the page redraws.
    """

    author_name = models.CharField(max_length=80)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="review_notes",
    )
    text = models.TextField()
    page_path = models.CharField(max_length=500, db_index=True)
    page_query = models.CharField(max_length=500, blank=True, default="")
    screen_label = models.CharField(max_length=200, blank=True, default="")
    anchor = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    resolved = models.BooleanField(default=False, db_index=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_review_notes",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = (models.Index(fields=["page_path", "page_query", "resolved"], name="review_note_screen_idx"),)

    def __str__(self):
        return f"{self.author_name} on {self.page_path}: {self.text[:40]}"

    @property
    def page_url(self):
        return self.page_path + (f"?{self.page_query}" if self.page_query else "")


class ReviewSwitch(models.Model):
    """The owner's own on/off for the comment tool. One row; "server" follows REVIEW_NOTES_ENABLED."""

    MODE_SERVER = "server"
    MODE_ON = "on"
    MODE_OFF = "off"
    MODE_CHOICES = (
        (MODE_SERVER, "Follow the server setting"),
        (MODE_ON, "On"),
        (MODE_OFF, "Off"),
    )

    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default=MODE_SERVER)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "review tool switch"

    def __str__(self):
        return f"Review tool: {self.get_mode_display()}"

    @classmethod
    def current_mode(cls):
        return cls.objects.filter(pk=1).values_list("mode", flat=True).first() or cls.MODE_SERVER

    @classmethod
    def set_mode(cls, mode, user=None):
        if mode not in dict(cls.MODE_CHOICES):
            raise ValueError("Unknown review tool mode.")
        switch, _ = cls.objects.get_or_create(pk=1)
        switch.mode = mode
        switch.updated_by = user if getattr(user, "is_authenticated", False) else None
        switch.save()
        return switch
