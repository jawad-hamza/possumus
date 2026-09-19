"""Switchable feature modules: the vendor sets the ceiling, the shop toggles within it.

Two layers decide whether a module is on:

1. The CEILING comes from the signed licence key (``modules`` list in the payload).
   No list means everything, which is the Enterprise edition, an old key, or the trial.
2. Inside the ceiling the shop owner switches modules on and off in Settings > Features.

``module_enabled(key)`` is the one question the rest of the app asks. The answer is
computed once per request (see ``ModuleStateMiddleware``) and thrown away when the
settings row or the licence changes, so templates and decorators never add a query.

A few modules already had a switch on SystemSettings before this registry existed
(tax, expiry, price override, line discount). Those are not duplicated: the module IS
that flag (``settings_flags``), and ``SystemSettings.load()`` masks the flag to False
whenever the licence does not include the module.
"""
from contextvars import ContextVar
from dataclasses import dataclass

# Areas that can never be switched off: without them there is no shop to run.
CORE_MODULES = ("dashboard", "pos", "products", "settings")


@dataclass(frozen=True)
class FeatureModule:
    key: str
    label: str
    description: str
    # Plain words for the Settings page: what disappears when this is off.
    hides: str
    icon: str = "bi-toggle-on"
    group: str = "Back office"
    # Keys of pos.services.access.MODULE_DEFINITIONS that this module gates as a whole.
    access_modules: tuple = ()
    # The module is these existing SystemSettings booleans: on when any of them is on.
    settings_flags: tuple = ()
    # Other SystemSettings booleans that are forced off while this module is off.
    dependent_flags: tuple = ()


FEATURE_MODULES = (
    FeatureModule(
        "purchases",
        "Purchases",
        "Record supplier bills so stock and cost update themselves.",
        "Purchases menu, purchase bills, supplier payables on the dashboard.",
        icon="bi-box-seam",
        group="Back office",
        access_modules=("purchases",),
    ),
    FeatureModule(
        "stock_control",
        "Stock control",
        "Stock adjustments, stock take and opening stock corrections.",
        "Stock control menu and the adjustment screens.",
        icon="bi-clipboard-check",
        group="Back office",
        access_modules=("stock_control",),
    ),
    FeatureModule(
        "parties",
        "Customers and suppliers",
        "Party lists, statements, settlements and selling on credit.",
        "Parties menu, party statements, credit sales at the till, the Sales by Customer report.",
        icon="bi-people",
        group="Back office",
        access_modules=("parties",),
        dependent_flags=("allow_credit_sales",),
    ),
    FeatureModule(
        "returns",
        "Returns",
        "Take goods back from a customer or send them back to a supplier.",
        "Return buttons on invoices and purchase bills, the Returns report.",
        icon="bi-arrow-counterclockwise",
        group="Back office",
    ),
    FeatureModule(
        "expenses",
        "Expenses",
        "Rent, wages, utilities and other bills, paid or unpaid.",
        "Expenses menu and the expense cards on the dashboard and reports.",
        icon="bi-receipt-cutoff",
        group="Money",
        access_modules=("expenses",),
    ),
    FeatureModule(
        "accounts",
        "Accounts",
        "Ledgers, money desk, trial balance, profit and loss, balance sheet.",
        "Accounts menu and every financial statement. The books still post in the background.",
        icon="bi-journal-text",
        group="Money",
        access_modules=("accounts",),
    ),
    FeatureModule(
        "tax",
        "Tax",
        "Charge GST, VAT or sales tax on sales.",
        "Tax on the till and receipts, the Tax Summary report.",
        icon="bi-percent",
        group="Money",
        settings_flags=("gst_enabled",),
    ),
    FeatureModule(
        "reports",
        "Reports",
        "Sales, stock and team reports with CSV export.",
        "Reports menu and report links across the app.",
        icon="bi-graph-up",
        group="Back office",
        access_modules=("reports",),
    ),
    FeatureModule(
        "expiry",
        "Expiry tracking",
        "Expiry dates on products and purchases with early warnings.",
        "Expiry date fields, expiry alerts and the Expiry report.",
        icon="bi-hourglass-split",
        group="Products",
        settings_flags=("enable_product_expiry",),
    ),
    FeatureModule(
        "weights_units",
        "Weights and measures",
        "Sell by kg, gram, litre, metre or foot in fractions.",
        "Weight, volume and length units in the product form. Items stay whole-number.",
        icon="bi-speedometer",
        group="Products",
    ),
    FeatureModule(
        "markup",
        "Markup pricing",
        "Set the sale price as cost plus a percentage or a fixed amount.",
        "Markup fields on products and the default markup setting.",
        icon="bi-calculator",
        group="Products",
    ),
    FeatureModule(
        "variants",
        "Sizes and variants",
        "Sell one item in several sizes, each with its own price and stock.",
        "The sizes section on products, family rows in the product list and the size picker at the till. Sizes stay on sale as separate products.",
        icon="bi-collection",
        group="Products",
    ),
    FeatureModule(
        "catalog_io",
        "Catalogue import and export",
        "Load or download the product list as a CSV file.",
        "Import and Export buttons on the product list.",
        icon="bi-filetype-csv",
        group="Products",
    ),
    FeatureModule(
        "price_control",
        "Price override and line discounts",
        "Let the cashier change a price or discount one line at the till.",
        "Price and discount boxes on cart lines.",
        icon="bi-tag",
        group="Till",
        settings_flags=("allow_price_override", "allow_line_discount"),
    ),
    FeatureModule(
        "hold_sale",
        "Hold and resume sale",
        "Park a sale and serve the next customer.",
        "Hold button and held sales on the till.",
        icon="bi-pause-circle",
        group="Till",
    ),
    FeatureModule(
        "team",
        "Team and roles",
        "More logins, cashiers and custom roles.",
        "Adding users and roles, the Employees report. Existing logins keep working.",
        icon="bi-person-badge",
        group="Administration",
    ),
    FeatureModule(
        "backups",
        "Backups",
        "Take, download, upload and restore backup snapshots.",
        "Backups tab in Settings.",
        icon="bi-safe2",
        group="Administration",
    ),
)
MODULE_LOOKUP = {module.key: module for module in FEATURE_MODULES}
MODULE_KEYS = tuple(module.key for module in FEATURE_MODULES)

