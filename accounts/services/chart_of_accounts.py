"""Default chart of accounts and the lookup used by every posting service."""

from accounts.models import Account

DEFAULT_ACCOUNTS = (
    ("Cash", Account.ASSET),
    ("Bank", Account.ASSET),
    ("Accounts Receivable", Account.ASSET),
    ("Inventory", Account.ASSET),
    ("Accounts Payable", Account.LIABILITY),
    ("Sales Tax Payable", Account.LIABILITY),
    ("Expense Payable", Account.LIABILITY),
    ("Loan Payable", Account.LIABILITY),
    ("Owner Capital", Account.EQUITY),
    ("Opening Balance Equity", Account.EQUITY),
    ("Sales", Account.INCOME),
    ("Cost of Goods Sold", Account.EXPENSE),
    ("Purchase", Account.EXPENSE),
    ("Salaries Expense", Account.EXPENSE),
    ("Interest Expense", Account.EXPENSE),
)
DEFAULT_ACCOUNT_TYPES = dict(DEFAULT_ACCOUNTS)


def ensure_default_accounts():
    """Create any missing default account. Returns the titles that were created."""
    created = []
    for title, account_type in DEFAULT_ACCOUNTS:
        _, was_created = Account.objects.get_or_create(title=title, defaults={"type": account_type})
        if was_created:
            created.append(title)
    return created


def get_core_account(title: str) -> Account:
    """Return an active account, creating it first when it is one of the defaults."""
    account = Account.objects.filter(title=title).first()
    if account is None and title in DEFAULT_ACCOUNT_TYPES:
        account, _ = Account.objects.get_or_create(title=title, defaults={"type": DEFAULT_ACCOUNT_TYPES[title]})
    if account is None or not account.is_active:
        raise ValueError(f"Required account not found or inactive: {title}")
    return account
