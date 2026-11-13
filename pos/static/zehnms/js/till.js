/*
 * The till.
 *
 * Every cart change is one POST to /pos/ajax/till/<action>/; the answer carries the whole bill and the
 * page patches itself, so the screen never reloads while a customer waits. The forms in the markup
 * still post to the page itself when this script is not running.
 *
 * Keys: F2 scan line, F3 product list, F4 quantity, F6 discount, F7 customer, F8 hold or resume,
 * F9 cash, F10 bank, F11 credit, Del remove line, Up/Down move between lines, + and - change the
 * selected line, Esc clears or closes. Enter in a cell saves and returns to the scan line.
 * An item sold in sizes shows once in the suggestions; choosing it lists its sizes under the scan
 * line (1-9 picks one, arrows and Enter too, Esc goes back). A scanned code adds its exact size.
 */
(function () {
    'use strict';

    var doc = document;
    var root = doc.getElementById('till');
    if (!root || !window.tillSend) { return; }

    function $(selector, scope) { return (scope || doc).querySelector(selector); }
    function $all(selector, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(selector)); }

    var scan = doc.getElementById('barcode-input');
    var suggestList = doc.getElementById('till-suggest');
    var ledger = doc.getElementById('till-ledger');
    var noticeBox = doc.getElementById('till-notice');
    var heldBox = doc.getElementById('till-held');
    var holdForm = doc.getElementById('till-hold-form');
    var side = doc.getElementById('till-side');
    var checkoutForm = doc.getElementById('checkout-form');
    var customerSelect = doc.getElementById('customer_id');
    var customerHidden = doc.getElementById('selected_customer_id_hidden');
    var creditBox = doc.getElementById('is_credit');
    var cashRadio = doc.getElementById('pay_cash');
    var bankRadio = doc.getElementById('pay_bank');
    var bankSelect = doc.getElementById('bank_account_id');
    var bankWrapper = doc.getElementById('bank-account-wrapper');
    var discountInput = doc.getElementById('discount_rate');
    var taxInput = doc.getElementById('tax_rate');
    var tenderWrap = doc.getElementById('till-tender');
    var tendered = doc.getElementById('till-tendered');
    var changeOut = doc.getElementById('till-change');
    var changeLabel = doc.getElementById('till-change-label');
    var closeBtns = $all('.t-close-btn');
    var closeLabels = $all('.t-close-label');
    var clearBtn = doc.getElementById('till-clear-btn');
    var creditNote = doc.getElementById('till-credit-note');
    var browseKey = doc.getElementById('till-browse-key');

    var currency = root.dataset.currency || '';
    var total = parseFloat(root.dataset.total) || 0;
    var coarse = window.matchMedia('(pointer: coarse)').matches;
    var selected = ($('.t-line.is-selected', ledger) || {}).dataset ? $('.t-line.is-selected', ledger).dataset.line : '';
    var lineCount = $all('.t-line', ledger).length;
    var busy = false;
    var perf = { samples: [] };

    /* ---------- small helpers ---------- */

    function money(value) {
        var fixed = (Math.round(value * 100) / 100).toFixed(2);
        var parts = fixed.split('.');
        parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
        return (currency ? currency + ' ' : '') + parts.join('.');
    }

    function focusScan(force) {
        if (!scan || (coarse && !force)) { return; }
        scan.focus({ preventScroll: true });
    }

    function lineEl(id) {
        return id ? $('.t-line[data-line="' + String(id).replace(/"/g, '') + '"]', ledger) : null;
    }

    function lines() { return $all('.t-line', ledger); }

    /* ---------- the note line under the bill ---------- */

    var noticeTimer = null;

    function idleNotice() {
        noticeBox.textContent = '';
        var span = doc.createElement('span');
        if ($('.t-line.has-issue', ledger)) {
            span.className = 'k-out';
            span.textContent = 'Fix the marked lines before closing this sale.';
        } else {
            span.className = 't-notice-idle';
            span.textContent = lineCount ? lineCount + (lineCount === 1 ? ' line' : ' lines') + ' on this bill' : 'Ready for the next customer';
        }
        noticeBox.appendChild(span);
    }

    function notify(notice) {
        window.clearTimeout(noticeTimer);
        if (!notice || !notice.text) { idleNotice(); return; }
        var kind = { out: 'k-out', due: 'k-due', 'in': 'k-in' }[notice.kind] || '';
        noticeBox.textContent = '';
        var span = doc.createElement('span');
        span.className = kind;
        span.textContent = notice.text;
        noticeBox.appendChild(span);
        if (notice.can_create && root.dataset.productAddUrl) {
            var link = doc.createElement('a');
            // Saving the new product comes straight back to this bill.
            link.href = root.dataset.productAddUrl + '?barcode=' + encodeURIComponent(notice.code || '') + '&next=' + encodeURIComponent(window.location.pathname);
            link.textContent = 'Create this product';
            noticeBox.appendChild(link);
        }
        noticeTimer = window.setTimeout(idleNotice, notice.kind === 'in' ? 5000 : 9000);
    }

    /* ---------- the sale's context: customer, how it is paid, bill discount ---------- */

    function isCredit() { return !!(creditBox && creditBox.checked); }
    function method() { return bankRadio && bankRadio.checked ? 'bank' : 'cash'; }

    function contextParams() {
        return {
            customer_id: customerSelect ? customerSelect.value : '',
            credit: isCredit() ? '1' : '0',
            payment_method: method(),
            bank_account_id: bankSelect && method() === 'bank' && !isCredit() ? bankSelect.value : '',
            discount_rate: discountInput ? discountInput.value : '0',
            discount_type: window.ZehnTillDiscount ? window.ZehnTillDiscount.kind() : 'percent',
            discount_value: discountInput ? discountInput.value : '0',
            tax_rate: taxInput ? taxInput.value : '0'
        };
    }

    function showMode() {
        var credit = isCredit();
        var bank = method() === 'bank';
        if (bankWrapper) { bankWrapper.hidden = !bank || credit; }
        if (tenderWrap) { tenderWrap.hidden = bank || credit; }
        if (customerHidden && customerSelect) { customerHidden.value = customerSelect.value; }
        closeLabels.forEach(function (label) { label.textContent = credit ? 'Put on credit' : (bank ? 'Take bank payment' : 'Take cash'); });
        closeBtns.forEach(function (button) {
            button.classList.toggle('is-credit', credit);
            button.classList.toggle('btn-primary', !credit);
        });
        var needsCustomer = credit && customerSelect && !customerSelect.value;
        if (creditNote) { creditNote.hidden = !needsCustomer; }
        showChange();
    }

    function showChange() {
        if (!changeOut || !tendered) { return; }
        var typed = parseFloat(String(tendered.value).replace(/,/g, ''));
        var box = changeOut.parentNode;
        if (!tendered.value.trim() || isNaN(typed)) {
            changeLabel.textContent = 'Change';
            changeOut.textContent = money(0);
            box.classList.remove('is-short', 'has-change');
            return;
        }
        var diff = Math.round((typed - total) * 100) / 100;
        box.classList.toggle('is-short', diff < 0);
        box.classList.toggle('has-change', diff > 0);
        changeLabel.textContent = diff < 0 ? 'Still to take' : 'Change';
        changeOut.textContent = money(Math.abs(diff));
    }

    function applyContext(context) {
        if (!context) { return; }
        if (customerSelect && doc.activeElement !== customerSelect) { customerSelect.value = context.customer_id || ''; }
        if (creditBox) { creditBox.checked = !!context.credit; }
        if (bankRadio && cashRadio) {
            bankRadio.checked = context.payment_method === 'bank';
            cashRadio.checked = context.payment_method !== 'bank';
        }
        if (bankSelect && doc.activeElement !== bankSelect) { bankSelect.value = context.bank_account_id || ''; }
        if (discountInput && doc.activeElement !== discountInput) { discountInput.value = Number(context.discount_value || context.discount_rate || 0).toFixed(2); }
        if (taxInput && taxInput.type !== 'hidden' && doc.activeElement !== taxInput) { taxInput.value = Number(context.tax_rate || 0).toFixed(2); }
    }

    function applyCustomer(customer) {
        // pages/till-customer.js listens: the credit limit note and the sale's order booker follow the customer.
        doc.dispatchEvent(new CustomEvent('till:customer', { detail: customer || null }));
        var note = doc.getElementById('till-customer-note');
        if (!note) { return; }
        note.textContent = '';
        if (!customer || customer.balance_type === 'clear') { note.hidden = true; return; }
        var span = doc.createElement('span');
        span.className = 'k-state ' + 'k-state--due';
        span.textContent = customer.balance_type === 'receivable'
            ? 'Owes ' + customer.balance
            : 'Paid ' + customer.balance + ' in advance';
        note.appendChild(span);
        note.hidden = false;
    }

    function applyTotals(totals) {
        if (!totals) { return; }
        total = parseFloat(totals.total) || 0;
        var set = function (id, text) { var el = doc.getElementById(id); if (el) { el.textContent = text; } };
        set('subtotal-amount', totals.subtotal_display);
        set('line-discount-amount', totals.line_discount_display);
        set('discount-amount', totals.discount_display);
        set('tax-amount', totals.tax_display);
        set('tax-summary-label', totals.tax_label);
        set('final-total', totals.total_display);
        var lineDiscountRow = doc.getElementById('line-discount-row');
        if (lineDiscountRow) { lineDiscountRow.hidden = !(parseFloat(totals.line_discount) > 0); }
        var block = doc.getElementById('till-total-block');
        if (block) { block.classList.toggle('t-total--long', total >= 1000000); }
        var compare = doc.getElementById('till-compare');
        if (compare) {
            compare.textContent = '';
            (totals.comparison || []).forEach(function (option) {
                var row = doc.createElement('div');
                row.className = 't-sum';
                var dt = doc.createElement('dt');
                dt.textContent = option.method_label ? 'If paid by ' + option.method_label.toLowerCase() : option.label;
                if (option.method_label) { dt.setAttribute('data-tip', option.label); }
                var dd = doc.createElement('dd');
                dd.className = 'k-num';
                dd.textContent = option.total_display;
                row.appendChild(dt);
                row.appendChild(dd);
                compare.appendChild(row);
            });
            compare.hidden = !(totals.comparison || []).length;
        }
    }

    /* ---------- one request at a time: the cart lives in the session ---------- */

    var queue = Promise.resolve();

    function encode(params) {
        return Object.keys(params).map(function (key) {
            return encodeURIComponent(key) + '=' + encodeURIComponent(params[key] == null ? '' : params[key]);
        }).join('&');
    }

    function send(action, params, after) {
        var started = performance.now();
        queue = queue.then(function () {
            var body = contextParams();
            Object.keys(params || {}).forEach(function (key) { body[key] = params[key]; });
            if (!('selected' in body)) { body.selected = selected || ''; }
            busy = true;
            root.setAttribute('aria-busy', 'true');
            return window.tillSend(root.dataset.actionUrl.replace('__action__', action), encode(body))
                .then(function (response) {
                    return response.json().catch(function () { return null; }).then(function (data) {
                        return { response: response, data: data };
                    });
                })
                .then(function (result) {
                    var status = result.response.status;
                    var data = result.data;
                    if (status === 401 || status === 402) {
                        window.location.assign((data && data.redirect) || root.dataset.loginUrl);
                        return;
                    }
                    if (!data || !data.html) {
                        notify({ kind: 'out', text: (data && data.message) || 'The till could not do that. Try again.' });
                        return;
                    }
                    var answered = performance.now();
                    apply(data, after);
                    window.requestAnimationFrame(function () {
                        window.requestAnimationFrame(function () {
                            var sample = { action: action, server: Math.round(answered - started), painted: Math.round(performance.now() - started) };
                            perf.last = sample;
                            perf.samples.push(sample);
                            if (perf.samples.length > 50) { perf.samples.shift(); }
                        });
                    });
                })
                .catch(function () {
                    notify({ kind: 'out', text: 'Could not reach the server. Check the connection and try again.' });
                })
                .then(function () {
                    busy = false;
                    root.removeAttribute('aria-busy');
                });
        });
        return queue;
    }

    /* ---------- patch the page from an answer ---------- */

    function apply(data, after) {
        var keep = after || {};
        $all('tbody', ledger).forEach(function (body) { body.remove(); });
        $('thead', ledger).insertAdjacentHTML('afterend', data.html.bill);
        if (heldBox) { heldBox.innerHTML = data.html.held; }

        selected = data.selected || '';
        lineCount = (data.lines || []).length;
        applyTotals(data.totals);
        applyContext(data.context);
        if (window.ZehnTillDiscount) { window.ZehnTillDiscount.apply(data); }
        applyCustomer(data.customer);
        showMode();

        setClosable(!data.checkout_disabled && lineCount > 0);
        if (clearBtn) { clearBtn.disabled = !lineCount; disarm(clearBtn); }
        notify(data.notice);

        var line = lineEl(selected);
        if (line && line.scrollIntoView && !keep.noScroll) { line.scrollIntoView({ block: 'nearest' }); }

        if (keep.focusField) {
            var target = fieldOf(keep.focusLine || selected, keep.focusField);
            if (target) { target.focus(); target.select(); return; }
        }
        if (keep.focus && keep.focus.isConnected) { keep.focus.focus(); return; }
        if (!keep.stay) { focusScan(); }
    }

    function setClosable(open) {
        closeBtns.forEach(function (button) { button.disabled = !open; });
    }

    function canClose() {
        return closeBtns.some(function (button) { return !button.disabled; });
    }

    function shownCloseBtn() {
        return closeBtns.filter(function (button) { return button.offsetParent !== null; })[0] || null;
    }

    function select(id, scroll) {
        selected = id || '';
        lines().forEach(function (line) { line.classList.toggle('is-selected', line.dataset.line === selected); });
        var line = lineEl(selected);
        if (line && scroll !== false && line.scrollIntoView) { line.scrollIntoView({ block: 'nearest' }); }
    }

    function moveSelection(step) {
        var all = lines();
        if (!all.length) { return; }
        var index = all.findIndex(function (line) { return line.dataset.line === selected; });
        index = index === -1 ? all.length - 1 : Math.min(all.length - 1, Math.max(0, index + step));
        select(all[index].dataset.line);
    }

    function fieldOf(id, field) {
        var line = lineEl(id);
        if (!line) { return null; }
        var input = $('[data-field="' + field + '"]', line);
        return input && !input.readOnly ? input : null;
    }

    /* ---------- the scan line: code, name, or a quantity and then either ---------- */

    var catalogue = [];
    var byCode = {};
    var families = {};
    var hits = [];
    var cursor = 0;
    var picking = null; // the family whose sizes the list shows, with the typed quantity
    var UNITS = {
        kg: 'kg', g: 'g', gm: 'g', gram: 'g', grams: 'g', l: 'l', ltr: 'l', litre: 'l', liter: 'l', ml: 'ml', m: 'm', ft: 'ft',
        ctn: 'ctn', ctns: 'ctn', carton: 'ctn', cartons: 'ctn', box: 'box', boxes: 'box', bx: 'box',
        pc: 'pcs', pcs: 'pcs', piece: 'pcs', pieces: 'pcs'
    };

    fetch(root.dataset.catalogueUrl, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (response) { return response.ok ? response.json() : { products: [] }; })
        .then(function (data) {
            catalogue = data.products || [];
            var byId = {};
            catalogue.forEach(function (product) {
                byId[product.id] = product;
                if (product.sku) { byCode[product.sku.toLowerCase()] = product; }
                if (product.barcode) { byCode[product.barcode.toLowerCase()] = product; }
            });
            (data.families || []).forEach(function (family) {
                families[family.id] = {
                    id: family.id,
                    name: family.name,
                    sizes: (family.sizes || []).map(function (id) { return byId[id]; }).filter(Boolean)
                };
            });
            if (side && !side.hidden) { renderBrowse(); }
        })
        .catch(function () { catalogue = []; });

    /* "0.5 flour", "2*soap", "2 x soap", "500g sugar" -> { quantity, unit, term } */
    function parseQuantity(text) {
        var match = /^(\d+(?:\.\d+)?|\.\d+)\s*(?:\*|x\s)\s*(.+)$/i.exec(text);
        if (match) { return { quantity: match[1], unit: '', term: match[2].trim() }; }
        match = /^(\d+(?:\.\d+)?|\.\d+)\s*([a-z]{1,6})\s+(.+)$/i.exec(text);
        if (match && UNITS[match[2].toLowerCase()]) {
            return { quantity: match[1], unit: UNITS[match[2].toLowerCase()], term: match[3].trim() };
        }
        match = /^(\d+(?:\.\d+)?|\.\d+)\s+(.+)$/.exec(text);
        if (match) { return { quantity: match[1], unit: '', term: match[2].trim() }; }
        return null;
    }

    function search(term, limit) {
        var parts = term.toLowerCase().split(/\s+/).filter(Boolean);
        if (!parts.length) { return []; }
        var first = parts[0];
        var found = [];
        for (var i = 0; i < catalogue.length; i += 1) {
            var product = catalogue[i];
            var ok = true;
            for (var j = 0; j < parts.length; j += 1) {
                if (product.find.indexOf(parts[j]) === -1) { ok = false; break; }
            }
            if (!ok) { continue; }
            var name = product.name.toLowerCase();
            var rank = 1;
            if (product.sku.toLowerCase() === term.toLowerCase() || product.barcode.toLowerCase() === term.toLowerCase()) { rank = 5; }
            else if (name.indexOf(first) === 0) { rank = 4; }
            else if (name.indexOf(' ' + first) !== -1) { rank = 3; }
            else if (product.sku.toLowerCase().indexOf(first) === 0) { rank = 2; }
            found.push({ product: product, rank: rank - (product.out ? 0.5 : 0) });
        }
        found.sort(function (a, b) { return b.rank - a.rank; });
        return found.slice(0, limit).map(function (entry) { return entry.product; });
    }

    function unitFits(product, unit) {
        if (!unit || unit === product.unit) { return true; }
        // a pack word fits a product packed that way ("5 ctn" of something sold by the carton)
        if (product.packs && product.packs.indexOf(unit) !== -1) { return true; }
        return (unit === 'g' && product.unit === 'kg') || (unit === 'kg' && product.unit === 'g') ||
            (unit === 'ml' && product.unit === 'l') || (unit === 'l' && product.unit === 'ml');
    }

    /* Sizes of one item show once, as their family; one matching size ("bread small") stays itself. */
    function foldFamilies(list, term) {
        var matched = {};
        list.forEach(function (hit) {
            var family = hit.product.family;
            if (family && families[family]) { matched[family] = (matched[family] || 0) + 1; }
        });
        var exactCode = (term || '').toLowerCase();
        var seen = {};
        var folded = [];
        list.forEach(function (hit) {
            var family = hit.product.family;
            var exact = exactCode && (hit.product.sku.toLowerCase() === exactCode || hit.product.barcode.toLowerCase() === exactCode);
            if (!family || !families[family] || matched[family] < 2 || exact) { folded.push(hit); return; }
            var key = family + '|' + hit.quantity + '|' + hit.unit;
            if (seen[key]) { return; }
            seen[key] = true;
            folded.push({ family: families[family], quantity: hit.quantity, unit: hit.unit });
        });
        return folded;
    }

    function suggest() {
        var text = scan.value.trim();
        hits = [];
        picking = null;
        if (text.length >= 1 && catalogue.length) {
            var plain = foldFamilies(search(text, 40).map(function (product) { return { product: product, quantity: '', unit: '' }; }), text);
            var parsed = parseQuantity(text);
            var counted = !parsed ? [] : foldFamilies(search(parsed.term, 40)
                .filter(function (product) { return unitFits(product, parsed.unit); })
                .map(function (product) { return { product: product, quantity: parsed.quantity, unit: parsed.unit }; }), parsed.term);
            // "7 up" names a product; "7 soap" counts one. The whole text wins when a name starts with it.
            var first = plain.length ? (plain[0].family ? plain[0].family.name : plain[0].product.name) : '';
            var named = plain.length && first.toLowerCase().indexOf(text.toLowerCase()) === 0;
            hits = (named ? plain.concat(counted) : counted.concat(plain)).slice(0, 8);
        }
        cursor = 0;
        drawSuggest();
    }

    /* The size picker opens in the same list, right under the scan line: 1-9 picks, arrows and Enter, Esc closes. */
    function openPicker(hit) {
        var sizes = hit.family.sizes.filter(function (product) { return unitFits(product, hit.unit); });
        if (!sizes.length) { sizes = hit.family.sizes; }
        picking = hit;
        hits = sizes.map(function (product) { return { product: product, quantity: hit.quantity, unit: hit.unit, size: true }; });
        cursor = 0;
        drawSuggest();
    }

    function closePicker() {
        if (!picking) { return false; }
        picking = null;
        suggest();
        return true;
    }

    function drawSuggest() {
        suggestList.textContent = '';
        var open = hits.length > 0;
        suggestList.hidden = !open;
        suggestList.classList.toggle('is-picking', !!picking);
        suggestList.setAttribute('aria-label', picking ? 'Sizes of ' + picking.family.name : 'Matching products');
        scan.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (picking) {
            var head = doc.createElement('li');
            head.className = 't-suggest-head t-suggest-none';
            head.setAttribute('role', 'presentation');
            var title = doc.createElement('span');
            title.className = 't-suggest-name';
            title.textContent = (picking.quantity ? picking.quantity + (picking.unit ? ' ' + picking.unit : '') + ' of ' : '') +
                picking.family.name + ': pick a size';
            var how = doc.createElement('span');
            how.className = 't-item-meta';
            how.textContent = hits.length > 1 ? '1-' + Math.min(hits.length, 9) + ', or arrows and Enter. Esc closes.' : 'Enter adds it. Esc closes.';
            head.appendChild(title);
            head.appendChild(how);
            suggestList.appendChild(head);
        }
        hits.forEach(function (hit, index) {
            var item = doc.createElement('li');
            item.className = 't-suggest-item' + (hit.family ? ' t-suggest-family' : '');
            item.id = 'till-suggest-' + index;
            item.setAttribute('role', 'option');
            item.setAttribute('aria-selected', index === cursor ? 'true' : 'false');
            item.dataset.index = String(index);

            var name = doc.createElement('span');
            name.className = 't-suggest-name';
            if (hit.size && index < 9) {
                var key = doc.createElement('span');
                key.className = 'k-key';
                key.setAttribute('aria-hidden', 'true');
                key.textContent = String(index + 1);
                name.appendChild(key);
            }
            if (hit.quantity && !hit.size) {
                var qty = doc.createElement('span');
                qty.className = 't-suggest-qty';
                var sample = hit.product || hit.family.sizes[0] || {};
                var unit = hit.unit || (sample.fraction ? sample.unit : '');
                qty.textContent = hit.quantity + (unit ? ' ' + unit : '') + ' of ';
                name.appendChild(qty);
            }
            var stock = doc.createElement('span');
            var price = doc.createElement('span');
            price.className = 'k-num';
            if (hit.family) {
                var sizes = hit.family.sizes;
                name.appendChild(doc.createTextNode(hit.family.name));
                var count = doc.createElement('span');
                count.className = 't-item-meta';
                count.textContent = sizes.length + ' sizes';
                name.appendChild(count);
                var anyIn = sizes.some(function (product) { return !product.out; });
                stock.className = anyIn ? 't-item-meta' : 'k-state k-state--out';
                stock.textContent = anyIn ? 'Pick a size' : 'Out of stock';
                price.textContent = sizes.length ? (sizes[0].price === sizes[sizes.length - 1].price
                    ? sizes[0].price : sizes[0].price + ' to ' + sizes[sizes.length - 1].price) : '';
            } else {
                name.appendChild(doc.createTextNode(hit.size && hit.product.label ? hit.product.label : hit.product.name));
                var code = doc.createElement('span');
                code.className = 't-item-meta';
                code.textContent = hit.product.sku;
                name.appendChild(code);
                stock.className = hit.product.out ? 'k-state k-state--out' : (hit.product.low ? 'k-state k-state--due' : 't-item-meta');
                stock.textContent = hit.product.out ? 'Out of stock' : hit.product.stock;
                price.textContent = hit.product.price;
            }

            item.appendChild(name);
            item.appendChild(stock);
            item.appendChild(price);
            suggestList.appendChild(item);
        });
        scan.setAttribute('aria-activedescendant', open ? 'till-suggest-' + cursor : '');
        var current = doc.getElementById('till-suggest-' + cursor);
        if (current && current.scrollIntoView) { current.scrollIntoView({ block: 'nearest' }); }
    }

    function closeSuggest() {
        hits = [];
        picking = null;
        drawSuggest();
    }

    function addHit(hit) {
        if (!hit) { return; }
        if (hit.family) { openPicker(hit); return; }
        scan.value = '';
        closeSuggest();
        send('add', { product_id: hit.product.id, quantity: hit.quantity, quantity_unit: hit.unit });
    }

    function enterScan() {
        if (picking && hits.length) { addHit(hits[cursor]); return; }
        var text = scan.value.trim();
        if (!text) { return; }
        var exact = byCode[text.toLowerCase()];
        if (exact) { addHit({ product: exact, quantity: '', unit: '' }); return; }
        if (hits.length) { addHit(hits[cursor]); return; }
        var parsed = parseQuantity(text);
        if (parsed && byCode[parsed.term.toLowerCase()]) {
            addHit({ product: byCode[parsed.term.toLowerCase()], quantity: parsed.quantity, unit: parsed.unit });
            return;
        }
        scan.value = '';
        closeSuggest();
        if (parsed && /^[^\s]+$/.test(parsed.term)) {
            send('scan', { code: parsed.term, quantity: parsed.quantity, quantity_unit: parsed.unit });
        } else {
            send('scan', { code: text });
        }
    }

    scan.addEventListener('input', suggest);
    scan.addEventListener('keydown', function (event) {
        var empty = !scan.value;
        if (picking && /^[1-9]$/.test(event.key) && !event.ctrlKey && !event.altKey && !event.metaKey) {
            event.preventDefault();
            var pick = hits[Number(event.key) - 1];
            if (pick) { addHit(pick); }
            return;
        }
        if (picking && event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            closePicker();
            return;
        }
        if (event.key === 'Enter') {
            event.preventDefault();
            enterScan();
        } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            var step = event.key === 'ArrowDown' ? 1 : -1;
            if (hits.length) {
                cursor = (cursor + step + hits.length) % hits.length;
                drawSuggest();
            } else {
                moveSelection(step);
            }
        } else if (event.key === 'Escape') {
            if (hits.length || scan.value) {
                event.preventDefault();
                event.stopPropagation();
                scan.value = '';
                closeSuggest();
            }
        } else if (empty && (event.key === '+' || event.key === '=') && selected) {
            event.preventDefault();
            send('increase', { product_id: selected });
        } else if (empty && event.key === '-' && selected) {
            event.preventDefault();
            send('decrease', { product_id: selected });
        } else if (empty && event.key === 'Delete' && selected) {
            event.preventDefault();
            send('remove', { product_id: selected });
        }
    });
    scan.addEventListener('blur', function () { window.setTimeout(closeSuggest, 120); });
    suggestList.addEventListener('mousedown', function (event) {
        var item = event.target.closest('.t-suggest-item');
        if (!item) { return; }
        event.preventDefault();
        addHit(hits[Number(item.dataset.index)]);
        focusScan(true);
    });

    /* ---------- figures edited in place ---------- */

    var FIELD_ORDER = ['qty', 'rate', 'disc'];

    function commit(input, after) {
        if (input.dataset.sent === input.value) { return false; }
        var line = input.closest('.t-line');
        if (!line) { return false; }
        if (input.value === input.defaultValue) { return false; }
        input.dataset.sent = input.value;
        var id = line.dataset.line;
        if (input.dataset.field === 'qty') {
            send('quantity', { product_id: id, quantity: input.value, selected: id }, after);
        } else {
            var params = { product_id: id, selected: id };
            params[input.name] = input.value;
            send('price', params, after);
        }
        return true;
    }

    function nextField(input) {
        var line = input.closest('.t-line');
        var at = FIELD_ORDER.indexOf(input.dataset.field);
        for (var i = at + 1; i < FIELD_ORDER.length; i += 1) {
            if (fieldOf(line.dataset.line, FIELD_ORDER[i])) { return { focusLine: line.dataset.line, focusField: FIELD_ORDER[i] }; }
        }
        var all = lines();
        var next = all[all.indexOf(line) + 1];
        return next ? { focusLine: next.dataset.line, focusField: 'qty' } : {};
    }

    function unitSelectOf(line) {
        return line ? $('[data-unit-select]', line) : null;
    }

    /* Carton, box or piece for a line. A quantity still being typed goes with the change. */
    function changeUnit(line, unit, quantityInput) {
        var id = line.dataset.line;
        var params = { product_id: id, unit: unit, selected: id };
        if (quantityInput && quantityInput.value !== '' && quantityInput.value !== quantityInput.defaultValue) {
            params.quantity = quantityInput.value;
            quantityInput.dataset.sent = quantityInput.value;
        }
        send('unit', params, { focusLine: id, focusField: 'qty', stay: true, noScroll: true });
    }

    ledger.addEventListener('focusin', function (event) {
        var unitSelect = event.target.closest('[data-unit-select]');
        if (unitSelect) { select(unitSelect.closest('.t-line').dataset.line, false); return; }
        var input = event.target.closest('.t-cell-input');
        if (!input) { return; }
        select(input.closest('.t-line').dataset.line, false);
        if (!input.readOnly) { input.select(); }
    });

    ledger.addEventListener('keydown', function (event) {
        var unitChoice = event.target.closest('[data-unit-select]');
        if (unitChoice) {
            if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); focusScan(true); }
            return;
        }
        var input = event.target.closest('.t-cell-input');
        if (!input) { return; }
        if (input.dataset.field === 'qty' && event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
            // C, B or P in the quantity box picks the unit: "5" then "c" is five cartons.
            var packs = unitSelectOf(input.closest('.t-line'));
            var wanted = packs ? $('option[data-key="' + event.key.toLowerCase() + '"]', packs) : null;
            if (wanted) {
                event.preventDefault();
                if (wanted.value !== packs.value || input.value !== input.defaultValue) {
                    changeUnit(input.closest('.t-line'), wanted.value, input);
                }
                return;
            }
        }
        if (event.key === 'Enter') {
            event.preventDefault();
            if (!commit(input, {})) { focusScan(true); }
        } else if (event.key === 'Tab' && !event.shiftKey) {
            var target = nextField(input);
            if (commit(input, target)) {
                event.preventDefault();
            } else if (target.focusField) {
                event.preventDefault();
                var field = fieldOf(target.focusLine, target.focusField);
                if (field) { field.focus(); }
            }
        } else if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            input.value = input.defaultValue;
            focusScan(true);
        }
    });

    ledger.addEventListener('change', function (event) {
        var unitSelect = event.target.closest('[data-unit-select]');
        if (unitSelect) {
            var unitLine = unitSelect.closest('.t-line');
            changeUnit(unitLine, unitSelect.value, $('[data-field="qty"]', unitLine));
            return;
        }
        var input = event.target.closest('.t-cell-input');
        if (input) { commit(input, { stay: true, noScroll: true }); }
    });

    ledger.addEventListener('click', function (event) {
        var chip = event.target.closest('[data-quantity-chip]');
        var line = event.target.closest('.t-line');
        if (chip && line) {
            send('quantity', { product_id: line.dataset.line, quantity: chip.dataset.quantityChip, selected: line.dataset.line });
            return;
        }
        if (line && !event.target.closest('input, button, a, select')) {
            select(line.dataset.line, false);
            focusScan();
        }
    });

    /* ---------- forms in the markup answer through the same endpoints ---------- */

    function arm(button, label) {
        if (button.classList.contains('is-armed')) { return true; }
        button.classList.add('is-armed');
        if (label) {
            button.dataset.label = button.textContent;
            button.textContent = label;
        }
        button.dataset.armTimer = String(window.setTimeout(function () { disarm(button); }, 3000));
        return false;
    }

    function disarm(button) {
        if (!button || !button.classList.contains('is-armed')) { return; }
        window.clearTimeout(Number(button.dataset.armTimer));
        button.classList.remove('is-armed');
        if (button.dataset.label) { button.textContent = button.dataset.label; delete button.dataset.label; }
    }

    root.addEventListener('submit', function (event) {
        var form = event.target;
        var value = function (name) { var field = form.elements[name]; return field ? field.value : ''; };
        if (form === checkoutForm) {
            event.preventDefault();
            closeSale();
            return;
        }
        if (form.id === 'till-customer-form') {
            event.preventDefault();
            saveCustomer(form);
            return;
        }
        if (form.hasAttribute('data-remove-form')) {
            event.preventDefault();
            send('remove', { product_id: value('product_id') });
        } else if (form.hasAttribute('data-quantity-form') || form.hasAttribute('data-inline-pricing')) {
            event.preventDefault();
        } else if (form.hasAttribute('data-held-resume')) {
            event.preventDefault();
            send('resume', { held_id: value('held_id') });
        } else if (form.hasAttribute('data-held-discard')) {
            event.preventDefault();
            var button = $('button', form);
            if (arm(button)) {
                send('discard', { held_id: value('held_id') });
            } else {
                notify({ kind: 'due', text: 'Press the bin again to discard this held bill.' });
            }
        } else if (form === holdForm) {
            event.preventDefault();
            holdOrResume(event.submitter);
        } else if (form.id === 'till-browse-form') {
            event.preventDefault();
            var picked = event.submitter;
            if (picked && picked.value) { send('add', { product_id: picked.value }, { focus: picked }); }
        }
    });

    function holdOrResume() {
        if (lineCount) {
            var label = holdForm ? holdForm.elements.hold_label : null;
            send('hold', { hold_label: label ? label.value : '' }).then(function () { if (label) { label.value = ''; } });
            return;
        }
        var first = heldBox ? $('[data-held]', heldBox) : null;
        if (first) {
            send('resume', { held_id: first.dataset.held });
        } else {
            notify({ kind: 'due', text: 'Nothing to hold yet, and no held bill to bring back.' });
        }
    }

    if (clearBtn) {
        clearBtn.addEventListener('click', function () {
            if (arm(clearBtn, 'Press again to clear this bill')) { send('clear', {}); }
        });
    }

    /* ---------- line tools ---------- */

    function editSelected(field) {
        var input = fieldOf(selected, field);
        if (input) { input.focus(); input.select(); return true; }
        return false;
    }

    doc.getElementById('till-qty-key').addEventListener('click', function () {
        if (!editSelected('qty')) { notify({ kind: 'due', text: 'Add a line first, then change its quantity.' }); }
    });

    doc.getElementById('till-disc-key').addEventListener('click', function () {
        var active = doc.activeElement;
        // a third press, with the bill discount box in hand, flips it between percent and cash
        if (active && active === discountInput && window.ZehnTillDiscount) { window.ZehnTillDiscount.toggle(); return; }
        var onLineDiscount = active && active.dataset && active.dataset.field === 'disc';
        if (!onLineDiscount && editSelected('disc')) { return; }
        if (discountInput) { discountInput.focus(); discountInput.select(); }
    });

    doc.getElementById('till-remove-key').addEventListener('click', function () {
        if (selected) { send('remove', { product_id: selected }); }
    });

    /* ---------- side panel: the product list, a new customer ---------- */

    var browseRows = doc.getElementById('till-browse-rows');
    var browseFilter = doc.getElementById('product-search-input');
    var browseNote = doc.getElementById('product-search-meta');
    var category = root.dataset.selectedCategory || '';
    var BROWSE_LIMIT = 150;

    function openSide(name) {
        if (!side) { return; }
        $all('[data-panel]', side).forEach(function (panel) { panel.hidden = panel.dataset.panel !== name; });
        side.hidden = false;
        root.classList.add('has-side');
        if (browseKey) { browseKey.setAttribute('aria-expanded', name === 'browse' ? 'true' : 'false'); }
        try { window.localStorage.setItem('till.browse', name === 'browse' ? '1' : '0'); } catch (error) { /* no storage */ }
        if (name === 'browse') {
            renderBrowse();
            if (!coarse && browseFilter) { browseFilter.focus(); }
        } else {
            var first = $('input, textarea', $('[data-panel="' + name + '"]', side));
            if (first) { first.focus(); }
        }
    }

    function closeSide() {
        if (!side || side.hidden) { return false; }
        side.hidden = true;
        root.classList.remove('has-side');
        if (browseKey) { browseKey.setAttribute('aria-expanded', 'false'); }
        try { window.localStorage.setItem('till.browse', '0'); } catch (error) { /* no storage */ }
        focusScan(true);
        return true;
    }

    function renderBrowse() {
        if (!browseRows || !catalogue.length) { return; }
        var words = (browseFilter ? browseFilter.value : '').toLowerCase().split(/\s+/).filter(Boolean);
        var list = catalogue.filter(function (product) {
            if (category && String(product.category) !== category) { return false; }
            return words.every(function (word) { return product.find.indexOf(word) !== -1; });
        });
        browseRows.textContent = '';
        list.slice(0, BROWSE_LIMIT).forEach(function (product) {
            var item = doc.createElement('li');
            var button = doc.createElement('button');
            button.className = 't-browse-btn';
            button.name = 'product_id';
            button.value = String(product.id);
            button.disabled = product.out;
            var parts = [
                ['t-browse-name', product.name],
                ['t-browse-price k-num', product.price],
                ['t-item-meta', product.sku],
                [product.out ? 'k-state k-state--out' : (product.low ? 'k-state k-state--due' : 't-browse-stock k-num'), product.out ? 'Out of stock' : product.stock]
            ];
            parts.forEach(function (part) {
                var span = doc.createElement('span');
                span.className = part[0];
                span.textContent = part[1];
                button.appendChild(span);
            });
            item.appendChild(button);
            browseRows.appendChild(item);
        });
        if (!list.length) {
            var none = doc.createElement('li');
            none.className = 't-browse-none';
            none.textContent = words.length ? 'Nothing in this list matches what you typed.' : 'No products in this category yet.';
            browseRows.appendChild(none);
        }
        if (browseNote) {
            browseNote.textContent = list.length > BROWSE_LIMIT
                ? 'Showing the first ' + BROWSE_LIMIT + ' of ' + list.length + '. Type to narrow the list.'
                : '';
        }
    }

    if (browseKey) {
        browseKey.addEventListener('click', function () {
            var open = side && !side.hidden && !$('[data-panel="browse"]', side).hidden;
            if (open) { closeSide(); } else { openSide('browse'); }
        });
    }
    if (browseFilter) {
        browseFilter.addEventListener('input', renderBrowse);
        browseFilter.addEventListener('keydown', function (event) {
            if (event.key === 'ArrowDown') {
                var first = $('.t-browse-btn:not(:disabled)', browseRows);
                if (first) { event.preventDefault(); first.focus(); }
            }
        });
    }
    if (browseRows) {
        browseRows.addEventListener('keydown', function (event) {
            if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') { return; }
            var buttons = $all('.t-browse-btn:not(:disabled)', browseRows);
            var at = buttons.indexOf(doc.activeElement);
            if (at === -1) { return; }
            event.preventDefault();
            var next = buttons[at + (event.key === 'ArrowDown' ? 1 : -1)];
            if (next) { next.focus(); } else if (event.key === 'ArrowUp' && browseFilter) { browseFilter.focus(); }
        });
    }
    var cats = doc.getElementById('till-cats');
    if (cats) {
        cats.addEventListener('click', function (event) {
            var link = event.target.closest('[data-category]');
            if (!link || !catalogue.length) { return; }
            event.preventDefault();
            category = link.dataset.category;
            $all('[data-category]', cats).forEach(function (item) { item.classList.toggle('is-on', item === link); });
            renderBrowse();
        });
    }
    if (side) {
        side.addEventListener('click', function (event) {
            if (event.target.closest('[data-till-close]')) { closeSide(); }
        });
    }

    var newCustomer = doc.getElementById('till-new-customer');
    if (newCustomer && side && $('[data-panel="customer"]', side)) {
        newCustomer.addEventListener('click', function (event) {
            event.preventDefault();
            openSide('customer');
        });
    }

    function saveCustomer(form) {
        var button = $('button.btn-primary', form);
        $all('[data-error-for]', form).forEach(function (box) { box.hidden = true; box.textContent = ''; });
        button.disabled = true;
        window.tillSend(root.dataset.customerUrl, new URLSearchParams(new FormData(form)).toString())
            .then(function (response) { return response.json().then(function (data) { return { status: response.status, data: data }; }); })
            .then(function (result) {
                var data = result.data || {};
                if (!data.ok) {
                    var errors = data.errors || { __all__: [data.message || 'The customer could not be saved.'] };
                    Object.keys(errors).forEach(function (name) {
                        var box = $('[data-error-for="' + name + '"]', form) || $('[data-error-for="__all__"]', form);
                        box.textContent = errors[name].join(' ');
                        box.hidden = false;
                    });
                    var firstBad = form.elements[Object.keys(errors)[0]];
                    if (firstBad && firstBad.focus) { firstBad.focus(); }
                    return;
                }
                var option = doc.createElement('option');
                option.value = String(data.customer.id);
                option.textContent = data.customer.name;
                customerSelect.appendChild(option);
                customerSelect.value = option.value;
                form.reset();
                closeSide();
                send('context', {}).then(function () {
                    notify({ kind: 'in', text: data.customer.name + ' is on this bill.' });
                });
            })
            .catch(function () {
                var box = $('[data-error-for="__all__"]', form);
                box.textContent = 'Could not reach the server. Check the connection and try again.';
                box.hidden = false;
            })
            .then(function () { button.disabled = false; });
    }

    /* ---------- how it is paid ---------- */

    var contextTimer = null;

    function contextChanged(delay) {
        showMode();
        window.clearTimeout(contextTimer);
        contextTimer = window.setTimeout(function () { send('context', {}, { stay: true, noScroll: true }); }, delay || 0);
    }

    function payBy(kind) {
        if (kind === 'credit') {
            if (!creditBox) { notify({ kind: 'due', text: 'Credit sale is disabled in Settings for this business.' }); return; }
            creditBox.checked = true;
        } else {
            if (creditBox) { creditBox.checked = false; }
            (kind === 'bank' ? bankRadio : cashRadio).checked = true;
        }
        contextChanged();
        if (kind === 'credit' && customerSelect && !customerSelect.value) { customerSelect.focus(); return; }
        if (kind === 'cash' && tendered) { tendered.focus(); tendered.select(); return; }
        if (kind === 'bank' && bankSelect && bankSelect.options.length > 1) { bankSelect.focus(); return; }
        var button = shownCloseBtn();
        if (button && !button.disabled) { button.focus(); }
    }

    var armed = '';

    function again(kind) {
        // the same key a second time, while the focus is still where the first press put it, closes the sale
        var same = kind === 'credit' ? isCredit() : (!isCredit() && method() === kind);
        return armed === kind && same && checkoutForm.contains(doc.activeElement);
    }

    function payKey(kind) {
        if (again(kind)) { closeSale(); return; }
        payBy(kind);
        armed = kind;
    }

    checkoutForm.addEventListener('focusout', function (event) {
        if (!event.relatedTarget || !checkoutForm.contains(event.relatedTarget)) { armed = ''; }
    });

    doc.getElementById('till-cash-key').addEventListener('click', function () { payKey('cash'); });
    doc.getElementById('till-bank-key').addEventListener('click', function () { payKey('bank'); });

    if (cashRadio) { cashRadio.addEventListener('change', function () { if (creditBox) { creditBox.checked = false; } contextChanged(); }); }
    if (bankRadio) { bankRadio.addEventListener('change', function () { if (creditBox) { creditBox.checked = false; } contextChanged(); }); }
    if (creditBox) { creditBox.addEventListener('change', function () { contextChanged(); }); }
    if (customerSelect) { customerSelect.addEventListener('change', function () { contextChanged(); }); }
    if (bankSelect) { bankSelect.addEventListener('change', function () { contextChanged(); }); }
    if (discountInput) { discountInput.addEventListener('input', function () { contextChanged(300); }); }
    if (taxInput && taxInput.type !== 'hidden') { taxInput.addEventListener('input', function () { contextChanged(300); }); }
    if (tendered) { tendered.addEventListener('input', showChange); }
    [discountInput, taxInput, tendered].forEach(function (input) {
        if (!input || input.type === 'hidden') { return; }
        input.addEventListener('focus', function () { input.select(); });
    });

    /* ---------- closing the sale: a quiet confirmation, then the receipt opens ---------- */

    var closing = false;

    function closeSale() {
        if (closing || !canClose()) { return; }
        if (isCredit() && customerSelect && !customerSelect.value) {
            showMode();
            customerSelect.focus();
            return;
        }
        /* a bank payment carries its receipt: till-receipt.js says what is missing, beside the field */
        if (window.ZehnTillReceipt && !window.ZehnTillReceipt.ready()) { return; }
        closing = true;
        setClosable(false);
        window.clearTimeout(contextTimer);
        queue = queue.then(function () {
            var body = new URLSearchParams(new FormData(checkoutForm));
            body.set('respond', 'json');
            return (window.ZehnTillReceipt ? window.ZehnTillReceipt.send : window.tillSend)(checkoutForm.getAttribute('action') || window.location.pathname, body.toString())
                .then(function (response) {
                    return response.json().catch(function () { return null; }).then(function (data) {
                        return { response: response, data: data };
                    });
                })
                .then(function (result) {
                    var data = result.data;
                    if (result.response.status === 401 || result.response.status === 402) {
                        window.location.assign((data && data.redirect) || root.dataset.loginUrl);
                        return;
                    }
                    if (data && data.ok && data.redirect) {
                        sold(data);
                        return;
                    }
                    closing = false;
                    if (data && data.html) {
                        apply(data, { stay: true });
                    } else {
                        setClosable(true);
                        notify({ kind: 'out', text: (data && data.message) || 'The sale was not saved. Try again.' });
                    }
                })
                .catch(function () {
                    closing = false;
                    setClosable(true);
                    notify({ kind: 'out', text: 'Could not reach the server. The sale was not saved.' });
                });
        });
    }

    function sold(data) {
        var done = doc.getElementById('till-stamp');
        var credit = data.stamp === 'Credit';
        notify({ kind: 'in', text: 'Sale ' + data.sale + ' saved. Opening the receipt.' });
        if (done) {
            // A calm confirmation while the receipt loads: nothing waits on it.
            done.textContent = '';
            var badge = doc.createElement('span');
            badge.className = 'k-badge k-badge--lg ' + (credit ? 'k-badge--amber' : 'k-badge--green');
            var icon = doc.createElement('i');
            icon.className = 'bi ' + (credit ? 'bi-clock-history' : 'bi-check-circle-fill');
            icon.setAttribute('aria-hidden', 'true');
            badge.appendChild(icon);
            badge.appendChild(doc.createTextNode(credit ? 'On credit' : 'Paid'));
            var what = doc.createElement('span');
            what.className = 't-done-note';
            what.textContent = 'Sale ' + data.sale + ' saved. Opening the receipt.';
            done.appendChild(badge);
            done.appendChild(what);
            done.hidden = false;
        }
        window.location.assign(data.redirect);
    }

    /* ---------- keys the shell does not hand out ---------- */

    if (window.ZehnShell && window.ZehnShell.describeKey && doc.getElementById('is_credit')) {
        window.ZehnShell.describeKey('F11', 'Put on credit');
    }

    doc.addEventListener('keydown', function (event) {
        if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) { return; }
        var target = event.target;
        var typing = target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName || '');

        if (event.key === 'F11') {
            event.preventDefault();
            payKey('credit');
            return;
        }
        if (event.key === 'Escape') {
            if (doc.querySelector('.modal.show, .dropdown-menu.show')) { return; }
            var keys = doc.getElementById('kKeys');
            var gotoList = doc.getElementById('kGotoList');
            if ((keys && !keys.hidden) || (gotoList && !gotoList.hidden)) { return; }
            if (target && target.classList && target.classList.contains('t-cell-input')) { return; }
            if (closeSide()) { event.preventDefault(); return; }
            if (target !== scan) { event.preventDefault(); focusScan(true); }
            return;
        }
        if (typing) { return; }
        if (event.key === '/') {
            event.preventDefault();
            focusScan(true);
            return;
        }
        if (event.key === 'Delete' && selected) {
            event.preventDefault();
            send('remove', { product_id: selected });
        } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            if (target && target.closest && target.closest('#till-browse-rows')) { return; }
            event.preventDefault();
            moveSelection(event.key === 'ArrowDown' ? 1 : -1);
        } else if ((event.key === '+' || event.key === '=') && selected) {
            event.preventDefault();
            send('increase', { product_id: selected });
        } else if (event.key === '-' && selected) {
            event.preventDefault();
            send('decrease', { product_id: selected });
        } else if (event.key.length === 1 && event.key !== ' ' && !coarse && !(target && target.closest && target.closest('button, a, summary'))) {
            // a stray keystroke belongs to the scan line
            focusScan(true);
        }
    }, true);

    /* the scan line takes the focus back whenever nothing else holds it */
    doc.addEventListener('click', function () {
        window.setTimeout(function () {
            if (!busy && (doc.activeElement === doc.body || doc.activeElement === null)) { focusScan(); }
        }, 0);
    });

    /* coming back from the receipt must not show the bill that was just closed */
    window.addEventListener('pageshow', function (event) { if (event.persisted) { window.location.reload(); } });

    /* ---------- start ---------- */

    showMode();
    try {
        if (side && side.hidden && !coarse && window.matchMedia('(min-width: 992px)').matches && window.localStorage.getItem('till.browse') === '1') {
            openSide('browse');
        } else if (side && !side.hidden) {
            root.classList.add('has-side');
        }
    } catch (error) { /* no storage */ }
    focusScan();

    window.ZehnTill = { send: send, perf: perf, openSide: openSide, closeSide: closeSide, select: select };
})();