# Reports that belong to a module (slug in pos.services.reporting). A report without an
# entry only needs the Reports module itself.
REPORT_MODULES = {
    "sales-by-customer": "parties",
    "tax-summary": "tax",
    "returns": "returns",
    "expiry": "expiry",
    "employees": "team",
}

# ---------------------------------------------------------------------------- editions
# OPEN DECISION (see PRODUCT.md): which editions exist and what each one includes is not
# final. These presets are plain data used by `manage.py license_issue --edition`; edit
# the tuples freely. A key that was already issued keeps the list that was signed into it.
# "modules": None means every module, including ones added in later versions.
EDITION_BASIC = "basic"
EDITION_STANDARD = "standard"
EDITION_ENTERPRISE = "enterprise"
_BASIC_MODULES = ("reports", "price_control", "hold_sale", "backups")
_STANDARD_MODULES = _BASIC_MODULES + (
    "purchases",
    "stock_control",
    "parties",
    "returns",
    "expenses",
    "tax",
    "variants",
    "catalog_io",
    "team",
)
EDITIONS = {
    EDITION_BASIC: {
        "label": "Basic",
        "summary": "Till, products and simple reports.",
        "modules": _BASIC_MODULES,
    },
    EDITION_STANDARD: {
        "label": "Standard",
        "summary": "Basic plus purchases, parties, returns, expenses, stock control, tax, sizes and team.",
        "modules": _STANDARD_MODULES,
    },
    EDITION_ENTERPRISE: {
        "label": "Enterprise",
        "summary": "Everything: adds accounts, expiry, weights and measures, markup pricing.",
        "modules": None,
    },
}


class ModuleError(ValueError):
    """Raised with a message that is safe to show to the shop owner or the vendor."""


class ModuleLocked(ModuleError):
    """The licence does not include this module, so it cannot be switched on."""


def get_module(key):
    try:
        return MODULE_LOOKUP[key]
    except KeyError as exc:
        raise ModuleError(f"Unknown module '{key}'.") from exc


def parse_module_list(text):
    """Vendor side: 'purchases, parties' -> ('purchases', 'parties'), rejecting unknown names."""
    if isinstance(text, str):
        names = [part.strip().lower() for part in text.split(",")]
    else:
        names = [str(part).strip().lower() for part in (text or ())]
    names = [name for name in names if name]
    unknown = sorted({name for name in names if name not in MODULE_LOOKUP})
    if unknown:
        raise ModuleError(
            f"Unknown module{'s' if len(unknown) != 1 else ''}: {', '.join(unknown)}. "
            f"Choose from: {', '.join(MODULE_KEYS)}."
        )
    # Registry order, no repeats, so the same choice always signs the same payload.
    return tuple(key for key in MODULE_KEYS if key in set(names))


