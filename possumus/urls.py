from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path

from accounts.receipt_views import serve_public_media


urlpatterns = [
    path("admin/", admin.site.urls),
    # Review notes: the Comment tool's API and the owner's private list of notes.
    path("review/", include("review.urls")),
    path("", include("pos.urls")),
    path("distribution/", include("distribution.urls")),
    # Not "pricing/": the public plan checkout pages already live there.
    path("trade/", include("pricing.urls")),
    path("api/", include("api.urls")),
]

# The installed desktop copy has no web server in front of it, so Django hands out uploads itself.
# Hosted deployments serve MEDIA_ROOT from nginx or keep media in S3-compatible storage.
if settings.SERVE_MEDIA_FILES:
    media_prefix = settings.MEDIA_URL.strip("/")
    urlpatterns.insert(
        0,
        re_path(
            rf"^{media_prefix}/(?P<path>.*)$",
            # Everything under MEDIA_ROOT except bank receipts, which need a sign-in and an access check.
            serve_public_media,
            {"document_root": settings.MEDIA_ROOT},
        ),
    )
