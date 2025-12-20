from django.contrib import admin
from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path("products/", views.product_list, name="product_list"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("pos/", views.pos_screen, name="pos_screen"),
    path("invoice/<int:sale_id>/", views.invoice, name="invoice"),
    path("pos/add-customer/", views.add_customer_from_pos, name="add_customer_from_pos"),
    path("reports/daily-sales/", views.daily_sales_report, name="daily_sales_report"),
    path("invoice/<int:sale_id>/pdf/", views.invoice_pdf, name="invoice_pdf"),
    path("pos/ajax/category/<int:category_id>/products/", views.products_by_category, name="products_by_category"),






]
