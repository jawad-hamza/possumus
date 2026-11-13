/* The till, beside the customer box: the credit limit line and the sale's order booker.
 *
 * till.js announces the customer of the bill with a "till:customer" event after every answer
 * from the server. The credit limit line is redrawn each time. The order booker box follows
 * the customer only when the customer changes, so a booker picked by hand for this sale
 * stays while lines are added.
 */
(function () {
    'use strict';

    var doc = document;
    var note = doc.getElementById('till-credit-limit');
    var booker = doc.getElementById('order_booker_id');
    var customerSelect = doc.getElementById('customer_id');
    var lastCustomer = customerSelect ? String(customerSelect.value || '') : '';

    function drawLimit(customer) {
        if (!note) { return; }
        note.textContent = '';
        if (!customer || !customer.credit_note) { note.hidden = true; return; }
        var span = doc.createElement('span');
        span.className = 'k-state ' + (customer.credit_over ? 'k-state--out' : 'k-state--none');
        span.textContent = customer.credit_note;
        note.appendChild(span);
        note.hidden = false;
    }

    function followBooker(customer) {
        if (!booker) { return; }
        var current = customer ? String(customer.id) : '';
        if (current === lastCustomer) { return; }
        lastCustomer = current;
        var wanted = customer && customer.order_booker_id ? String(customer.order_booker_id) : '';
        var known = Array.prototype.some.call(booker.options, function (option) { return option.value === wanted; });
        booker.value = known ? wanted : '';
    }

    doc.addEventListener('till:customer', function (event) {
        drawLimit(event.detail);
        followBooker(event.detail);
    });
})();
