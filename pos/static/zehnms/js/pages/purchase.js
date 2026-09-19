/* Purchase entry: line rules, product type-ahead, Enter to move on, running totals, quick setup. */
(function () {
    'use strict';

    var doc = document;
    var config = {};
    try {
        config = JSON.parse((doc.getElementById('purchaseConfig') || {}).textContent || '{}');
    } catch (error) {
        config = {};
    }
    var ids = config.ids || {};
    var fractionalUnits = config.fractionalUnits || [];
    var pieceUnit = config.pieceUnit || '';

    var totalFormsInput = doc.getElementById('id_items-TOTAL_FORMS');
    var lineTemplate = doc.getElementById('purchaseLineTemplate');
    var lineBody = doc.getElementById('purchaseLinesBody');
    var addLineButton = doc.getElementById('addPurchaseLineBtn');
    var purchaseForm = doc.getElementById('purchaseForm');
    var paidField = purchaseForm ? purchaseForm.querySelector('[name="paid_amount"]') : null;
    var supplierField = ids.supplier ? doc.getElementById(ids.supplier) : null;

    function $all(selector, root) {
        return Array.prototype.slice.call((root || doc).querySelectorAll(selector));
    }

    function roundMoney(value) {
        return Math.round((value + Number.EPSILON) * 100) / 100;
    }

    function formatMoney(value) {
        return roundMoney(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function isHidden(row) {
        return row.classList.contains('d-none');
    }

    /* ---- lines ---- */

    function bindRemoveButton(row) {
        var removeButton = row.querySelector('[data-remove-line]');
        if (!removeButton) {
            return;
        }
        removeButton.addEventListener('click', function () {
            var deleteInput = row.querySelector('input[type="checkbox"][name$="-DELETE"]');
            if (deleteInput) {
                deleteInput.checked = true;
            }
            row.classList.add('d-none');
            refreshTotals();
            var next = visibleRows()[0];
            if (next) { focusProduct(next); }
        });
    }

    function addPurchaseLine() {
        if (!totalFormsInput || !lineTemplate || !lineBody) {
            return null;
        }

        var formIndex = Number(totalFormsInput.value);
        var markup = lineTemplate.innerHTML.replaceAll('__prefix__', String(formIndex));
        var wrapper = doc.createElement('tbody');
        wrapper.innerHTML = markup.trim();
        var row = wrapper.querySelector('tr');
        if (!row) {
            return null;
        }

        lineBody.appendChild(row);
        totalFormsInput.value = String(formIndex + 1);
        bindRemoveButton(row);
        buildCombo(row);
        refreshLineRules(row);
        refreshTotals();
        return row;
    }

    function lineParts(row) {
        var select = row.querySelector('select[name$="-product"]');
        return {
            select: select,
            option: select && select.value ? select.selectedOptions[0] : null,
            quantity: row.querySelector('input[name$="-quantity"]'),
            cost: row.querySelector('input[name$="-cost_price"]'),
            sale: row.querySelector('input[name$="-sale_price"]'),
            unit: row.querySelector('[data-line-unit]'),
            hint: row.querySelector('[data-line-price-hint]'),
            amount: row.querySelector('[data-line-amount]')
        };
    }

    // Step, unit label and price lock follow the chosen product; typed values are left alone.
    function refreshLineRules(row) {
        var parts = lineParts(row);
        var data = parts.option ? parts.option.dataset : {};
        var hasMarkup = Boolean(data.markup || data.markupAmount);
        if (parts.quantity) {
            parts.quantity.step = data.step || '0.001';
        }
        if (parts.unit) {
            parts.unit.textContent = data.unit || 'qty';
        }
        if (parts.sale) {
            parts.sale.readOnly = hasMarkup;
            parts.sale.classList.toggle('bg-light', hasMarkup);
        }
        if (parts.hint) {
            parts.hint.textContent = !hasMarkup ? '' : (data.markup
                ? 'Cost + ' + parseFloat(data.markup) + '% (set on the product)'
                : 'Cost + ' + formatMoney(parseFloat(data.markupAmount)) + ' (set on the product)');
            parts.hint.classList.toggle('d-none', !hasMarkup);
        }
    }

    function applyLinePricing(row) {
        var parts = lineParts(row);
        if (!parts.option || !parts.sale) {
            return;
        }
        var data = parts.option.dataset;
        var cost = parseFloat(parts.cost ? parts.cost.value : '');
        if (data.markup) {
            parts.sale.value = cost > 0 ? roundMoney(cost * (1 + parseFloat(data.markup) / 100)).toFixed(2) : '';
        } else if (data.markupAmount) {
            // A fixed amount on cost: cost + 20.
            parts.sale.value = cost > 0 ? roundMoney(cost + parseFloat(data.markupAmount)).toFixed(2) : '';
        } else if (!parts.sale.value && parseFloat(data.price) > 0) {
            parts.sale.value = data.price;
        }
    }

    function visibleRows() {
        return lineBody ? $all('.purchase-line-row', lineBody).filter(function (row) { return !isHidden(row); }) : [];
    }

    /* ---- running totals ---- */

    function write(selector, text) {
        $all(selector).forEach(function (node) { node.textContent = text; });
    }

    function refreshTotals() {
        var total = 0;
        var lines = 0;
        visibleRows().forEach(function (row) {
            var parts = lineParts(row);
            var quantity = parseFloat(parts.quantity ? parts.quantity.value : '');
            var cost = parseFloat(parts.cost ? parts.cost.value : '');
            var amount = parts.option && quantity > 0 && cost >= 0 ? roundMoney(quantity * cost) : null;
            if (parts.amount) {
                parts.amount.textContent = amount === null ? '' : formatMoney(amount);
            }
            if (amount !== null) {
                total += amount;
                lines += 1;
            }
        });
        total = roundMoney(total);
        var paid = parseFloat(paidField ? paidField.value : '');
        if (!Number.isFinite(paid)) { paid = 0; }
        var balance = roundMoney(total - paid);
        var money = (config.currency ? config.currency + ' ' : '');

        write('[data-lines-total]', formatMoney(total));
        write('[data-lines-count]', lines ? '(' + lines + (lines === 1 ? ' line)' : ' lines)') : '');
        write('[data-sum-total]', money + formatMoney(total));
        write('[data-sum-balance]', money + formatMoney(balance));
        write('[data-bar-total]', money + formatMoney(total));
        write('[data-bar-balance]', money + formatMoney(balance));

        $all('[data-sum-balance], [data-bar-balance]').forEach(function (node) {
            node.classList.toggle('k-due', balance > 0);
            node.classList.toggle('k-out', balance < 0);
        });
        var note = doc.querySelector('[data-sum-note]');
        if (note) {
            if (!note.dataset.rest) { note.dataset.rest = note.textContent; }
            if (balance < 0) {
                note.textContent = 'Paid amount cannot exceed purchase total.';
            } else if (balance > 0 && supplierField && !supplierField.value) {
                note.textContent = 'A balance stays unpaid: choose a supplier so it goes on their account.';
            } else if (balance > 0) {
                note.textContent = 'The balance goes on the supplier’s account.';
            } else {
                note.textContent = note.dataset.rest;
            }
            note.classList.toggle('k-out', balance < 0 || (balance > 0 && supplierField && !supplierField.value));
        }
    }

    /* ---- product type-ahead over the native select ---- */

    var comboCount = 0;

    function optionLabel(option) {
        return option ? option.textContent.replace(/\s+/g, ' ').trim() : '';
    }

    function syncCombo(select) {
        var input = select && select._stInput;
        if (input) {
            input.value = select.value ? optionLabel(select.selectedOptions[0]) : '';
        }
    }

    function buildCombo(row) {
        var select = row.querySelector('select[name$="-product"]');
        if (!select || select._stInput) { return; }
        comboCount += 1;
        var listId = 'stComboList' + comboCount;
        var wrap = doc.createElement('div');
        wrap.className = 'st-combo';
        var input = doc.createElement('input');
        input.type = 'text';
        input.className = 'form-control';
        input.autocomplete = 'off';
        input.spellcheck = false;
        input.placeholder = 'Type a product name or SKU';
        input.setAttribute('role', 'combobox');
        input.setAttribute('aria-autocomplete', 'list');
        input.setAttribute('aria-expanded', 'false');
        input.setAttribute('aria-controls', listId);
        input.setAttribute('aria-label', 'Product');
        var list = doc.createElement('ul');
        list.className = 'st-combo-list';
        list.id = listId;
        list.setAttribute('role', 'listbox');
        list.hidden = true;

        select.parentNode.insertBefore(wrap, select);
        wrap.appendChild(input);
        wrap.appendChild(list);
        wrap.appendChild(select);
        select.classList.add('st-combo-native');
        select.tabIndex = -1;
        select.setAttribute('aria-hidden', 'true');
        select._stInput = input;
        syncCombo(select);

        var shown = [];
        var cursor = -1;

        function close() {
            list.hidden = true;
            list.textContent = '';
            shown = [];
            cursor = -1;
            input.setAttribute('aria-expanded', 'false');
            input.removeAttribute('aria-activedescendant');
        }

        function mark() {
            $all('li[role="option"]', list).forEach(function (item, index) {
                var active = index === cursor;
                item.setAttribute('aria-selected', active ? 'true' : 'false');
                if (active) {
                    input.setAttribute('aria-activedescendant', item.id);
                    if (item.scrollIntoView) { item.scrollIntoView({ block: 'nearest' }); }
                }
            });
        }

        function open() {
            var words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
            shown = [];
            var options = select.options;
            for (var i = 0; i < options.length && shown.length < 12; i += 1) {
                if (!options[i].value) { continue; }
                var label = optionLabel(options[i]).toLowerCase();
                var hit = words.every(function (word) { return label.indexOf(word) !== -1; });
                if (hit) { shown.push(options[i]); }
            }
            list.textContent = '';
            if (!shown.length) {
                var none = doc.createElement('li');
                none.className = 'st-combo-none';
                none.textContent = 'No product matches. Press F2 to create it in quick setup.';
                list.appendChild(none);
            }
            shown.forEach(function (option, index) {
                var item = doc.createElement('li');
                item.id = listId + '-' + index;
                item.setAttribute('role', 'option');
                var name = doc.createElement('span');
                name.textContent = optionLabel(option);
                var unit = doc.createElement('span');
                unit.textContent = option.dataset.unit || '';
                item.appendChild(name);
                item.appendChild(unit);
                item.addEventListener('mousedown', function (event) {
                    event.preventDefault();
                    choose(option);
                });
                list.appendChild(item);
            });
            cursor = shown.length ? 0 : -1;
            list.hidden = false;
            input.setAttribute('aria-expanded', 'true');
            mark();
            // Keep the list clear of the action bar at the foot of the page.
            var scroller = doc.getElementById('content');
            if (scroller) {
                var room = scroller.getBoundingClientRect().bottom - 96 - list.getBoundingClientRect().bottom;
                if (room < 0) { scroller.scrollBy(0, -room); }
            }
        }

        function choose(option) {
            select.value = option ? option.value : '';
            syncCombo(select);
            close();
            select.dispatchEvent(new Event('change', { bubbles: true }));
            if (option) {
                var quantity = row.querySelector('input[name$="-quantity"]');
                if (quantity) { quantity.focus(); quantity.select(); }
            }
        }

        input.addEventListener('input', function () {
            if (select.value) {
                select.value = '';
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }
            open();
        });
        input.addEventListener('focus', function () { input.select(); });
        input.addEventListener('blur', function () {
            close();
            syncCombo(select);
        });
        input.addEventListener('keydown', function (event) {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                if (list.hidden) { open(); return; }
                if (!shown.length) { return; }
                cursor = (cursor + (event.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length;
                mark();
            } else if (event.key === 'Enter') {
                event.preventDefault();
                event.stopPropagation();
                if (!list.hidden && shown[cursor]) {
                    choose(shown[cursor]);
                } else if (select.value) {
                    var quantity = row.querySelector('input[name$="-quantity"]');
                    if (quantity) { quantity.focus(); quantity.select(); }
                } else {
                    open();
                }
            } else if (event.key === 'Escape' && !list.hidden) {
                event.preventDefault();
                event.stopPropagation();
                close();
                syncCombo(select);
            }
        });
    }

    function focusProduct(row) {
        var select = row.querySelector('select[name$="-product"]');
        var target = select && select._stInput ? select._stInput : select;
        if (target) { target.focus(); }
    }

    /* ---- Enter walks the line, and the last box starts the next line ---- */

    function lineStops(row) {
        var parts = lineParts(row);
        return [parts.quantity, parts.cost, parts.sale].filter(function (field) {
            return field && !field.readOnly && !field.disabled;
        });
    }

    function onLineEnter(event) {
        if (event.key !== 'Enter' || event.ctrlKey || event.altKey || event.metaKey) { return; }
        var field = event.target;
        if (!field.matches || !field.matches('input[name$="-quantity"], input[name$="-cost_price"], input[name$="-sale_price"]')) { return; }
        var row = field.closest('.purchase-line-row');
        if (!row) { return; }
        event.preventDefault();
        var stops = lineStops(row);
        var next = stops[stops.indexOf(field) + 1];
        if (next) {
            next.focus();
            next.select();
            return;
        }
        var rows = visibleRows();
        var following = rows[rows.indexOf(row) + 1] || addPurchaseLine();
        if (following) { focusProduct(following); }
    }

    /* ---- quick setup ---- */

    function appendOption(select, value, label, selected, dataset) {
        if (!select) {
            return;
        }

        var stringValue = String(value);
        var option = Array.prototype.slice.call(select.options).find(function (item) { return item.value === stringValue; });
        if (!option) {
            option = doc.createElement('option');
            option.value = stringValue;
            option.textContent = label;
            select.appendChild(option);
        } else {
            option.textContent = label;
        }
        Object.keys(dataset || {}).forEach(function (key) {
            option.dataset[key] = dataset[key];
        });

        if (selected) {
            select.value = stringValue;
        }
    }

    function visibleProductSelects() {
        return $all('select[name$="-product"]').filter(function (select) {
            var row = select.closest('tr');
            return !(row && row.classList.contains('d-none'));
        });
    }

    function productOptionData(product) {
        var unit = product.unit || pieceUnit;
        return {
            unit: unit,
            step: fractionalUnits.indexOf(unit) !== -1 ? '0.001' : '1',
            markup: product.markup_percent || '',
            markupAmount: product.markup_amount || '',
            minMargin: product.minimum_margin_percent || '0',
            price: product.price || ''
        };
    }

    function assignProductToPurchaseLine(product) {
        var targetSelect = visibleProductSelects().find(function (select) { return !select.value; });
        if (!targetSelect) {
            var newRow = addPurchaseLine();
            targetSelect = newRow ? newRow.querySelector('select[name$="-product"]') : null;
        }
        if (!targetSelect) {
            return;
        }

        appendOption(targetSelect, product.id, product.name + ' (' + product.sku + ')', true, productOptionData(product));
        syncCombo(targetSelect);
        var row = targetSelect.closest('tr');
        if (row) {
            refreshLineRules(row);
            applyLinePricing(row);
            refreshTotals();
        }
    }

    function setFeedback(form, messages, tone) {
        var feedback = form.querySelector('[data-form-feedback]');
        if (!feedback) {
            return;
        }

        if (!messages || messages.length === 0) {
            feedback.textContent = '';
            feedback.className = 'st-feedback d-none';
            return;
        }

        feedback.className = 'st-feedback' + (tone === 'success' ? '' : ' k-out');
        feedback.textContent = '';
        messages.forEach(function (message, index) {
            if (index) { feedback.appendChild(doc.createElement('br')); }
            feedback.appendChild(doc.createTextNode(message));
        });
    }

    function flattenErrors(errorMap) {
        var messages = [];
        Object.keys(errorMap || {}).forEach(function (key) {
            (errorMap[key] || []).forEach(function (message) { messages.push(message); });
        });
        return messages;
    }

    function submitQuickForm(form) {
        var button = form.querySelector('button[type="submit"]');
        if (button) { button.disabled = true; }
        form.setAttribute('aria-busy', 'true');
        return fetch(form.action, {
            method: 'POST',
            body: new FormData(form),
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (payload) {
                if ((response.status === 401 || response.status === 402) && payload.redirect) {
                    window.location = payload.redirect;
                    return;
                }
                if (!response.ok) {
                    var problems = flattenErrors(payload.errors || {});
                    setFeedback(form, problems.length ? problems : ['This could not be saved. Check the entries and try again.'], 'error');
                    return;
                }

                var kind = form.dataset.kind;
                // The reply may not carry unit or markup, so keep what was typed before the form resets.
                var typedValue = function (suffix) {
                    var field = form.querySelector('[name$="-' + suffix + '"]');
                    return field ? field.value : '';
                };
                if (kind === 'product' && payload.product) {
                    payload.product.unit = payload.product.unit || typedValue('unit');
                    payload.product.markup_percent = payload.product.markup_percent || typedValue('markup_percent');
                    payload.product.markup_amount = payload.product.markup_amount || typedValue('markup_amount');
                    payload.product.minimum_margin_percent = payload.product.minimum_margin_percent || typedValue('minimum_margin_percent');
                }
                if (kind === 'brand') {
                    appendOption(doc.getElementById(ids.quickBrand), payload.brand.id, payload.brand.name, true);
                    setFeedback(form, ['Brand "' + payload.brand.name + '" is ready for product creation.'], 'success');
                }

                if (kind === 'category') {
                    appendOption(doc.getElementById(ids.quickCategory), payload.category.id, payload.category.name, true);
                    setFeedback(form, ['Category "' + payload.category.name + '" is ready for product creation.'], 'success');
                }

                if (kind === 'supplier') {
                    appendOption(doc.getElementById(ids.supplier), payload.supplier.id, payload.supplier.name, true);
                    appendOption(doc.getElementById(ids.quickSupplier), payload.supplier.id, payload.supplier.name, true);
                    setFeedback(form, ['Supplier "' + payload.supplier.name + '" has been selected on this purchase.'], 'success');
                    refreshTotals();
                }

                if (kind === 'product') {
                    $all('select[name$="-product"]').forEach(function (select) {
                        appendOption(select, payload.product.id, payload.product.name + ' (' + payload.product.sku + ')', false, productOptionData(payload.product));
                    });
                    // The blank row kept for new lines must offer it too.
                    if (lineTemplate && lineTemplate.content) {
                        $all('select[name$="-product"]', lineTemplate.content).forEach(function (select) {
                            appendOption(select, payload.product.id, payload.product.name + ' (' + payload.product.sku + ')', false, productOptionData(payload.product));
                        });
                    }
                    assignProductToPurchaseLine(payload.product);
                    setFeedback(form, ['Product "' + payload.product.name + '" is now available in the purchase lines.'], 'success');
                }

                form.reset();
            });
        }).catch(function () {
            setFeedback(form, ['Could not reach the shop server. Check that it is running and try again.'], 'error');
        }).then(function () {
            if (button) { button.disabled = false; }
            form.removeAttribute('aria-busy');
        });
    }

    var work = doc.getElementById('purchaseWork');
    var quick = doc.getElementById('quickSetup');
    var quickToggles = $all('[data-quick-toggle]');

    function setQuick(open) {
        if (!work || !quick) { return; }
        quick.hidden = !open;
        work.classList.toggle('is-side-open', open);
        quickToggles.forEach(function (button) { button.setAttribute('aria-expanded', open ? 'true' : 'false'); });
        if (open) {
            var openForm = quick.querySelector('details[open] input:not([type="hidden"]), details[open] select');
            if (openForm) { openForm.focus(); }
            if (quick.scrollIntoView && window.matchMedia('(max-width: 1199.98px)').matches) { quick.scrollIntoView({ block: 'start' }); }
        } else if (quickToggles[0]) {
            quickToggles[0].focus();
        }
    }

    quickToggles.forEach(function (button) {
        button.addEventListener('click', function () { setQuick(quick ? quick.hidden : false); });
    });
    $all('[data-quick-close]').forEach(function (button) {
        button.addEventListener('click', function () { setQuick(false); });
    });
    // "Add supplier" / "Add product" beside the fields open quick setup on that form; the purchase stays as typed.
    $all('[data-quick-open]').forEach(function (link) {
        link.addEventListener('click', function (event) {
            var kind = link.getAttribute('data-quick-open');
            var form = doc.getElementById('quick' + kind.charAt(0).toUpperCase() + kind.slice(1) + 'Form');
            var section = form ? form.closest('details') : null;
            if (!quick || !section || event.ctrlKey || event.metaKey || event.shiftKey) { return; }
            event.preventDefault();
            section.open = true;
            if (quick.hidden) { setQuick(true); }
            var first = form.querySelector('input:not([type="hidden"]), select, textarea');
            if (first) { first.focus(); }
            if (section.scrollIntoView) { section.scrollIntoView({ block: 'nearest' }); }
        });
    });

    /* ---- wiring ---- */

    $all('.purchase-line-row').forEach(bindRemoveButton);
    if (lineBody) {
        $all('.purchase-line-row', lineBody).forEach(function (row) {
            buildCombo(row);
            refreshLineRules(row);
        });
        lineBody.addEventListener('change', function (event) {
            var row = event.target.closest('.purchase-line-row');
            if (row && event.target.matches('select[name$="-product"]')) {
                refreshLineRules(row);
                applyLinePricing(row);
                refreshTotals();
            }
        });
        lineBody.addEventListener('input', function (event) {
            var row = event.target.closest('.purchase-line-row');
            if (row && event.target.matches('input[name$="-cost_price"]')) {
                applyLinePricing(row);
            }
            refreshTotals();
        });
        lineBody.addEventListener('keydown', onLineEnter);
    }
    if (addLineButton) {
        addLineButton.addEventListener('click', function () {
            var row = addPurchaseLine();
            if (row) { focusProduct(row); }
        });
    }
    if (paidField) {
        paidField.addEventListener('input', refreshTotals);
    }
    if (supplierField) {
        supplierField.addEventListener('change', refreshTotals);
    }
    $all('[data-pay-full]').forEach(function (button) {
        button.addEventListener('click', function () {
            if (!paidField) { return; }
            var total = 0;
            visibleRows().forEach(function (row) {
                var parts = lineParts(row);
                var quantity = parseFloat(parts.quantity ? parts.quantity.value : '');
                var cost = parseFloat(parts.cost ? parts.cost.value : '');
                if (parts.option && quantity > 0 && cost >= 0) { total += roundMoney(quantity * cost); }
            });
            paidField.value = roundMoney(total).toFixed(2);
            paidField.dispatchEvent(new Event('input', { bubbles: true }));
        });
    });

    $all('.quick-create-form').forEach(function (form) {
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            submitQuickForm(form);
        });
    });

    refreshTotals();
})();
