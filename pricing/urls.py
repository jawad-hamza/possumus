"""URLs of the pricing app. Included from the project urls under trade/."""
from django.urls import path

from . import views

urlpatterns = [
    path("price-lists/", views.price_list_list, name="pricing_price_list_list"),
    path("price-lists/new/", views.price_list_add, name="pricing_price_list_add"),
    path("price-lists/<int:pk>/", views.price_list_detail, name="pricing_price_list_detail"),
    path("price-lists/<int:pk>/edit/", views.price_list_edit, name="pricing_price_list_edit"),
    path("schemes/", views.scheme_list, name="pricing_scheme_list"),
    path("schemes/new/", views.scheme_add, name="pricing_scheme_add"),
    path("schemes/<int:pk>/edit/", views.scheme_edit, name="pricing_scheme_edit"),
    path("schemes/<int:pk>/switch/", views.scheme_toggle, name="pricing_scheme_toggle"),
]
