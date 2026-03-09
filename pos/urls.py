from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from . import views


urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("setup/admin/", views.setup_admin, name="setup_admin"),
    path("settings/", views.settings_page, name="settings_page"),
    path("test/", views.test, name="test"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("pos/", views.pos_screen, name="pos_screen"),
    path("pos/ajax/scan/<str:sku>/", views.scan_barcode, name="scan_barcode"),
    path("pos/ajax/category/<int:category_id>/products/", views.products_by_category, name="products_by_category"),
    path("ajax/quick/brands/", views.quick_create_brand, name="quick_create_brand"),
    path("ajax/quick/categories/", views.quick_create_category, name="quick_create_category"),
    path("ajax/quick/suppliers/", views.quick_create_supplier, name="quick_create_supplier"),
    path("ajax/quick/products/", views.quick_create_product, name="quick_create_product"),
    path("invoice/<int:sale_id>/", views.invoice, name="invoice"),
    path("invoice/<int:sale_id>/pdf/", views.invoice_pdf, name="invoice_pdf"),
    path("invoice/<int:sale_id>/return/", views.sale_return_view, name="sale_return"),
    path("reports/", views.reports_home, name="reports_home"),
    path("reports/daily-sales/", views.daily_sales_report, name="daily_sales_report"),
    path("reports/low-stock/", views.low_stock_list, name="low_stock_list"),
    path("products/", views.product_list, name="product_list"),
    path("products/add/", views.product_add, name="product_add"),
    path("products/<int:pk>/", views.product_view, name="product_view"),
    path("products/<int:pk>/edit/", views.product_edit, name="product_edit"),
    path("products/<int:pk>/delete/", views.product_delete, name="product_delete"),
    path("inventory/adjustments/", views.stock_adjustments, name="stock_adjustments"),
    path("brands/add/", views.brand_add, name="brand_add"),
    path("categories/add/", views.category_add, name="category_add"),
    path("categories/<int:pk>/edit/", views.category_edit, name="category_edit"),
    path("purchases/", views.purchase_list, name="purchase_list"),
    path("purchases/add/", views.purchase_add, name="purchase_add"),
    path("purchases/<int:pk>/", views.purchase_detail, name="purchase_detail"),
    path("purchases/<int:pk>/return/", views.purchase_return_view, name="purchase_return"),
    path("purchases/<int:pk>/edit/", views.purchase_edit, name="purchase_edit"),
    path("pos/add-customer/", views.add_customer_from_pos, name="add_customer_from_pos"),
    path("pos/add-supplier/", views.add_supplier_from_pos, name="add_supplier_from_pos"),
    path("parties/", views.parties_home, name="parties_home"),
    path("parties/customers/", views.customer_list, name="customer_list"),
    path("parties/customers/add/", views.add_customer_from_pos, name="customer_add"),
    path("parties/customer/<int:id>/", views.customer_detail, name="customer_detail"),
    path("parties/suppliers/", views.supplier_list, name="supplier_list"),
    path("parties/suppliers/add/", views.add_supplier_from_pos, name="supplier_add"),
    path("parties/supplier/<int:id>/", views.supplier_detail, name="supplier_detail"),
    path("parties/statement/", views.party_statement_page, name="party_statement_page"),
    path("accounts/", include("accounts.urls")),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

