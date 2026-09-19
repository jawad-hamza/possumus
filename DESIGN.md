---
name: ZehnMS
description: The modern business app a shopkeeper already knows how to use: white panels on a light grey ground, slate ink, one blue accent, money colours only for money.
colors:
  accent: "#2563eb"
  accent-hover: "#1d4ed8"
  accent-active: "#1e40af"
  accent-tint: "#eff4ff"
  on-accent: "#ffffff"
  ground: "#f3f4f6"
  panel: "#ffffff"
  sunk: "#eef0f3"
  hover: "#f5f6f8"
  row-hover: "#f7f8fa"
  border: "#e2e5e9"
  border-strong: "#cdd2d9"
  border-field: "#aeb5c0"
  divider: "#edeff2"
  ink-title: "#111827"
  ink: "#1f2937"
  ink-soft: "#5b6472"
  ink-faint: "#667085"
  ink-disabled: "#6b7280"
  on-ink: "#f9fafb"
  money-in: "#15803d"
  money-out: "#b91c1c"
  money-due: "#b45309"
  money-in-ink: "#166534"
  money-out-ink: "#991b1b"
  money-due-ink: "#92400e"
  money-in-wash: "#dcfce7"
  money-out-wash: "#fee2e2"
  money-due-wash: "#fef3c7"
  badge-neutral-bg: "#eceef1"
  badge-neutral-ink: "#374151"
  badge-blue-bg: "#dbeafe"
  badge-blue-ink: "#1e40af"
typography:
  display:
    fontFamily: "Public Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "2.5rem"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "-0.02em"
    fontFeature: "tnum, lnum"
  headline:
    fontFamily: "Public Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "1.375rem"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Public Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.005em"
  body:
    fontFamily: "Public Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "tnum, lnum"
  label:
    fontFamily: "Public Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "0"
  caption:
    fontFamily: "Public Sans, Segoe UI, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 600
    lineHeight: 1.45
rounded:
  xs: "4px"
  sm: "6px"
  md: "8px"
  lg: "12px"
  pill: "999px"
spacing:
  "1": "4px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "5": "24px"
  "6": "32px"
  "7": "48px"
  "8": "64px"
  panel-pad: "20px"
  panel-gap: "20px"
  gutter: "28px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "38px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
    textColor: "{colors.on-accent}"
  button-primary-active:
    backgroundColor: "{colors.accent-active}"
    textColor: "{colors.on-accent}"
  button-secondary:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "38px"
  button-secondary-hover:
    backgroundColor: "{colors.sunk}"
    textColor: "{colors.ink-title}"
  button-link:
    backgroundColor: "transparent"
    textColor: "{colors.accent-hover}"
  button-link-hover:
    backgroundColor: "{colors.accent-tint}"
    textColor: "{colors.accent-active}"
  input:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "6px 12px"
    height: "38px"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: "20px"
  nav-item:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "38px"
  nav-item-active:
    backgroundColor: "{colors.accent-tint}"
    textColor: "{colors.accent-hover}"
  badge:
    backgroundColor: "{colors.badge-neutral-bg}"
    textColor: "{colors.badge-neutral-ink}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    padding: "2px 9px"
  badge-green:
    backgroundColor: "{colors.money-in-wash}"
    textColor: "{colors.money-in-ink}"
  badge-amber:
    backgroundColor: "{colors.money-due-wash}"
    textColor: "{colors.money-due-ink}"
  badge-red:
    backgroundColor: "{colors.money-out-wash}"
    textColor: "{colors.money-out-ink}"
  badge-blue:
    backgroundColor: "{colors.badge-blue-bg}"
    textColor: "{colors.badge-blue-ink}"
  table-row:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    padding: "10px 12px"
    height: "48px"
  table-head:
    textColor: "{colors.ink-soft}"
    padding: "8px 12px"
    height: "40px"
  tooltip:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.on-ink}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
    width: "300px"
---

# Design System: ZehnMS

## Overview

**Creative North Star: "The Familiar Counter"**

ZehnMS looks like the business software a shopkeeper has already used: Zoho Books, QuickBooks, Vyapar, Square POS, Loyverse. It is recognisable on first open and works without teaching. The system does not try to be novel. Its job is polish and calm: white panels on a light grey ground, slate ink, one blue accent, few lines, and plenty of room. The rule is to cut clutter while keeping every convention the user already knows.

Density is set for work, not for show. Rows are 48px, controls are 38px (44px on phones), and every figure is tabular and right-aligned. The till fits the 1366x768 viewport without scrolling, and its total is the largest figure in the product. Explanations do not sit on the page. Each one waits behind a small info glyph and opens in a dark tooltip on hover, focus or tap. Errors are the exception and always stay visible. The system is light only, uses Public Sans as its only typeface, and loads nothing from a CDN.

