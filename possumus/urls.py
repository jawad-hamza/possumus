from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve as serve_media_file


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("pos.urls")),
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
            serve_media_file,
            {"document_root": settings.MEDIA_ROOT},
        ),
    )
