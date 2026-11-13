/* The scheme form: show the route or customer box for the scope chosen, and the fields of the reward chosen. */
(function () {
    'use strict';

    var form = document.querySelector('[data-scheme-form]');
    if (!form) { return; }
    var scope = form.querySelector('[data-scheme-scope]');
    var reward = form.querySelector('[data-scheme-reward]');

    function show(attribute, value) {
        Array.prototype.forEach.call(form.querySelectorAll('[' + attribute + ']'), function (part) {
            part.hidden = part.getAttribute(attribute) !== value;
        });
    }

    function sync() {
        if (scope) { show('data-scope-part', scope.value); }
        if (reward) { show('data-reward-part', reward.value); }
    }

    if (scope) { scope.addEventListener('change', sync); }
    if (reward) { reward.addEventListener('change', sync); }
    sync();
})();