The owner rejected an earlier themed build (a ruled, cloth-bound ledger book). Tinted or themed surfaces, decorative rules and stamps have no place here. Neutral white is the standing preference.

**Key Characteristics:**
- White panels (8px corners, hairline neutral border, the faintest shadow) on a light grey ground, one level deep
- Slate ink in three steps (title, body, muted), tabular lining figures everywhere
- One blue accent for primary actions, links, focus, selection and the active menu item
- Green, red and amber carry money meaning and stock state only
- Tables with no grid: faint row dividers, hover tint, muted sentence-case heads
- Pill badges for status; tooltips for help; sentence case throughout
- Public Sans only, light only, no CDN assets

## Colors

A neutral cool-grey system with one saturated blue. Colour appears only where it means something: blue marks an action, and green, red or amber mark money.

### Primary
- **Counter Blue** (accent): The only accent. It fills the one primary button of a page or panel (including "Take cash", "Receive payment" and "Record funds"). It also colours link text (as the deeper accent-hover shade, 6.7:1 on white), the focus ring, the selected row and tab, and the active sidebar item. Hover darkens one step (accent-hover), press darkens two (accent-active). The four accent values are the rebrand switch.
- **Blue Wash** (accent-tint): The active sidebar item, selected table rows, hover behind text buttons and the open info glyph.

### Tertiary (money meaning only)
- **Received Green** (money-in): Money in, profit, paid. Used on figures and paid/received pills, never on buttons.
- **Owed Red** (money-out): Money out, loss, overdue, out of stock, failed. Also the confirm step of a delete.
- **Due Amber** (money-due): Owed, due, on credit, low stock.
- Each has a darker ink shade and a pale wash for pills and note banners. Pills always use ink on wash, never white on colour.

### Neutral
- **Counter Grey** (ground): The page ground behind every panel and the till.
- **Panel White** (panel): Every panel, the sidebar, the top head, inputs, secondary buttons.
- **Sunk Grey** (sunk): Disabled and read-only fields, secondary-button hover, input-group add-ons, search field in the head.
- **Row Hover** (row-hover) and **Hover** (hover): Table row and list hover.
- **Hairline** (border): Panel edges, table head rule, the head and sidebar edges. **Divider** (divider) is fainter and separates table rows. **Strong Hairline** (border-strong) outlines secondary buttons and draws the rule above a totals row. **Field Edge** (border-field) outlines inputs so they read as editable.
- **Title Slate** (ink-title): Page and panel titles, key figures, totals. **Body Slate** (ink): Running text and table cells. **Muted Slate** (ink-soft): Table heads, notes, secondary lines (5.4:1 on ground). **Faint Slate** (ink-faint): Placeholders and the resting info glyph.

### Named Rules
**The One Blue Rule.** Blue is the only accent, and every primary action is blue, including actions that receive money. There is no green button. The success variant renders as the blue primary, and green stays on figures.

**The Money Meaning Rule.** Green, red and amber mean money in, money out and money owed (plus in-stock, out-of-stock and low-stock pills). They are never decoration, section colour or chart styling for ordinary series.

**The Neutral Ground Rule.** Surfaces are white or neutral grey. Nothing is tinted, themed or gradient-filled.

## Typography

**Display Font:** Public Sans (self-hosted variable woff2, weights 100 to 900), falling back to Segoe UI, system-ui
**Body Font:** Public Sans
**Label/Mono Font:** Public Sans for labels. A system monospace stack (not loaded) is used only for licence keys and code.

**Character:** One plain, legible sans across the whole product. Hierarchy comes from size and a single step of weight (400 to 600), not from a second face.

