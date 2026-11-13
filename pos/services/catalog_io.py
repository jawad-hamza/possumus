"""CSV import and export for the product catalog.

Import is two passes on purpose. `parse_catalog` reads the whole file and validates every
row without writing anything, so the user sees all the problems at once. Only a clean file
reaches `apply_catalog`, which writes everything inside one transaction: either the whole
file lands or none of it does.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.services.audit import record_audit
from pos.models import (
    Brand,
    Category,
    Product,
    ProductGroup,
    StockAdjustment,
    Supplier,
    SystemSettings,
    format_qty,
    packaging_enabled,
    quantize_money,
    quantize_qty,
)
from pos.services.stock_control import create_stock_adjustment


MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
EXAMPLE_SKU_PREFIX = "EXAMPLE-"
UTF8_BOM = chr(0xFEFF)
NO_BREAK_SPACE = chr(0xA0)

_HAS_TRACK_STOCK = any(f.name == "track_stock" for f in Product._meta.get_fields())

COLUMNS = [
    "name",
    "sku",
    "barcode",
    "category",
    "brand",
    "supplier",
    "unit",
    "cost_price",
    "markup_percent",
    "price",
    "minimum_margin_percent",
    "opening_stock",
    "low_stock_threshold",
    "location",
    "expiry_date",
]
if _HAS_TRACK_STOCK:
    COLUMNS.append("track_stock")
# Added later, so they come last and older spreadsheets keep their column order.
# markup_amount is the other way to price from cost; the variant pair names a size of one item ("Bread", "Small").
COLUMNS.extend(["markup_amount", "variant_group", "variant_label"])
# Cartons and boxes. With these filled, cost_price, price and markup_amount are for the largest pack.
PACKING_COLUMNS = ("pieces_per_box", "boxes_per_carton")
COLUMNS.extend(PACKING_COLUMNS)
# Kept per base unit in the database; in the file they are for the largest pack the row has.
PACK_MONEY_COLUMNS = ("cost_price", "price", "markup_amount")
MAX_PACK_COUNT = 100000

COLUMN_GUIDE = [
    ("name", "Required", "Product name as it should print on the receipt."),
    ("sku", "Optional", "Your own item code. Left blank, one is generated. Existing products are matched by this."),
    ("barcode", "Optional", "Barcode printed on the pack, if you scan."),
    ("category", "Optional", "Created automatically when it does not exist yet."),
    ("brand", "Optional", "Created automatically when it does not exist yet."),
    ("supplier", "Optional", "Created automatically when it does not exist yet."),
    ("unit", "Optional", "pcs, box, pack, dozen, kg, g, l, ml, m or ft. Blank means pcs."),
    ("cost_price", "Optional", "What one unit costs you."),
    ("markup_percent", "Optional", "Sale price = cost + this %. Leave blank to type the price yourself."),
    ("price", "Required unless markup is set", "Sale price per unit."),
    ("minimum_margin_percent", "Optional", "Lowest margin the till will allow on this item."),
    ("opening_stock", "Optional", "Quantity on the shelf today. New products only; weights may use decimals (2.5)."),
    ("low_stock_threshold", "Optional", "Warn when stock falls to this level. Blank means 5."),
    ("location", "Optional", "Shelf, rack or room."),
    ("expiry_date", "Optional", "YYYY-MM-DD, for example 2027-03-31."),
]
if _HAS_TRACK_STOCK:
    COLUMN_GUIDE.append(("track_stock", "Optional", "no for services and other items without shelf stock. Blank means yes."))
COLUMN_GUIDE.extend(
    [
        ("markup_amount", "Optional", "Or: sale price = cost + this fixed amount. Fill one markup at most."),
        ("variant_group", "Optional", "For an item sold in sizes: the item's name, the same on every size (Bread)."),
        ("variant_label", "With variant_group", "This row's size, such as Small, 500 g or Pack of 6."),
    ]
)

# What changes in the guide while cartons and boxes are switched on.
_PACK_COLUMN_GUIDE = {
    "cost_price": "What one carton costs you. A row without cartons: one box. Without boxes either: one unit.",
    "price": "Sale price of the same pack as the cost: the carton, else the box, else one unit.",
    "markup_amount": "Or: sale price = cost + this fixed amount, for the same pack as the cost. Fill one markup at most.",
    "opening_stock": "Quantity on the shelf today, counted in single units (pieces), not cartons. New products only; weights may use decimals (2.5).",
    "low_stock_threshold": "Warn when stock falls to this many single units (pieces). Blank means 5.",
}
_PACKING_COLUMN_GUIDE = [
    ("pieces_per_box", "Optional", "Pieces in one box. Blank or 1: the product has no boxes."),
    ("boxes_per_carton", "Optional", "Boxes in one carton (pieces, when there are no boxes). Blank or 1: no cartons."),
]


def column_guide():
    """The column help for the import page, worded in packs only while cartons and boxes are switched on."""
    if not packaging_enabled():
        return list(COLUMN_GUIDE)
    return [
        (column, requirement, _PACK_COLUMN_GUIDE.get(column, help_text))
        for column, requirement, help_text in COLUMN_GUIDE
    ] + _PACKING_COLUMN_GUIDE

_EXAMPLE_ROWS = [
    {
        "name": "Example Flour",
        "sku": f"{EXAMPLE_SKU_PREFIX}1",
        "category": "Grocery",
        "brand": "Example Mills",
        "supplier": "Example Traders",
        "unit": "kg",
        "cost_price": "80.00",
        "markup_percent": "25",
        "minimum_margin_percent": "10",
        "opening_stock": "50.5",
        "low_stock_threshold": "10",
        "location": "Rack 1",
    },
    {
        "name": "Example Soap Bar",
        "sku": f"{EXAMPLE_SKU_PREFIX}2",
        "barcode": "1234567890123",
        "category": "Toiletries",
        "unit": "pcs",
        "cost_price": "45.00",
        "price": "60.00",
        "opening_stock": "24",
        "expiry_date": "2027-12-31",
    },
]
# Offered while cartons and boxes are switched on: prices for one carton, stock in pieces (10 cartons).
_PACKED_EXAMPLE_ROW = {
    "name": "Example Biscuits",
    "sku": f"{EXAMPLE_SKU_PREFIX}3",
    "category": "Grocery",
    "unit": "pcs",
    "pieces_per_box": "12",
    "boxes_per_carton": "12",
    "cost_price": "1000.00",
    "price": "1250.00",
    "opening_stock": "1440",
    "low_stock_threshold": "288",
}

_UNIT_ALIASES = {
    "piece": "pcs", "pieces": "pcs", "pc": "pcs", "each": "pcs", "unit": "pcs", "nos": "pcs", "no": "pcs",
    "boxes": "box", "packs": "pack", "packet": "pack", "pkt": "pack", "doz": "dozen", "dz": "dozen",
    "kilogram": "kg", "kilograms": "kg", "kilo": "kg", "kgs": "kg",
    "gram": "g", "grams": "g", "gm": "g", "gms": "g",
    "litre": "l", "litres": "l", "liter": "l", "liters": "l", "ltr": "l",
    "millilitre": "ml", "milliliter": "ml", "mls": "ml",
    "metre": "m", "metres": "m", "meter": "m", "meters": "m", "mtr": "m",
    "foot": "ft", "feet": "ft",
}
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y")
_THOUSANDS_PATTERN = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_FORMULA_LEADERS = ("=", "+", "-", "@")
_TRUE_WORDS = {"1", "yes", "y", "true", "t"}
_FALSE_WORDS = {"0", "no", "n", "false", "f"}


@dataclass
class RowPlan:
    row_number: int
    product: Product
    is_new: bool
    category_name: str = ""
    brand_name: str = ""
    supplier_name: str = ""
    opening_stock: Decimal = Decimal("0")
    update_fields: list = field(default_factory=list)
    variant_group_name: str = ""


@dataclass
class CatalogPlan:
    rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    new_categories: list = field(default_factory=list)
    new_brands: list = field(default_factory=list)
    new_suppliers: list = field(default_factory=list)
    skipped_examples: int = 0

    @property
    def is_valid(self):
        return not self.errors

    @property
    def create_count(self):
        return sum(1 for row in self.rows if row.is_new)

    @property
    def update_count(self):
        return sum(1 for row in self.rows if not row.is_new)

    @property
    def opening_stock_count(self):
        return sum(1 for row in self.rows if row.opening_stock > 0)

    def add_error(self, row_number, label, messages):
        self.errors.append({"row": row_number, "label": label, "messages": list(messages)})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _safe_cell(value):
    """Spreadsheets run cells starting with = + - @ as formulas; a leading apostrophe keeps them text."""
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_LEADERS):
        return f"'{text}"
    return text


def _csv_text(rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _safe_cell(row.get(column, "")) for column in COLUMNS})
    # The BOM makes Excel read the file as UTF-8, so names in any script survive.
    return UTF8_BOM + buffer.getvalue()


def template_csv_text():
    rows = list(_EXAMPLE_ROWS)
    if packaging_enabled():
        rows.append(_PACKED_EXAMPLE_ROW)
    return _csv_text(rows)


def _pack_money(product, value):
    """A stored per-unit figure as the file carries it: for the largest pack, two places."""
    return quantize_money(Decimal(value or 0) * product.pack_factor(product.top_pack_level))


def export_catalog_csv_text(queryset=None):
    if queryset is None:
        queryset = Product.objects.all()
    queryset = queryset.select_related("category", "brand", "supplier", "variant_group").order_by("name", "variant_label", "id")
    packaging = packaging_enabled()

    rows = []
    for product in queryset:
        row = {
            "name": product.name,
            "sku": product.sku,
            "barcode": product.barcode or "",
            "category": product.category.name if product.category_id else "",
            "brand": product.brand.name if product.brand_id else "",
            "supplier": product.supplier.name if product.supplier_id else "",
            "unit": product.unit,
            # Money is written for the largest pack the row states, so the import reads back the same figures.
            "cost_price": f"{_pack_money(product, product.cost_price):.2f}",
            "markup_percent": "" if product.markup_percent is None else f"{product.markup_percent:.2f}",
            "markup_amount": "" if product.markup_amount is None else f"{_pack_money(product, product.markup_amount):.2f}",
            "variant_group": product.variant_group.name if product.variant_group_id else "",
            "variant_label": product.variant_label if product.variant_group_id else "",
            "price": f"{_pack_money(product, product.price):.2f}",
            "minimum_margin_percent": f"{Decimal(product.minimum_margin_percent or 0):.2f}",
            # Current stock, so the file can seed another install as its opening stock. Always in base units.
            "opening_stock": format_qty(product.stock),
            "low_stock_threshold": format_qty(product.low_stock_threshold),
            "location": product.location,
            "expiry_date": product.expiry_date.isoformat() if product.expiry_date else "",
            # Blank while cartons and boxes are switched off: every figure is then for one unit.
            "pieces_per_box": str(product.pieces_per_box) if packaging else "",
            "boxes_per_carton": str(product.boxes_per_carton) if packaging else "",
        }
        if _HAS_TRACK_STOCK:
            row["track_stock"] = "yes" if product.track_stock else "no"
        rows.append(row)
    return _csv_text(rows)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _decode(raw):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _detect_delimiter(text):
    first_line = text.split("\n", 1)[0]
    counts = {delimiter: first_line.count(delimiter) for delimiter in (",", ";", "\t", "|")}
    delimiter = max(counts, key=counts.get)
    return delimiter if counts[delimiter] else ","


def _normalize_header(value):
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def _clean_cell(value):
    text = (value or "").strip()
    if len(text) > 1 and text[0] == "'" and text[1] in _FORMULA_LEADERS:
        text = text[1:]
    return text


class _CellError(Exception):
    pass


def _parse_decimal(text, column, *, decimal_comma):
    value = text.replace(" ", "").replace(NO_BREAK_SPACE, "")
    if decimal_comma:
        if "," in value:
            value = value.replace(".", "").replace(",", ".")
    elif _THOUSANDS_PATTERN.match(value):
        value = value.replace(",", "")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise _CellError(f"{column}: '{text}' is not a number. Use digits only, for example 1250.50.") from exc
    if not number.is_finite():
        raise _CellError(f"{column}: '{text}' is not a number.")
    if number < 0:
        raise _CellError(f"{column}: cannot be negative.")
    return number


def _parse_pack_count(text, column):
    """How many a box or a carton holds: a whole number, 1 meaning the level is not there."""
    try:
        count = Decimal(text.replace(" ", "").replace(NO_BREAK_SPACE, ""))
    except InvalidOperation as exc:
        raise _CellError(f"{column}: '{text}' is not a number. Use a whole number such as 12.") from exc
    if not count.is_finite() or count != count.to_integral_value() or count < 1:
        raise _CellError(f"{column}: use a whole number, 1 or more.")
    if count > MAX_PACK_COUNT:
        raise _CellError(f"{column}: is too large.")
    return int(count)


def _parse_date(text):
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise _CellError(f"expiry_date: '{text}' is not a date. Use YYYY-MM-DD, for example 2027-03-31.")


def _parse_unit(text):
    key = text.strip().lower().rstrip(".")
    valid = {code for code, _label in Product.UNIT_CHOICES}
    if key in valid:
        return key
    if key in _UNIT_ALIASES:
        return _UNIT_ALIASES[key]
    for code, label in Product.UNIT_CHOICES:
        if key == label.lower():
            return code
    raise _CellError(f"unit: '{text}' is not supported. Use one of: {', '.join(sorted(valid))}.")


def _parse_bool(text, column):
    key = text.strip().lower()
    if key in _TRUE_WORDS:
        return True
    if key in _FALSE_WORDS:
        return False
    raise _CellError(f"{column}: use yes or no.")


def _name_max_length(model):
    return model._meta.get_field("name").max_length


def _validation_messages(exc):
    if hasattr(exc, "message_dict"):
        return [
            message if field_name == "__all__" else f"{field_name}: {message}"
            for field_name, field_messages in exc.message_dict.items()
            for message in field_messages
        ]
    return list(exc.messages)


class _SkuGenerator:
    def __init__(self, taken):
        self.taken = taken
        self.counter = Product.objects.count()

    def next(self):
        while True:
            self.counter += 1
            candidate = f"SKU-{self.counter:05d}"
            if candidate.lower() not in self.taken:
                self.taken.add(candidate.lower())
                return candidate


# ---------------------------------------------------------------------------
# Pass 1: validate everything, write nothing
# ---------------------------------------------------------------------------

def parse_catalog(raw, *, update_existing=False):
    plan = CatalogPlan()

    if not raw or not raw.strip():
        plan.add_error(None, "File", ["The file is empty."])
        return plan
    if len(raw) > MAX_IMPORT_BYTES:
        plan.add_error(None, "File", [f"The file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB. Split it into smaller files."])
        return plan

    text = _decode(raw)
    delimiter = _detect_delimiter(text)
    # Spreadsheets that separate columns with ';' write decimals with a comma.
    decimal_comma = delimiter == ";"
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        records = list(reader)
    except csv.Error as exc:
        plan.add_error(None, "File", [f"This does not look like a CSV file ({exc}). Save the sheet as CSV and try again."])
        return plan

    if not records:
        plan.add_error(None, "File", ["The file is empty."])
        return plan

    headers = [_normalize_header(cell) for cell in records[0]]
    if "name" not in headers:
        plan.add_error(
            None,
            "File",
            ["The first row must hold the column names and include 'name'. Download the template to see the layout."],
        )
        return plan
    duplicate_headers = sorted({header for header in headers if header and headers.count(header) > 1})
    if duplicate_headers:
        plan.add_error(None, "File", [f"These columns appear more than once: {', '.join(duplicate_headers)}."])
        return plan
    unknown = [header for header in headers if header and header not in COLUMNS]
    if unknown:
        plan.warnings.append(f"Ignored columns that are not part of the catalog: {', '.join(unknown)}.")
    present = {header for header in headers if header in COLUMNS}

    data_records = records[1:]
    if len(data_records) > MAX_IMPORT_ROWS:
        plan.add_error(None, "File", [f"The file has more than {MAX_IMPORT_ROWS} rows. Split it into smaller files."])
        return plan

    existing_by_sku = {}
    existing_barcodes = {}
    for product in Product.objects.all():
        existing_by_sku[product.sku.lower()] = product
        if product.barcode:
            existing_barcodes[product.barcode.lower()] = product.pk

    known_categories = {name.lower() for name in Category.objects.values_list("name", flat=True)}
    known_brands = {name.lower() for name in Brand.objects.values_list("name", flat=True)}
    known_suppliers = {name.lower() for name in Supplier.objects.values_list("name", flat=True)}
    party_limits = {
        "category": _name_max_length(Category),
        "brand": _name_max_length(Brand),
        "supplier": _name_max_length(Supplier),
    }

    seen_skus = {}
    seen_barcodes = {}
    taken_skus = set(existing_by_sku)
    for record in data_records:
        cells = dict(zip(headers, record))
        sku = _clean_cell(cells.get("sku"))
        if sku:
            taken_skus.add(sku.lower())
    sku_generator = _SkuGenerator(taken_skus)
    default_markup = SystemSettings.load().default_markup_percent
    packaging = packaging_enabled()
    ignored_opening_stock = 0
    # Sizes already in the shop and in this file: (family, size) -> who holds it.
    taken_sizes = {
        (group_name.lower(), size.lower()): pk
        for pk, group_name, size in Product.objects.filter(variant_group__isnull=False).values_list(
            "pk", "variant_group__name", "variant_label"
        )
    }
    seen_sizes = {}

    for row_number, record in enumerate(data_records, start=2):
        cells = {header: _clean_cell(value) for header, value in zip(headers, record) if header in COLUMNS}
        if not any(cells.values()):
            continue

        sku = cells.get("sku", "")
        group_name = cells.get("variant_group", "")
        name = cells.get("name", "") or group_name
        label = name or sku or f"Row {row_number}"
        if sku.upper().startswith(EXAMPLE_SKU_PREFIX):
            plan.skipped_examples += 1
            continue

        problems = []
        existing = existing_by_sku.get(sku.lower()) if sku else None

        if sku:
            if sku.lower() in seen_skus:
                problems.append(f"sku: '{sku}' is also used on row {seen_skus[sku.lower()]}.")
            else:
                seen_skus[sku.lower()] = row_number
        if existing is not None and not update_existing:
            problems.append(
                f"sku: '{sku}' already belongs to '{existing.name}'. "
                "Tick 'Update products that already exist' to change it, or remove this row."
            )

        is_new = existing is None
        product = Product() if is_new else existing
        touched = []
        # What the shop holds for one base unit, read before the row starts changing the product.
        stored_money = {} if is_new else {column: getattr(product, column) for column in PACK_MONEY_COLUMNS}

        def assign(attribute, value):
            setattr(product, attribute, value)
            touched.append(attribute)

        if not name:
            problems.append("name: is required.")
        else:
            assign("name", group_name or name)

        size_label = cells.get("variant_label", "")
        if "variant_group" in present:
            if group_name:
                if len(group_name) > _name_max_length(ProductGroup):
                    problems.append(f"variant_group: is longer than {_name_max_length(ProductGroup)} characters.")
                elif not size_label:
                    problems.append("variant_label: name this size, such as Small, 500 g or Pack of 6.")
                else:
                    key = (group_name.lower(), size_label.lower())
                    holder = taken_sizes.get(key)
                    if key in seen_sizes:
                        problems.append(f"variant_label: '{size_label}' of '{group_name}' is also on row {seen_sizes[key]}.")
                    elif holder is not None and holder != product.pk:
                        problems.append(f"variant_label: '{group_name}' already has a size called '{size_label}'.")
                    else:
                        seen_sizes[key] = row_number
                assign("variant_label", size_label)
            elif size_label:
                problems.append("variant_label: fill variant_group too, with the item's name.")
            else:
                # A blank family cell takes the product out of its family.
                assign("variant_group", None)
                assign("variant_label", "")
        elif size_label:
            problems.append("variant_label: fill variant_group too, with the item's name.")

        if is_new:
            product.sku = sku or sku_generator.next()

        if "barcode" in present:
            barcode = cells.get("barcode", "")
            if barcode:
                owner = existing_barcodes.get(barcode.lower())
                if owner is not None and owner != product.pk:
                    problems.append(f"barcode: '{barcode}' is already used by another product.")
                elif barcode.lower() in seen_barcodes:
                    problems.append(f"barcode: '{barcode}' is also used on row {seen_barcodes[barcode.lower()]}.")
                else:
                    seen_barcodes[barcode.lower()] = row_number
            assign("barcode", barcode or None)

        party_names = {}
        for column in ("category", "brand", "supplier"):
            value = cells.get(column, "")
            if value and len(value) > party_limits[column]:
                problems.append(f"{column}: is longer than {party_limits[column]} characters.")
                value = ""
            party_names[column] = value

        numbers = {}
        for column in (
            "cost_price",
            "markup_percent",
            "markup_amount",
            "price",
            "minimum_margin_percent",
            "opening_stock",
            "low_stock_threshold",
        ):
            value = cells.get(column, "")
            if not value:
                continue
            try:
                numbers[column] = _parse_decimal(value, column, decimal_comma=decimal_comma)
            except _CellError as exc:
                problems.append(str(exc))

        try:
            if cells.get("unit"):
                assign("unit", _parse_unit(cells["unit"]))
            elif is_new:
                product.unit = Product.UNIT_PIECE
            if "expiry_date" in present:
                assign("expiry_date", _parse_date(cells["expiry_date"]) if cells.get("expiry_date") else None)
            if _HAS_TRACK_STOCK and cells.get("track_stock"):
                assign("track_stock", _parse_bool(cells["track_stock"], "track_stock"))
        except _CellError as exc:
            problems.append(str(exc))

        # Packing before money: cost, price and the fixed markup are for the largest pack the row has.
        packing = {}
        for column in PACKING_COLUMNS:
            if not cells.get(column):
                continue
            try:
                packing[column] = _parse_pack_count(cells[column], column)
            except _CellError as exc:
                problems.append(str(exc))
        if packaging:
            for column, count in packing.items():
                assign(column, count)
        elif any(count > 1 for count in packing.values()):
            problems.append(
                f"{next(column for column, count in packing.items() if count > 1)}: cartons and boxes are switched off "
                "in this shop. Switch them on in Settings > Features, or empty the packing columns and give prices per unit."
            )
        factor = product.pack_factor(product.top_pack_level)
        # A packed product in a file that does not state the packing: its prices cannot be read safely.
        packing_unstated = packaging and not is_new and product.has_packaging and len(packing) < len(PACKING_COLUMNS)

        def assign_money(attribute, value):
            """A money cell is for the row's largest pack; the product keeps it for one base unit."""
            if value != quantize_money(value):
                problems.append(f"{attribute}: use at most 2 decimal places.")
                return
            stored = stored_money.get(attribute)
            if stored is not None and quantize_money(Decimal(stored) * factor) == value:
                # The figure the shop already has, as an export writes it: left exactly as stored.
                return
            if packing_unstated:
                problems.append(
                    f"{attribute}: this product is packed ({product.packing_display}). Fill pieces_per_box and "
                    "boxes_per_carton on this row so the figure is read for the right pack."
                )
                return
            assign(attribute, product.quantize_unit_money(value / factor))

        if "cost_price" in numbers:
            assign_money("cost_price", numbers["cost_price"])
        if "minimum_margin_percent" in numbers:
            assign("minimum_margin_percent", numbers["minimum_margin_percent"])
        if "low_stock_threshold" in numbers:
            assign("low_stock_threshold", quantize_qty(numbers["low_stock_threshold"]))
        if "location" in present:
            assign("location", cells.get("location", ""))

        if "markup_percent" in present:
            assign("markup_percent", numbers.get("markup_percent"))
        if "markup_amount" in present:
            if numbers.get("markup_amount") is None:
                assign("markup_amount", None)
            else:
                assign_money("markup_amount", numbers["markup_amount"])
        # One markup at a time: a file that fills only one of the two columns switches the product to it.
        if numbers.get("markup_amount") is not None and "markup_percent" not in present:
            assign("markup_percent", None)
        if numbers.get("markup_percent") is not None and "markup_amount" not in present:
            assign("markup_amount", None)
        if "price" in numbers:
            assign_money("price", numbers["price"])

        if is_new and "price" not in numbers and not product.has_markup and default_markup is not None:
            # Same default the Add Product form offers.
            product.markup_percent = default_markup
        if "price" not in numbers and (is_new or "price" in present):
            if not product.has_markup:
                problems.append("price: enter a sale price, or a markup_percent or markup_amount to calculate it from the cost.")
            elif is_new:
                # No cost yet: the first purchase prices the item from its markup.
                product.price = Decimal("0")
        if not is_new and (touched and "price" not in touched) and product.has_markup:
            # Product.clean() recalculates the price from cost and markup.
            touched.append("price")

        opening_stock = quantize_qty(numbers.get("opening_stock", Decimal("0")))
        if opening_stock > 0:
            if not is_new:
                ignored_opening_stock += 1
                opening_stock = Decimal("0")
            elif product.unit not in Product.FRACTIONAL_UNITS and opening_stock != opening_stock.to_integral_value():
                problems.append(f"opening_stock: '{product.unit}' items are counted in whole numbers.")
            elif opening_stock >= Decimal("1000000000"):
                problems.append("opening_stock: is too large.")
            elif _HAS_TRACK_STOCK and not getattr(product, "track_stock", True):
                problems.append("opening_stock: leave blank for items that do not track stock.")

        if not problems:
            try:
                product.full_clean(exclude=["category", "brand", "supplier", "stock"], validate_unique=False)
            except ValidationError as exc:
                problems.extend(_validation_messages(exc))

        if problems:
            plan.add_error(row_number, label, problems)
            continue

        for column, known, bucket in (
            ("category", known_categories, plan.new_categories),
            ("brand", known_brands, plan.new_brands),
            ("supplier", known_suppliers, plan.new_suppliers),
        ):
            value = party_names[column]
            if value and value.lower() not in known:
                known.add(value.lower())
                bucket.append(value)

        update_fields = list(dict.fromkeys(touched))
        for column in ("category", "brand", "supplier"):
            if column in present:
                update_fields.append(column)

        plan.rows.append(
            RowPlan(
                row_number=row_number,
                product=product,
                is_new=is_new,
                category_name=party_names["category"] if "category" in present else "",
                brand_name=party_names["brand"] if "brand" in present else "",
                supplier_name=party_names["supplier"] if "supplier" in present else "",
                opening_stock=opening_stock,
                update_fields=update_fields,
                variant_group_name=group_name if "variant_group" in present else "",
            )
        )

    if ignored_opening_stock:
        plan.warnings.append(
            f"opening_stock was ignored for {ignored_opening_stock} product(s) that already exist. "
            "Change their stock through Stock Adjustments or a purchase."
        )
    if plan.skipped_examples:
        plan.warnings.append(f"Skipped {plan.skipped_examples} example row(s) from the template.")
    if not plan.rows and not plan.errors:
        plan.add_error(None, "File", ["No product rows were found below the header row."])
    return plan


