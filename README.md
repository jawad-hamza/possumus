# POS IMS

POS IMS is a Django-based point of sale, inventory, parties, expenses, and accounting system built for small and medium retail operations.

It combines day-to-day selling with stock movement, supplier and customer balances, expense tracking, money desk operations, and double-entry accounting so operational actions stay aligned with the books.

## What the system covers

- POS checkout with barcode or SKU scan
- Product, category, brand, supplier, and customer management
- Purchases with cost price, sale price, stock increase, and supplier payable handling
- Credit sales and customer receivable tracking
- Category-based GST or manual cart tax
- Stock control, stock take, sale returns, and purchase returns
- Expense tracking and later payment settlement
- Cash, generic bank, and multiple bank ledgers
- Owner capital, money transfer, and money breakdown views
- Accounting dashboard, trial balance, profit and loss, and balance sheet
- Backup snapshots and audit logging
- Admin and cashier role-based access

## Main apps

- `pos` - POS, products, purchases, parties, dashboard, reports, settings, and operational workflows
- `accounts` - chart of accounts, bank ledgers, vouchers, party ledgers, expenses, money desk, audit log, and backups
- `api` - party statement and settlement endpoints used by the UI
- `possumus` - Django project configuration

## Core business flow

### 1. Setup

1. Create the first admin account from `setup/admin/`.
2. Open `Settings` and configure:
   - business identity and receipt branding
   - GST mode and label
   - POS permissions such as credit sales, price override, and line discount
   - bank ledgers
   - cashier accounts
3. Add categories, brands, and suppliers as needed.

### 2. Build stock through purchases

Use `Purchases` to bring products into inventory. Purchases:

- increase stock quantity
- update product cost and sale pricing
- post inventory value into accounting
- record what was paid now versus what remains payable to the supplier

### 3. Sell through POS

Use `POS` to:

- scan by barcode or SKU
- add products to cart
- edit line price or discount within business rules
- sell for cash, bank, or credit
- attach a customer for credit sales
- print a receipt sized for the configured thermal printer width

### 4. Manage follow-up activity

- `Parties` tracks customer receivables and supplier payables.
- `Expenses` records rent, utilities, transport, and other operating costs.
- `Money Desk` records owner capital, bank transfers, and later expense payments.
- `Stock Control` is for corrections, losses, damages, and stock take differences.
- `Returns` reverse sales or purchases and adjust stock and accounting accordingly.

## Accounting behavior

This system uses double-entry accounting.

Examples:

- Paid purchase: Inventory increases and Cash or Bank decreases.
- Credit purchase: Inventory increases and Accounts Payable increases.
- Cash sale: Cash increases, Sales increases, and stock moves to cost of goods sold.
- Credit sale: Accounts Receivable increases instead of immediate money.
- Unpaid expense: Expense increases and Expense Payable increases.
- Later expense payment: Expense Payable decreases and Cash or Bank decreases.
- Owner capital: Cash or Bank increases and Owner Capital increases.

## Roles

### Admin

- full access across the system
- manage settings, team, pricing rules, backups, and accounting

### Cashier

- access limited to POS and their own receipts
- session login and logout times are recorded
- each sale is tagged to the cashier who created it

## Key operational areas

### POS

- live cart with protected pricing rules
- customer selection for credit sale
- tax mode awareness from settings
- touch-friendly layout

### Purchases

- quick setup for missing supplier, category, brand, or product
- partial payment or full credit handling
- supplier ledger alignment

### Parties

- customer detail and supplier detail pages
- statement history
- settlements through the API-backed flow

### Accounts

- money available now, money breakdown, and bank ledger visibility
- trial balance, profit and loss, balance sheet, and account ledger views
- business-language KPI cards on dashboard

### Settings

- branding and receipt identity
- GST and POS rules
- bank ledgers
- user and role management
- audit history and backup snapshots

## Local development

This project currently runs with:

- Django on SQLite for local use
- Python virtual environment stored in `.venv`
- media uploads for company logos

Typical local commands:

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test
.\.venv\Scripts\python.exe manage.py runserver
```

## Documentation files created in this repository

- `README.md` - project overview and quick start
- `docs/HOW_TO_USE.md` - operator-facing usage guide
- `docs/FULL_DOCUMENTATION.md` - full introduction and technical/business documentation
- `docs/TUTORIAL.md` - step-by-step training guide with workflow screenshots
- `docs/How_to_Use_POS_IMS.pdf` - PDF user guide
- `docs/POS_IMS_Full_Documentation.pdf` - PDF introduction and documentation
- `docs/POS_IMS_Source_Code.pdf` - PDF export of source files
- `docs/POS_IMS_Source_Code.txt` - text export used to build the source-code PDF
- `docs/generate_pdfs.py` - generator script for the documentation PDFs
- `docs/capture_snapshots.py` - renders authenticated HTML pages for documentation screenshots
- `docs/capture_images.ps1` - turns rendered HTML pages into PNG snapshots using a local browser
- `docs/snapshots/images/` - representative app screenshots used in the manuals

## Regenerating the PDFs

After updating the Markdown docs or source files, regenerate the PDFs with:

```powershell
.\.venv\Scripts\python.exe docs\capture_snapshots.py
.\.venv\Scripts\python.exe docs\generate_pdfs.py
```

## Current deployment note

The application is documented here for local and operational use. Before production deployment, review the Django deployment settings separately, especially secret management, hosts, HTTPS, and secure cookie settings.
