/* Customer forms (the full page and the till's new-customer panel): route, order booker, city.
 *
 * Choosing a route fills the order booker when exactly one covers that route, and the city
 * when none is typed yet. A choice the user made himself is never overwritten. The same
 * rule runs again on the server, so the form works without this script.
 *
 * [data-fs-new="<field name>"] is a "New route" / "New order booker" button: it shows the
 * text box of that name so a missing one is typed in place, and hides it again when empty.
 */
(function () {
    'use strict';

    var doc = document;
    var holder = doc.getElementById('fs-route-defaults');
    var defaults = {};
    if (holder) {
        try { defaults = JSON.parse(holder.textContent) || {}; } catch (error) { defaults = {}; }
    }

    function field(form, name) {
        return form.querySelector('[name="' + name + '"]');
    }

    function wire(form) {
        var route = field(form, 'route');
        var booker = field(form, 'order_booker');
        var city = field(form, 'city');

        if (route) {
            if (booker) {
                booker.addEventListener('change', function () { delete booker.dataset.fsFilled; });
            }
            route.addEventListener('change', function () {
                var known = defaults[route.value];
                if (booker && (!booker.value || booker.dataset.fsFilled === '1')) {
                    booker.value = known && known.booker ? known.booker : '';
                    if (booker.value) { booker.dataset.fsFilled = '1'; } else { delete booker.dataset.fsFilled; }
                }
                if (city && known && known.city && !city.value.trim()) { city.value = known.city; }
            });
        }

        Array.prototype.forEach.call(form.querySelectorAll('[data-fs-new]'), function (button) {
            var input = field(form, button.getAttribute('data-fs-new'));
            if (!input) { return; }
            var row = input.closest('[data-fs-new-row]') || input;
            button.setAttribute('aria-expanded', row.hidden ? 'false' : 'true');
            button.addEventListener('click', function () {
                var open = row.hidden;
                row.hidden = !open;
                button.setAttribute('aria-expanded', open ? 'true' : 'false');
                if (open) {
                    input.focus();
                } else {
                    input.value = '';
                }
            });
        });

        // After the form is cleared (the till resets it once the customer is saved) the typed-new boxes fold away.
        form.addEventListener('reset', function () {
            Array.prototype.forEach.call(form.querySelectorAll('[data-fs-new-row]'), function (row) { row.hidden = true; });
            Array.prototype.forEach.call(form.querySelectorAll('[data-fs-new]'), function (button) { button.setAttribute('aria-expanded', 'false'); });
            if (booker) { delete booker.dataset.fsFilled; }
        });
    }

    Array.prototype.forEach.call(doc.querySelectorAll('form[data-fs-customer], #till-customer-form'), wire);
})();
