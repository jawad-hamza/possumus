"""URLs of routes, order bookers and the customer edit page. Included from pos.urls at the site root."""
from django.urls import path

from .views import field_sales as views

urlpatterns = [
    path("parties/customer/<int:id>/edit/", views.customer_edit, name="customer_edit"),
    path("parties/supplier/<int:id>/edit/", views.supplier_edit, name="supplier_edit"),
    path("parties/routes/", views.route_list, name="route_list"),
    path("parties/routes/add/", views.route_add, name="route_add"),
    path("parties/routes/<int:pk>/edit/", views.route_edit, name="route_edit"),
    path("parties/order-bookers/", views.order_booker_list, name="order_booker_list"),
    path("parties/order-bookers/add/", views.order_booker_add, name="order_booker_add"),
    path("parties/order-bookers/<int:pk>/edit/", views.order_booker_edit, name="order_booker_edit"),
    path("ajax/quick/routes/", views.quick_create_route, name="quick_create_route"),
    path("ajax/quick/order-bookers/", views.quick_create_order_booker, name="quick_create_order_booker"),
]
