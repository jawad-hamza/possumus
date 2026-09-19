/* Stock side pages: product list filters, entry-page keys, product pricing, stock adjustments, returns. */
(function () {
    'use strict';

    var doc = document;

    function isTyping(target) {
        return !!target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName || '');
    }

    /* ---- product list: live search, live filters, sorting from the column heads ---- */

    (function () {
        var form = doc.getElementById('product-filter-form');
        if (!form) { return; }
        var queryInput = doc.getElementById('q');
        var sortSelect = doc.getElementById('sort');
        var liveSelects = Array.prototype.slice.call(form.querySelectorAll('select'));
        var timer = null;

        function submitForm() {
            if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
        }

        if (queryInput) {
            queryInput.addEventListener('input', function () {
                window.clearTimeout(timer);
                timer = window.setTimeout(submitForm, 250);
            });
            // The list reloads while you type: put the caret back where it was.
            if (queryInput.value && /[?&]q=[^&]/.test(window.location.search)) {
                queryInput.focus();
                try { queryInput.setSelectionRange(queryInput.value.length, queryInput.value.length); } catch (error) { /* search inputs may refuse */ }
            }
        }

        liveSelects.forEach(function (field) {
            field.addEventListener('change', submitForm);
        });

        // Rarely used filters fold behind one button (on a phone, all of them) so the list starts high on the screen.
        var filterToggle = form.querySelector('[data-filter-toggle]');
        if (filterToggle) {
            form.classList.add('is-collapsible');
            filterToggle.setAttribute('aria-expanded', form.classList.contains('is-open') ? 'true' : 'false');
            filterToggle.addEventListener('click', function () {
                var open = form.classList.toggle('is-open');
                filterToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            });
        }

        // A family of sizes is one row; its sizes fold out underneath. Without this script they simply stay open.
        Array.prototype.slice.call(doc.querySelectorAll('[data-family-toggle]')).forEach(function (toggle) {
            var rows = Array.prototype.slice.call(doc.querySelectorAll('[data-family-of="' + toggle.getAttribute('data-family-toggle') + '"]'));
            function setOpen(open) {
                toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
                rows.forEach(function (row) { row.hidden = !open; });
            }
            setOpen(toggle.getAttribute('aria-expanded') === 'true');
            toggle.addEventListener('click', function () { setOpen(toggle.getAttribute('aria-expanded') !== 'true'); });
        });

        // Optional columns (brand, supplier, location, cost, margin) are chosen once and remembered on this device.
        var ledger = doc.getElementById('product-ledger');
        var columnsMenu = doc.querySelector('[data-columns-menu]');
        if (ledger && columnsMenu) {
            var storeKey = 'zehnms.products.columns';
            var chosen = [];
            try { chosen = JSON.parse(window.localStorage.getItem(storeKey) || '[]') || []; } catch (error) { chosen = []; }
            var boxes = Array.prototype.slice.call(columnsMenu.querySelectorAll('input[type="checkbox"]'));
            var applyColumns = function () {
                boxes.forEach(function (box) {
                    ledger.classList.toggle('st-show-' + box.value, box.checked);
                });
            };
            boxes.forEach(function (box) {
                box.checked = chosen.indexOf(box.value) !== -1;
                box.addEventListener('change', function () {
                    applyColumns();
                    var picked = boxes.filter(function (item) { return item.checked; }).map(function (item) { return item.value; });
                    try { window.localStorage.setItem(storeKey, JSON.stringify(picked)); } catch (error) { /* private window */ }
                });
            });
            applyColumns();
        }

        Array.prototype.slice.call(doc.querySelectorAll('[data-sort]')).forEach(function (button) {
            button.addEventListener('click', function (event) {
                if (!sortSelect) { return; }
                event.preventDefault();
                sortSelect.value = button.value;
                submitForm();
            });
        });
    })();

    /* ---- entry pages: Esc goes back, the first mistake gets the focus ---- */

    (function () {
        var form = doc.querySelector('form[data-st-entry]');
        if (!form) { return; }
        var backUrl = form.getAttribute('data-back-url') || '';
        // Only ever leave for a path inside this app.
        try {
            if (backUrl.charAt(0) !== '/' || new URL(backUrl, window.location.origin).origin !== window.location.origin) { backUrl = ''; }
        } catch (error) {
            backUrl = '';
        }
        var dirty = false;
        var leaving = false;

        // Fields may sit outside the form element and point at it with a form attribute.
        function touched(event) {
            if (event.target && event.target.form === form) { dirty = true; }
        }
        doc.addEventListener('input', touched);
        doc.addEventListener('change', touched);
        form.addEventListener('submit', function () { leaving = true; });

        var firstError = form.querySelector('.st-field--error input, .st-field--error select, .st-field--error textarea, tr.has-error input:not([type="hidden"])');
        if (firstError) {
            dirty = true;
            firstError.focus();
        }

        doc.addEventListener('keydown', function (event) {
            if (event.key !== 'Escape' || event.defaultPrevented || leaving || !backUrl) { return; }
            if (doc.querySelector('.modal.show, .dropdown-menu.show, .offcanvas.show')) { return; }
            if (isTyping(event.target)) {
                event.target.blur();
                return;
            }
            if (dirty && !window.confirm('Leave this page? What you typed has not been saved.')) { return; }
            leaving = true;
            window.location.assign(backUrl);
        });
    })();

    /* ---- product form: price from markup, margin against the minimum, unit-aware steps ---- */

    (function () {
        var form = doc.querySelector('form[data-product-form]');
        if (!form) { return; }

        function byName(name) { return form.querySelector('[name="' + name + '"]'); }

        // Another size of an existing item: start on the size label; without sizes, at the end of the name.
        var nameField = byName('name');
        var sizeField = byName('variant_label');
        if (form.hasAttribute('data-another-size')) {
            if (sizeField) {
                sizeField.focus();
            } else if (nameField) {
                nameField.focus();
                try { nameField.setSelectionRange(nameField.value.length, nameField.value.length); } catch (error) { /* not a text input */ }
            }
        }

        var unitField = byName('unit');
        var costField = byName('cost_price');
        var markupField = byName('markup_percent');
        var amountField = byName('markup_amount');
        var priceByField = byName('price_by');
        var priceByInputs = Array.prototype.slice.call(form.querySelectorAll('[data-price-by-input]'));
        var priceField = byName('price');
        var minimumMarginField = byName('minimum_margin_percent');
        var quantityFields = [byName('low_stock_threshold'), byName('opening_stock')].filter(Boolean);
        var previewPrice = doc.getElementById('pricing-preview-price');
        var previewMargin = doc.getElementById('pricing-preview-margin');
        var fractionalUnits = [];
        try { fractionalUnits = JSON.parse(form.getAttribute('data-fractional-units') || '[]'); } catch (error) { fractionalUnits = []; }
        var pieceUnit = form.getAttribute('data-piece-unit') || '';
        var currencySymbol = form.getAttribute('data-currency') || '';
        var sum = {
            cost: doc.querySelector('[data-sum-cost]'),
            markupLabel: doc.querySelector('[data-sum-markup-label]'),
            markup: doc.querySelector('[data-sum-markup]'),
            sale: doc.querySelector('[data-sum-sale]'),
            unit: doc.querySelector('[data-sum-unit]'),
            floor: doc.querySelector('[data-sum-floor]'),
            floorLabel: doc.querySelector('[data-sum-floor-label]')
        };
        // markupField is absent while the Markup Pricing module is off; the rest still works.
        if (!costField || !priceField || !previewPrice || !previewMargin) { return; }

        function numberFrom(field) {
            if (!field || field.value.trim() === '') { return null; }
            var value = parseFloat(field.value);
            return Number.isFinite(value) ? value : null;
        }

        function roundMoney(value) {
            return Math.round((value + Number.EPSILON) * 100) / 100;
        }

        function unitSuffix() {
            return unitField && unitField.value && unitField.value !== pieceUnit ? ' / ' + unitField.value : '';
        }

        function write(node, text) {
            if (node) { node.textContent = text; }
        }

        function refreshQuantitySteps() {
            var fractional = unitField && fractionalUnits.indexOf(unitField.value) !== -1;
            quantityFields.forEach(function (field) {
                field.step = fractional ? '0.001' : '1';
                field.min = '0';
            });
        }

        /* Which way the price is set: 'manual', 'percent' or 'amount'. Only the matching input shows. */
        function priceBy() {
            if (!priceByField) { return 'manual'; }
            return priceByField.value || 'manual';
        }

        function showPriceByInput() {
            var mode = priceBy();
            priceByInputs.forEach(function (wrap) {
                wrap.hidden = wrap.getAttribute('data-price-by-input') !== mode;
            });
        }

        function refreshSum(cost, markup, price, minimumMargin) {
            var blank = '–';
            write(sum.cost, cost !== null ? cost.toFixed(2) : blank);
            write(sum.unit, unitSuffix());
            if (price !== null && cost !== null && cost > 0) {
                var difference = roundMoney(price - cost);
                var percent = markup !== null ? markup : ((price - cost) / cost) * 100;
                var shown = Math.round(percent * 100) / 100;
                var mode = priceBy();
                write(sum.markupLabel, mode === 'percent' ? 'Markup ' + shown + '%' : (mode === 'amount' ? 'Amount on cost (' + shown + '%)' : 'Added on cost ' + shown + '%'));
                write(sum.markup, (difference < 0 ? '− ' : '+ ') + Math.abs(difference).toFixed(2));
            } else {
                write(sum.markupLabel, priceByField ? 'Markup' : 'Added on cost');
                write(sum.markup, blank);
            }
            write(sum.sale, price !== null ? currencySymbol + ' ' + price.toFixed(2) : blank);
            write(sum.floorLabel, 'Lowest price (' + minimumMargin.toFixed(2) + '% margin)');
            write(sum.floor, cost !== null && cost > 0 ? currencySymbol + ' ' + roundMoney(cost * (1 + minimumMargin / 100)).toFixed(2) : blank);
        }

        function refreshPricing() {
            var cost = numberFrom(costField);
            var mode = priceBy();
            var markup = mode === 'percent' ? numberFrom(markupField) : null;
            var amount = mode === 'amount' ? numberFrom(amountField) : null;
            var minimumMargin = numberFrom(minimumMarginField) || 0;
            var usesMarkup = markup !== null || amount !== null;

            // With a markup the server recalculates the price, so the field mirrors it and is locked.
            priceField.readOnly = usesMarkup;
            priceField.classList.toggle('bg-light', usesMarkup);
            if (usesMarkup && cost !== null && cost > 0) {
                priceField.value = (markup !== null ? roundMoney(cost * (1 + markup / 100)) : roundMoney(cost + amount)).toFixed(2);
            }

            var price = numberFrom(priceField);
            refreshSum(cost, markup, price, minimumMargin);
            if (price === null) {
                previewPrice.textContent = priceByField ? 'Enter the cost, then the sale price or a markup.' : 'Enter the cost and the sale price.';
                previewMargin.textContent = '';
                previewMargin.className = 'st-verdict';
                return;
            }

            previewPrice.textContent = usesMarkup && cost
                ? 'Sale price ' + currencySymbol + ' ' + price.toFixed(2) + unitSuffix() + ' = cost ' + cost.toFixed(2) + ' + ' + (markup !== null ? markup + '%' : amount.toFixed(2))
                : 'Sale price ' + currencySymbol + ' ' + price.toFixed(2) + unitSuffix();

            if (!cost || cost <= 0) {
                previewMargin.textContent = 'Add a cost to see the margin.';
                previewMargin.className = 'st-verdict st-dim';
                return;
            }
            var margin = ((price - cost) / cost) * 100;
            var minimumPrice = roundMoney(cost * (1 + minimumMargin / 100));
            var healthy = margin + 0.0001 >= minimumMargin;
            previewMargin.textContent = 'Margin ' + margin.toFixed(2) + '% vs minimum ' + minimumMargin.toFixed(2) + '%'
                + (healthy ? ' - OK' : ' - too low, lowest allowed price is ' + currencySymbol + ' ' + minimumPrice.toFixed(2));
            previewMargin.className = 'st-verdict' + (healthy ? '' : ' k-due');
        }

        [costField, markupField, amountField, priceField, minimumMarginField].forEach(function (field) {
            if (field) { field.addEventListener('input', refreshPricing); }
        });
        if (priceByField) {
            priceByField.addEventListener('change', function () {
                showPriceByInput();
                refreshPricing();
                var shown = priceBy() === 'percent' ? markupField : (priceBy() === 'amount' ? amountField : priceField);
                if (shown) { shown.focus(); }
            });
            showPriceByInput();
        }
        if (unitField) {
            unitField.addEventListener('change', function () {
                refreshQuantitySteps();
                refreshPricing();
            });
        }
        // Stored quantities arrive as 5.000; show them the way they are typed.
        quantityFields.forEach(function (field) {
            if (field.value !== '' && Number.isFinite(parseFloat(field.value))) { field.value = String(parseFloat(field.value)); }
        });
        refreshQuantitySteps();
        refreshPricing();
    })();

    /* ---- stock adjustments: mode, reason and unit follow the choices ---- */

    (function () {
        var form = doc.getElementById('stock-adjustment-form');
        if (!form) { return; }
        var modeField = doc.getElementById('id_adjustment_mode');
        var reasonField = doc.getElementById('id_reason_preset');
        var customReasonField = doc.getElementById('id_custom_reason');
        var deltaWrap = doc.querySelector('.quantity-delta-wrap');
        var afterWrap = doc.querySelector('.quantity-after-wrap');
        var customWrap = doc.querySelector('.custom-reason-wrap');
        var productField = form.querySelector('[name="product"]');
        var quantityFields = [form.querySelector('[name="quantity_delta"]'), form.querySelector('[name="quantity_after"]')].filter(Boolean);
        var stockHint = doc.getElementById('stock-on-hand-hint');
        var productUnits = window.ZehnStockUnits || {};

        // Weighed products take decimals (0.5 kg); counted products take whole numbers.
        function syncProductUnit() {
            var info = productField ? productUnits[productField.value] : null;
            quantityFields.forEach(function (field) {
                field.step = info ? info.step : '0.001';
            });
            if (stockHint) {
                stockHint.textContent = info ? 'On hand now: ' + info.stock : '';
                stockHint.classList.toggle('d-none', !info);
            }
        }

        function syncStockAdjustmentUi() {
            var mode = modeField ? modeField.value : '';
            var reason = reasonField ? reasonField.value : '';

            if (deltaWrap) { deltaWrap.style.display = mode === 'stock_take' ? 'none' : 'block'; }
            if (afterWrap) { afterWrap.style.display = mode === 'stock_take' ? 'block' : 'none'; }
            if (customWrap) { customWrap.style.display = reason === 'other' ? 'block' : 'none'; }
            if (mode === 'stock_take' && reasonField && !reasonField.value) {
                reasonField.value = 'stock_take_difference';
            }
            if (reason !== 'other' && customReasonField) {
                customReasonField.value = customReasonField.value || '';
            }
        }

        if (modeField) { modeField.addEventListener('change', syncStockAdjustmentUi); }
        if (reasonField) { reasonField.addEventListener('change', syncStockAdjustmentUi); }
        if (productField) { productField.addEventListener('change', syncProductUnit); }
        syncStockAdjustmentUi();
        syncProductUnit();
    })();

    /* ---- returns: a line may not take back more than is left to return ---- */

    (function () {
        var ledger = doc.querySelector('[data-return-ledger]');
        if (!ledger) { return; }
        var inputs = Array.prototype.slice.call(ledger.querySelectorAll('input[name^="qty_"]'));
        var formId = inputs.length ? inputs[0].getAttribute('form') : '';
        var form = formId ? doc.getElementById(formId) : null;
        var save = form ? form.querySelector('[data-return-save]') : null;
        var count = doc.querySelector('[data-return-count]');

        function check() {
            var over = 0;
            var chosen = 0;
            inputs.forEach(function (input) {
                var row = input.closest('tr');
                var note = row ? row.querySelector('[data-over-note]') : null;
                var value = parseFloat(input.value);
                var max = parseFloat(input.max);
                var tooMuch = Number.isFinite(value) && Number.isFinite(max) && value > max + 1e-9;
                var negative = Number.isFinite(value) && value < 0;
                if (Number.isFinite(value) && value > 0) { chosen += 1; }
                if (row) { row.classList.toggle('is-over', tooMuch || negative); }
                input.setAttribute('aria-invalid', tooMuch || negative ? 'true' : 'false');
                if (note) {
                    note.hidden = !(tooMuch || negative);
                    note.textContent = tooMuch ? 'Only ' + (input.getAttribute('data-returnable') || input.max) + ' can be returned.' : (negative ? 'Enter 0 or more.' : '');
                }
                if (tooMuch || negative) { over += 1; }
            });
            if (save) { save.disabled = over > 0; }
            if (count) {
                count.textContent = over > 0
                    ? 'Fix the marked line' + (over > 1 ? 's' : '') + ' before saving.'
                    : (chosen ? chosen + ' line' + (chosen > 1 ? 's' : '') + ' to return.' : 'Enter a quantity on at least one line.');
                count.classList.toggle('k-out', over > 0);
            }
        }

        inputs.forEach(function (input) {
            input.addEventListener('input', check);
            input.addEventListener('focus', function () { input.select(); });
        });
        check();
    })();
})();
