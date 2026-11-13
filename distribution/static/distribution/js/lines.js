/* Line editor of the order and stock return forms: product, quantity, unit (carton, box or
   piece), rate, optional discount and condition, and the amount of each line.
   Products come from <script id="distProducts" type="application/json">. */
(function () {
    'use strict';

    var doc = document;
    var table = doc.getElementById('distLines');
    var dataNode = doc.getElementById('distProducts');
    if (!table || !dataNode) { return; }
    var products = {};
    try { products = JSON.parse(dataNode.textContent || '{}'); } catch (error) { products = {}; }
    var body = table.querySelector('tbody');
    var template = doc.getElementById('distLineTemplate');
    var addButton = doc.getElementById('distAddLine');
    var totalNodes = doc.querySelectorAll('[data-dist-total]');
    var countNode = doc.querySelector('[data-dist-count]');

    function num(value) {
        var parsed = parseFloat(String(value || '').replace(/,/g, ''));
        return isFinite(parsed) ? parsed : 0;
    }

    function money(value) {
        return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function field(row, name) { return row.querySelector('[name="' + name + '"]'); }

    function levelOf(product, code) {
        if (!product) { return null; }
        for (var i = 0; i < product.levels.length; i += 1) {
            if (product.levels[i].code === code) { return product.levels[i]; }
        }
        return null;
    }

    function fillUnits(row, product, keep) {
        var select = field(row, 'line_unit');
        if (!select) { return; }
        var current = keep || select.value;
        select.innerHTML = '';
        if (!product) {
            select.appendChild(new Option('-', ''));
            return;
        }
        product.levels.forEach(function (level) {
            select.appendChild(new Option(level.label, level.code));
        });
        select.value = levelOf(product, current) ? current : product['default'];
        // A plain product has one unit: the box stays (every line posts one unit) but reads as text.
        select.classList.toggle('dist-one-unit', product.levels.length < 2);
        select.tabIndex = product.levels.length < 2 ? -1 : 0;
    }

    function hint(row, product) {
        var node = row.querySelector('[data-line-hint]');
        if (!node) { return; }
        var parts = [];
        if (product && product.stock_text && product.stock !== '') { parts.push('In stock ' + product.stock_text); }
        if (product && product.damaged_text) { parts.push('Damaged held ' + product.damaged_text); }
        node.textContent = parts.join(' · ');
        node.hidden = !parts.length;
    }

    // The chosen customer's trade price list: { product id: { level: rate } }. Other products keep their own price.
    var customerRates = {};

    function rateFor(row, product, level) {
        var productId = field(row, 'line_product').value;
        var listed = customerRates[productId];
        return listed && listed[level.code] ? listed[level.code] : level.price;
    }

    function setRate(row, product) {
        var rate = field(row, 'line_rate');
        var unit = field(row, 'line_unit');
        var level = levelOf(product, unit ? unit.value : '');
        if (rate && level) {
            rate.value = rateFor(row, product, level);
            // Remembered, so a change of customer re-prices only the rates nobody typed over.
            rate.setAttribute('data-auto-rate', rate.value);
        }
    }

    function repriceForCustomer() {
        Array.prototype.forEach.call(body.querySelectorAll('tr'), function (row) {
            var rate = field(row, 'line_rate');
            var product = products[field(row, 'line_product').value];
            if (!rate || !product) { return; }
            var auto = rate.getAttribute('data-auto-rate');
            if (auto !== null && auto === rate.value) { setRate(row, product); }
        });
        refresh();
    }

    function refresh() {
        var total = 0;
        var count = 0;
        Array.prototype.forEach.call(body.querySelectorAll('tr'), function (row) {
            var product = field(row, 'line_product');
            var qty = num(field(row, 'line_quantity') && field(row, 'line_quantity').value);
            var rate = num(field(row, 'line_rate') && field(row, 'line_rate').value);
            var discountInput = field(row, 'line_discount');
            var discount = discountInput ? Math.min(Math.max(num(discountInput.value), 0), 100) : 0;
            var unitRate = Math.round(rate * (100 - discount)) / 100;
            var amount = Math.round(unitRate * qty * 100) / 100;
            var cell = row.querySelector('[data-amount]');
            if (cell) { cell.textContent = product && product.value ? money(amount) : ''; }
            if (product && product.value && qty > 0) { count += 1; total += amount; }
        });
        Array.prototype.forEach.call(totalNodes, function (node) { node.textContent = money(total); });
        if (countNode) { countNode.textContent = count ? '(' + count + ' line' + (count === 1 ? '' : 's') + ')' : ''; }
    }

    function wire(row, fresh) {
        var productSelect = field(row, 'line_product');
        var product = products[productSelect.value];
        fillUnits(row, product, row.getAttribute('data-unit'));
        if (fresh && product) { setRate(row, product); }
        hint(row, product);
    }

    function addLine(focus) {
        var fragment = template.content.cloneNode(true);
        var row = fragment.querySelector('tr');
        body.appendChild(fragment);
        wire(row, false);
        refresh();
        if (focus) { field(row, 'line_product').focus(); }
        return row;
    }

    body.addEventListener('change', function (event) {
        var row = event.target.closest('tr');
        if (!row) { return; }
        var name = event.target.getAttribute('name');
        var product = products[field(row, 'line_product').value];
        if (name === 'line_product') {
            fillUnits(row, product);
            setRate(row, product);
            hint(row, product);
            var qty = field(row, 'line_quantity');
            if (qty && !qty.value && product) { qty.value = '1'; }
            if (qty) { qty.focus(); qty.select(); }
        } else if (name === 'line_unit') {
            setRate(row, product);
        }
        refresh();
    });

    body.addEventListener('input', refresh);

    body.addEventListener('click', function (event) {
        var button = event.target.closest('[data-remove-line]');
        if (!button) { return; }
        var row = button.closest('tr');
        if (body.querySelectorAll('tr').length > 1) {
            row.parentNode.removeChild(row);
        } else {
            Array.prototype.forEach.call(row.querySelectorAll('input:not([type="hidden"]), select'), function (input) {
                if (input.name === 'line_discount') { input.value = '0'; } else if (input.tagName === 'SELECT' && input.name === 'line_condition') { input.selectedIndex = 0; } else { input.value = ''; }
            });
            wire(row, false);
        }
        refresh();
    });

    // Enter on the last box of the last line starts a new line, as on the purchase form.
    body.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter' || event.target.tagName === 'SELECT') { return; }
        var row = event.target.closest('tr');
        var inputs = row.querySelectorAll('input:not([type="hidden"])');
        if (event.target === inputs[inputs.length - 1] && row === body.lastElementChild) {
            event.preventDefault();
            addLine(true);
        }
    });

    if (addButton) { addButton.addEventListener('click', function () { addLine(true); }); }
    Array.prototype.forEach.call(body.querySelectorAll('tr'), function (row) {
        var rate = field(row, 'line_rate');
        wire(row, rate && !rate.value);
    });
    refresh();

    /* ---- the order form: choosing the customer fills route and order booker and shows the terms ---- */
    var customer = doc.querySelector('[data-dist-customer]');
    var terms = doc.getElementById('distTerms');
    if (customer && terms) {
        var url = customer.getAttribute('data-terms-url');
        var route = doc.querySelector('[data-dist-route]');
        var booker = doc.querySelector('[data-dist-booker]');
        var loadTerms = function (reprice) {
            if (!customer.value) {
                terms.hidden = true;
                customerRates = {};
                if (reprice) { repriceForCustomer(); }
                return;
            }
            fetch(url.replace('/0/', '/' + customer.value + '/'), { credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' } })
                .then(function (response) { return response.json(); })
                .then(function (payload) {
                    var info = payload && payload.customer;
                    if (!info) { return; }
                    customerRates = info.rates || {};
                    if (reprice) { repriceForCustomer(); }
                    var bits = [];
                    var balance = info.owes ? 'Owes <strong>' + info.balance_text + '</strong>' : (info.advance ? 'Paid in advance <strong>' + info.balance_text + '</strong>' : 'Balance <strong>clear</strong>');
                    bits.push('<span>' + balance + '</span>');
                    if (info.has_limit) {
                        bits.push(info.is_over
                            ? '<span class="dist-over">Credit limit <strong>' + info.limit_text + '</strong>, over by <strong>' + info.over_text + '</strong></span>'
                            : '<span>Credit limit <strong>' + info.limit_text + '</strong>, <strong>' + info.left_text + '</strong> left</span>');
                    } else {
                        bits.push('<span>No credit limit</span>');
                    }
                    if (info.price_list) {
                        var listBit = doc.createElement('span');
                        listBit.textContent = 'Buys at ';
                        var listName = doc.createElement('strong');
                        listName.textContent = info.price_list;
                        listBit.appendChild(listName);
                        bits.push(listBit.outerHTML);
                    }
                    terms.innerHTML = bits.join('');
                    terms.hidden = false;
                    if (reprice && route && info.route_id) { route.value = info.route_id; }
                    if (reprice && booker && info.order_booker_id) { booker.value = info.order_booker_id; }
                })
                .catch(function () { terms.hidden = true; });
        };
        customer.addEventListener('change', function () { loadTerms(true); });
        // An order opened for editing (or sent back with an error) already has its customer: load the rates, change nothing.
        if (customer.value) { loadTerms(false); }
    }

    /* ---- the stock return form: bank account only for bank settlement ---- */
    var settlement = doc.querySelector('[data-dist-settlement]');
    var bankBox = doc.querySelector('[data-dist-bank]');
    if (settlement && bankBox) {
        var sync = function () { bankBox.hidden = settlement.value !== 'bank'; };
        settlement.addEventListener('change', sync);
        sync();
    }
})();
