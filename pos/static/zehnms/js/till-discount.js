/*
 * The bill discount on the till: one box that takes a percentage or a cash amount.
 *
 * The switch beside the box says which of the two was typed. The other figure shows beside the box as
 * the cashier types (worked out here from the subtotal), and every answer from the server then puts the
 * exact figure there: the amount beside a percentage, the percentage beside an amount. till.js posts the
 * box and the unit with each request and hands every answer to apply().
 *
 * Keys: F6 reaches the box; F6 again flips percent / cash and shows the same discount in the other unit.
 * In the box, "%" says the figure typed is a percentage, and "r" or "c" (or the first letter of the
 * currency) says it is cash, keeping the figure. Tab from the box lands on the switch, where the arrow
 * keys choose.
 */
(function () {
    'use strict';

    var doc = document;
    var box = doc.getElementById('discount_rate');
    var percentRadio = doc.getElementById('discount_type_percent');
    var amountRadio = doc.getElementById('discount_type_amount');
    if (!box || !percentRadio || !amountRadio) { return; }

    var other = doc.getElementById('discount-amount');
    var note = doc.getElementById('till-discount-note');
    var root = doc.querySelector('[data-currency]');
    var currency = root ? root.dataset.currency || '' : '';
    // the discount now on the bill, in both units: flipping the unit starts from it instead of from nothing
    var known = { percent: box.dataset.percent || '0.00', amount: box.dataset.amount || '0.00' };
    var subtotal = parseFloat(box.dataset.subtotal) || 0;
    var choosing = 0;

    var cashKeys = ['r', 'c'];
    var initial = currency.charAt(0).toLowerCase();
    if (/^[a-z]$/.test(initial) && initial !== 'e' && cashKeys.indexOf(initial) < 0) { cashKeys.push(initial); }

    function kind() { return amountRadio.checked ? 'amount' : 'percent'; }

    function dress() {
        // a percentage stops at 100; a cash amount is held to the bill by the server, which knows the lines
        if (kind() === 'amount') { box.removeAttribute('max'); } else { box.setAttribute('max', '100'); }
    }

    function money(value) {
        var parts = (Math.round(value * 100) / 100).toFixed(2).split('.');
        parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
        return (currency ? currency + ' ' : '') + parts.join('.');
    }

    function percentText(value) {
        var rounded = Math.round(value * 100) / 100;
        if (value > 0 && rounded === 0) { return 'under 0.01%'; }
        return String(rounded) + '%';
    }

    function preview() {
        // a quick figure while the cashier types; the server's answer replaces it with the exact one
        if (!other) { return; }
        var typed = parseFloat(box.value) || 0;
        if (kind() === 'amount') {
            other.textContent = typed > 0 && subtotal > 0 && typed <= subtotal ? '(' + percentText(typed * 100 / subtotal) + ')' : '';
        } else {
            other.textContent = '= ' + money(subtotal * Math.min(Math.max(typed, 0), 100) / 100);
        }
    }

    function send() {
        if (!window.ZehnTill) { return; }
        // answers to earlier requests must not put the switch back while this one is on its way
        choosing += 1;
        window.ZehnTill.send('context', {}, { stay: true, noScroll: true }).then(function () { choosing -= 1; });
    }

    function switched() {
        box.value = kind() === 'amount' ? known.amount : known.percent;
        dress();
        preview();
        box.focus();
        box.select();
        send();
    }

    function toggle() {
        (kind() === 'amount' ? percentRadio : amountRadio).checked = true;
        switched();
    }

    function choose(wanted) {
        // the figure in the box stays; only what it means changes
        if (kind() === wanted) { return; }
        (wanted === 'amount' ? amountRadio : percentRadio).checked = true;
        dress();
        preview();
        send();
    }

    function apply(data) {
        var discount = data && data.discount;
        if (!discount) { return; }
        known = { percent: discount.percent, amount: discount.amount };
        if (data.totals) { subtotal = parseFloat(data.totals.subtotal) || 0; }
        if (!choosing) {
            amountRadio.checked = discount.type === 'amount';
            percentRadio.checked = discount.type !== 'amount';
            dress();
        }
        if (other) { other.textContent = discount.other; }
        if (discount.error) { box.setAttribute('aria-invalid', 'true'); } else { box.removeAttribute('aria-invalid'); }
        if (note) {
            note.textContent = discount.error || '';
            note.hidden = !discount.error;
        }
    }

    box.addEventListener('keydown', function (event) {
        if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) { return; }
        var key = (event.key || '').toLowerCase();
        if (key === '%') {
            event.preventDefault();
            choose('percent');
        } else if (cashKeys.indexOf(key) >= 0) {
            event.preventDefault();
            choose('amount');
        }
    });
    box.addEventListener('input', preview);
    percentRadio.addEventListener('change', switched);
    amountRadio.addEventListener('change', switched);
    dress();

    window.ZehnTillDiscount = { apply: apply, kind: kind, toggle: toggle };
})();
