# POS IMS Step-by-Step Tutorial

This tutorial is written as a practical guided walkthrough. It is meant for a business owner, manager, or staff member learning the system from zero.

## 1. Sign in and understand the workspace

Start by signing in with your admin account. After login, the system opens the operational dashboard where you can quickly see the condition of sales, stock, money, dues, and business activity.

### Dashboard overview

![Dashboard snapshot](snapshots/images/dashboard.png)

What to notice first:

- top KPI cards for urgent business numbers
- navigation on the left for each major work area
- quick links into POS, purchases, parties, accounts, and reports

## 2. Set business rules before live use

Before selling to real customers, make sure the business rules are decided.

Configure:

- branding and receipt identity
- GST or VAT mode
- whether credit sale is allowed
- whether cashiers may override price
- whether cashiers may give line discount
- thermal printer width
- bank ledgers
- staff login accounts

If needed, do this from `Settings` before starting operations.

## 3. Review the product catalog

Open the products area to see sale items, stock, category, brand, supplier, and location information.

### Products page

![Products snapshot](snapshots/images/products.png)

Use this page to:

- search items live by name, SKU, barcode, brand, category, or location
- sort and filter stock
- inspect where products are stored
- open product details

## 4. Add or receive stock through purchases

For most businesses, the best place to create real saleable stock is the purchase workflow.

### Purchase list and purchase intake

![Purchases snapshot](snapshots/images/purchases.png)

Workflow:

1. Open `Purchases`.
2. Add a supplier or choose an existing one.
3. Create missing brand, category, or product if necessary.
4. Add purchase lines with quantity, unit cost, and sale price.
5. Enter how much was paid now.
6. Save the purchase.

What the system does:

- increases stock
- updates product cost and sale price
- increases inventory value
- creates supplier payable if anything remains unpaid

## 5. Inspect a purchase document

After creating a purchase, open the purchase detail to review the bill and balance.

### Purchase detail

![Purchase detail snapshot](snapshots/images/purchase_detail.png)

Use this view to:

- confirm line items
- review total, paid, and due values
- open follow-up actions such as edit or return

## 6. Use POS for selling

The POS page is the main selling workspace.

### POS checkout screen

![POS snapshot](snapshots/images/pos.png)

Daily flow:

1. Scan barcode or type SKU.
2. Search products while typing if needed.
3. Add items to cart.
4. Adjust quantity.
5. Edit unit price or line discount if the rules allow it.
6. Choose payment method.
7. Choose customer if the sale is on credit.
8. Complete checkout.

Important rule:

Even if a user edits price, the system still protects minimum margin rules in the background.

## 7. Review the invoice

Once a sale is completed, the invoice page becomes the proof of sale and the base for any future return.

### Invoice page

![Invoice snapshot](snapshots/images/invoice.png)

This page is important because it:

- shows sold items and totals
- shows the operator who made the sale
- supports the print flow
- acts as the reference point for sale return

## 8. Track customers and suppliers

The `Parties` section is where you follow who owes the business and who the business owes.

### Parties home

![Parties snapshot](snapshots/images/parties.png)

### Customers list

![Customers snapshot](snapshots/images/customers.png)

### Suppliers list

![Suppliers snapshot](snapshots/images/suppliers.png)

Use these pages to:

- find customers and suppliers quickly
- inspect balances
- open detailed statements
- settle old balances

## 9. Review a customer ledger

Customer detail pages are especially important for credit sales and collection follow-up.

### Customer detail

![Customer detail snapshot](snapshots/images/customer_detail.png)

From here you can:

- view customer information
- inspect statement rows
- understand current receivable position
- follow settlement history

## 10. Review a supplier ledger

Supplier detail pages are the equivalent view for payables.

### Supplier detail

![Supplier detail snapshot](snapshots/images/supplier_detail.png)

From here you can:

- understand total purchases from that supplier
- see current due balance
- review payment history
- settle old payable balances

## 11. Use Stock Control only for corrections

Stock Control is not the normal entry point for buying or selling. It is used when real stock and system stock do not match.

### Stock Control page

![Stock control snapshot](snapshots/images/stock_control.png)

Use it for:

- damaged stock
- lost stock
- stock take difference
- opening balance correction

## 12. Record expenses

Use the expense page for operating costs like rent, electricity, gas, internet, transport, and repairs.

### Expenses page

![Expenses snapshot](snapshots/images/expenses.png)

Process:

1. Create or choose an expense type.
2. Enter the bill amount.
3. Enter any amount paid now.
4. Save the expense.
5. If money remains unpaid, the due amount stays visible as a liability.

## 13. Use Money Desk to explain money movement

Money Desk is where non-sale money operations are recorded.

### Money Desk page

![Money Desk snapshot](snapshots/images/money_desk.png)

Use this area to:

- record owner capital or opening funds
- transfer money between cash and bank
- pay old unpaid expense bills

This is the page that keeps `Money Available Now` meaningful and traceable.

## 14. Understand the accounting dashboard

The accounts dashboard translates accounting into business-friendly language.

### Accounts overview

![Accounts snapshot](snapshots/images/accounts.png)

Use it to monitor:

- available money
- customers owing you
- suppliers you still owe
- operating bills due
- stock value
- links to financial statements

## 15. Drill into money movement

If someone asks why the money card changed, the money breakdown page answers it.

### Money breakdown

![Money breakdown snapshot](snapshots/images/money_breakdown.png)

This page helps explain:

- how cash changed
- how bank changed
- what vouchers caused movement
- whether money came in or went out

## 16. Read reports

The reports area is for management review rather than transaction entry.

### Reports home

![Reports snapshot](snapshots/images/reports.png)

### Daily sales report

![Daily sales report snapshot](snapshots/images/daily_sales_report.png)

These pages help with:

- daily performance review
- gross profit visibility
- invoice count and collection tracking
- low stock follow-up

## 17. Daily operating order recommended for staff

A practical routine for daily use is:

1. review dashboard
2. receive stock through purchases if new goods arrive
3. sell through POS
4. review customer and supplier balances when needed
5. record expenses the same day
6. use Money Desk for capital, transfers, and later bill payments
7. review reports at closing time

## 18. Closing notes

The biggest strength of this system is alignment:

- sales are not separate from stock
- stock is not separate from purchases
- purchases are not separate from supplier balances
- expenses are not separate from liabilities
- money is not separate from accounting

That makes the software useful not just as a billing screen, but as an operational system for running the business.
