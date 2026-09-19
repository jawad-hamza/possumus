/*
 * ZehnMS application shell.
 *
 * Keys owned by the shell (the desktop window already owns F5, Ctrl+R, Alt+Left, Alt+Right and Ctrl+P):
 *   Ctrl+K        open the go-to box
 *   Alt+1 .. 0    open module 1 to 10, in rail order
 *   Ctrl+B        collapse or expand the rail
 *   /             focus the first field of the page (when not typing)
 *   F1            show or hide the shortcut list
 *   Esc           close the go-to box, the drawer or the shortcut list
 *
 * Keys a page may claim, by putting data-k-key on a control:
 *   F2 F3 F4 F6 F7 F8 F9 F10, and Alt+letter.
 *   <button data-k-key="F9" data-k-key-label="Take cash"> is clicked when F9 is pressed;
 *   an input, select or textarea is focused instead. It is listed under "On this page".
 *
 * Public helpers: window.ZehnShell.{flash, registerKey, describeKey, openGoto, closeAll,
 *   fitActions, dressLedgers, seatLedgers (a no-op kept for older scripts)}. Call dressLedgers() after a script
 *   shows or builds a table that stacks on a phone. window.ZehnTip.{show, hide, attach, scan} drives the help tooltips.
 */
(function () {
    'use strict';

    var RESERVED = { 'F5': 1, 'F11': 1, 'F12': 1, 'Ctrl+R': 1, 'Ctrl+P': 1, 'Alt+ArrowLeft': 1, 'Alt+ArrowRight': 1 };
    var PHONE = '(max-width: 991.98px)';
    var doc = document;
    var body = doc.body;

    function $(selector, root) { return (root || doc).querySelector(selector); }
    function $all(selector, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(selector)); }
    function isPhone() { return window.matchMedia(PHONE).matches; }
    /* a control that sits in the cell itself, not one folded away in a row-actions dropdown */
    function hasOwnField(cell) {
        return Array.prototype.some.call(cell.querySelectorAll('input:not([type="hidden"]), select, textarea'), function (el) {
            return !el.closest('.dropdown-menu');
        });
    }

    function isTyping(target) {
        if (!target || !target.tagName) { return false; }
        var tag = target.tagName;
        return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
    }

    /* ---- helpers every page may call (kept as globals) ---- */

    function getCookie(name) {
        var match = doc.cookie.split(';')
            .map(function (entry) { return entry.trim(); })
            .find(function (entry) { return entry.indexOf(name + '=') === 0; });
        return match ? decodeURIComponent(match.split('=').slice(1).join('=')) : '';
    }

    function getCsrfToken() {
        var hidden = $('input[name="csrfmiddlewaretoken"]');
        return getCookie('csrftoken') || (hidden ? hidden.value : '');
    }

    function getBrowserTimeZone() {
        try {
            return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        } catch (error) {
            return '';
        }
    }

    function updateBrowserTimeZoneLabels(browserTimeZone) {
        if (!body) { return; }
        var preferred = body.dataset.preferredTimeZone || '';
        var effective = preferred || browserTimeZone || body.dataset.effectiveTimeZone || '';
        var detectedLabel = browserTimeZone || 'Unavailable on this device';
        var effectiveLabel = effective || 'Unavailable';

        $all('[data-browser-time-zone-label]').forEach(function (el) { el.textContent = detectedLabel; });
        $all('[data-effective-time-zone-label]').forEach(function (el) { el.textContent = effectiveLabel; });
        $all('[data-browser-time-zone-input]').forEach(function (input) { input.value = browserTimeZone || ''; });
    }

    function syncBrowserTimeZone(browserTimeZone) {
        if (!body || !browserTimeZone) { return; }

        var syncUrl = body.dataset.timeZoneSyncUrl || '';
        var sessionTimeZone = body.dataset.sessionTimeZone || '';
        var preferred = body.dataset.preferredTimeZone || '';
        if (!syncUrl || sessionTimeZone === browserTimeZone) {
            if (!preferred) { body.dataset.effectiveTimeZone = browserTimeZone; }
            updateBrowserTimeZoneLabels(browserTimeZone);
            return;
        }

        var csrfToken = getCsrfToken();
        if (!csrfToken) {
            updateBrowserTimeZoneLabels(browserTimeZone);
            return;
        }

        fetch(syncUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-CSRFToken': csrfToken,
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: new URLSearchParams({ time_zone: browserTimeZone }).toString()
        })
            .then(function (response) {
                if (!response.ok) { throw new Error('Unable to sync browser time zone.'); }
                return response.json();
            })
            .then(function (payload) {
                if (payload && payload.time_zone) { body.dataset.sessionTimeZone = payload.time_zone; }
                if (!preferred) { body.dataset.effectiveTimeZone = browserTimeZone; }
                updateBrowserTimeZoneLabels(browserTimeZone);
            })
            .catch(function () {
                updateBrowserTimeZoneLabels(browserTimeZone);
            });
    }

    window.getCookie = getCookie;
    window.getCsrfToken = getCsrfToken;
    window.getBrowserTimeZone = getBrowserTimeZone;
    window.updateBrowserTimeZoneLabels = updateBrowserTimeZoneLabels;
    window.syncBrowserTimeZone = syncBrowserTimeZone;

    /* ---- rail and drawer ---- */

    var rail = doc.getElementById('sidebar');
    var scrim = doc.getElementById('mobileOverlay');
    var lastFocus = null;

    function store(key, value) {
        try {
            if (value === undefined) { return window.localStorage.getItem(key); }
            window.localStorage.setItem(key, value);
        } catch (error) { /* storage can be unavailable */ }
        return null;
    }

    function setDrawer(open) {
        if (!rail) { return; }
        rail.classList.toggle('show', open);
        if (scrim) { scrim.classList.toggle('show', open); }
        $all('[data-k-action="drawer"]').forEach(function (btn) { btn.setAttribute('aria-expanded', open ? 'true' : 'false'); });
        if (open) {
            lastFocus = doc.activeElement;
            var first = $('.k-rail-link.active', rail) || $('.k-rail-link', rail);
            if (first) { first.focus(); }
        } else if (lastFocus && lastFocus.focus) {
            lastFocus.focus();
            lastFocus = null;
        }
    }

    function toggleSidebar() {
        if (isPhone()) { toggleSidebarMobile(); return; }
        var collapsed = body.classList.toggle('k-rail-min');
        if (rail) { rail.classList.toggle('collapsed', collapsed); }
        store('sidebarCollapsed', collapsed ? 'true' : 'false');
        fitActions();
    }

    function toggleSidebarMobile() {
        setDrawer(!(rail && rail.classList.contains('show')));
    }

    window.toggleSidebar = toggleSidebar;
    window.toggleSidebarMobile = toggleSidebarMobile;

    if (rail && !isPhone() && store('sidebarCollapsed') === 'true') {
        body.classList.add('k-rail-min');
        rail.classList.add('collapsed');
    }

    /* ---- modules: numbering, tab bar state ---- */

    /* the Alt+digit of a module is told by its tooltip and the shortcut sheet, never printed in the menu */
    var moduleLinks = $all('.k-rail-link[data-k-module]');
    moduleLinks.forEach(function (link, index) {
        if (index > 9) { return; }
        var digit = index === 9 ? '0' : String(index + 1);
        link.dataset.kDigit = digit;
        if (!link.hasAttribute('data-tip')) { link.setAttribute('data-tip', (link.dataset.goto || '') + ' (Alt+' + digit + ')'); }
    });

    (function markTab() {
        var active = $('.k-rail-link.active');
        if (!active) { return; }
        var href = active.getAttribute('href');
        var visibleTabs = $all('.k-tabbar > a.k-tab').slice(0, 4);
        var hit = visibleTabs.filter(function (tab) { return tab.getAttribute('href') === href; })[0];
        if (hit) {
            hit.classList.add('active');
            hit.setAttribute('aria-current', 'page');
        } else {
            var more = $('.k-tabbar > button.k-tab');
            if (more) { more.classList.add('active'); }
        }
        active.setAttribute('aria-current', 'page');
    })();

    if ($('.k-head-actions') && $('.k-head-actions').children.length) {
        body.classList.add('k-has-actions');
    }

    /* ---- page actions: what does not fit folds into one More menu ---- */

    /* A row of actions never scrolls out of sight: what does not fit is moved (not copied) into one More menu.
       The head's row keeps only its first action on a phone; an in-page row marked k-fit keeps as many as fit. */
    var folds = [];

    function makeFold(bar, menuId, firstOnlyWhenNarrow) {
        var items = Array.prototype.slice.call(bar.children);
        var wrap = null;
        var btn = null;
        var menu = null;

        function setOpen(open) {
            if (!menu || !btn) { return false; }
            var wasOpen = !menu.hidden;
            menu.hidden = !open;
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) {
                menu.classList.remove('is-end');
                if (menu.getBoundingClientRect().right > window.innerWidth - 4) { menu.classList.add('is-end'); }
            }
            return wasOpen && !open;
        }

        function build() {
            wrap = doc.createElement('div');
            wrap.className = 'k-more';
            btn = doc.createElement('button');
            btn.type = 'button';
            btn.className = 'btn btn-outline-secondary k-more-btn';
            btn.setAttribute('aria-haspopup', 'true');
            btn.setAttribute('aria-expanded', 'false');
            btn.setAttribute('aria-controls', menuId);
            btn.appendChild(doc.createTextNode('More '));
            var caret = doc.createElement('i');
            caret.className = 'bi bi-chevron-down';
            caret.setAttribute('aria-hidden', 'true');
            btn.appendChild(caret);
            menu = doc.createElement('div');
            menu.className = 'k-more-menu';
            menu.id = menuId;
            menu.hidden = true;
            wrap.appendChild(btn);
            wrap.appendChild(menu);
            btn.addEventListener('click', function () { setOpen(menu.hidden); });
            btn.addEventListener('keydown', function (event) {
                if (event.key !== 'ArrowDown') { return; }
                event.preventDefault();
                setOpen(true);
                var first = $('a, button, input, select', menu);
                if (first) { first.focus(); }
            });
            menu.addEventListener('click', function (event) {
                if (event.target.closest('a, button')) { setOpen(false); }
            });
            doc.addEventListener('mousedown', function (event) {
                if (menu && !menu.hidden && !wrap.contains(event.target)) { setOpen(false); }
            });
        }

        function fit() {
            if (!items.length) { return; }
            setOpen(false);
            items.forEach(function (item) { bar.appendChild(item); });
            if (wrap && wrap.parentNode) { wrap.parentNode.removeChild(wrap); }
            bar.classList.remove('is-fitted');
            if (bar.scrollWidth <= bar.clientWidth + 1 || items.length < 2) {
                bar.classList.add('is-fitted');
                return;
            }
            if (!wrap) { build(); }
            bar.appendChild(wrap);
            var keep = items.length - 1;
            if (firstOnlyWhenNarrow && window.matchMedia('(max-width: 575.98px)').matches) { keep = 1; }
            while (keep >= 1) {
                items.forEach(function (item, index) {
                    if (index < keep) { bar.insertBefore(item, wrap); } else { menu.appendChild(item); }
                });
                if (keep === 1 || bar.scrollWidth <= bar.clientWidth + 1) { break; }
                keep -= 1;
            }
            bar.classList.add('is-fitted');
        }

        return {
            fit: fit,
            close: function () {
                var closed = setOpen(false);
                if (closed && btn) { btn.focus(); }
                return closed;
            }
        };
    }

    var actionsBar = $('.k-head-actions');
    if (actionsBar) { folds.push(makeFold(actionsBar, 'kMoreMenu', true)); }
    $all('.k-fit').forEach(function (bar, index) { folds.push(makeFold(bar, 'kMoreMenu' + (index + 2), false)); });

    function fitActions() {
        folds.forEach(function (fold) { fold.fit(); });
    }

    function closeMore() {
        var closed = false;
        folds.forEach(function (fold) { if (fold.close()) { closed = true; } });
        return closed;
    }

    /* ---- ledgers: the parts of a phone entry, the tap that opens one, the seat in the page margin ---- */

    var NARROW = '(max-width: 575.98px)';

    function cellText(cell) { return (cell.textContent || '').replace(/\s+/g, ' ').trim(); }

    function columnParts(table) {
        var headRow = table.tHead && table.tHead.rows[0];
        if (!headRow) { return null; }
        var isLedger = table.classList.contains('k-ledger');
        var cols = [];
        Array.prototype.forEach.call(headRow.cells, function (th) {
            for (var i = 0; i < (th.colSpan || 1); i += 1) {
                cols.push({
                    th: th,
                    text: cellText(th),
                    quiet: !cellText(th) || (th.children.length === 1 && th.firstElementChild.classList.contains('visually-hidden')),
                    numeric: /(^|\s)(k-num|text-end|num)(\s|$)/.test(th.className),
                    suffix: th.dataset.kSuffix || '',
                    part: th.dataset.k || ''
                });
            }
        });
        if (cols.some(function (col) { return col.part; })) {
            cols.forEach(function (col, index) {
                if (!col.part) { col.part = index === 0 && isLedger ? 'margin' : 'more'; }
            });
            return cols;
        }

        var free = cols.filter(function (col, index) {
            if (index === 0 && isLedger) { col.part = 'margin'; return false; }
            if (col.quiet) { col.part = 'more'; return false; }
            return true;
        });
        var figures = free.filter(function (col) { return col.numeric; });
        var words = free.filter(function (col) { return !col.numeric; });
        var debit = figures.filter(function (col) { return /^(debit|dr)$/i.test(col.text); })[0];
        var credit = figures.filter(function (col) { return /^(credit|cr)$/i.test(col.text); })[0];
        var moneyIn = figures.filter(function (col) { return /^money in$/i.test(col.text); })[0];
        var moneyOut = figures.filter(function (col) { return /^money out$/i.test(col.text); })[0];
        if (debit && credit) {
            debit.part = 'amount'; debit.suffix = 'Dr';
            credit.part = 'amount'; credit.suffix = 'Cr';
        } else if (moneyIn && moneyOut) {
            moneyIn.part = 'amount'; moneyIn.suffix = 'in';
            moneyOut.part = 'amount'; moneyOut.suffix = 'out';
        }
        var open = figures.filter(function (col) { return !col.part; });
        if (open.length > 1 || (open.length === 1 && debit && credit)) {
            var running = open.filter(function (col) { return /balance|outstanding|closing|remaining|on hand|stock|due/i.test(col.text); }).pop();
            if (running) { running.part = 'balance'; open = open.filter(function (col) { return col !== running; }); }
        }
        if (!(debit && credit) && !(moneyIn && moneyOut) && open.length) {
            var main = open.filter(function (col) { return /total|amount|net|value|sales?$|price/i.test(col.text); }).pop() || open[open.length - 1];
            main.part = 'amount';
            open = open.filter(function (col) { return col !== main; });
            if (open.length && !figures.some(function (col) { return col.part === 'balance'; })) { open[open.length - 1].part = 'balance'; }
        }
        var dated = words.filter(function (col) { return /\b(date|day|when|time)\b/i.test(col.text); })[0];
        var what = words.filter(function (col) { return col !== dated; })[0] || dated;
        if (what) { what.part = 'what'; }
        var metas = 0;
        if (dated && dated !== what) { dated.part = 'meta'; metas += 1; }
        words.forEach(function (col) {
            if (col.part || metas >= 2) { return; }
            col.part = 'meta';
            metas += 1;
        });
        cols.forEach(function (col) { if (!col.part) { col.part = 'more'; } });
        return cols;
    }

    function partRow(row, cols, inHead) {
        var at = 0;
        var hasMore = false;
        Array.prototype.forEach.call(row.cells, function (cell) {
            var span = cell.colSpan || 1;
            var covered = cols.slice(at, at + span);
            at += span;
            if (!covered.length) { return; }
            if (!cell.dataset.k) {
                var part = covered[0].part;
                if (cell.classList.contains('k-empty') || (span > 1 && span >= cols.length - 1)) {
                    part = cell.classList.contains('k-empty') || row.cells.length <= 2 ? 'note' : 'what';
                } else if (span > 1) {
                    part = covered.some(function (col) { return col.part === 'what'; }) ? 'what'
                        : (covered.filter(function (col) { return col.part !== 'margin' && col.part !== 'more'; })[0] || covered[0]).part;
                }
                if (!inHead && part !== 'margin' && part !== 'note' && !cell.children.length && /^[-–—]?$/.test(cellText(cell))) { part = 'hide'; }
                if (!inHead && part !== 'what' && part !== 'note' && hasOwnField(cell)) { part = 'field'; }
                cell.dataset.k = part;
                if (!inHead && covered[0].suffix && part === 'amount' && span === 1) { cell.dataset.kSuffix = covered[0].suffix; }
            }
            if (!inHead && (cell.dataset.k === 'more' || cell.dataset.k === 'field')) {
                if (!cell.hasAttribute('data-label') && span === 1 && !covered[0].quiet) { cell.setAttribute('data-label', covered[0].text); }
                if (cell.dataset.k === 'more' && (cellText(cell) || cell.children.length)) { hasMore = true; }
            }
        });
        return hasMore;
    }

    function armEntry(row) {
        row.classList.add('k-has-more');
        if (window.matchMedia(NARROW).matches) {
            row.tabIndex = 0;
            row.setAttribute('aria-expanded', row.classList.contains('k-open') ? 'true' : 'false');
        }
    }

    function partLedger(table) {
        var cols = columnParts(table);
        if (!cols) { return; }
        if (table.tHead) { Array.prototype.forEach.call(table.tHead.rows, function (row) { partRow(row, cols, true); }); }
        Array.prototype.forEach.call(table.tBodies, function (section) {
            Array.prototype.forEach.call(section.rows, function (row) {
                if (partRow(row, cols, false)) { armEntry(row); }
            });
        });
        if (table.tFoot) { Array.prototype.forEach.call(table.tFoot.rows, function (row) { partRow(row, cols, false); }); }
    }

    function watchLedger(table) {
        if (table.dataset.kWatched || !window.MutationObserver) { return; }
        table.dataset.kWatched = '1';
        var pending = false;
        new MutationObserver(function () {
            if (pending) { return; }
            pending = true;
            window.requestAnimationFrame(function () { pending = false; partLedger(table); });
        }).observe(table, { childList: true, subtree: true });
    }

    function toggleEntry(row) {
        var open = row.classList.toggle('k-open');
        row.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    doc.addEventListener('click', function (event) {
        if (!window.matchMedia(NARROW).matches) { return; }
        var row = event.target.closest && event.target.closest('.k-ledger--stack > tbody > tr.k-has-more');
        if (!row || event.target.closest('a, button, input, select, textarea, label, summary')) { return; }
        toggleEntry(row);
    });

    doc.addEventListener('keydown', function (event) {
        if ((event.key !== 'Enter' && event.key !== ' ') || !event.target.classList || !event.target.classList.contains('k-has-more')) { return; }
        if (!window.matchMedia(NARROW).matches) { return; }
        event.preventDefault();
        toggleEntry(event.target);
    });

    /* kept for older page scripts: there is no page margin any more, so nothing is seated in it */
    function seatLedgers() {}

    function dressLedgers(root) {
        $all('table.k-ledger--stack', root).forEach(function (table) {
            partLedger(table);
            watchLedger(table);
        });
        seatLedgers();
    }

    /* ---- go-to box ---- */

    var gotoBox = $('.k-goto');
    var gotoInput = doc.getElementById('kGoto');
    var gotoList = doc.getElementById('kGotoList');
    var places = [];
    var shown = [];
    var cursor = 0;

    function collectPlaces() {
        var seen = {};
        places = [];
        $all('a[data-goto]').forEach(function (link) {
            var href = link.getAttribute('href');
            var label = link.dataset.goto || link.textContent.trim();
            if (!href || !label || seen[href]) { return; }
            seen[href] = true;
            places.push({
                label: label,
                href: href,
                group: link.dataset.gotoGroup || (link.dataset.kDigit && !isPhone() ? 'Alt ' + link.dataset.kDigit : ''),
                hay: (label + ' ' + (link.dataset.gotoGroup || '') + ' ' + (link.dataset.gotoKeys || '')).toLowerCase()
            });
        });
    }

    function score(place, query) {
        if (!query) { return 1; }
        var label = place.label.toLowerCase();
        if (label.indexOf(query) === 0) { return 4; }
        if (label.indexOf(' ' + query) !== -1) { return 3; }
        if (place.hay.indexOf(query) !== -1) { return 2; }
        var at = 0;
        for (var i = 0; i < query.length; i += 1) {
            at = label.indexOf(query[i], at);
            if (at === -1) { return 0; }
            at += 1;
        }
        return 1;
    }

    function renderPlaces() {
        var query = gotoInput.value.trim().toLowerCase();
        shown = places
            .map(function (place, index) { return { place: place, rank: score(place, query), index: index }; })
            .filter(function (entry) { return entry.rank > 0; })
            .sort(function (a, b) { return b.rank - a.rank || a.index - b.index; })
            .map(function (entry) { return entry.place; });
        if (cursor >= shown.length) { cursor = 0; }

        gotoList.textContent = '';
        if (!shown.length) {
            var none = doc.createElement('li');
            none.className = 'k-goto-none';
            none.textContent = 'Nothing by that name. Try a module, such as Products or Reports.';
            gotoList.appendChild(none);
        }
        shown.forEach(function (place, index) {
            var item = doc.createElement('li');
            item.className = 'k-goto-item';
            item.id = 'kGotoItem' + index;
            item.setAttribute('role', 'option');
            item.setAttribute('aria-selected', index === cursor ? 'true' : 'false');
            item.dataset.index = String(index);
            var name = doc.createElement('span');
            name.textContent = place.label;
            item.appendChild(name);
            if (place.group) {
                var group = doc.createElement('small');
                group.textContent = place.group;
                item.appendChild(group);
            }
            gotoList.appendChild(item);
        });
        gotoInput.setAttribute('aria-activedescendant', shown.length ? 'kGotoItem' + cursor : '');
        var current = doc.getElementById('kGotoItem' + cursor);
        if (current && current.scrollIntoView) { current.scrollIntoView({ block: 'nearest' }); }
    }

    function openGoto() {
        if (!gotoInput) { return; }
        if (!places.length) { collectPlaces(); }
        gotoBox.classList.add('is-open');
        gotoList.hidden = false;
        gotoInput.setAttribute('aria-expanded', 'true');
        cursor = 0;
        renderPlaces();
        gotoInput.focus();
        gotoInput.select();
    }

    function closeGoto(restoreFocus) {
        if (!gotoInput || gotoList.hidden) { return false; }
        gotoBox.classList.remove('is-open');
        gotoList.hidden = true;
        gotoInput.value = '';
        gotoInput.setAttribute('aria-expanded', 'false');
        if (restoreFocus) { gotoInput.blur(); }
        return true;
    }

    function visit(index) {
        var place = shown[index];
        if (place) { window.location.assign(place.href); }
    }

    if (gotoInput) {
        gotoInput.addEventListener('focus', function () { if (gotoList.hidden) { openGoto(); } });
        gotoInput.addEventListener('input', function () { cursor = 0; renderPlaces(); });
        gotoInput.addEventListener('keydown', function (event) {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                if (!shown.length) { return; }
                cursor = (cursor + (event.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length;
                renderPlaces();
            } else if (event.key === 'Enter') {
                event.preventDefault();
                visit(cursor);
            } else if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                closeGoto(true);
            }
        });
        gotoList.addEventListener('mousedown', function (event) {
            var item = event.target.closest('.k-goto-item');
            if (!item) { return; }
            event.preventDefault();
            visit(Number(item.dataset.index));
        });
        doc.addEventListener('mousedown', function (event) {
            if (!gotoList.hidden && !gotoBox.contains(event.target) && !event.target.closest('[data-k-action="goto"]')) {
                closeGoto(false);
            }
        });
    }

    /* ---- help in a tooltip ----
       Pattern (a field label, a panel title, the page title):
         <span class="k-tip"><button type="button" class="k-tip-btn" aria-label="About Barcode"
           aria-describedby="tipBarcode" aria-expanded="false"><i class="bi bi-info-circle" aria-hidden="true"></i></button>
           <span class="k-tip-body" id="tipBarcode" role="tooltip" hidden>Help text</span></span>
       A short hint on any control: data-tip="Hold this sale (F8)".
       Shows on hover and keyboard focus, pins on click or tap, Esc or a click elsewhere closes it; one at a time. */

    var tipPop = null;
    var tipOwner = null;
    var tipPinned = false;
    var tipTimer = 0;
    var tipSeq = 0;
    var TIP_OWNER = '.k-tip-btn, [data-tip]';

    function tipLayer() {
        if (tipPop) { return tipPop; }
        tipPop = doc.createElement('div');
        tipPop.className = 'k-tip-pop';
        tipPop.id = 'kTipPop';
        tipPop.hidden = true;
        body.appendChild(tipPop);
        tipPop.addEventListener('pointerenter', function () { window.clearTimeout(tipTimer); });
        tipPop.addEventListener('pointerleave', function () { if (!tipPinned) { hideTipSoon(); } });
        return tipPop;
    }

    function tipBody(owner) {
        if (!owner.classList.contains('k-tip-btn')) { return null; }
        var id = owner.getAttribute('aria-describedby');
        return (id && doc.getElementById(id)) || (owner.parentNode && $('.k-tip-body', owner.parentNode));
    }

    function placeTip() {
        if (!tipOwner || !tipPop || tipPop.hidden) { return; }
        var box = tipOwner.getBoundingClientRect();
        var vw = window.innerWidth;
        var vh = window.innerHeight;
        if (!box.width && !box.height) { hideTip(); return; }
        if (box.bottom < 0 || box.top > vh) { hideTip(); return; }
        var width = tipPop.offsetWidth;
        var height = tipPop.offsetHeight;
        var top = box.top - height - 8;
        if (top < 8) { top = Math.min(box.bottom + 8, vh - height - 8); }
        var left = box.left + box.width / 2 - width / 2;
        left = Math.max(8, Math.min(left, vw - width - 8));
        tipPop.style.top = Math.max(8, top) + 'px';
        tipPop.style.left = left + 'px';
    }

    function showTip(owner, pinned) {
        window.clearTimeout(tipTimer);
        var source = tipBody(owner);
        var text = owner.getAttribute('data-tip');
        if (!source && !text) { return; }
        if (source && !(source.textContent || '').trim()) { return; }
        var pop = tipLayer();
        if (tipOwner && tipOwner !== owner && tipOwner.classList.contains('k-tip-btn')) { tipOwner.setAttribute('aria-expanded', 'false'); }
        if (source) {
            pop.innerHTML = source.innerHTML;
            $all('[id]', pop).forEach(function (el) { el.removeAttribute('id'); });
            pop.setAttribute('aria-hidden', 'true');
            owner.setAttribute('aria-expanded', 'true');
        } else {
            pop.textContent = text;
            pop.setAttribute('role', 'tooltip');
            pop.removeAttribute('aria-hidden');
        }
        tipOwner = owner;
        tipPinned = !!pinned;
        pop.classList.remove('is-open');
        pop.hidden = false;
        placeTip();
        void pop.offsetWidth;
        pop.classList.add('is-open');
    }

    function hideTip() {
        window.clearTimeout(tipTimer);
        if (!tipPop || tipPop.hidden) { tipOwner = null; tipPinned = false; return false; }
        tipPop.classList.remove('is-open');
        tipPop.hidden = true;
        if (tipOwner && tipOwner.classList.contains('k-tip-btn')) { tipOwner.setAttribute('aria-expanded', 'false'); }
        tipOwner = null;
        tipPinned = false;
        return true;
    }

    function hideTipSoon() {
        window.clearTimeout(tipTimer);
        tipTimer = window.setTimeout(hideTip, 120);
    }

    function tipOwnerOf(target) {
        return target && target.closest ? target.closest(TIP_OWNER) : null;
    }

    doc.addEventListener('pointerover', function (event) {
        if (event.pointerType !== 'mouse') { return; }
        var owner = tipOwnerOf(event.target);
        if (!owner || owner === tipOwner) { if (owner) { window.clearTimeout(tipTimer); } return; }
        if (tipPinned) { return; }
        window.clearTimeout(tipTimer);
        var wait = owner.classList.contains('k-tip-btn') ? 60 : 450;
        tipTimer = window.setTimeout(function () { showTip(owner, false); }, wait);
    });

    doc.addEventListener('pointerout', function (event) {
        if (event.pointerType !== 'mouse') { return; }
        var owner = tipOwnerOf(event.target);
        if (!owner) { return; }
        var to = event.relatedTarget;
        if (to && (owner.contains(to) || (tipPop && tipPop.contains(to)))) { return; }
        if (owner !== tipOwner) { window.clearTimeout(tipTimer); return; }
        if (!tipPinned) { hideTipSoon(); }
    });

    doc.addEventListener('focusin', function (event) {
        var owner = tipOwnerOf(event.target);
        if (!owner || owner !== event.target) { return; }
        var keyboard = true;
        try { keyboard = owner.matches(':focus-visible'); } catch (error) { /* older engines */ }
        if (keyboard) { showTip(owner, false); }
    });

    doc.addEventListener('focusout', function (event) {
        if (event.target === tipOwner && !tipPinned) { hideTip(); }
    });

    doc.addEventListener('click', function (event) {
        var btn = event.target.closest && event.target.closest('.k-tip-btn');
        if (btn) {
            event.preventDefault();
            event.stopPropagation();
            if (tipOwner === btn && tipPinned) { hideTip(); } else { showTip(btn, true); }
            return;
        }
        if (tipOwner && !(tipPop && tipPop.contains(event.target))) {
            if (event.target.closest && event.target.closest('[data-tip]') === tipOwner && !tipPinned) { hideTip(); return; }
            hideTip();
        }
    }, true);

    window.addEventListener('scroll', function () {
        if (tipOwner) { window.requestAnimationFrame(placeTip); }
    }, true);

    function makeTip(label, content, name) {
        tipSeq += 1;
        var id = (content.id ? content.id + '_tip' : 'kTip' + tipSeq);
        var wrap = doc.createElement('span');
        wrap.className = 'k-tip';
        var btn = doc.createElement('button');
        btn.type = 'button';
        btn.className = 'k-tip-btn';
        btn.setAttribute('aria-label', name ? 'About ' + name : 'More about this');
        btn.setAttribute('aria-describedby', id);
        btn.setAttribute('aria-expanded', 'false');
        var icon = doc.createElement('i');
        icon.className = 'bi bi-info-circle';
        icon.setAttribute('aria-hidden', 'true');
        btn.appendChild(icon);
        var holder = doc.createElement('span');
        holder.className = 'k-tip-body';
        holder.id = id;
        holder.setAttribute('role', 'tooltip');
        holder.hidden = true;
        holder.appendChild(content);
        wrap.appendChild(btn);
        wrap.appendChild(holder);
        label.appendChild(wrap);
        return wrap;
    }

    /* help written under a field in older markup moves behind the info icon beside that field's label.
       Only a plain note in a field wrapper with one label moves; mark a note data-k-keep to leave it on the page. */
    function tipifyHelp(root) {
        $all('div.form-text, span.helptext, div.helptext, small.form-text', root).forEach(function (help) {
            if (help.closest('.k-tip-body, .k-tip-pop, [data-k-keep], .k-till') || help.hasAttribute('data-k-keep')) { return; }
            if (help.hasAttribute('aria-live') || help.classList.contains('d-none') || help.hidden) { return; }
            if (/(^|\s)(k-due|k-out|k-in|text-danger|text-warning|k-error)(\s|$)/.test(help.className)) { return; }
            if (Array.prototype.some.call(help.attributes, function (attr) { return attr.name.indexOf('data-') === 0; })) { return; }
            if (!(help.textContent || '').trim()) { return; }
            var holder = help.parentElement;
            if (!holder) { return; }
            var control = holder.querySelector('input:not([type="hidden"]), select, textarea');
            var label = null;
            if (control && control.id) {
                var named = $all('label[for="' + control.id + '"]');
                if (named.length === 1) { label = named[0]; }
            }
            if (!label) {
                var labels = Array.prototype.filter.call(holder.children, function (child) {
                    return child.tagName === 'LABEL' || child.classList.contains('form-label') || child.classList.contains('k-label');
                });
                if (labels.length !== 1) {
                    var check = holder.querySelector(':scope > .form-check > .form-check-label, :scope > .form-switch > .form-check-label');
                    if (!check || labels.length) { return; }
                    labels = [check];
                }
                label = labels[0];
            }
            if ($('.k-tip', label)) { return; }
            var name = (label.textContent || '').replace(/\s+/g, ' ').replace(/\*$/, '').trim();
            if (!help.id && control) {
                var described = (control.getAttribute('aria-describedby') || '').split(/\s+/).filter(function (ref) {
                    return /_helptext$/.test(ref) && !doc.getElementById(ref);
                })[0];
                if (described) { help.id = described; }
            }
            makeTip(label, help, name);
            help.classList.add('k-tip-moved');
        });
    }

    /* a control that answers a key says so in its tooltip; the key chip is printed on the till only */
    function tipKeys() {
        if (body.classList.contains('k-till')) { return; }
        $all('[data-k-key]').forEach(function (el) {
            if (el.hasAttribute('data-tip') || /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) { return; }
            var label = el.dataset.kKeyLabel || el.getAttribute('aria-label') || '';
            var key = el.dataset.kKey;
            el.setAttribute('data-tip', (label ? label + ' ' : 'Shortcut ') + '(' + key + ')');
        });
    }

    window.ZehnTip = { show: showTip, hide: hideTip, attach: makeTip, scan: tipifyHelp };

    /* ---- shortcut list ---- */

    var keysPanel = doc.getElementById('kKeys');
    var pageKeys = {};

    function comboOf(event) {
        var key = event.key;
        if (!key) { return ''; }
        if (key.length === 1) { key = key.toUpperCase(); }
        return (event.ctrlKey || event.metaKey ? 'Ctrl+' : '') + (event.altKey ? 'Alt+' : '') + key;
    }

    function normalise(combo) {
        return String(combo || '').split('+').map(function (part) {
            part = part.trim();
            if (/^ctrl$/i.test(part)) { return 'Ctrl'; }
            if (/^alt$/i.test(part)) { return 'Alt'; }
            if (/^f\d{1,2}$/i.test(part)) { return part.toUpperCase(); }
            return part.length === 1 ? part.toUpperCase() : part;
        }).join('+');
    }

    function registerKey(combo, handler, label) {
        combo = normalise(combo);
        if (!combo || RESERVED[combo]) { return false; }
        pageKeys[combo] = { run: handler, label: label || '' };
        return true;
    }

    /* a key the page answers itself: listed in the shortcut sheet, never dispatched from here */
    function describeKey(combo, label) {
        combo = String(combo || '').trim();
        if (!combo || !label) { return false; }
        pageKeys[combo] = { run: null, label: label };
        return true;
    }

    function collectPageKeys() {
        $all('[data-k-key]').forEach(function (el) {
            var label = el.dataset.kKeyLabel || el.getAttribute('aria-label') || el.textContent.replace(/\s+/g, ' ').trim();
            registerKey(el.dataset.kKey, function () {
                if (el.disabled || el.getAttribute('aria-disabled') === 'true') { return; }
                if (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) && el.type !== 'checkbox' && el.type !== 'radio' && el.type !== 'submit' && el.type !== 'button') {
                    el.focus();
                    if (el.select) { el.select(); }
                } else {
                    el.click();
                }
            }, label);
        });
    }

    function renderPageKeys() {
        var list = doc.getElementById('kKeysPage');
        var head = doc.getElementById('kKeysPageHead');
        if (!list) { return; }
        list.textContent = '';
        var combos = Object.keys(pageKeys).filter(function (combo) { return pageKeys[combo].label; });
        combos = combos.filter(function (combo) { return pageKeys[combo].run; })
            .concat(combos.filter(function (combo) { return !pageKeys[combo].run; }));
        if (head) { head.hidden = !combos.length; }
        combos.forEach(function (combo) {
            var row = doc.createElement('div');
            row.className = 'k-keys-row';
            var dt = doc.createElement('dt');
            dt.textContent = pageKeys[combo].label;
            var dd = doc.createElement('dd');
            combo.split('+').forEach(function (part) {
                var kbd = doc.createElement('kbd');
                kbd.className = 'k-key';
                kbd.textContent = part;
                dd.appendChild(kbd);
            });
            row.appendChild(dt);
            row.appendChild(dd);
            list.appendChild(row);
        });
    }

    function renderModuleKeys() {
        var list = doc.getElementById('kKeysModules');
        if (!list || list.dataset.kDone) { return; }
        list.dataset.kDone = '1';
        moduleLinks.forEach(function (link) {
            if (!link.dataset.kDigit) { return; }
            var row = doc.createElement('div');
            row.className = 'k-keys-row';
            var dt = doc.createElement('dt');
            dt.textContent = link.dataset.goto || (link.textContent || '').trim();
            var dd = doc.createElement('dd');
            ['Alt', link.dataset.kDigit].forEach(function (part) {
                var kbd = doc.createElement('kbd');
                kbd.className = 'k-key';
                kbd.textContent = part;
                dd.appendChild(kbd);
            });
            row.appendChild(dt);
            row.appendChild(dd);
            list.appendChild(row);
        });
        var head = doc.getElementById('kKeysModulesHead');
        if (head) { head.hidden = !list.children.length; }
    }

    function setKeys(open) {
        if (!keysPanel) { return; }
        if (open) { collectPageKeys(); renderPageKeys(); renderModuleKeys(); }
        keysPanel.hidden = !open;
        $all('[data-k-action="keys"][aria-expanded]').forEach(function (btn) { btn.setAttribute('aria-expanded', open ? 'true' : 'false'); });
        if (open) {
            var close = $('.btn-close', keysPanel);
            if (close) { close.focus(); }
        }
    }

    function closeAll() {
        var closed = closeGoto(true);
        if (closeMore()) { closed = true; }
        if (rail && rail.classList.contains('show')) { setDrawer(false); closed = true; }
        if (keysPanel && !keysPanel.hidden) { setKeys(false); closed = true; }
        return closed;
    }

    /* ---- one click handler for shell actions ---- */

    doc.addEventListener('click', function (event) {
        var trigger = event.target.closest('[data-k-action]');
        if (!trigger) { return; }
        var action = trigger.dataset.kAction;
        if (action === 'drawer') { toggleSidebarMobile(); }
        else if (action === 'drawer-close') { setDrawer(false); }
        else if (action === 'rail-toggle') { toggleSidebar(); }
        else if (action === 'goto') { if (gotoList && !gotoList.hidden) { closeGoto(true); } else { openGoto(); } }
        else if (action === 'keys') { setKeys(keysPanel ? keysPanel.hidden : false); }
    });

    doc.addEventListener('submit', function (event) {
        var form = event.target;
        if (form && form.dataset && form.dataset.kConfirm && !window.confirm(form.dataset.kConfirm)) {
            event.preventDefault();
        }
    });

    /* ---- keyboard layer ---- */

    doc.addEventListener('keydown', function (event) {
        if (event.defaultPrevented || event.isComposing) { return; }
        var combo = comboOf(event);
        var typing = isTyping(event.target);

        if (combo === 'Ctrl+K') {
            event.preventDefault();
            openGoto();
            return;
        }
        if (combo === 'Ctrl+B' && !typing) {
            event.preventDefault();
            toggleSidebar();
            return;
        }
        if (combo === 'F1') {
            event.preventDefault();
            setKeys(keysPanel ? keysPanel.hidden : false);
            return;
        }
        if (event.key === 'Escape') {
            if (hideTip()) {
                event.preventDefault();
                event.stopPropagation();
                return;
            }
            if (doc.querySelector('.modal.show, .dropdown-menu.show, .offcanvas.show')) { return; }
            if (closeAll()) { event.preventDefault(); }
            return;
        }
        if (event.altKey && !event.ctrlKey && !event.metaKey && /^Digit\d$/.test(event.code || '')) {
            var digit = event.code.slice(5);
            var target = moduleLinks.filter(function (link) { return link.dataset.kDigit === digit; })[0];
            if (target) {
                event.preventDefault();
                window.location.assign(target.getAttribute('href'));
            }
            return;
        }
        if (event.key === '/' && !typing && !event.ctrlKey && !event.altKey && !event.metaKey) {
            var field = $('#content [autofocus], #content input:not([type="hidden"]):not([disabled]):not([readonly]), #content select:not([disabled]), #content textarea:not([disabled])');
            if (field) {
                event.preventDefault();
                field.focus();
                if (field.select) { field.select(); }
            }
            return;
        }

        var bound = pageKeys[combo];
        if (!bound && /^F\d+$/.test(combo)) {
            collectPageKeys();
            bound = pageKeys[combo];
        }
        if (bound && bound.run && (!typing || /^F\d+$/.test(combo) || event.altKey || event.ctrlKey)) {
            event.preventDefault();
            bound.run(event);
        }
    });

    /* ---- the page scrolls inside #content; keep window.scrollTo meaningful for older page scripts ---- */

    var scroller = doc.getElementById('content');
    if (scroller && scroller.scrollTo) {
        var nativeScrollTo = window.scrollTo.bind(window);
        window.scrollTo = function () {
            nativeScrollTo.apply(null, arguments);
            scroller.scrollTo.apply(scroller, arguments);
        };
    }

    /* ---- flash messages ---- */

    function armFlash(alertEl) {
        if (!alertEl || alertEl.dataset.kArmed) { return; }
        alertEl.dataset.kArmed = '1';
        var sticky = /alert-(danger|error|warning)/.test(alertEl.className);
        if (sticky) { return; }
        var timer = window.setTimeout(dismiss, 7000);
        alertEl.addEventListener('mouseenter', function () { window.clearTimeout(timer); });
        alertEl.addEventListener('focusin', function () { window.clearTimeout(timer); });
        function dismiss() {
            if (!alertEl.isConnected) { return; }
            if (window.bootstrap && window.bootstrap.Alert) {
                window.bootstrap.Alert.getOrCreateInstance(alertEl).close();
            } else {
                alertEl.remove();
            }
        }
    }

    function flash(message, kind) {
        var host = doc.getElementById('kFlash');
        if (!host) {
            host = doc.createElement('div');
            host.className = 'k-flash';
            host.id = 'kFlash';
            host.setAttribute('role', 'status');
            host.setAttribute('aria-live', 'polite');
            body.appendChild(host);
        }
        watchFlash(host);
        var alertEl = doc.createElement('div');
        alertEl.className = 'alert alert-' + (kind || 'info') + ' alert-dismissible fade show';
        alertEl.setAttribute('role', 'alert');
        alertEl.appendChild(doc.createTextNode(message));
        var close = doc.createElement('button');
        close.type = 'button';
        close.className = 'btn-close';
        close.setAttribute('data-bs-dismiss', 'alert');
        close.setAttribute('aria-label', 'Dismiss');
        alertEl.appendChild(close);
        host.insertBefore(alertEl, $('.alert', host) || host.firstChild);
        armFlash(alertEl);
        foldFlash(host);
        return alertEl;
    }

    /* ---- date fields: an empty one is marked so its dd-mm-yyyy reads as placeholder text ---- */

    /* The browser's own empty-date text (e.g. "dd-----yyyy") cannot be restyled, so an empty, unfocused
       date field hides it and shows a placeholder laid over the field instead. */
    var DATE_PLACEHOLDER = 'DD/MM/YYYY';

    function placeDatePlaceholder(input, ph) {
        var style = window.getComputedStyle(input);
        var padLeft = parseFloat(style.paddingLeft || 0) + parseFloat(style.borderLeftWidth || 0);
        ph.style.left = (input.offsetLeft + padLeft) + 'px';
        ph.style.top = input.offsetTop + 'px';
        ph.style.height = input.offsetHeight + 'px';
        /* a narrow field (phone From / To) cannot fit the full format beside its calendar icon */
        var room = input.clientWidth - padLeft - parseFloat(style.paddingRight || 0) - 22;
        ph.textContent = input.getAttribute('data-placeholder') || DATE_PLACEHOLDER;
        if (ph.scrollWidth > room) { ph.textContent = 'Date'; }
    }

    function datePlaceholder(input) {
        if (input._kDatePh) { return input._kDatePh; }
        var parent = input.parentNode;
        if (!parent) { return null; }
        if (window.getComputedStyle(parent).position === 'static') { parent.style.position = 'relative'; }
        var ph = doc.createElement('span');
        ph.className = 'k-date-ph';
        ph.setAttribute('aria-hidden', 'true');
        ph.textContent = input.getAttribute('data-placeholder') || DATE_PLACEHOLDER;
        parent.insertBefore(ph, input.nextSibling);
        input._kDatePh = ph;
        input.classList.add('has-date-ph');
        if (window.ResizeObserver) {
            new window.ResizeObserver(function () { placeDatePlaceholder(input, ph); }).observe(input);
        }
        return ph;
    }

    function markDate(input) {
        if (!input || input.type !== 'date') { return; }
        var empty = !input.value;
        input.classList.toggle('is-empty', empty);
        var ph = datePlaceholder(input);
        if (!ph) { return; }
        var show = empty && doc.activeElement !== input && input.offsetParent !== null;
        ph.hidden = !show;
        if (show) { placeDatePlaceholder(input, ph); }
    }

    $all('input[type="date"]').forEach(markDate);
    ['input', 'change', 'focusin', 'focusout'].forEach(function (name) {
        doc.addEventListener(name, function (event) { markDate(event.target); }, true);
    });
    window.addEventListener('resize', function () { $all('input[type="date"]').forEach(markDate); });
    /* date fields inside tabs, drawers and side panels become visible after a click; place their placeholder then */
    doc.addEventListener('click', function () {
        window.setTimeout(function () { $all('input[type="date"]').forEach(markDate); }, 60);
    }, true);
    window.addEventListener('hashchange', function () { $all('input[type="date"]').forEach(markDate); });
    doc.addEventListener('reset', function (event) {
        window.setTimeout(function () { $all('input[type="date"]', event.target).forEach(markDate); }, 0);
    }, true);

    /* at most three toasts show, newest on top; the rest fold behind a "N older" line */
    var FLASH_SHOWN = 3;

    function foldFlash(host) {
        if (!host) { return; }
        var alerts = $all('.alert', host);
        var more = $('.k-flash-more', host);
        alerts.forEach(function (alertEl, index) { alertEl.classList.toggle('is-folded', index >= FLASH_SHOWN); });
        var folded = Math.max(0, alerts.length - FLASH_SHOWN);
        if (!folded || host.classList.contains('is-open')) {
            if (more) { more.remove(); }
            return;
        }
        if (!more) {
            more = doc.createElement('button');
            more.type = 'button';
            more.className = 'k-flash-more';
            more.addEventListener('click', function () {
                host.classList.add('is-open');
                foldFlash(host);
            });
        }
        more.textContent = folded + ' older message' + (folded === 1 ? '' : 's');
        host.appendChild(more);
    }

    function watchFlash(host) {
        if (host.dataset.kWatched) { return; }
        host.dataset.kWatched = '1';
        host.addEventListener('closed.bs.alert', function () {
            if (!$('.alert', host)) { host.classList.remove('is-open'); }
            foldFlash(host);
        });
    }

    (function dressFlash() {
        var host = doc.getElementById('kFlash');
        if (!host) { return; }
        /* the server lists messages oldest first: turn them so the newest is on top */
        $all('.alert', host).reverse().forEach(function (alertEl) { host.appendChild(alertEl); });
        $all('.alert', host).forEach(armFlash);
        foldFlash(host);
        watchFlash(host);
    })();

    window.ZehnShell = {
        flash: flash,
        registerKey: registerKey,
        describeKey: describeKey,
        openGoto: openGoto,
        closeAll: closeAll,
        fitActions: fitActions,
        dressLedgers: dressLedgers,
        seatLedgers: seatLedgers
    };

    /* ---- start ---- */

    function start() {
        var browserTimeZone = getBrowserTimeZone();
        updateBrowserTimeZoneLabels(browserTimeZone);
        syncBrowserTimeZone(browserTimeZone);
        collectPageKeys();
        tipifyHelp();
        tipKeys();
        fitActions();
        dressLedgers();
        if (doc.fonts && doc.fonts.ready) { doc.fonts.ready.then(function () { fitActions(); }); }
    }

    var relayout = 0;
    var lastWidth = window.innerWidth;
    window.addEventListener('resize', function () {
        if (window.innerWidth === lastWidth) { return; }
        lastWidth = window.innerWidth;
        window.cancelAnimationFrame(relayout);
        relayout = window.requestAnimationFrame(function () { fitActions(); placeTip(); });
    });

    if (doc.readyState === 'loading') {
        doc.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
