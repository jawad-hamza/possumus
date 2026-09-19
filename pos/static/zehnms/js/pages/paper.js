(function () {
    'use strict';

    var desk = document.getElementById('paperDesk');
    if (!desk) { return; }

    var STORE_KEY = 'zehnms.paper';
    var printBtn = document.getElementById('paperPrint');
    var printLabel = document.getElementById('paperPrintLabel');
    var newSale = document.getElementById('paperNewSale');
    var rollPage = document.getElementById('kPaperRollPage');
    var sheetPage = document.getElementById('kPaperA4Page');
    var choices = Array.prototype.slice.call(desk.querySelectorAll('input[name="paper"]'));

    function remembered() {
        try { return window.localStorage.getItem(STORE_KEY); } catch (error) { return null; }
    }

    function remember(value) {
        try { window.localStorage.setItem(STORE_KEY, value); } catch (error) { /* private window */ }
    }

    function setPaper(value) {
        var sheet = value === 'a4';
        desk.dataset.paper = sheet ? 'a4' : 'roll';
        if (rollPage) { rollPage.media = sheet ? 'not all' : 'print'; }
        if (sheetPage) { sheetPage.media = sheet ? 'print' : 'not all'; }
        if (printLabel && printBtn) {
            printLabel.textContent = sheet ? printBtn.dataset.a4Label : printBtn.dataset.rollLabel;
        }
        choices.forEach(function (input) { input.checked = input.value === desk.dataset.paper; });
    }

    choices.forEach(function (input) {
        input.addEventListener('change', function () {
            if (!input.checked) { return; }
            setPaper(input.value);
            remember(input.value);
        });
    });

    function asked() {
        var match = /[?&]paper=(a4|roll)(?:&|$)/.exec(window.location.search);
        return match ? match[1] : null;
    }

    setPaper((asked() || remembered()) === 'a4' ? 'a4' : 'roll');

    if (printBtn) {
        printBtn.addEventListener('click', function () { window.print(); });
    }

    if (window.ZehnShell && window.ZehnShell.describeKey) {
        window.ZehnShell.describeKey('Ctrl+Shift+P', 'Print the receipt');
        if (newSale) { window.ZehnShell.describeKey('Enter', 'New sale'); }
    }

    document.addEventListener('keydown', function (event) {
        if (event.defaultPrevented || event.isComposing || event.repeat) { return; }
        var key = event.key || '';

        if ((event.ctrlKey || event.metaKey) && event.shiftKey && !event.altKey && key.toLowerCase() === 'p') {
            event.preventDefault();
            window.print();
            return;
        }

        if (key !== 'Enter' || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || !newSale) { return; }
        var target = event.target;
        if (target && target.closest && target.closest('a, button, input, select, textarea, summary, [role="button"], [contenteditable="true"], .modal.show, .dropdown-menu.show')) { return; }
        event.preventDefault();
        window.location.assign(newSale.getAttribute('href'));
    });
}());
