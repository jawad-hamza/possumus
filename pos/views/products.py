"""Products, brands, categories, quick-create endpoints and stock adjustments."""
from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, ProtectedError, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..forms import (
    BrandForm,
    CategoryForm,
    ProductForm,
    QuickBrandForm,
    QuickCategoryForm,
    QuickProductForm,
    StockAdjustmentForm,
    SupplierForm,
    purchase_option_attrs,
)
from ..models import (
    Brand,
    Category,
    Product,
    PurchaseItem,
    SaleItem,
    StockAdjustment,
    Supplier,
    ZERO_QTY,
    format_qty,
)
from ..services.access import (
    ACCESS_MANAGE,
    any_module_access_required,
    feature_required,
    module_access_required,
)
from ..services.catalog_io import (
    MAX_IMPORT_BYTES,
    CatalogImportError,
    apply_catalog,
    column_guide,
    export_catalog_csv_text,
    parse_catalog,
    template_csv_text,
)
from ..services.navigation import return_context, with_query, with_touched_row
from ..services.quick_add import quick_add_context
from ..services.stock_control import create_stock_adjustment
from ..services.business_rules import get_system_settings
from ..services.modules import module_enabled

# Cartons and boxes on the product screens.
from django.db.models import DecimalField, ExpressionWrapper, OuterRef, Subquery
from ..forms import PACK_UNIT_WORDS, default_stock_unit, quantity_in_largest_unit
from ..models import PACK_BOX, PACK_CARTON, PACK_PIECE, normalize_pack_level, packaging_enabled, quantize_money


PRODUCT_SORT_OPTIONS = [
    ("name_asc", "Name A-Z"),
    ("name_desc", "Name Z-A"),
    ("brand", "Brand"),
    ("category", "Category"),
    ("supplier", "Supplier"),
    ("location", "Location A-Z"),
    ("location_desc", "Location Z-A"),
    ("stock_low", "Lowest Stock"),
    ("stock_high", "Highest Stock"),
    ("price_low", "Lowest Sale Price"),
    ("price_high", "Highest Sale Price"),
    ("recent", "Newest First"),
]


def _clean_choice_id(value):
    value = (value or "").strip()
    return value if value.isdigit() else ""


