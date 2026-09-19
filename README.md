# ZehnMS

ZehnMS is a point of sale, inventory and double-entry accounting system for small and
medium shops of any kind, in any country. It is a server-rendered Django application that
ships in three forms: an offline Windows installer for a single shop, a Vercel deployment,
or a classic VPS install.

One install or deployment serves one shop.

## What it covers

- POS checkout with barcode / SKU scan, held carts, cash, bank and credit sales, thermal receipts and PDF invoices
- Products with units (`pcs`, `box`, `pack`, `dozen`, `kg`, `g`, `l`, `ml`, `m`, `ft`); weighed and measured goods sell in fractions (1 kg, 0.5 kg, 250 g) with quantities kept to three decimals
- Pricing by fixed sale price or by **markup percent on cost**, alongside a minimum-margin guard
- Configurable currency symbol and code, configurable tax label and tax mode (per category, per payment method, or manual)
- Purchases with weighted-average cost, supplier payables, purchase returns
- Sale returns, stock adjustments, opening stock, low-stock and expiry reports
- Customers and suppliers with ledgers, statements and settlements
- Expenses, money desk (owner capital, transfers), multiple bank ledgers
- Trial balance, profit and loss, balance sheet, account ledgers
- Owner, cashier and custom roles; per-user timezones; audit log; backups
- Trial period and offline licence files for the desktop build; optional online subscription billing and Google sign-in for hosted deployments

## Quick start (development)

Python 3.12 or newer (developed and tested on 3.14).

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

On Linux/macOS use `python3 -m venv .venv` and `.venv/bin/python` instead.

Open http://127.0.0.1:8000/, create the owner account and the business, then add products or
record a purchase and start selling. No configuration is needed for development: SQLite,
debug mode and console email are the defaults. To change anything, copy `.env.example` to
`.env`; it documents every setting.

```powershell
.\.venv\Scripts\python.exe manage.py test
.\.venv\Scripts\python.exe manage.py check --deploy
python -m pyflakes pos accounts api possumus desktop scripts
```

## Ways to ship it

| Target | Command | Details |
| --- | --- | --- |
| Windows installer (offline, one shop) | `powershell -ExecutionPolicy Bypass -File desktop\build.ps1` | [desktop/README.md](desktop/README.md) |
| Vercel + hosted Postgres | `vercel deploy --prod` | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#3-vercel) |
| VPS (gunicorn, PostgreSQL, nginx) | `pip install -r requirements-server.txt` | [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#4-vps-gunicorn--postgresql--nginx) |

`GET /healthz/` answers `{"status": "ok", "db": "ok"}` on every target.

## Project layout

| Path | Content |
| --- | --- |
| `pos/` | Main app: POS, products, purchases, parties, reports, settings, onboarding, billing, licensing. Views live in the `pos/views/` package, business logic in `pos/services/` |
| `accounts/` | Ledger: chart of accounts, vouchers, journal posting, expenses, money desk, statements, audit log, backups (`accounts/services/`) |
| `api/` | Two DRF endpoints used by the UI (party statement, settlements); session authentication only |
| `possumus/` | Django project package: `settings.py` (environment driven), `urls.py`, `wsgi.py` |
| `desktop/` | Windows launcher, PyInstaller spec, Inno Setup script, build script |
| `scripts/vercel_build.py`, `vercel.json`, `.python-version`, `.vercelignore` | Vercel deployment |
| `licensing/` | Licence key pair location (`public_key.pem` is bundled; `private_key.pem` is git-ignored and must never be shipped) |
| `docs/` | User guide, tutorial, full documentation, deployment guide |

Requirements files: `requirements.txt` (runtime, exact pins, cross-platform),
`requirements-server.txt` (+ gunicorn), `requirements-desktop.txt` (+ waitress, PyInstaller),
`requirements-dev.txt` (+ waitress, pyflakes, ruff).

## How the books stay right

Every operational action posts a balanced journal entry:

- Paid purchase: Inventory up, Cash or Bank down. Credit purchase: Inventory up, Accounts Payable up.
- Cash sale: Cash up, Sales up, and cost moves from Inventory to Cost of Goods Sold. Credit sale: Accounts Receivable instead of cash.
- Unpaid expense: Expense up, Expense Payable up; paying it later moves Expense Payable against Cash or Bank.
- Owner capital: Cash or Bank up, Owner Capital up. Opening stock credits Opening Balance Equity.
- Returns reverse the original sale or purchase, stock included.

Money is `Decimal` rounded through `quantize_money`; quantities are `Decimal` with three
places through `quantize_qty`; stock changes lock the product row. The default chart of
accounts is created when the business is set up.

## Roles

- **Owner / admin**: everything, including settings, team, pricing rules, backups and accounting. The owner cannot be demoted or disabled.
- **Cashier**: POS and their own receipts; can return only their own sales; sign-in and sign-out times are recorded.
- **Custom roles**: per-module view / manage access.

## Documentation

- [docs/HOW_TO_USE.md](docs/HOW_TO_USE.md): operator guide
- [docs/TUTORIAL.md](docs/TUTORIAL.md): step-by-step training walkthrough
- [docs/FULL_DOCUMENTATION.md](docs/FULL_DOCUMENTATION.md): business and technical reference
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): local, desktop installer, Vercel, VPS
- [desktop/README.md](desktop/README.md): building and supporting the Windows installer
- `.env.example`: every configuration variable

The PDF manuals in `docs/` are generated from the Markdown files:

```powershell
.\.venv\Scripts\python.exe docs\capture_snapshots.py
.\.venv\Scripts\python.exe docs\generate_pdfs.py
```
