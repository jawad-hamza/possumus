from django.contrib import admin
from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path("products/", views.product_list, name="product_list"),
    path("products/add/", views.product_add, name="product_add"),
    path("products/<int:pk>/", views.product_view, name="product_view"),
    path("products/<int:pk>/edit/", views.product_edit, name="product_edit"),
    path("products/<int:pk>/delete/", views.product_delete, name="product_delete"),
    path("pos/ajax/scan/<str:sku>/",views.scan_barcode,name="scan_barcode"),
    path("test/", views.test, name="test"),

    path("dashboard/", views.dashboard, name="dashboard"),
    path("pos/", views.pos_screen, name="pos_screen"),
    path("invoice/<int:sale_id>/", views.invoice, name="invoice"),
    path("pos/add-customer/", views.add_customer_from_pos, name="add_customer_from_pos"),
    path("reports/daily-sales/", views.daily_sales_report, name="daily_sales_report"),
    path("invoice/<int:sale_id>/pdf/", views.invoice_pdf, name="invoice_pdf"),
    path("pos/ajax/category/<int:category_id>/products/", views.products_by_category, name="products_by_category"),






]