def _filtered_querystring(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


def _json_form_errors(form):
    return {
        field: [entry["message"] for entry in entries]
        for field, entries in form.errors.get_json_data().items()
    }


def _filtered_products(request):
    """Products matching the list page's search and filters (shared with the CSV export)."""
    query = request.GET.get("q", "").strip()
    selected_brand = _clean_choice_id(request.GET.get("brand"))
    selected_category = _clean_choice_id(request.GET.get("category"))
    selected_supplier = _clean_choice_id(request.GET.get("supplier"))
    selected_status = request.GET.get("status", "").strip()

    queryset = Product.objects.select_related("category", "supplier", "brand")

    if query:
        matches = (
            Q(name__icontains=query)
            | Q(variant_label__icontains=query)
            | Q(sku__icontains=query)
            | Q(barcode__icontains=query)
            | Q(category__name__icontains=query)
            | Q(brand__name__icontains=query)
            | Q(supplier__name__icontains=query)
            | Q(location__icontains=query)
        )
        words = query.split()
        if len(words) > 1:
            # "bread small": every word somewhere in the family name, the size, the SKU or the barcode.
            every_word = Q()
            for word in words:
                every_word &= (
                    Q(name__icontains=word)
                    | Q(variant_label__icontains=word)
                    | Q(sku__icontains=word)
                    | Q(barcode__icontains=word)
                )
            matches |= every_word
        queryset = queryset.filter(matches)

    if selected_brand:
        queryset = queryset.filter(brand_id=selected_brand)
    if selected_category:
        queryset = queryset.filter(category_id=selected_category)
    if selected_supplier:
        queryset = queryset.filter(supplier_id=selected_supplier)

    if selected_status == "active":
        queryset = queryset.filter(is_active=True)
    elif selected_status == "inactive":
        queryset = queryset.filter(is_active=False)
    elif selected_status == "low_stock":
        queryset = queryset.filter(is_active=True, track_stock=True, stock__lte=F("low_stock_threshold"))
    else:
        selected_status = ""

    return queryset, {
        "search_query": query,
        "selected_brand": selected_brand,
        "selected_category": selected_category,
        "selected_supplier": selected_supplier,
        "selected_status": selected_status,
    }


def _family_summary(group_id, name, variants, expanded, stock_unit=None):
    """The one row a family of sizes shows in the list: how many sizes, price range, stock altogether."""
    # Each size is priced per its own largest pack, as its row shows it.
    prices = [variant.top_pack_price for variant in variants]
    price_units = {variant.top_pack_label if variant.has_packaging else "" for variant in variants}
    tracked = [variant for variant in variants if variant.track_stock]
    units = {variant.unit for variant in tracked}
    total_stock = sum((variant.stock for variant in tracked), ZERO_QTY)
    stock_total_display = ""
    if len(units) == 1:
        packings = {(variant.pieces_per_box, variant.boxes_per_carton) for variant in tracked}
        if len(packings) == 1:
            # Every size is packed the same way, so the total reads in those packs.
            stock_total_display = tracked[0].format_quantity(total_stock, stock_unit)
        else:
            stock_total_display = f"{format_qty(total_stock)} {next(iter(units))}"
    return {
        "is_family": True,
        "group_id": group_id,
        "name": name,
        "variants": variants,
        "size_count": len(variants),
        "price_low": min(prices),
        "price_high": max(prices),
        # Named only when every size is priced by the same pack.
        "price_unit": next(iter(price_units)) if len(price_units) == 1 else "",
        # One figure only when every size is counted in the same unit.
        "stock_total": total_stock if len(units) == 1 else None,
        "stock_unit": next(iter(units)) if len(units) == 1 else "",
        "stock_total_display": stock_total_display,
        "low_count": sum(1 for variant in tracked if variant.is_active and variant.stock <= variant.low_stock_threshold),
        "active_count": sum(1 for variant in variants if variant.is_active),
        "expanded": expanded,
    }


def _latest_purchase_line(product_ref):
    """Subquery: the newest purchase line of the product in the outer row."""
    return Subquery(
        PurchaseItem.objects.filter(product_id=product_ref).order_by("-purchase__created_at", "-id").values("pk")[:1]
    )


def _attach_last_purchases(products):
    """Give each product its newest purchase line as .last_purchase: one query for the whole page."""
    line_ids = [product.last_purchase_item_id for product in products if getattr(product, "last_purchase_item_id", None)]
    lines = PurchaseItem.objects.select_related("purchase", "purchase__supplier").in_bulk(line_ids) if line_ids else {}
    for product in products:
        line = lines.get(getattr(product, "last_purchase_item_id", None))
        if line is not None:
            # The line names its unit through the product; reuse the row already loaded.
            line.product = product
        product.last_purchase = line


def _paginate_with_families(queryset, page_number, expanded=False, stock_unit=None, with_last_purchase=False):
    """Page through the list with the sizes of one item folded into a single family entry.

    Returns (page, rows). page.object_list holds the products on the page in list order;
    rows is what the table draws: a product, or a family with its matching sizes.
    """
    grouping = module_enabled("variants")
    entries = []
    family_slot = {}
    for product_id, group_id in queryset.values_list("id", "variant_group_id"):
        if grouping and group_id:
            if group_id in family_slot:
                entries[family_slot[group_id]][1].append(product_id)
                continue
            family_slot[group_id] = len(entries)
        entries.append((group_id if grouping else None, [product_id]))

    page = Paginator(entries, 20).get_page(page_number)
    page_entries = list(page.object_list)
    wanted = [product_id for _group_id, ids in page_entries for product_id in ids]
    page_products = Product.objects.select_related("category", "supplier", "brand")
    if with_last_purchase:
        page_products = page_products.annotate(last_purchase_item_id=_latest_purchase_line(OuterRef("pk")))
    loaded = page_products.in_bulk(wanted)
    if with_last_purchase:
        _attach_last_purchases(loaded.values())

    rows = []
    flat = []
    for group_id, ids in page_entries:
        members = [loaded[product_id] for product_id in ids if product_id in loaded]
        if not members:
            continue
        flat.extend(members)
        if group_id:
            rows.append(_family_summary(group_id, members[0].name, members, expanded, stock_unit))
        else:
            rows.append({"is_family": False, "product": members[0]})
    page.object_list = flat
    return page, rows


def _stock_unit_choice(request, settings_obj):
    """The unit the list counts stock from: the "Stock in" choice, else the shop's stock unit."""
    if not packaging_enabled():
        return PACK_PIECE
    return normalize_pack_level(
        request.GET.get("stock_unit"),
        default=normalize_pack_level(settings_obj.stock_default_unit, default=PACK_CARTON),
    )


@module_access_required("products")
def product_list(request):
    queryset, selected = _filtered_products(request)
    selected_sort = request.GET.get("sort", "name_asc").strip()
    settings_obj = get_system_settings()
    today = timezone.localdate()
    expiry_alert_end = today + timedelta(days=int(settings_obj.expiry_alert_days or 30))

    sort_map = {
        "name_asc": ("name",),
        "name_desc": ("-name",),
        "brand": ("brand__name", "name"),
        "category": ("category__name", "name"),
        "supplier": ("supplier__name", "name"),
        "location": ("location", "name"),
        "location_desc": ("-location", "name"),
        "stock_low": ("stock", "name"),
        "stock_high": ("-stock", "name"),
        "price_low": ("price", "name"),
        "price_high": ("-price", "name"),
        "recent": ("-id",),
    }
    if selected_sort not in sort_map:
        selected_sort = "name_asc"

    packaging = packaging_enabled()
    stock_unit = _stock_unit_choice(request, settings_obj)
    if packaging:
        # The price column shows the largest pack's price, so "lowest price" sorts by that figure.
        queryset = queryset.annotate(
            list_price=ExpressionWrapper(
                F("price") * F("pieces_per_box") * F("boxes_per_carton"),
                output_field=DecimalField(max_digits=24, decimal_places=6),
            )
        )
        sort_map["price_low"] = ("list_price", "name")
        sort_map["price_high"] = ("-list_price", "name")

    queryset = queryset.order_by(*sort_map[selected_sort], "variant_label", "id")

    show_purchases = module_enabled("purchases")
    products, product_rows = _paginate_with_families(
        queryset,
        request.GET.get("page"),
        expanded=bool(selected["search_query"]),
        stock_unit=stock_unit,
        with_last_purchase=show_purchases,
    )
    recent_purchase_lines = []
    if show_purchases:
        recent_purchase_lines = list(
            PurchaseItem.objects.select_related("product", "purchase", "purchase__supplier")
            .order_by("-purchase__created_at", "-id")[:8]
        )

    if settings_obj.enable_product_expiry:
        for product in products.object_list:
            expiry_date = product.expiry_date
            if not expiry_date:
                product.expiry_status_tone = "unknown"
                product.expiry_status_label = "Not set"
            elif expiry_date < today:
                product.expiry_status_tone = "below"
                product.expiry_status_label = f"Expired {(today - expiry_date).days}d ago"
            elif expiry_date <= expiry_alert_end:
                product.expiry_status_tone = "advance"
                product.expiry_status_label = f"{(expiry_date - today).days}d left"
            else:
                product.expiry_status_tone = "healthy"
                product.expiry_status_label = "OK"

    summary_queryset = queryset
    context = {
        "products": products,
        "product_rows": product_rows,
        **selected,
        "brands": Brand.objects.filter(is_active=True).order_by("name"),
        "categories": Category.objects.filter(is_active=True).order_by("name"),
        "suppliers": Supplier.objects.filter(is_active=True).order_by("name"),
        "selected_sort": selected_sort,
        "sort_options": PRODUCT_SORT_OPTIONS,
        "filter_querystring": _filtered_querystring(request),
        "summary_total": summary_queryset.count(),
        "summary_active": summary_queryset.filter(is_active=True).count(),
        "summary_low_stock": summary_queryset.filter(is_active=True, track_stock=True, stock__lte=F("low_stock_threshold")).count(),
        "summary_inactive": summary_queryset.filter(is_active=False).count(),
        "expiry_tracking_enabled": settings_obj.enable_product_expiry,
        "stock_unit": stock_unit,
        "stock_unit_options": [
            (PACK_CARTON, PACK_UNIT_WORDS[PACK_CARTON]),
            (PACK_BOX, PACK_UNIT_WORDS[PACK_BOX]),
            (PACK_PIECE, "Pieces"),
        ],
        "show_purchases": show_purchases,
        "recent_purchase_lines": recent_purchase_lines,
        # Every column of the table, shown or not, for the row that spans it when the list is empty.
        "column_count": 10 + int(bool(settings_obj.enable_product_expiry)) + int(show_purchases),
    }
    return render(request, "pos/product_list.html", context)


def _sibling_variants(product):
    """The sizes of the family this product belongs to, itself included; empty for a plain product."""
    if product is None or not product.variant_group_id or not module_enabled("variants"):
        return []
    return list(Product.objects.filter(variant_group_id=product.variant_group_id).order_by("price", "variant_label", "id"))


def _next_free_sku(base_sku):
    """First unused "<sku>-2", "<sku>-3"... so another size starts with a SKU that will save."""
    max_length = Product._meta.get_field("sku").max_length
    for number in range(2, 200):
        suffix = f"-{number}"
        candidate = f"{base_sku[: max_length - len(suffix)]}{suffix}"
        if not Product.objects.filter(sku__iexact=candidate).exists():
            return candidate
    return ""


def _initial_for_another_size(source):
    """Everything the sizes of one item share. Cost, price, barcode and stock are typed fresh."""
    # The form reads the fixed markup per the largest pack and the alert level with its unit.
    markup_amount = source.markup_amount
    if markup_amount is not None:
        markup_amount = quantize_money(markup_amount * source.pack_factor(source.top_pack_level))
    alert_quantity, alert_unit = quantity_in_largest_unit(
        source, source.low_stock_threshold, default_stock_unit() if packaging_enabled() else PACK_PIECE
    )
    return {
        "name": source.name,
        "sku": _next_free_sku(source.sku),
        "brand": source.brand_id,
        "category": source.category_id,
        "supplier": source.supplier_id,
        "unit": source.unit,
        "pieces_per_box": source.pieces_per_box,
        "boxes_per_carton": source.boxes_per_carton,
        "markup_percent": source.markup_percent,
        "markup_amount": markup_amount,
        "minimum_margin_percent": source.minimum_margin_percent,
        "low_stock_threshold": format_qty(alert_quantity),
        "low_stock_unit": alert_unit,
        "location": source.location,
    }


@module_access_required("products", minimum=ACCESS_MANAGE)
def product_add(request):
    # ?copy=<id> starts another size of an existing item: same details, its own SKU and price.
    copy_source = None
    copy_id = request.GET.get("copy", "")
    if copy_id.isdigit():
        copy_source = Product.objects.filter(pk=int(copy_id)).first()
    initial = _initial_for_another_size(copy_source) if copy_source and request.method != "POST" else None
    if request.method != "POST" and not copy_source:
        # The till's "not found, add it" link brings the scanned code along.
        scanned = (request.GET.get("barcode") or "").strip()[:64]
        if scanned:
            initial = {"barcode": scanned}
    form = ProductForm(request.POST or None, initial=initial, variant_source=copy_source)
    back = return_context(request, reverse("product_list"))
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    product = form.save()
                    opening_stock = form.cleaned_data.get("opening_stock")
                    if opening_stock:
                        create_stock_adjustment(
                            product=product,
                            adjustment_mode=StockAdjustment.OPENING_STOCK,
                            quantity_delta=opening_stock,
                            reason="Opening stock",
                            actor=request.user,
                        )
            except ValueError as exc:
                # The product and its opening stock are saved together or not at all.
                form.add_error("opening_stock", str(exc))
            else:
                messages.success(request, f"Product '{product.display_name}' added successfully.")
                if request.POST.get("save_and_new"):
                    # The list the user started from is still remembered on the next blank form.
                    return redirect(with_query(
                        reverse("product_add"),
                        # Entering sizes one after another: the next one starts from the size just saved.
                        copy=product.id if copy_source else None,
                        next=back["next_url"] or None,
                    ))
                if back["next_url"]:
                    return redirect(with_touched_row(back["next_url"], product.id))
                return redirect("product_view", pk=product.id)
        messages.error(request, "Please fix the highlighted product fields.")

    return render(
        request,
        "pos/product_form.html",
        {
            "form": form,
            "title": "Add Product",
            "copy_source": copy_source,
            **back,
            **quick_add_context(request, "brand", "category", "supplier"),
            # A plain product about to get its first sibling is listed as the size it will become.
            "variant_siblings": _sibling_variants(copy_source)
            or ([copy_source] if copy_source is not None and module_enabled("variants") else []),
        },
    )


def _csv_download(text, filename):
    response = HttpResponse(text, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@module_access_required("products", minimum=ACCESS_MANAGE)
@feature_required("catalog_io")
def product_export(request):
    """CSV of the products currently matching the list filters, in the same layout the import reads."""
    queryset, _selected = _filtered_products(request)
    filename = f"products-{timezone.localdate().isoformat()}.csv"
    return _csv_download(export_catalog_csv_text(queryset), filename)


@module_access_required("products", minimum=ACCESS_MANAGE)
@feature_required("catalog_io")
def product_import_template(request):
    return _csv_download(template_csv_text(), "products-import-template.csv")


def _import_success_message(result):
    parts = [f"{result['created']} product(s) added"]
    if result["updated"]:
        parts.append(f"{result['updated']} updated")
    if result["opening_stock_rows"]:
        parts.append(f"opening stock recorded for {result['opening_stock_rows']}")
    created_lookups = [
        f"{result[key]} new {label}"
        for key, label in (
            ("categories_created", "categories"),
            ("brands_created", "brands"),
            ("suppliers_created", "suppliers"),
        )
        if result[key]
    ]
    if created_lookups:
        parts.append(", ".join(created_lookups))
    return f"Import finished: {'; '.join(parts)}."


@module_access_required("products", minimum=ACCESS_MANAGE)
@feature_required("catalog_io")
def product_import(request):
    plan = None
    checked_only = False
    update_existing = request.POST.get("update_existing") == "on"

    if request.method == "POST":
        upload = request.FILES.get("catalog_file")
        if upload is None:
            messages.error(request, "Choose the CSV file to import.")
        elif upload.size > MAX_IMPORT_BYTES:
            messages.error(request, f"The file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB. Split it into smaller files.")
        else:
            plan = parse_catalog(upload.read(), update_existing=update_existing)
            checked_only = request.POST.get("mode") == "check"
            if not plan.is_valid:
                messages.error(request, "Nothing was imported. Fix the rows listed below and upload the file again.")
            elif not checked_only:
                try:
                    result = apply_catalog(plan, actor=request.user)
                except CatalogImportError as exc:
                    plan.add_error(None, "Import", [str(exc), "Nothing was saved."])
                    messages.error(request, "Nothing was imported. See the reason below.")
                else:
                    messages.success(request, _import_success_message(result))
                    for warning in plan.warnings:
                        messages.warning(request, warning)
                    # Land on the list with the new products on top, ready to check.
                    return redirect(f"{reverse('product_list')}?sort=recent")

    return render(
        request,
        "pos/product_import.html",
        {
            "plan": plan,
            "checked_only": checked_only and plan is not None and plan.is_valid,
            "update_existing": update_existing,
            "column_guide": column_guide(),
            "max_size_mb": MAX_IMPORT_BYTES // (1024 * 1024),
        },
    )


@module_access_required("products")
def product_view(request, pk):
    product = get_object_or_404(Product.objects.select_related("category", "supplier", "brand"), id=pk)
    # Lines are kept in the unit they were sold or bought in; totals are in the base unit.
    total_sold = SaleItem.objects.filter(product=product).aggregate(total=Sum(F("quantity") * F("unit_factor")))["total"] or ZERO_QTY
    total_purchased = (
        PurchaseItem.objects.filter(product=product).aggregate(total=Sum(F("quantity") * F("unit_factor")))["total"] or ZERO_QTY
    )
    recent_sales = list(SaleItem.objects.filter(product=product).select_related("sale").order_by("-sale__created_at")[:10])
    recent_purchases = list(
        PurchaseItem.objects.filter(product=product)
        .select_related("purchase", "purchase__supplier")
        .order_by("-purchase__created_at")[:10]
    )
    for line in (*recent_sales, *recent_purchases):
        # Each line names its own unit ("10 Ctn") through the product: reuse the one already loaded.
        line.product = product
    return render(
        request,
        "pos/product_view.html",
        {
            "product": product,
            "variant_siblings": _sibling_variants(product),
            **return_context(request, reverse("product_list")),
            "total_sold": total_sold,
            "total_purchased": total_purchased,
            "recent_sales": recent_sales,
            "recent_purchases": recent_purchases,
        },
    )


@module_access_required("products", minimum=ACCESS_MANAGE)
def product_edit(request, pk):
    product = get_object_or_404(Product, id=pk)
    form = ProductForm(request.POST or None, instance=product)
    back = return_context(request, reverse("product_view", args=[product.id]))

    if request.method == "POST":
        if form.is_valid():
            product = form.save()
            messages.success(request, f"Product '{product.display_name}' updated.")
            if back["next_url"]:
                return redirect(with_touched_row(back["next_url"], product.id))
            return redirect("product_view", pk=product.id)
        messages.error(request, "Please fix the highlighted product fields.")

    return render(
        request,
        "pos/product_form.html",
        {
            "form": form,
            "title": "Edit Product",
            "variant_siblings": _sibling_variants(product),
            **back,
            **quick_add_context(request, "brand", "category", "supplier"),
        },
    )


@module_access_required("products", minimum=ACCESS_MANAGE)
def product_delete(request, pk):
    product = get_object_or_404(Product, id=pk)
    back = return_context(request, reverse("product_view", args=[product.id]))

    if request.method == "POST":
        if SaleItem.objects.filter(product=product).exists() or PurchaseItem.objects.filter(product=product).exists():
            messages.error(request, "This product has transaction history and cannot be deleted.")
            return redirect("product_view", pk=product.id)

        name = product.display_name
        try:
            product.delete()
        except ProtectedError:
            messages.error(request, "This product has stock or return history and cannot be deleted. Mark it inactive instead.")
            return redirect("product_view", pk=product.id)
        messages.success(request, f"Product '{name}' deleted successfully.")
        # Never back to a page of the product that no longer exists.
        own_pages = (reverse("product_view", args=[pk]), reverse("product_edit", args=[pk]))
        if back["next_url"] and not back["next_url"].split("?")[0].startswith(own_pages):
            return redirect(back["next_url"])
        return redirect("product_list")

    return render(request, "pos/product_confirm_delete.html", {"product": product, **back})


@module_access_required("products", minimum=ACCESS_MANAGE)
def brand_add(request):
    form = BrandForm(request.POST or None)
    back = return_context(request, reverse("product_list"))
    if request.method == "POST":
        if form.is_valid():
            brand = form.save()
            messages.success(request, f"Brand '{brand.name}' added successfully.")
            return redirect(back["back_url"])
        messages.error(request, "Please fix the brand form.")

    return render(request, "pos/brand_form.html", {"form": form, **back})


@module_access_required("products", minimum=ACCESS_MANAGE)
def category_add(request):
    form = CategoryForm(request.POST or None)
    back = return_context(request, reverse("settings_page"))
    if request.method == "POST":
        if form.is_valid():
            category = form.save()
            messages.success(request, f"Category '{category.name}' added successfully.")
            return redirect(back["back_url"])
        messages.error(request, "Please fix the category form.")

    return render(
        request,
        "pos/category_form.html",
        {
            "form": form,
            "title": "Add Category",
            "submit_label": "Save Category",
            **back,
        },
    )


@module_access_required("products", minimum=ACCESS_MANAGE)
def category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk)
    form = CategoryForm(request.POST or None, instance=category)
    back = return_context(request, reverse("settings_page"))
    if request.method == "POST":
        if form.is_valid():
            category = form.save()
            messages.success(request, f"Category '{category.name}' updated.")
            return redirect(back["back_url"])
        messages.error(request, "Please fix the category form.")

    return render(
        request,
        "pos/category_form.html",
        {
            "form": form,
            "title": f"Edit {category.name}",
            "submit_label": "Update Category",
            "category": category,
            **back,
        },
    )


@module_access_required("products", minimum=ACCESS_MANAGE)
@require_POST
def quick_create_brand(request):
    form = QuickBrandForm(request.POST, prefix="quick_brand")
    if form.is_valid():
        brand = form.save()
        return JsonResponse({"status": "ok", "brand": {"id": brand.id, "name": brand.name}})
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


@module_access_required("products", minimum=ACCESS_MANAGE)
@require_POST
def quick_create_category(request):
    form = QuickCategoryForm(request.POST, prefix="quick_category")
    if form.is_valid():
        category = form.save()
        return JsonResponse({"status": "ok", "category": {"id": category.id, "name": category.name}})
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


@any_module_access_required(("products", "purchases", "parties"), minimum=ACCESS_MANAGE)
@require_POST
def quick_create_supplier(request):
    form = SupplierForm(request.POST, prefix="quick_supplier")
    if form.is_valid():
        supplier = form.save()
        return JsonResponse({"status": "ok", "supplier": {"id": supplier.id, "name": supplier.name}})
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


@module_access_required("products", minimum=ACCESS_MANAGE)
@require_POST
def quick_create_product(request):
    form = QuickProductForm(request.POST, prefix="quick_product")
    if form.is_valid():
        product = form.save()
        return JsonResponse(
            {
                "status": "ok",
                "product": {
                    "id": product.id,
                    "name": product.display_name,
                    "sku": product.sku,
                    "price": str(product.price),
                    "unit": product.unit,
                    "markup_percent": str(product.markup_percent) if product.markup_percent is not None else "",
                    "markup_amount": str(product.markup_amount) if product.markup_amount is not None else "",
                    "minimum_margin_percent": str(product.minimum_margin_percent),
                    "pieces_per_box": product.pieces_per_box,
                    "boxes_per_carton": product.boxes_per_carton,
                    "brand_id": product.brand_id,
                    "category_id": product.category_id,
                    "supplier_id": product.supplier_id,
                    "location": product.location,
                    "expiry_date": product.expiry_date.isoformat() if product.expiry_date else "",
                    # What a server-rendered option of the purchase line dropdown carries, so the
                    # new product behaves on a line like any other. Price and markup amount are
                    # per base unit here, whatever pack they were typed for.
                    "option_attrs": purchase_option_attrs(product),
                },
            }
        )
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


@module_access_required("stock_control", minimum=ACCESS_MANAGE)
def stock_adjustments(request):
    selected_product_id = (request.GET.get("product") or request.POST.get("product") or "").strip()
    initial = {}
    if selected_product_id.isdigit():
        initial["product"] = selected_product_id

    form = StockAdjustmentForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            adjustment = create_stock_adjustment(
                product=form.cleaned_data["product"],
                adjustment_mode=form.cleaned_data["adjustment_mode"],
                quantity_after=form.cleaned_data.get("quantity_after"),
                quantity_delta=form.cleaned_data.get("quantity_delta"),
                reason=form.cleaned_data["reason"],
                notes=form.cleaned_data.get("notes") or "",
                actor=request.user,
            )
            adjusted = adjustment.product
            if adjusted.has_packaging:
                messages.success(
                    request,
                    f"Stock updated for {adjusted.display_name}: {adjusted.format_quantity(adjustment.quantity_after)} on hand.",
                )
            else:
                messages.success(request, f"Stock updated for {adjusted.display_name}.")
            next_url = return_context(request, "")["next_url"]
            if next_url:
                return redirect(with_touched_row(next_url, adjustment.product_id))
            return redirect(f"{reverse('stock_adjustments')}?product={adjustment.product_id}")
        except ValueError as exc:
            form.add_error(None, str(exc))
            messages.error(request, "Please review the stock adjustment details.")

    adjustments = StockAdjustment.objects.select_related("product", "voucher", "created_by").order_by("-created_at", "-id")[:20]
    return render(
        request,
        "pos/stock_adjustments.html",
        {
            "form": form,
            "adjustments": adjustments,
            "selected_product_id": selected_product_id,
            "page_title": "Stock Adjustments",
            **return_context(request, reverse("product_list")),
            **quick_add_context(request, "product"),
        },
    )
