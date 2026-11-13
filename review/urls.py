from django.urls import path

from . import views

urlpatterns = [
    path("notes/", views.review_notes, name="review_notes"),
    path("api/notes/", views.notes_api, name="review_notes_api"),
    path("api/notes/<int:pk>/", views.note_api, name="review_note_api"),
    path("api/notes/<int:pk>/resolve/", views.note_resolve_api, name="review_note_resolve_api"),
]
