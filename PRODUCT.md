# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Two people use it in the same working day, and both matter equally.

- **At the counter:** the shop owner or a hired cashier rings up sales on a desktop or laptop with keyboard, mouse and a USB barcode scanner. Customers are waiting, the shop is noisy, training is minimal. The job is to find or scan items, take weights and quantities, apply a price or discount, take payment or put it on credit, and hand over a receipt, without leaving the till screen.
- **Away from the counter:** the owner, often on a phone or tablet, checks how the day went, what is running low, who owes money and what is owed to suppliers, and records purchases, expenses and payments.

Staff accounts have roles. Cashiers see the till and their own receipts; the owner sees everything.

## Product Purpose

ZehnMS runs a shop end to end: selling, stock, purchasing, customers and suppliers, expenses, and the books. Every sale, purchase, return, expense and payment posts to a double-entry ledger automatically, so the owner gets a true profit and loss, balance sheet and party balances without a separate accounting tool.

Success is a shopkeeper who can sell within minutes of installing, trusts the stock and money figures, and never needs to touch a second program.

## Positioning

A complete solution for any shop, in one product, that each shop can cut down to what it needs.

- **Works fully offline, paid once.** Installed on the shop's own PC from a Windows setup file, runs with no internet, data stays in the shop, unlocked by a licence key after a 10-day trial.
- **Real accounting built in**, not just invoices.
- **Simple to learn**: modules a shop does not need are switched off so the screen stays small.
- **Fits any trade**: weights and units, markup-on-cost pricing, minimum margin, expiry dates, flexible tax, configurable currency.

The owner of ZehnMS sets the ceiling; the shop toggles within it. An edition or licence fixes which modules an install includes. The Enterprise edition includes everything. Inside that ceiling the shop owner can turn modules on and off in Settings.

## Operating Context

- First market is Pakistan with an English interface. Currency (Rs by default), tax label and rates are settings, not constants; other countries follow later.
- Hardware at the counter: PC or laptop, USB barcode scanner acting as a keyboard, thermal receipt printer (paper width is a setting, 50 mm and up), sometimes a weighing scale read by eye.
- Goods are sold by piece, box, pack or dozen, and by weight, volume or length (flour at 1 kg or 500 g). Quantities carry up to three decimals.
- Pricing is set by typing a sale price, or as a markup on cost given either as a percentage or as a fixed amount (cost + Rs 20); a minimum margin is enforced either way. Cost is a weighted average of purchases.
- One item often comes in sizes at different prices (bread: small, medium, large). Sizes are grouped variants under one product name; each size keeps its own code, price, cost and stock, and the till offers a size picker.
- Adding a supporting record (a brand, a supplier, a customer) must never cost the user his place or his typing: it is added in place beside the form, or the app returns to exactly where he started.
- Credit sales to named customers and credit purchases from suppliers are normal; party ledgers and settlements are daily work.
- Delivery forms: a Windows installer for one shop (offline, SQLite), and a hosted deployment (Vercel with Postgres). The installed edition opens as a standalone application window with its own icon, like Vyapar: no browser tabs or address bar, and the shop never sees a URL. This must not cost performance, so the window uses the system web engine rather than a bundled browser, and the app's assets are served locally. An online subscription (SaaS) model comes later; today one install or deployment serves one business.
- A vendor-side builder is planned: choose the features for a customer, and it produces a setup file with that feature set.

## Capabilities and Constraints

Confirmed and present in the codebase: till with cart, barcode scan, line and cart discounts, price override, credit sales, cash and bank payments, hold and resume sale; products with categories, brands, suppliers, units, markup, expiry; purchases with supplier payables; sale and purchase returns; stock adjustments, stock take, opening stock; customers and suppliers with statements and settlements; expenses, bank accounts, transfers, owner capital; trial balance, profit and loss, balance sheet, ledgers; sales, stock, expiry and employee reports; thermal receipt and PDF invoice; roles and custom roles with per-module access; audit log; backup and restore; trial and licence gating.

Constraints:
- Django server-rendered templates with Bootstrap 5; no frontend build step. The desktop edition must work with no internet, so nothing the interface needs may depend on a CDN at run time.
- One business per install. Multi-tenant SaaS is deferred.
- English only for now; no right-to-left requirement yet.
- Money is two-decimal; quantities three-decimal.

Open decisions:
- The exact list of switchable modules and which editions exist below Enterprise.
- How the setup builder is delivered (desktop tool or web page) and whether the feature ceiling travels in the licence key, the build, or both.
- Pricing and plan names. The Starter, Growth and Scale plans and checkout links in the code are placeholders.

## Brand Commitments

- Name: **ZehnMS**. The code still says "Vendora" in user-facing places and "Possumus" / "POS IMS" in older ones; all of these are to be replaced. The internal Python package stays `possumus`.
- The owner named the Vyapar app as the usability reference: everyday shop tasks done in one flow, without hopping between screens.
- Standing visual preference (owner's decision, 2026-09-19, after seeing a ledger-book themed build): a familiar, modern business-app look everywhere, the till included. It should sit comfortably beside Vyapar, Zoho Books / QuickBooks and Square POS / Loyverse, and match their level of polish. Clean and calm: few lines and borders, generous breathing room, a neutral white look rather than tinted or themed colours.
- Guidance and hints belong in hover (and tap) tooltips, not in sentences printed on the page.
- The till's layout and flow from the earlier build are liked and stay: one scan line, the bill in the middle edited in place, the total as the largest figure, tender and change, F-key payments, side tasks beside the bill.
- No logo has been fixed yet.

## Evidence on Hand

- User guides with screenshots of the current interface: `docs/HOW_TO_USE.md`, `docs/TUTORIAL.md`, `docs/FULL_DOCUMENTATION.md`, `docs/snapshots/`.
- A working local database with demo data (`db.sqlite3`, not in git).
- `company_logos/FutureSpace_png_logo.png` is a client's logo, not the product's.
- There are no testimonials, customer names, usage numbers, press or real prices. Future work must not invent them.

## Product Principles

1. **The till never waits.** Anything a cashier does with a customer standing there is reachable from the till screen and the keyboard.
2. **The numbers are true.** Stock and money change only through recorded, auditable events; a figure on screen can always be traced to its entries.
3. **Small by default, complete on demand.** A shop sees only the modules it uses; everything else exists and is one switch away.
4. **No country or trade is assumed.** Currency, tax, units and wording are settings.
5. **The shop owns its data.** It works offline, backs up locally, and exports to CSV.
