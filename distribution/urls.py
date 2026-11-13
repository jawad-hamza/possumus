"""URLs of the distribution app. Included from the project urls under distribution/."""
from django.urls import path
from django.shortcuts import redirect

from .views import loads, masters, orders, recovery, returns
from .views.common import distribution_access


@distribution_access()
def home(request):
    return redirect("distribution_order_list")


urlpatterns = [
    path("", home, name="distribution_home"),
    # orders
    path("orders/", orders.order_list, name="distribution_order_list"),
    path("orders/new/", orders.order_add, name="distribution_order_add"),
    path("orders/invoice/", orders.order_bulk_invoice, name="distribution_order_bulk_invoice"),
    path("orders/<int:pk>/", orders.order_detail, name="distribution_order_detail"),
    path("orders/<int:pk>/edit/", orders.order_edit, name="distribution_order_edit"),
    path("orders/<int:pk>/invoice/", orders.order_invoice, name="distribution_order_invoice"),
    path("orders/<int:pk>/cancel/", orders.order_cancel, name="distribution_order_cancel"),
    path("ajax/customer/<int:pk>/", orders.customer_terms_json, name="distribution_customer_terms"),
    # loads
    path("loads/", loads.load_list, name="distribution_load_list"),
    path("loads/new/", loads.load_add, name="distribution_load_add"),
    path("loads/<int:pk>/", loads.load_detail, name="distribution_load_detail"),
    path("loads/<int:pk>/edit/", loads.load_edit, name="distribution_load_edit"),
    path("loads/<int:pk>/invoices/", loads.load_add_invoices, name="distribution_load_add_invoices"),
    path("loads/<int:pk>/invoices/<int:sale_id>/remove/", loads.load_remove_invoice, name="distribution_load_remove_invoice"),
    path("loads/<int:pk>/dispatch/", loads.load_dispatch, name="distribution_load_dispatch"),
    path("loads/<int:pk>/reopen/", loads.load_reopen, name="distribution_load_reopen"),
    path("loads/<int:pk>/print/", loads.load_print, name="distribution_load_print"),
    path("loads/<int:pk>/settle/", loads.load_settle, name="distribution_load_settle"),
    # stock returns
    path("stock-returns/", returns.return_list, name="distribution_return_list"),
    path("stock-returns/new/<str:kind>/", returns.return_add, name="distribution_return_add"),
    path("stock-returns/<int:pk>/", returns.return_detail, name="distribution_return_detail"),
    # recovery
    path("recovery/", recovery.recovery_entry, name="distribution_recovery"),
    path("recovery/list/", recovery.recovery_list, name="distribution_recovery_list"),
    # masters
    path("delivery-men/", masters.delivery_man_list, name="distribution_delivery_man_list"),
    path("delivery-men/add/", masters.delivery_man_add, name="distribution_delivery_man_add"),
    path("delivery-men/<int:pk>/edit/", masters.delivery_man_edit, name="distribution_delivery_man_edit"),
    path("vehicles/", masters.vehicle_list, name="distribution_vehicle_list"),
    path("vehicles/add/", masters.vehicle_add, name="distribution_vehicle_add"),
    path("vehicles/<int:pk>/edit/", masters.vehicle_edit, name="distribution_vehicle_edit"),
    path("ajax/quick/delivery-men/", masters.quick_delivery_man, name="distribution_quick_delivery_man"),
    path("ajax/quick/vehicles/", masters.quick_vehicle, name="distribution_quick_vehicle"),
    path("ajax/quick/customers/", masters.quick_customer, name="distribution_quick_customer"),
]
