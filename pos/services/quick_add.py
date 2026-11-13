"""Add a supporting record (brand, category, supplier...) in a side panel without leaving the form.

quick_add_context() hands a page the small forms its panel shows, only for the kinds the
signed-in user may create. Each form posts to a JSON endpoint that uses the same permission
and validation as the full page; the page's return-flow.js puts the new record into the right
select, selects it and gives focus back to that field.
"""
from django.urls import reverse

from .access import ACCESS_MANAGE, is_admin, user_can_access_module


def _can_manage(user, *modules):
    return any(user_can_access_module(user, module, ACCESS_MANAGE) for module in modules)


def _kinds():
    # Imported late: accounts.forms imports pos models, and this module is imported by pos views.
    from accounts.forms import BankAccountForm, ExpenseCategoryForm

    from ..forms import QuickBrandForm, QuickCategoryForm, QuickProductForm, SupplierForm
    from ..forms_field_sales import QuickOrderBookerForm, QuickRouteForm
    from .field_sales import field_sales_enabled

    return {
        "route": {
            "title": "Add route",
            "save": "Save route",
            "url": "quick_create_route",
            "form": lambda: QuickRouteForm(prefix="quick_route"),
            "allowed": lambda user: field_sales_enabled() and _can_manage(user, "parties"),
        },
        "order_booker": {
            "title": "Add order booker",
            "save": "Save order booker",
            "url": "quick_create_order_booker",
            "form": lambda: QuickOrderBookerForm(prefix="quick_order_booker"),
            "allowed": lambda user: field_sales_enabled() and _can_manage(user, "parties"),
        },
        "brand": {
            "title": "Add brand",
            "save": "Save brand",
            "url": "quick_create_brand",
            "form": lambda: QuickBrandForm(prefix="quick_brand"),
            "allowed": lambda user: _can_manage(user, "products"),
        },
        "category": {
            "title": "Add category",
            "save": "Save category",
            "url": "quick_create_category",
            "form": lambda: QuickCategoryForm(prefix="quick_category"),
            "allowed": lambda user: _can_manage(user, "products"),
        },
        "supplier": {
            "title": "Add supplier",
            "save": "Save supplier",
            "url": "quick_create_supplier",
            "form": lambda: SupplierForm(prefix="quick_supplier"),
            "allowed": lambda user: _can_manage(user, "products", "purchases", "parties"),
        },
        "product": {
            "title": "Add product",
            "save": "Save product",
            "url": "quick_create_product",
            "form": lambda: QuickProductForm(prefix="quick_product"),
            "allowed": lambda user: _can_manage(user, "products"),
        },
        "expense_type": {
            "title": "Add expense type",
            "save": "Save expense type",
            "url": "quick_create_expense_type",
            "form": lambda: ExpenseCategoryForm(prefix="quick_expense_type"),
            "allowed": lambda user: _can_manage(user, "expenses"),
        },
        "bank": {
            "title": "Add bank account",
            "save": "Save bank account",
            "url": "quick_create_bank_account",
            "form": lambda: BankAccountForm(prefix="quick_bank"),
            "allowed": lambda user: is_admin(user),
        },
    }


def quick_add_context(request, *kinds):
    """{"quick_add": [panel form, ...], "quick_add_kinds": {kind: True}} for the kinds this user may add."""
    available = _kinds()
    panels = []
    for kind in kinds:
        spec = available[kind]
        if not spec["allowed"](request.user):
            continue
        panels.append(
            {
                "kind": kind,
                "title": spec["title"],
                "save_label": spec["save"],
                "url": reverse(spec["url"]),
                "form": spec["form"](),
            }
        )
    return {"quick_add": panels, "quick_add_kinds": {panel["kind"]: True for panel in panels}}