def edition_modules(edition):
    """Module tuple of an edition preset; None means everything."""
    edition = (edition or "").strip().lower()
    if edition not in EDITIONS:
        raise ModuleError(f"Unknown edition '{edition}'. Choose from: {', '.join(EDITIONS)}.")
    modules = EDITIONS[edition]["modules"]
    return None if modules is None else parse_module_list(modules)


def edition_label(edition):
    edition = (edition or "").strip().lower()
    if edition in EDITIONS:
        return EDITIONS[edition]["label"]
    return edition.replace("_", " ").title()


# ------------------------------------------------------------------------------- state
@dataclass(frozen=True)
class ModuleState:
    # None: no ceiling, every module may be used.
    ceiling: frozenset | None
    edition: str
    # "licence" when a licence key is in force, otherwise "open" (trial, subscription).
    source: str
    enabled: frozenset

    def within_ceiling(self, key):
        return self.ceiling is None or key in self.ceiling


# None outside a request: nothing is cached, every question is answered from the database.
# Inside a request the middleware puts a dict here and the first answer is kept in it.
_request_scope = ContextVar("zehnms_module_state_scope", default=None)


def begin_request_scope():
    return _request_scope.set({})


def end_request_scope(token=None):
    if token is not None:
        _request_scope.reset(token)
    else:
        _request_scope.set(None)


def invalidate_module_state():
    """Call after the settings row or the licence changes."""
    holder = _request_scope.get()
    if holder is not None:
        holder.clear()


def licence_ceiling():
    """(ceiling, edition, source) from the licence in force. No licence means no ceiling."""
    from pos.services.licensing import get_active_license

    active = get_active_license()
    if active is None:
        return None, "", "open"
    modules = getattr(active, "modules", None)
    edition = getattr(active, "edition", "") or ""
    if modules is None:
        return None, edition, "licence"
    return frozenset(key for key in modules if key in MODULE_LOOKUP), edition, "licence"


def _compute_state(settings_row=None):
    if settings_row is None:
        from pos.models import SystemSettings

        settings_row = SystemSettings.load_raw()
    ceiling, edition, source = licence_ceiling()
    switched_off = {str(key) for key in (settings_row.disabled_modules or []) if isinstance(key, str)}
    masked = getattr(settings_row, "_masked_flags", None) or {}

    enabled = set()
    for module in FEATURE_MODULES:
        if ceiling is not None and module.key not in ceiling:
            continue
        if module.settings_flags:
            # A masked flag reads False on the instance; the stored value is what the owner chose.
            if any(masked.get(flag, getattr(settings_row, flag, False)) for flag in module.settings_flags):
                enabled.add(module.key)
        elif module.key not in switched_off:
            enabled.add(module.key)
    return ModuleState(ceiling=ceiling, edition=edition, source=source, enabled=frozenset(enabled))


def get_module_state(settings_row=None):
    holder = _request_scope.get()
    if holder is not None and "state" in holder:
        return holder["state"]
    state = _compute_state(settings_row)
    if holder is not None:
        holder["state"] = state
    return state


def module_enabled(key):
    """THE question: is this module inside the licence ceiling AND switched on by the shop?"""
    if key in CORE_MODULES:
        return True
    get_module(key)
    return key in get_module_state().enabled


def module_within_ceiling(key):
    if key in CORE_MODULES:
        return True
    get_module(key)
    return get_module_state().within_ceiling(key)


def access_module_enabled(access_key):
    """For the role system: an access area is open unless a module that owns it is off."""
    state = None
    for module in FEATURE_MODULES:
        if access_key in module.access_modules:
            state = state or get_module_state()
            if module.key not in state.enabled:
                return False
    return True


def module_for_access_key(access_key):
    for module in FEATURE_MODULES:
        if access_key in module.access_modules:
            return module
    return None


def enabled_module_map():
    """{key: bool} for templates (context name: feature_modules)."""
    enabled = get_module_state().enabled
    return {key: key in enabled for key in MODULE_KEYS}