### Hierarchy
- **Display** (600, 40px, 1.1, -0.02em): Only for the till total and other hero money figures. The till total runs a little larger (48px) in its panel and drops to 40px when the figure is long.
- **Headline** (600, 22px, 1.25, -0.01em): Page titles in the fixed head and stat figures (the dashboard's net sales, received, to receive, to pay).
- **Title** (600, 16px, 1.25): Panel titles, next to their info glyph. The sub-heading h2 over panels on the ground is 18px.
- **Body** (400, 14px, 1.5; 15px on screens under 992px): Table cells, form values, running text. Figures use tabular lining numerals.
- **Label** (600, 13px, 1.35): Form labels above fields and button text (buttons are 14px at 600). Table heads use the same 13px size at 500 in muted slate, sentence case.
- **Caption** (600, 12px): Pill badges. Key chips are 11px.

### Named Rules
**The One Face Rule.** Public Sans is the only typeface. No display face, no second family and no webfont from a CDN.

**The Sentence Case Rule.** Headings, table heads, buttons, labels and badges are sentence case with no added tracking. Uppercase appears only on the single-letter avatar and on printed receipt column heads.

**The Tabular Figures Rule.** Every number (tables, inputs, stats, totals) uses tabular lining numerals and sits right-aligned in its column.

## Layout

The app shell is a white sidebar rail (232px, collapses to a 64px icon rail with Ctrl+B), a fixed white head (60px; 56px on phones) holding the page title with its info glyph, the page's head actions, the Go to search and the user menu, and a scrolling content area on the grey ground with a 28px gutter (16px on phones). Under 992px the rail becomes a drawer and a 60px bottom tab bar appears.

Spacing runs on a 4/8 scale (4, 8, 12, 16, 24, 32, 48, 64). Panels pad and space themselves at 20px (16px on phones). Grids of panels use the gap for spacing, never margins. Forms use a two-column field grid on desktop and one column under 768px, with labels above fields.

The till is a fixed, self-scrolling screen with no page scroll at 1366x768. The scan bar sits in the head. The bill is a table panel in the middle, edited in place. The right column holds the total as the largest figure, then subtotal and tax, customer, the Cash/Bank/Credit tender switch, tendered and change, and one full-width primary pay button. On phones the till becomes one column, with the total and the pay button pinned to the bottom.

Breakpoints are Bootstrap's: 576px (phone lists stack), 768px (form grid collapses), 992px (rail to drawer, bigger touch controls, 16px inputs).

### Named Rules
**The Head Actions Rule.** Page actions go in the fixed head. The primary action comes first and is blue, and extra actions fold into "More". When a side panel already holds an action, the head copy of it shows only on phones.

**The One Save Row Rule.** Entry pages pin a single save row to the bottom on phones. Long forms may use a sticky action bar.

**The Side-by-Side Rule.** Supporting records (a brand, a supplier, a customer) are added in a side panel next to the form, so the user keeps his place and his typing.

## Elevation & Depth

The system is almost flat. Panels separate from the grey ground by tone and a hairline border, with a shadow so faint it barely registers. Floating things (menus, popovers, tooltips, toasts, drawers) get one soft lifted shadow. There are exactly two shadows, and buttons and inputs have none. An input's focus halo is a ring, not an elevation.

### Shadow Vocabulary
- **Panel** (`box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04)`): Every panel at rest.
- **Pop** (`box-shadow: 0 10px 28px rgba(16, 24, 40, 0.14), 0 2px 6px rgba(16, 24, 40, 0.06)`): Dropdown menus, tooltips, toasts, drawers, modals.

### Named Rules
**The One Level Rule.** Panels never nest. A panel inside a panel loses its box and becomes a spaced group, and a row of stats sits in one panel or in one panel per stat.

## Shapes

The corners are soft and even. Panels, tooltips, large buttons and the till's pay button use 8px. Buttons, inputs, sidebar items and menu items use 6px. Key chips use 4px. Auth sheets and a few larger surfaces use 12px. Status badges, avatars and the info glyph's hover disc are full pills or circles. All borders are one hairline (1px). The totals row's heavier rule is only a darker hairline, never a thicker or double one.

## Components

### Buttons
Quiet and conventional. There is one filled button per page or panel.
- **Shape:** Gently rounded (6px; 8px at the large size), 38px tall (44px on phones; 32px small, 46px large), 14px semibold label, icon gap about 7px.
- **Primary:** Counter Blue fill with white text. Hover darkens one step, press two. It is the only filled button in its scope.
- **Secondary:** White with a strong hairline outline and body slate text. Hover goes sunk grey. Every other Bootstrap variant maps to this style.
- **Tertiary (link):** Blue text with no box. Hover shows the blue wash.
- **Destructive:** Asking to delete uses a neutral outline button that turns red on hover. Only the confirm step is filled red.
- **Icon-only:** A square quiet button with an aria-label and a short tooltip. Row actions appear on row hover or focus, and always on touch.
- **Focus:** A 2px blue outline offset 2px, with no glow.
- **Loading:** 70% opacity with a progress cursor and aria-busy.

### Key chips
Small outlined 11px chips (F9, F10, Enter). They print inside buttons **only on the till**, on desktop, and hide on touch and under 576px. Everywhere else a shortcut lives in the button's tooltip ("Hold this sale (F8)") and in the F1 shortcut sheet.

### Chips / Badges
- **Style:** Pill, 12px semibold, dark ink on a pale wash of the same hue, no border.
- **Meaning:** Green = paid or received. Amber = due, low stock or credit. Red = overdue, out or failed. Blue = held or in progress. Neutral grey = draft, inactive or settled.

### Cards / Containers (panels)
- **Corner Style:** 8px.
- **Background:** Panel White on the Counter Grey ground.
- **Shadow Strategy:** The Panel shadow only (see Elevation & Depth).
- **Border:** One hairline.
- **Internal Padding:** 20px (16px on phones). A table that is a direct child of a panel runs edge to edge with its first and last cells inset to the panel padding.
- **Head:** A 16px semibold title with its info glyph on the left, an optional short muted note (a count, never a sentence), and actions on the right.

### Stat blocks
A muted 13px label, then a 22px semibold tabular figure in title slate, then a muted note. The figure turns green, red or amber only when it states money in, out or owed.

### Tables
The no-grid table is the system's workhorse.
- No vertical lines and no zebra stripes. Rows are 48px, split by the faint divider, and tinted on hover.
- The head row is 40px, 13px medium muted slate, sentence case, sitting on a hairline.
- Figures are right-aligned and tabular.
- Totals go in the table foot: semibold title slate under a strong hairline.
- A selected row takes the blue wash across the whole row, never a side stripe.
- Long tables scroll in a region with a sticky head.
- Under 576px, lists become **two-line entries**: what and amount on line one, meta and balance in soft ink on line two. A screen holds seven or more entries.
- Grouped size variants show as one family row with a size count and a price range.

### Inputs / Fields
- **Style:** A white field with a field-edge hairline, 6px corners, 38px tall (44px and 16px text on phones), and tabular numerals. The placeholder is in faint slate.
- **Focus:** A blue border plus a 3px translucent blue halo.
- **Error / Disabled:** An invalid field gets a red border, and the error message stays visible beneath it with an alert icon. Disabled and read-only fields go sunk grey with muted text.
- **Empty choices:** Selects say what empty means in plain words ("Choose a category", "No brand", "Walk-in customer", "All categories"), never a row of dashes.
- **Dates:** A drawn calendar glyph at field height. An empty date shows a designed "DD/MM/YYYY" placeholder ("Date" when narrow) in placeholder grey, not the browser's format text.

### Help tooltips (signature)
A 20px circular info glyph in faint slate sits beside a page title, panel title or field label, and goes blue-washed when hovered or open. The tooltip is dark slate with near-white 13px text, 8px corners and the Pop shadow, capped at 300px wide. It fades in over 140ms. It opens on hover, keyboard focus or tap, closes on Esc or an outside click, shows one at a time, and stays inside the viewport. Help text and page explanations live here, never printed on the page. Errors are never hidden in a tooltip.

### Navigation
- **Sidebar:** A white rail of icon-and-label links (14px medium, 38px rows, 6px corners) in small muted groups (Counter, Stock, Money, Office). Icons are muted at rest. Hover shows sunk grey with title ink. **Active** shows the blue wash with a blue icon and blue semibold label. The module is chosen by URL prefix, so every sub-page lights its module, and the link carries aria-current="page". Keyboard shortcuts appear in tooltips, never printed in the rail.
- **Phone:** The rail becomes a drawer. A bottom tab bar mirrors the rail's active state.
- **Tabs:** Underline tabs, or segmented pills (the till's Cash/Bank/Credit switch: the selected segment is white with a blue outline).

### Toasts
Toasts stack newest on top, with at most three visible. Older ones fold behind an "N older messages" line. Success and info toasts dismiss after 7s. On the till they sit low over the bill, clear of the total and the payment column.

### Motion
State changes only: color, background and border over 140ms with a fast ease-out. Motion drops to 0ms under reduced motion. Nothing loops, and nothing runs longer than 160ms.

## Do's and Don'ts

### Do:
- **Do** put the primary action first and make it the one blue filled button in its scope, including actions that take or receive money.
- **Do** keep money colours for money: green in, red out, amber owed, plus the in-stock, out and low stock pills.
- **Do** set every figure in tabular lining numerals, right-aligned, with totals semibold under a strong hairline.
- **Do** move help and explanations into info tooltips that open on hover, focus and tap and close on Esc. Keep errors on the page.
- **Do** light the sidebar module by URL prefix with the blue wash, blue icon and label, and aria-current.
- **Do** name the counting unit in list counts ("22 products in 20 rows, sizes grouped") and give each ledger entry one reference the shop knows ("Invoice INV-00024").
- **Do** check every screen at 1366x768 and 390x844. The till must not page-scroll at 1366x768.

### Don't:
- **Don't** nest a panel inside a panel.
- **Don't** draw vertical table lines, grids, zebra stripes or coloured side stripes on selected rows.
- **Don't** make a green (or any non-blue) primary button, or use green, red or amber as decoration.
- **Don't** print key chips outside the till. Shortcuts go in tooltips and the F1 sheet.
- **Don't** use a second typeface, uppercase tracked labels, kickers or eyebrow text above headings.
- **Don't** tint or theme surfaces, use gradients, or bring back ledger rules, cloth, stamps or condensed display type.
- **Don't** show Django's dashed empty choice or the browser's raw date format text.
- **Don't** load fonts, icons or scripts from a CDN, or add a dark theme.
- **Don't** add shadows beyond Panel and Pop, or animate longer than 160ms.
