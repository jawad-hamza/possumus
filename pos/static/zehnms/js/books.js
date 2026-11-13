/* Party ledgers: draws /api/party-statement/ rows newest first, a page at a time, and posts settlements. */
(function () {
    'use strict';

    var PAGE_SIZE = 25;
    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var FLASH_KEY = 'zehnBooksFlash';

    function amount(value) {
        var number = Number(value);
        if (!isFinite(number)) return String(value == null ? '' : value);
        return number.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    /* a customer statement reads like the printed one: 12,500 rather than 12,500.00; paisa still show */
    function statementAmount(value) {
        var number = Number(value);
        if (!isFinite(number)) return amount(value);
        var whole = Math.round(number * 100) % 100 === 0;
        return number.toLocaleString('en-US', { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: whole ? 0 : 2 });
    }

    function shortDate(iso) {
        var parts = String(iso || '').split('-');
        if (parts.length !== 3) return String(iso || '');
        return parts[2] + ' ' + (MONTHS[Number(parts[1]) - 1] || parts[1]) + ' ' + parts[0];
    }

    function words(text) {
        var clean = String(text || '').replace(/_/g, ' ').toLowerCase();
        return clean.charAt(0).toUpperCase() + clean.slice(1);
    }

    function make(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    /* part: the place the cell takes in a two-line phone entry (see k-ledger--stack) */
    function cell(row, className, label, text, part) {
        var td = make('td', className, text);
        if (label) td.setAttribute('data-label', label);
        if (part) td.setAttribute('data-k', part);
        row.appendChild(td);
        return td;
    }

    function figureCell(row, value, label, suffix, always, format) {
        var shown = Number(value) || always;
        var td = cell(row, 'k-num', label, shown ? (format || amount)(value) : '', 'amount');
        if (shown) td.setAttribute('data-k-suffix', suffix);
        return td;
    }

    function balanceCell(row, value, label, signed) {
        var number = Number(value) || 0;
        /* a statement shows the signed balance: a minus means the party has paid in advance */
        if (signed) return cell(row, 'k-num b-bal', label || 'Balance', statementAmount(number), 'balance');
        var td = cell(row, 'k-num b-bal', label || 'Balance', amount(Math.abs(number)), 'balance');
        td.appendChild(make('span', 'b-drcr', number > 0 ? 'Dr' : number < 0 ? 'Cr' : ''));
        return td;
    }

    /* goods on an entry: 20, 4.5, -1 for a return; nothing for a payment */
    function quantityCell(row, value, label) {
        var number = Number(value);
        var known = value !== null && value !== undefined && value !== '' && isFinite(number);
        var td = cell(row, 'k-num', label, known ? number.toLocaleString('en-US', { maximumFractionDigits: 3 }) : '', 'meta');
        if (known) td.setAttribute('data-k-suffix', String(label).toLowerCase());
        return td;
    }

    /* the bank receipt behind an entry: from the ledger to the proof (or to adding it) and back to this page */
    function receiptLink(receipt) {
        var link = make('a', 'k-receipt-link');
        link.href = receipt.url + '?next=' + encodeURIComponent(window.location.pathname + window.location.search);
        var icon = make('i', 'bi bi-paperclip');
        icon.setAttribute('aria-hidden', 'true');
        link.appendChild(icon);
        link.appendChild(document.createTextNode(receipt.attached ? 'Bank receipt' : 'Attach bank receipt'));
        return link;
    }

    /* what a balance means in the shop's words: they owe you = amber, you owe = red, settled = neutral */
    function meaning(type, role) {
        if (type === 'receivable') return { text: role === 'unified' ? 'They owe you' : 'Owes you', state: 'due' };
        if (type === 'payable') return { text: 'You owe them', state: 'out' };
        if (type === 'advance') {
            return role === 'supplier'
                ? { text: 'You paid in advance, they owe you', state: 'due' }
                : { text: 'Paid in advance, you owe them', state: 'out' };
        }
        return { text: 'Settled', state: 'none' };
    }

    var BADGE = { due: 'k-badge--amber', out: 'k-badge--red', in: 'k-badge--green', none: '' };

    function paintBalance(options) {
        var info = meaning(options.type, options.role);
        var number = Math.abs(Number(options.amount) || 0);
        if (options.figure) {
            options.figure.classList.remove('b-state--due', 'b-state--out', 'b-state--in', 'b-state--none');
            options.figure.classList.add('b-state--' + info.state);
            options.figure.textContent = (options.symbol ? options.symbol + ' ' : '') + amount(number);
        }
        if (options.words) {
            options.words.classList.remove('k-badge--amber', 'k-badge--red', 'k-badge--green');
            options.words.classList.add('k-badge');
            if (BADGE[info.state]) options.words.classList.add(BADGE[info.state]);
            options.words.textContent = info.text;
        }
        return info;
    }

    function ledger(options) {
        var body = options.body;
        var foot = options.foot || null;
        var pager = options.pager || null;
        var rows = [];
        var opening = 0;
        var closing = 0;
        var page = 0;
        /* a money column with nothing in it for the entries in view is left out (a cash-sale-only customer has no credits) */
        var show = { debit: true, credit: true };
        var table = body.closest ? body.closest('table') : null;
        var heads = table && table.tHead && table.tHead.rows[0] ? table.tHead.rows[0].cells : [];
        /* statement: the customer ledger as a statement. A page reads top to bottom in date order under an
           opening balance row, carries a quantity column (options.quantityLabel) and shows the signed balance. */
        var statement = !!options.statement;
        var quantityLabel = options.quantityLabel || 'Qty';
        var extra = statement ? 1 : 0;
        var totalQuantity = null;
        var periodStart = '';

        function paintHeads() {
            if (heads[2 + extra]) heads[2 + extra].hidden = !show.debit;
            if (heads[3 + extra]) heads[3 + extra].hidden = !show.credit;
        }

        function moneyCells(tr, debit, credit, always) {
            var format = statement ? statementAmount : null;
            if (show.debit) figureCell(tr, debit, 'Debit', 'Dr', always, format);
            if (show.credit) figureCell(tr, credit, 'Credit', 'Cr', always, format);
        }

        function span() {
            return 3 + extra + (show.debit ? 1 : 0) + (show.credit ? 1 : 0);
        }

        function message(text, className) {
            body.textContent = '';
            var row = make('tr');
            var td = make('td', className || 'k-empty', text);
            td.colSpan = span();
            td.setAttribute('data-k', 'note');
            row.appendChild(td);
            body.appendChild(row);
            if (foot) foot.textContent = '';
            if (pager) pager.hidden = true;
        }

        function drawFoot() {
            if (!foot) return;
            foot.textContent = '';
            if (!rows.length) return;
            var debit = 0;
            var credit = 0;
            rows.forEach(function (row) {
                debit += Number(row.debit) || 0;
                credit += Number(row.credit) || 0;
            });
            var tr = make('tr');
            var label = cell(tr, 'b-stack-title', '', 'Total for ' + rows.length + (rows.length === 1 ? ' entry' : ' entries'), 'what');
            label.colSpan = 2;
            if (statement) quantityCell(tr, totalQuantity, quantityLabel);
            moneyCells(tr, debit, credit, true);
            balanceCell(tr, closing, 'Closing', statement);
            foot.appendChild(tr);
        }

        function drawPager() {
            if (!pager) return;
            var pages = Math.ceil(rows.length / PAGE_SIZE);
            pager.hidden = pages <= 1;
            if (pages <= 1) return;
            pager.textContent = '';
            var first = page * PAGE_SIZE + 1;
            var last = Math.min(rows.length, first + PAGE_SIZE - 1);
            pager.appendChild(make('span', '', statement
                ? 'Entries ' + (rows.length - last + 1) + ' to ' + (rows.length - first + 1) + ' of ' + rows.length + ', in date order'
                : 'Entries ' + first + ' to ' + last + ' of ' + rows.length + ', newest first'));
            var buttons = make('span', 'b-pager-buttons');
            var newer = make('button', 'btn btn-sm btn-outline-secondary', 'Newer');
            var older = make('button', 'btn btn-sm btn-outline-secondary', 'Older');
            newer.type = 'button';
            older.type = 'button';
            newer.disabled = page === 0;
            older.disabled = page >= pages - 1;
            newer.addEventListener('click', function () { page -= 1; draw(true); });
            older.addEventListener('click', function () { page += 1; draw(true); });
            buttons.appendChild(newer);
            buttons.appendChild(older);
            pager.appendChild(buttons);
        }

        function draw(fromPager) {
            body.textContent = '';
            var pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
            var slice = rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
            var fragment = document.createDocumentFragment();
            /* rows are kept newest first so the first page is the latest; a statement page reads oldest first */
            if (statement) slice.reverse();
            if (statement && page === pages - 1 && (opening !== 0 || periodStart)) {
                var openRow = make('tr', 'b-bf');
                var openLabel = cell(openRow, 'b-stack-title', '', 'Opening balance' + (periodStart ? ' on ' + shortDate(periodStart) : ''), 'what');
                openLabel.colSpan = span() - 1;
                balanceCell(openRow, opening, 'Balance', true);
                fragment.appendChild(openRow);
            }
            slice.forEach(function (entry) {
                var tr = make('tr');
                cell(tr, '', 'Date', shortDate(entry.date), 'meta');
                var particulars = cell(tr, 'b-particulars', 'Entry', '', 'split');
                /* one reference per entry: the paper the shop knows (invoice number, receipt, opening balance) */
                var what = make('span', '', entry.document || (words(entry.voucher_type) + ' #' + entry.voucher_id));
                what.setAttribute('data-k', 'what');
                particulars.appendChild(what);
                if (entry.receipt) particulars.appendChild(receiptLink(entry.receipt));
                if (statement) quantityCell(tr, entry.quantity, quantityLabel);
                moneyCells(tr, entry.debit, entry.credit);
                balanceCell(tr, entry.running_balance, 'Balance', statement);
                fragment.appendChild(tr);
            });
            if (!statement && page === pages - 1 && opening !== 0) {
                var forward = make('tr', 'b-bf');
                var label = cell(forward, 'b-stack-title', '', 'Brought forward', 'what');
                label.colSpan = span() - 1;
                balanceCell(forward, opening);
                fragment.appendChild(forward);
            }
            body.appendChild(fragment);
            drawPager();
            if (fromPager && body.closest('table')) body.closest('table').scrollIntoView({ block: 'nearest' });
        }

        return {
            busy: function () {
                body.setAttribute('aria-busy', 'true');
            },
            fail: function (text) {
                body.removeAttribute('aria-busy');
                message(text || 'The ledger could not be loaded. Check the dates and try again.', 'k-empty k-out');
            },
            render: function (data) {
                body.removeAttribute('aria-busy');
                opening = Number(data.opening_balance) || 0;
                closing = Number(data.closing_balance) || 0;
                rows = (data.rows || []).map(function (entry, index) {
                    var copy = {};
                    Object.keys(entry).forEach(function (key) { copy[key] = entry[key]; });
                    copy.line = index + 1;
                    return copy;
                }).reverse();
                page = 0;
                totalQuantity = data.total_quantity === undefined ? null : data.total_quantity;
                periodStart = data.from_date || '';
                /* a statement keeps all its columns, so every customer's ledger has the same shape */
                show.debit = statement || !rows.length || rows.some(function (row) { return Number(row.debit); });
                show.credit = statement || !rows.length || rows.some(function (row) { return Number(row.credit); });
                paintHeads();
                if (!rows.length && opening === 0) {
                    message(options.empty || 'Nothing is written on this leaf yet.');
                    return;
                }
                draw(false);
                drawFoot();
            }
        };
    }

    function load(url, view) {
        view.busy();
        return fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
            .then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (data) {
                    if (!response.ok) throw new Error(data.error || 'The ledger could not be loaded.');
                    return data;
                });
            })
            .then(function (data) {
                view.render(data);
                return data;
            })
            .catch(function (error) {
                view.fail(error.message);
                return null;
            });
    }

    function csrf() {
        if (typeof window.getCsrfToken === 'function') return window.getCsrfToken();
        var match = document.cookie.match(/csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    function firstError(data) {
        if (!data || typeof data !== 'object') return '';
        if (typeof data.error === 'string') return data.error;
        if (typeof data.detail === 'string') return data.detail;
        var keys = Object.keys(data);
        for (var i = 0; i < keys.length; i += 1) {
            var value = data[keys[i]];
            if (Array.isArray(value) && value.length) return String(value[0]);
            if (typeof value === 'string') return value;
        }
        return '';
    }

    /* a bank settlement carries its receipt: the file field marked data-settle-receipt beside the bank ledger */
    function receiptFile(payload) {
        if (payload.payment_method !== 'bank') return null;
        var input = document.querySelector('[data-settle-receipt]');
        return input && input.files && input.files.length ? input.files[0] : null;
    }

    /* a file cannot travel as JSON, so the same fields go as a multipart form */
    function withReceipt(payload, file) {
        var form = new FormData();
        Object.keys(payload).forEach(function (key) {
            if (payload[key] != null && payload[key] !== '') form.append(key, payload[key]);
        });
        form.append('bank_receipt', file);
        return form;
    }

    function settle(options) {
        var errorBox = options.error;
        var button = options.button;
        function fail(text) {
            if (errorBox) {
                errorBox.textContent = text;
                errorBox.hidden = false;
            }
            if (button) {
                button.disabled = false;
                button.removeAttribute('aria-busy');
            }
        }
        if (errorBox) errorBox.hidden = true;
        if (!(Number(options.payload.amount) > 0)) {
            fail('Enter the amount paid. It must be more than zero.');
            if (options.amountField) options.amountField.focus();
            return;
        }
        if (button) {
            button.disabled = true;
            button.setAttribute('aria-busy', 'true');
        }
        var receipt = receiptFile(options.payload);
        fetch('/api/settlements/', {
            method: 'POST',
            credentials: 'same-origin',
            headers: receipt ? { 'X-CSRFToken': csrf() } : { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
            body: receipt ? withReceipt(options.payload, receipt) : JSON.stringify(options.payload)
        })
            .then(function (response) {
                if (response.ok) {
                    try { window.sessionStorage.setItem(FLASH_KEY, options.done || 'Payment recorded.'); } catch (ignore) { /* private mode */ }
                    window.location.reload();
                    return null;
                }
                return response.json().catch(function () { return {}; }).then(function (data) {
                    fail(firstError(data) || 'Settlement could not be saved.');
                });
            })
            .catch(function () {
                fail('Settlement could not be saved. Check that the app is still running and try again.');
            });
    }

    function showPendingFlash() {
        var text = null;
        try {
            text = window.sessionStorage.getItem(FLASH_KEY);
            if (text) window.sessionStorage.removeItem(FLASH_KEY);
        } catch (ignore) { /* private mode */ }
        if (text && window.ZehnShell && typeof window.ZehnShell.flash === 'function') window.ZehnShell.flash(text, 'success');
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', showPendingFlash);
    } else {
        showPendingFlash();
    }

    window.ZehnBooks = {
        amount: amount,
        meaning: meaning,
        paintBalance: paintBalance,
        ledger: ledger,
        load: load,
        settle: settle
    };
})();