def report_enabled(slug):
    """Should this report be offered? (The Reports module itself is checked by the access layer.)

    A module that is an existing settings flag (tax, expiry) only hides its report when the
    edition leaves it out: with the flag merely off the report still explains past data.
    """
    module_key = REPORT_MODULES.get(slug)
    if module_key is None:
        return True
    if MODULE_LOOKUP[module_key].settings_flags:
        return module_within_ceiling(module_key)
    return module_enabled(module_key)


def module_disabled_message(key):
    module = MODULE_LOOKUP.get(key)
    label = module.label if module else str(key).replace("_", " ").title()
    if module is not None and not module_within_ceiling(key):
        return f"{label} is not included in your edition."
    return f"{label} is switched off. An admin can turn it on in Settings > Features."


# ----------------------------------------------------------------- settings flag mask
def apply_module_mask(settings_obj):
    """Force the mapped SystemSettings booleans to False for modules that are not available.

    The stored value is remembered on the instance and written back by save(), so a shop
    that upgrades its licence gets its own choices back.
    """
    state = get_module_state(settings_row=settings_obj)
    masked = {}
    for module in FEATURE_MODULES:
        if module.settings_flags and not state.within_ceiling(module.key):
            flags = module.settings_flags
        elif module.dependent_flags and module.key not in state.enabled:
            flags = module.dependent_flags
        else:
            continue
        for flag in flags:
            if getattr(settings_obj, flag, False):
                masked[flag] = True
                setattr(settings_obj, flag, False)
    settings_obj._masked_flags = masked
    return settings_obj


def locked_settings_flags():
    """SystemSettings booleans the settings form must not let anyone turn on."""
    state = get_module_state()
    flags = set()
    for module in FEATURE_MODULES:
        if module.settings_flags and not state.within_ceiling(module.key):
            flags.update(module.settings_flags)
        elif module.dependent_flags and module.key not in state.enabled:
            flags.update(module.dependent_flags)
    return flags


# -------------------------------------------------------------------- the owner's switch
def set_module_switch(key, on):
    """Turn one module on or off. Raises ModuleLocked when the licence does not include it."""
    from pos.models import SystemSettings

    module = get_module(key)
    on = bool(on)
    if on and not module_within_ceiling(key):
        raise ModuleLocked(f"{module.label} is not included in your edition.")

    row = SystemSettings.load_raw()
    if module.settings_flags:
        for flag in module.settings_flags:
            setattr(row, flag, on)
        row.save(update_fields=[*module.settings_flags, "updated_at"])
    else:
        switched_off = [item for item in (row.disabled_modules or []) if item in MODULE_LOOKUP and item != key]
        if not on:
            switched_off.append(key)
        row.disabled_modules = [item for item in MODULE_KEYS if item in set(switched_off)]
        row.save(update_fields=["disabled_modules", "updated_at"])
    invalidate_module_state()
    return module


def features_overview():
    """Rows for Settings > Features, grouped for display."""
    state = get_module_state()
    groups = {}
    for module in FEATURE_MODULES:
        locked = not state.within_ceiling(module.key)
        groups.setdefault(module.group, []).append(
            {
                "module": module,
                "key": module.key,
                "locked": locked,
                "enabled": module.key in state.enabled,
            }
        )
    return [{"name": name, "rows": rows} for name, rows in groups.items()]


def edition_summary():
    state = get_module_state()
    if state.source != "licence":
        label = "All features"
        note = "Every feature is available while no licence key is in force."
    elif state.ceiling is None:
        label = edition_label(state.edition) or "Enterprise"
        note = "Your licence includes every feature."
    else:
        label = edition_label(state.edition) or "Custom"
        note = "Your licence decides which features can be switched on."
    return {
        "label": label,
        "note": note,
        "has_ceiling": state.ceiling is not None,
        "enabled_count": len(state.enabled),
        "available_count": len(MODULE_KEYS) if state.ceiling is None else len(state.ceiling),
        "total_count": len(MODULE_KEYS),
    }


# ------------------------------------------------------------------------------- units
def allowed_unit_choices(current_unit=None):
    """Product unit choices: whole-number units only while Weights & Measures is off.

    A product that already uses a fractional unit keeps it, so editing it never breaks.
    """
    from pos.models import Product

    if module_enabled("weights_units"):
        return list(Product.UNIT_CHOICES)
    return [
        (value, label)
        for value, label in Product.UNIT_CHOICES
        if value not in Product.FRACTIONAL_UNITS or value == current_unit
    ]
