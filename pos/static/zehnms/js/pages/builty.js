/* Builty (freight bill): the day picker, ticking purchases, the live split, adding a transporter in place. */
(function () {
    'use strict';

    var doc = document;

    function $all(selector, root) {
        return Array.prototype.slice.call((root || doc).querySelectorAll(selector));
    }

    function readJson(id) {
        var node = doc.getElementById(id);
        if (!node) { return null; }
        try { return JSON.parse(node.textContent || 'null'); } catch (error) { return null; }
    }

    /* ---- the builty page: reversing asks first ---- */
    $all('form[data-confirm]').forEach(function (form) {
        form.addEventListener('submit', function (event) {
            if (!window.confirm(form.getAttribute('data-confirm'))) { event.preventDefault(); }
        });
    });

    var form = doc.getElementById('builtyForm');
    if (!form) { return; }

    var config = readJson('builtyConfig') || {};
    var preview = readJson('builtyPreview') || {purchases: {}, hints: {}};
    var currency = config.currency || '';

    /* ---- day and supplier: show that day's purchases as soon as they change ---- */
    var dayForm = doc.getElementById('builty-day-form');
    $all('[data-day-picker]', dayForm || undefined).forEach(function (field) {
        field.addEventListener('change', function () {
            if (!dayForm || !field.value && field.type === 'date') { return; }
            if (dayForm.requestSubmit) { dayForm.requestSubmit(); } else { dayForm.submit(); }
        });
    });

    /* ---- ticking purchases ---- */
    var ticks = $all('[data-purchase-tick]', form);
    var tickAll = form.querySelector('[data-tick-all]');

    function syncTickAll() {
        if (!tickAll) { return; }
        var on = ticks.filter(function (box) { return box.checked; }).length;
        tickAll.checked = ticks.length > 0 && on === ticks.length;
        tickAll.indeterminate = on > 0 && on < ticks.length;
    }
    if (tickAll) {
        tickAll.addEventListener('change', function () {
            ticks.forEach(function (box) { box.checked = tickAll.checked; });
            refresh();
        });
    }
    ticks.forEach(function (box) {
        box.addEventListener('change', function () {
            syncTickAll();
            refresh();
        });
    });

    /* ---- bank ledger and receipt only for a bank payment ---- */
    var payment = doc.getElementById(config.payment || 'id_payment_method');
    function syncPayment() {
        var bank = payment && payment.value === 'bank';
        $all('[data-bank-only]', form).forEach(function (wrap) { wrap.hidden = !bank; });
    }
    if (payment) { payment.addEventListener('change', syncPayment); }

    /* ---- the live split: the same rule the server posts with ---- */
    var amountField = doc.getElementById(config.amount || 'id_amount');
    var basisField = doc.getElementById(config.basis || 'id_basis');
    var body = form.querySelector('[data-split-body]');
    var totalCell = form.querySelector('[data-split-total]');
    var hint = form.querySelector('[data-split-hint]');
    var barAmount = form.querySelector('[data-bar-amount]');
    var barLines = form.querySelector('[data-bar-lines]');
    var barPlural = form.querySelector('[data-bar-plural]');

    function number(value) {
        var parsed = parseFloat(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    function grouped(value, places) {
        return value.toLocaleString('en-US', {minimumFractionDigits: places, maximumFractionDigits: places});
    }

    function money(value) { return grouped(value, 2); }

    // Up to four places, trailing zeros dropped: 10, 0.3333, 2.5.
    function fine(value) {
        return String(Math.round(value * 10000) / 10000);
    }

    function quantity(value) {
        return String(Math.round(value * 1000) / 1000);
    }

    function tickedLines() {
        var lines = [];
        ticks.forEach(function (box) {
            if (!box.checked) { return; }
            (preview.purchases[box.value] || []).forEach(function (line) { lines.push(line); });
        });
        return lines;
    }

    function countOf(line, basis) {
        if (basis === 'value') {
            return {weight: number(line.lineTotal), units: number(line.lineTotal), label: 'value', text: 'By value'};
        }
        var count = line.counts[basis] || line.counts.piece;
        var units = number(line.base) / (number(count.factor) || 1);
        return {weight: units, units: units, label: count.label, text: quantity(units) + ' ' + count.label};
    }

    // Largest remainder in whole paisas: the shares always add up to the amount.
    function splitPaisa(totalPaisa, weights) {
        var whole = weights.reduce(function (sum, weight) { return sum + weight; }, 0);
        if (whole <= 0) { return weights.map(function () { return 0; }); }
        var exact = weights.map(function (weight) { return totalPaisa * weight / whole; });
        var paisas = exact.map(function (value) { return Math.floor(value + 1e-9); });
        var left = totalPaisa - paisas.reduce(function (sum, paisa) { return sum + paisa; }, 0);
        var order = exact.map(function (value, index) { return index; }).sort(function (a, b) {
            var gap = (exact[b] - paisas[b]) - (exact[a] - paisas[a]);
            return Math.abs(gap) > 1e-9 ? gap : a - b;
        });
        for (var i = 0; i < order.length && left > 0; i += 1) {
            paisas[order[i]] += 1;
            left -= 1;
        }
        return paisas;
    }

    function cell(text, className, label) {
        var td = doc.createElement('td');
        td.className = className || '';
        if (label) { td.setAttribute('data-label', label); }
        td.textContent = text;
        return td;
    }

    function emptyRow(text) {
        var tr = doc.createElement('tr');
        var td = cell(text, 'k-empty');
        td.colSpan = 6;
        td.setAttribute('data-k', 'note');
        tr.appendChild(td);
        return tr;
    }

    function refresh() {
        var basis = basisField ? basisField.value : 'box';
        var amount = amountField ? number(amountField.value) : 0;
        var totalPaisa = Math.round(amount * 100);
        var lines = tickedLines();

        if (hint) { hint.textContent = (preview.hints || {})[basis] || ''; }
        if (barAmount) { barAmount.textContent = money(amount); }
        if (barLines) { barLines.textContent = String(lines.length); }
        if (barPlural) { barPlural.hidden = lines.length === 1; }
        if (!body) { return; }
        body.textContent = '';

        if (!lines.length) {
            body.appendChild(emptyRow('Tick a purchase and enter the freight amount to see the split.'));
            if (totalCell) { totalCell.textContent = money(0); }
            return;
        }

        var counts = lines.map(function (line) { return countOf(line, basis); });
        var shares = totalPaisa > 0 ? splitPaisa(totalPaisa, counts.map(function (count) { return count.weight; })) : lines.map(function () { return 0; });
        var sum = 0;

        lines.forEach(function (line, index) {
            var count = counts[index];
            var share = shares[index] / 100;
            var qty = number(line.quantity);
            sum += shares[index];

            var tr = doc.createElement('tr');
            var what = doc.createElement('td');
            what.className = 'st-wrap';
            what.setAttribute('data-k', 'what');
            var name = doc.createElement('span');
            name.className = 'st-name';
            name.textContent = line.product;
            var sub = doc.createElement('span');
            sub.className = 'st-sub';
            sub.textContent = 'Purchase #' + line.purchase;
            what.appendChild(name);
            what.appendChild(sub);
            tr.appendChild(what);

            var qtyCell = cell(quantity(qty) + ' ' + line.unitLabel, 'k-num text-end', 'Qty');
            qtyCell.setAttribute('data-k', 'meta');
            tr.appendChild(qtyCell);

            var counted = cell(count.text, 'k-num text-end', 'Counted as');
            counted.setAttribute('data-k', 'more');
            tr.appendChild(counted);

            var perUnit = basis === 'value' || count.units <= 0 ? '–' : currency + ' ' + fine(share / count.units) + ' / ' + count.label;
            var perCell = cell(totalPaisa > 0 ? perUnit : '–', 'k-num text-end', 'Freight per unit');
            perCell.setAttribute('data-k', 'more');
            tr.appendChild(perCell);

            var landed = qty > 0 ? number(line.cost) + share / qty : number(line.cost);
            var landedCell = cell(money(landed) + ' / ' + line.unitLabel, 'k-num text-end', 'Landed cost');
            landedCell.setAttribute('data-k', 'more');
            landedCell.title = 'Bought at ' + money(number(line.cost)) + ' / ' + line.unitLabel;
            tr.appendChild(landedCell);

            var shareCell = cell(money(share), 'k-num text-end', 'Freight on line');
            shareCell.setAttribute('data-k', 'amount');
            tr.appendChild(shareCell);
            body.appendChild(tr);
        });
        if (totalCell) { totalCell.textContent = money(sum / 100); }
    }

    if (amountField) { amountField.addEventListener('input', refresh); }
    if (basisField) { basisField.addEventListener('change', refresh); }

    /* ---- add a transporter beside the form, without leaving it ---- */
    var work = doc.getElementById('builtyWork');
    var quick = doc.getElementById('quickSetup');
    var transporter = doc.getElementById(config.transporter || 'id_transporter');

    function setQuick(open) {
        if (!work || !quick) { return; }
        quick.hidden = !open;
        work.classList.toggle('is-side-open', open);
        if (open) {
            var first = quick.querySelector('input:not([type="hidden"])');
            if (first) { first.focus(); }
            if (quick.scrollIntoView && window.matchMedia('(max-width: 1199.98px)').matches) { quick.scrollIntoView({block: 'start'}); }
        }
    }
    $all('[data-quick-open]').forEach(function (link) {
        link.addEventListener('click', function (event) {
            if (!quick || event.ctrlKey || event.metaKey || event.shiftKey) { return; }
            event.preventDefault();
            setQuick(true);
        });
    });
    $all('[data-quick-close]').forEach(function (button) {
        button.addEventListener('click', function () {
            setQuick(false);
            if (transporter) { transporter.focus(); }
        });
    });

    function feedback(quickForm, messages, tone) {
        var box = quickForm.querySelector('[data-form-feedback]');
        if (!box) { return; }
        box.className = 'st-feedback' + (tone === 'success' ? '' : ' k-out');
        box.textContent = messages.join(' ');
    }

    $all('.quick-create-form').forEach(function (quickForm) {
        quickForm.addEventListener('submit', function (event) {
            event.preventDefault();
            var button = quickForm.querySelector('button[type="submit"]');
            if (button) { button.disabled = true; }
            fetch(quickForm.action, {
                method: 'POST',
                body: new FormData(quickForm),
                headers: {'X-Requested-With': 'XMLHttpRequest'}
            }).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (payload) {
                    if ((response.status === 401 || response.status === 402) && payload.redirect) {
                        window.location = payload.redirect;
                        return;
                    }
                    if (!response.ok || !payload.supplier) {
                        var problems = [];
                        Object.keys(payload.errors || {}).forEach(function (key) {
                            (payload.errors[key] || []).forEach(function (message) { problems.push(message); });
                        });
                        feedback(quickForm, problems.length ? problems : ['The transporter could not be saved. Check the name and try again.'], 'error');
                        return;
                    }
                    if (transporter) {
                        var option = doc.createElement('option');
                        option.value = payload.supplier.id;
                        option.textContent = payload.supplier.name;
                        transporter.appendChild(option);
                        transporter.value = String(payload.supplier.id);
                        transporter.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    feedback(quickForm, ['Transporter "' + payload.supplier.name + '" is chosen on this builty.'], 'success');
                    quickForm.reset();
                });
            }).catch(function () {
                feedback(quickForm, ['Could not reach the shop server. Check that it is running and try again.'], 'error');
            }).then(function () {
                if (button) { button.disabled = false; }
            });
        });
    });

    syncTickAll();
    syncPayment();
    refresh();
})();