# ---------------------------------------------------------------------------
# Pass 2: write everything, or nothing
# ---------------------------------------------------------------------------

class _NameResolver:
    """Find a category/brand/supplier by name (any letter case), creating it once when missing."""

    def __init__(self, model):
        self.model = model
        self.cache = {}
        self.created = 0
        for obj in model.objects.order_by("-is_active", "id"):
            self.cache.setdefault(obj.name.lower(), obj)

    def get(self, name):
        if not name:
            return None
        key = name.lower()
        obj = self.cache.get(key)
        if obj is None:
            obj = self.model.objects.create(name=name)
            self.cache[key] = obj
            self.created += 1
        elif not obj.is_active:
            # The file asks for it by name, so it is in use again.
            obj.is_active = True
            obj.save(update_fields=["is_active"])
        return obj


class CatalogImportError(Exception):
    """The file validated but could not be written; nothing was saved."""


def apply_catalog(plan, *, actor=None):
    if not plan.is_valid:
        raise CatalogImportError("The file still has errors.")

    with transaction.atomic():
        categories = _NameResolver(Category)
        brands = _NameResolver(Brand)
        suppliers = _NameResolver(Supplier)
        groups = _NameResolver(ProductGroup)

        for row in plan.rows:
            product = row.product
            try:
                if row.variant_group_name:
                    group = groups.get(row.variant_group_name)
                    product.variant_group = group
                    product.name = group.name
                    for column in ("variant_group", "name"):
                        if column not in row.update_fields:
                            row.update_fields.append(column)
                if row.is_new or "category" in row.update_fields:
                    product.category = categories.get(row.category_name)
                if row.is_new or "brand" in row.update_fields:
                    product.brand = brands.get(row.brand_name)
                if row.is_new or "supplier" in row.update_fields:
                    product.supplier = suppliers.get(row.supplier_name)

                if row.is_new:
                    product.stock = Decimal("0")
                    product.save()
                elif row.update_fields:
                    # Never write the whole row back: stock may have moved since the file was read.
                    product.save(update_fields=row.update_fields)

                if row.opening_stock > 0:
                    create_stock_adjustment(
                        product=product,
                        adjustment_mode=StockAdjustment.OPENING_STOCK,
                        quantity_delta=row.opening_stock,
                        reason="Opening stock (catalog import)",
                        actor=actor,
                    )
            except (IntegrityError, ValueError, ValidationError) as exc:
                reason = " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
                raise CatalogImportError(f"Row {row.row_number} ({product.display_name}): {reason}") from exc

        result = {
            "created": plan.create_count,
            "updated": plan.update_count,
            "opening_stock_rows": plan.opening_stock_count,
            "categories_created": categories.created,
            "brands_created": brands.created,
            "suppliers_created": suppliers.created,
        }
        record_audit(
            action="catalog_imported",
            description=(
                f"Catalog import: {result['created']} product(s) added, {result['updated']} updated, "
                f"{result['opening_stock_rows']} with opening stock."
            ),
            actor=actor,
            metadata=result,
        )
    return result
