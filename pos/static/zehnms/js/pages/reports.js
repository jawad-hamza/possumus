/* Report pages: filters that apply themselves, folded-table labels, the remembered date shortcut. */
(function () {
    'use strict';

    var doc = document;
    var STORE_KEY = 'zehnms.report.preset';

    function all(selector, root) {
        return Array.prototype.slice.call((root || doc).querySelectorAll(selector));
    }

    function remembered() {
        try { return window.localStorage.getItem(STORE_KEY) || ''; } catch (error) { return ''; }
    }

    function remember(label) {
        try {
            if (label) { window.localStorage.setItem(STORE_KEY, label); } else { window.localStorage.removeItem(STORE_KEY); }
        } catch (error) { /* private mode: the shortcut is simply not remembered */ }
    }

    function iso(date) {
        var month = String(date.getMonth() + 1);
        var day = String(date.getDate());
        return date.getFullYear() + '-' + (month.length < 2 ? '0' : '') + month + '-' + (day.length < 2 ? '0' : '') + day;
    }

    /* The same shortcuts the server offers, worked out from the local date. */
    function presetDates(label) {
        var now = new Date();
        var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        function shifted(days) { return new Date(today.getFullYear(), today.getMonth(), today.getDate() + days); }
        switch (label) {
        case 'Today': return { start: iso(today), end: iso(today) };
        case 'Yesterday': return { start: iso(shifted(-1)), end: iso(shifted(-1)) };
        case 'Last 7 days': return { start: iso(shifted(-6)), end: iso(today) };
        case 'This month': return { start: iso(new Date(today.getFullYear(), today.getMonth(), 1)), end: iso(today) };
        case 'Last month': return {
            start: iso(new Date(today.getFullYear(), today.getMonth() - 1, 1)),
            end: iso(new Date(today.getFullYear(), today.getMonth(), 0))
        };
        case 'This year': return { start: iso(new Date(today.getFullYear(), 0, 1)), end: iso(today) };
        case 'All time': return { all: true };
        default: return null;
        }
    }

    /* Pick a product or a category and the report reloads: no second click on Apply. */
    all('#report-filter-form [data-auto-submit]').forEach(function (field) {
        field.addEventListener('change', function () {
            if (field.form.requestSubmit) { field.form.requestSubmit(); } else { field.form.submit(); }
        });
    });

    /* Folded entries on a phone read "label: value"; the labels come from the column heads. */
    all('table[data-rp-labels]').forEach(function (table) {
        var heads = all('thead th', table).map(function (th) { return th.textContent.replace(/\s+/g, ' ').trim(); });
        all('tbody > tr, tfoot > tr', table).forEach(function (row) {
            Array.prototype.forEach.call(row.children, function (cell, index) {
                if (!cell.hasAttribute('data-label') && heads[index] && cell.colSpan === 1) {
                    cell.setAttribute('data-label', heads[index]);
                }
            });
        });
    });

    /* Keep the open report in view when the index scrolls sideways. */
    var index = doc.querySelector('[data-rp-index]');
    var current = index && index.querySelector('[aria-current="page"]');
    if (index && current && index.scrollWidth > index.clientWidth) {
        var box = index.getBoundingClientRect();
        var spot = current.getBoundingClientRect();
        if (spot.right > box.right || spot.left < box.left) {
            index.scrollLeft += spot.left - box.left - 24;
        }
    }

    /* The date shortcut last used is the one the next report opens on. */
    var form = doc.querySelector('form[data-rp-range="range"]');
    if (form) {
        var presets = all('[data-rp-preset]');
        presets.forEach(function (link) {
            link.addEventListener('click', function () { remember(link.getAttribute('data-rp-preset')); });
        });
        /* Typing dates by hand forgets the shortcut; changing another filter does not. */
        var dateFields = all('input[type="date"]', form);
        var typed = dateFields.map(function (field) { return field.value; }).join('|');
        form.addEventListener('submit', function () {
            if (dateFields.map(function (field) { return field.value; }).join('|') !== typed) { remember(''); }
        });

        var query = window.location.search;
        var bare = !/[?&](start|end|from|to|all|page|export)=/.test(query);
        var wanted = remembered();
        if (bare && wanted) {
            var match = presets.filter(function (link) { return link.getAttribute('data-rp-preset') === wanted; })[0];
            if (match && !match.classList.contains('active')) {
                window.location.replace(match.href);
            }
        }
    }

    /* Extra columns (tax, cost) on the sales report: off by default, remembered per browser. */
    var EXTRA_KEY = 'zehnms.report.extra';
    var extraToggle = doc.querySelector('[data-rp-extra-toggle]');
    var extraRegion = doc.querySelector('[data-rp-extra]');
    if (extraToggle && extraRegion) {
        var wantExtra = false;
        try { wantExtra = window.localStorage.getItem(EXTRA_KEY) === '1'; } catch (error) { wantExtra = false; }
        extraToggle.checked = wantExtra;
        extraRegion.classList.toggle('rp-show-extra', wantExtra);
        extraToggle.addEventListener('change', function () {
            extraRegion.classList.toggle('rp-show-extra', extraToggle.checked);
            try { window.localStorage.setItem(EXTRA_KEY, extraToggle.checked ? '1' : '0'); } catch (error) { /* not remembered */ }
        });
    }

    /* Reports home: type to narrow the list; groups with no match step aside. */
    var search = doc.querySelector('[data-rp-search]');
    if (search) {
        var items = all('[data-rp-item]');
        var groups = all('[data-rp-group]');
        var none = doc.querySelector('[data-rp-none]');
        var filter = function () {
            var words = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
            items.forEach(function (item) {
                var hay = (item.getAttribute('data-rp-words') || item.textContent).toLowerCase();
                item.hidden = !words.every(function (word) { return hay.indexOf(word) !== -1; });
            });
            var anyShown = false;
            groups.forEach(function (group) {
                var shown = all('[data-rp-item]', group).some(function (item) { return !item.hidden; });
                group.hidden = !shown;
                if (shown) { anyShown = true; }
            });
            if (none) { none.hidden = anyShown; }
        };
        search.addEventListener('input', filter);
        search.addEventListener('keydown', function (event) {
            if (event.key === 'Enter') {
                var first = items.filter(function (item) { return !item.hidden; })[0];
                var link = first && first.querySelector('a[href]');
                if (link) { event.preventDefault(); window.location.href = link.href; }
            } else if (event.key === 'Escape' && search.value) {
                event.stopPropagation();
                search.value = '';
                filter();
            }
        });
    }

    /* On the reports index the links open on the remembered shortcut, and say so. */
    var memory = doc.querySelector('[data-rp-memory]');
    if (memory) {
        var label = remembered();
        var dates = label && label !== 'This month' ? presetDates(label) : null;
        var links = all('a[data-rp-ranged]');
        var original = links.map(function (link) { return link.getAttribute('href'); });
        if (dates && links.length) {
            links.forEach(function (link) {
                var url = new URL(link.href, window.location.href);
                if (!url.searchParams.has('start')) { return; }
                url.searchParams.delete('start');
                url.searchParams.delete('end');
                if (dates.all) {
                    url.searchParams.set('all', '1');
                } else {
                    url.searchParams.set('start', dates.start);
                    url.searchParams.set('end', dates.end);
                }
                link.setAttribute('href', url.pathname + url.search);
            });
            var name = memory.querySelector('[data-rp-memory-label]');
            if (name) { name.textContent = label; }
            memory.hidden = false;
            var reset = memory.querySelector('[data-rp-memory-reset]');
            if (reset) {
                reset.addEventListener('click', function () {
                    remember('');
                    links.forEach(function (link, position) { link.setAttribute('href', original[position]); });
                    memory.hidden = true;
                });
            }
        }
    }
})();
