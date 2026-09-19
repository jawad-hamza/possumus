---
version: 1
slug: "pos-templates-pos-pos-screen-html"
primary_target: "pos/templates/pos/pos_screen.html"
related_targets: ["pos/templates/pos/base.html"]
---

# App shell and till

Scope: the frame every signed-in screen lives in (`pos/templates/pos/base.html`) and the selling screen (`pos/templates/pos/pos_screen.html`); the same system then carries every other screen. Visitor mode: Operate. Redesign: the ledger-book themed build is the anti-reference; its till layout and flow are kept.

Audience and job: a cashier or owner at a counter PC under bright tube lights, keyboard and USB scanner, a customer waiting; and the owner on a phone. Ring up, buy stock, see what is owed, without learning anything new.

Constraints: light; feels like installed software; nothing fancy or slow; no internet needed, so no CDN assets; Django templates plus vanilla JS; behaviour frozen.

Owner's verdict on the previous build: the till is good; other pages looked messy: too many lines and borders, too much text, cramped, and the tinted and maroon colours felt old. Hints and guides should live in hover tooltips. Chosen path: the category standard, played straight, beside Vyapar, Zoho Books / QuickBooks and Square POS / Loyverse.

Unresolved: final logo and brand colour (the accent is one token).

## Direction contract

THESIS: The modern business app a shopkeeper already knows how to use, executed at the polish of Zoho Books and Square. It refuses clutter, not convention.

OWN-WORLD: White panels, 8px corners, hairline neutral border, faintest shadow, on a light grey ground. Slate ink, tabular figures. One blue accent for primary actions, links, focus and the active menu item. White sidebar, icon and label. Tables with no grid: faint row dividers, hover highlight. Soft inputs. Pill badges. Green, red and amber only for money meaning, plus low (amber) and out (red) stock pills, as in the reference apps.

STORY: He opens it, recognises it, and works. Help waits in a tooltip until asked.

FIRST VIEWPORT: Till at 1366x768, no page scroll. Scan bar on top. The bill as a clean table panel in the middle, edited in place. Right panel: the total as the largest figure, tender and change, F-keyed payment buttons.

FORM: Category standard (canon), owner's choice; references Vyapar, Zoho Books / QuickBooks, Square POS / Loyverse; seed 612437f0.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
