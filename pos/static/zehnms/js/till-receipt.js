/*
 * The bank receipt on the till.
 *
 * A sale paid by bank carries its proof: a photo, a screenshot or the PDF slip. The file input sits
 * inside the bank ledger field (pos/partials/_till_receipt.html), so it shows only while Bank is the
 * method. till.js asks this script two things when a sale is closed:
 *
 *   ZehnTillReceipt.ready()    may the sale go? When the receipt is missing or cannot be accepted
 *                              the reason is written under the field and the field takes the focus.
 *   ZehnTillReceipt.send(url)  post the checkout form as multipart, so the file travels with it.
 *
 * Keys stay as they were: F10 chooses Bank, F10 again (or Enter) closes the sale. If the receipt is
 * still missing the focus lands on the file field, Enter or Space opens the file picker, and once a
 * file is chosen the focus moves to the pay button, so Enter finishes. With Bank chosen, Ctrl+V
 * attaches a screenshot copied to the clipboard.
 */
(function () {
    'use strict';

    var doc = document;
    var box = doc.getElementById('till-receipt');
    var input = doc.getElementById('till-bank-receipt');
    var form = doc.getElementById('checkout-form');
    if (!box || !input || !form) { return; }

    var errorBox = doc.getElementById('till-receipt-error');
    var bankRadio = doc.getElementById('pay_bank');
    var creditBox = doc.getElementById('is_credit');
    var ledger = doc.getElementById('till-ledger');
    var required = box.dataset.required === '1';
    var maxBytes = Number(box.dataset.maxBytes) || 0;
    var maxLabel = box.dataset.maxLabel || '';
    var TYPES = /^(image\/(jpeg|png|webp)|application\/pdf)$/i;
    var NAMES = /\.(jpe?g|png|webp|pdf)$/i;
    var MISSING = 'Attach the bank receipt (a photo, a screenshot or the PDF slip) for this bank payment.';

    /* a receipt is wanted only for money that really arrives by bank: not for cash, not for credit */
    function wanted() {
        return !!(bankRadio && bankRadio.checked) && !(creditBox && creditBox.checked);
    }

    function chosen() {
        return input.files && input.files.length ? input.files[0] : null;
    }

    function say(text) {
        errorBox.textContent = '';
        errorBox.hidden = !text;
        input.classList.toggle('is-invalid', !!text);
        if (!text) {
            input.removeAttribute('aria-invalid');
            return;
        }
        input.setAttribute('aria-invalid', 'true');
        var icon = doc.createElement('i');
        icon.className = 'bi bi-exclamation-circle';
        icon.setAttribute('aria-hidden', 'true');
        errorBox.appendChild(icon);
        errorBox.appendChild(doc.createTextNode(text));
    }

    /* the server judges the content of the file; this only catches what is plain before sending */
    function problem(file) {
        if (!file) { return required ? MISSING : ''; }
        if (maxBytes && file.size > maxBytes) {
            return 'This receipt is too large. Choose a file under ' + maxLabel + '.';
        }
        if (file.type ? !TYPES.test(file.type) : !NAMES.test(file.name)) {
            return 'A bank receipt must be a JPG, PNG or WEBP image, or a PDF. This file is something else.';
        }
        return '';
    }

    function ready() {
        if (!wanted()) { say(''); return true; }
        var text = problem(chosen());
        say(text);
        if (text) { input.focus(); }
        return !text;
    }

    function send(url) {
        var body = new FormData(form);
        body.set('respond', 'json');
        if (!wanted()) { body.delete(input.name); }
        return fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': window.getCsrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
            body: body
        }).then(function (response) {
            /* a receipt the server refused is explained beside the field, not only under the bill */
            response.clone().json().then(function (data) {
                if (data && data.receipt_error) {
                    say(data.receipt_error);
                    input.focus();
                }
            }).catch(function () { /* not JSON: till.js reports it */ });
            return response;
        });
    }

    function clear() {
        input.value = '';
        say('');
    }

    function payButton() {
        var buttons = Array.prototype.slice.call(doc.querySelectorAll('.t-close-btn'));
        return buttons.filter(function (button) { return button.offsetParent !== null && !button.disabled; })[0] || null;
    }

    input.addEventListener('change', function () {
        var file = chosen();
        var text = file ? problem(file) : '';
        say(text);
        if (file && !text) {
            var button = payButton();
            if (button) { button.focus(); }
        }
    });

    /* a screenshot copied on this PC: Ctrl+V attaches it while Bank is the method */
    doc.addEventListener('paste', function (event) {
        var files = event.clipboardData && event.clipboardData.files;
        if (!wanted() || !files || !files.length || !TYPES.test(files[0].type || '')) { return; }
        try {
            var carrier = new DataTransfer();
            carrier.items.add(files[0]);
            input.files = carrier.files;
        } catch (error) {
            return; /* this browser cannot fill a file field from the clipboard */
        }
        event.preventDefault();
        input.dispatchEvent(new Event('change', { bubbles: true }));
    });

    /* the receipt belongs to one sale: a bill that is emptied, held or swapped for a held one drops it */
    if (ledger && window.MutationObserver) {
        new MutationObserver(function () {
            if (!ledger.querySelector('.t-line')) { clear(); }
        }).observe(ledger, { childList: true });
    }
    doc.addEventListener('submit', function (event) {
        var target = event.target;
        if (target && (target.id === 'till-hold-form' || (target.hasAttribute && target.hasAttribute('data-held-resume')))) { clear(); }
    }, true);

    window.ZehnTillReceipt = { ready: ready, send: send, clear: clear };
})();
