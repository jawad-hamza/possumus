/* Return flow: adding a supporting record never costs the user his place or his typing.
 *
 * 1. Add in place. A trigger [data-rf-quick="brand"][data-rf-target="<select id>"] opens the
 *    page's side panel [data-rf-panel] on the matching form [data-rf-quick-form="brand"]. The
 *    form posts to its JSON endpoint; the new record is put into the select, selected, and focus
 *    goes back to that select. The page's own form is never touched. Without the panel the
 *    trigger is a plain link to the full page (which carries ?next= back here).
 * 2. Lists remember. On a page with rows [data-rf-row="<id>"], the scroll position and the row
 *    the user opened are kept in sessionStorage for this exact address (page, filters and sort
 *    are in the address itself). Coming back restores the scroll and briefly highlights the row;
 *    "#touched-<id>" on the address (set by the server after a save) names the row to highlight.
 * 3. Unsaved typing survives. A form [data-rf-draft] keeps a sessionStorage draft while it is
 *    typed into, offers "Restore what you were typing" once when the page is opened again, and
 *    drops the draft when the form is submitted. [data-rf-bound] marks a form the server sent
 *    back with the posted values, where the draft is stale. [data-rf-grow="<button selector>"]
 *    adds formset rows before restoring (the purchase lines).
 */
(function () {
    'use strict';

    var doc = document;
    var LIST_PREFIX = 'rf:list:';
    var DRAFT_PREFIX = 'rf:draft:';
    var LIST_MAX_AGE = 4 * 60 * 60 * 1000;
    var DRAFT_MAX_AGE = 24 * 60 * 60 * 1000;

    function $all(selector, root) {
        return Array.prototype.slice.call((root || doc).querySelectorAll(selector));
    }

    var store = {
        get: function (key) {
            try {
                var raw = window.sessionStorage.getItem(key);
                return raw ? JSON.parse(raw) : null;
            } catch (error) {
                return null;
            }
        },
        set: function (key, value) {
            try { window.sessionStorage.setItem(key, JSON.stringify(value)); } catch (error) { /* storage full or blocked */ }
        },
        remove: function (key) {
            try { window.sessionStorage.removeItem(key); } catch (error) { /* blocked */ }
        }
    };

    var live = null;
    function announce(text) {
        if (!live) {
            live = doc.createElement('div');
            live.className = 'rf-live';
            live.setAttribute('role', 'status');
            live.setAttribute('aria-live', 'polite');
            doc.body.appendChild(live);
        }
        live.textContent = '';
        window.setTimeout(function () { live.textContent = text; }, 30);
    }

    /* The page body scrolls inside the shell; fall back to the window elsewhere. */
    function scroller() {
        var body = doc.getElementById('content');
        if (body) {
            var overflow = window.getComputedStyle(body).overflowY;
            if ((overflow === 'auto' || overflow === 'scroll') && body.scrollHeight > body.clientHeight) { return body; }
        }
        return doc.scrollingElement || doc.documentElement;
    }

    /* The address without the parameters that only say where to go back to. */
    function pageKey(prefix) {
        var params = new URLSearchParams(window.location.search);
        ['next', 'supplier_id', 'customer_id'].forEach(function (name) { params.delete(name); });
        var query = params.toString();
        return prefix + window.location.pathname + (query ? '?' + query : '');
    }

    /* ------------------------------------------------------------------ */
    /* 1. Add in place                                                     */
    /* ------------------------------------------------------------------ */

    var panel = doc.querySelector('[data-rf-panel]');
    var panelTitle = panel ? panel.querySelector('[data-rf-panel-title]') : null;
    var openTrigger = null;
    var openTarget = null;

    // The endpoint's reply names the record after its kind.
    var REPLY_KEY = { brand: 'brand', category: 'category', supplier: 'supplier', product: 'product', expense_type: 'expense_type', bank: 'bank', route: 'route', order_booker: 'order_booker', delivery_man: 'delivery_man', vehicle: 'vehicle', customer: 'customer' };

    function panelForm(kind) {
        return panel ? panel.querySelector('[data-rf-quick-form="' + kind + '"]') : null;
    }

    function openPanel(kind, trigger) {
        var form = panelForm(kind);
        if (!form) { return false; }
        if (openTrigger && openTrigger !== trigger) { openTrigger.setAttribute('aria-expanded', 'false'); }
        openTrigger = trigger;
        openTarget = trigger.getAttribute('data-rf-target') ? doc.getElementById(trigger.getAttribute('data-rf-target')) : null;
        $all('[data-rf-quick-form]', panel).forEach(function (other) { other.hidden = other !== form; });
        if (panelTitle) { panelTitle.textContent = form.getAttribute('data-rf-title') || 'Add'; }
        panel.hidden = false;
        doc.body.classList.add('rf-panel-open');
        trigger.setAttribute('aria-expanded', 'true');
        var first = form.querySelector('input:not([type="hidden"]):not([type="checkbox"]), select, textarea');
        if (first) { first.focus(); }
        return true;
    }

    function closePanel(giveFocusBack) {
        if (!panel || panel.hidden) { return; }
        panel.hidden = true;
        doc.body.classList.remove('rf-panel-open');
        if (openTrigger) { openTrigger.setAttribute('aria-expanded', 'false'); }
        if (giveFocusBack) {
            var back = openTarget || openTrigger;
            if (back) { back.focus(); }
        }
    }

    function showFeedback(form, problems) {
        var box = form.querySelector('[data-rf-feedback]');
        if (!box) { return; }
        box.textContent = '';
        if (!problems.length) { box.hidden = true; return; }
        var list = doc.createElement('ul');
        problems.forEach(function (problem) {
            var item = doc.createElement('li');
            item.textContent = problem;
            list.appendChild(item);
        });
        box.appendChild(list);
        box.hidden = false;
    }

    function flattenErrors(errors, form) {
        var problems = [];
        Object.keys(errors || {}).forEach(function (field) {
            var input = form.querySelector('[name$="-' + field + '"]');
            var label = input && input.id ? form.querySelector('label[for="' + input.id + '"]') : null;
            var name = label ? label.childNodes[0].textContent.trim() : '';
            (errors[field] || []).forEach(function (message) {
                problems.push(name && field !== '__all__' ? name + ': ' + message : message);
            });
        });
        return problems;
    }

    /* New options go in name order, after the blank "choose" option. */
    function insertOption(select, id, name) {
        var value = String(id);
        var existing = Array.prototype.find.call(select.options, function (option) { return option.value === value; });
        if (existing) { return existing; }
        var option = doc.createElement('option');
        option.value = value;
        option.textContent = name;
        var before = Array.prototype.find.call(select.options, function (candidate) {
            return candidate.value && candidate.textContent.localeCompare(name, undefined, { sensitivity: 'base' }) > 0;
        });
        select.insertBefore(option, before || null);
        return option;
    }

    function submitPanelForm(form) {
        var kind = form.getAttribute('data-rf-quick-form');
        var button = form.querySelector('button[type="submit"]');
        if (button) { button.disabled = true; }
        form.setAttribute('aria-busy', 'true');
        showFeedback(form, []);
        fetch(form.action, {
            method: 'POST',
            body: new FormData(form),
            credentials: 'same-origin',
            headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (payload) {
                if ((response.status === 401 || response.status === 402) && payload.redirect) {
                    window.location.assign(payload.redirect);
                    return;
                }
                var record = payload[REPLY_KEY[kind]];
                if (!response.ok || !record) {
                    var problems = flattenErrors(payload.errors, form);
                    if (!problems.length) { problems = [payload.message || 'This could not be saved. Check the entries and try again.']; }
                    showFeedback(form, problems);
                    var firstBad = payload.errors && Object.keys(payload.errors)[0];
                    var badInput = firstBad ? form.querySelector('[name$="-' + firstBad + '"]') : null;
                    if (badInput) { badInput.focus(); }
                    return;
                }
                var label = record.name + (kind === 'product' && record.sku ? ' (' + record.sku + ')' : '');
                // Other selects of the same kind on the page learn the new record too, unselected.
                var also = (openTrigger && openTrigger.getAttribute('data-rf-also')) || '';
                also.split(/\s+/).filter(Boolean).forEach(function (id) {
                    var other = doc.getElementById(id);
                    if (other && other !== openTarget) { insertOption(other, record.id, label); }
                });
                if (openTarget) {
                    insertOption(openTarget, record.id, label);
                    openTarget.value = String(record.id);
                    openTarget.dispatchEvent(new Event('input', { bubbles: true }));
                    openTarget.dispatchEvent(new Event('change', { bubbles: true }));
                }
                form.reset();
                closePanel(true);
                announce(label + ' added' + (openTarget ? ' and selected.' : '.'));
            });
        }).catch(function () {
            showFeedback(form, ['Could not reach the shop server. Check that it is running and try again.']);
        }).then(function () {
            if (button) { button.disabled = false; }
            form.removeAttribute('aria-busy');
        });
    }

    if (panel) {
        $all('[data-rf-quick]').forEach(function (trigger) {
            if (!panelForm(trigger.getAttribute('data-rf-quick'))) { return; }
            trigger.setAttribute('aria-controls', panel.id);
            trigger.setAttribute('aria-expanded', 'false');
            trigger.addEventListener('click', function (event) {
                if (event.ctrlKey || event.metaKey || event.shiftKey) { return; }
                if (!panel.hidden && openTrigger === trigger) {
                    event.preventDefault();
                    closePanel(true);
                    return;
                }
                if (openPanel(trigger.getAttribute('data-rf-quick'), trigger)) { event.preventDefault(); }
            });
        });
        $all('[data-rf-close]', panel).forEach(function (button) {
            button.addEventListener('click', function () { closePanel(true); });
        });
        panel.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                // Esc here closes the panel only; it must not also leave the page.
                event.preventDefault();
                event.stopPropagation();
                closePanel(true);
            }
        });
        $all('[data-rf-quick-form]', panel).forEach(function (form) {
            form.addEventListener('submit', function (event) {
                event.preventDefault();
                submitPanelForm(form);
            });
        });
    }

    /* ------------------------------------------------------------------ */
    /* 2. Lists remember where the user was                               */
    /* ------------------------------------------------------------------ */

    var rows = $all('[data-rf-row]');
    if (rows.length) {
        var listKey = pageKey(LIST_PREFIX);

        // opened: the user left this list through one of its links, so the next visit is a return.
        var saveList = function (rowId, opened) {
            var previous = store.get(listKey) || {};
            store.set(listKey, {
                scroll: scroller().scrollTop,
                row: rowId === undefined ? previous.row || '' : rowId,
                opened: opened === undefined ? !!previous.opened : opened,
                t: Date.now()
            });
        };

        doc.addEventListener('click', function (event) {
            var link = event.target.closest ? event.target.closest('a[href]') : null;
            if (!link || link.getAttribute('href').charAt(0) === '#') { return; }
            // Leaving through the module menu is not "opening something from this list".
            var viaMenu = !!link.closest('.k-rail, .k-tabbar, #sidebar');
            var row = link.closest('[data-rf-row]');
            saveList(row ? row.getAttribute('data-rf-row') : '', !viaMenu);
        }, true);
        window.addEventListener('pagehide', function () { saveList(); });

        var navigationType = function () {
            try {
                var entry = window.performance.getEntriesByType('navigation')[0];
                return entry ? entry.type : '';
            } catch (error) {
                return '';
            }
        };

        var restoreList = function () {
            var state = store.get(listKey);
            var match = /^#touched-(\d+)$/.exec(window.location.hash);
            var touched = match ? match[1] : '';
            if (match && window.history.replaceState) {
                window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search);
            }
            // A plain visit from the menu starts at the top; a return (save, cancel, back) lands where the user was.
            var returning = touched || navigationType() === 'back_forward' || navigationType() === 'reload' || (state && state.opened);
            var fresh = returning && state && Date.now() - (state.t || 0) < LIST_MAX_AGE;
            if (fresh && state.scroll) { scroller().scrollTop = state.scroll; }
            if (fresh) {
                if (!touched) { touched = state.row || ''; }
                state.opened = false;
                state.row = '';
                store.set(listKey, state);
            }
            if (!touched) { return; }
            var hits = $all('[data-rf-row="' + touched + '"]');
            if (!hits.length) { return; }
            var box = hits[0].getBoundingClientRect();
            var view = scroller() === doc.getElementById('content') ? scroller().getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
            if (box.top < view.top || box.bottom > view.bottom) { hits[0].scrollIntoView({ block: 'center' }); }
            hits.forEach(function (row) { row.classList.add('rf-touched'); });
            window.setTimeout(function () {
                hits.forEach(function (row) { row.classList.add('rf-touched-out'); });
                window.setTimeout(function () {
                    hits.forEach(function (row) { row.classList.remove('rf-touched', 'rf-touched-out'); });
                }, 400);
            }, 2400);
        };
        window.requestAnimationFrame(restoreList);
    }

    /* ------------------------------------------------------------------ */
    /* 3. Unsaved typing survives an accidental navigation                */
    /* ------------------------------------------------------------------ */

    function draftFields(form) {
        return Array.prototype.filter.call(form.elements, function (element) {
            if (!element.name || element.disabled) { return false; }
            if (element.name === 'csrfmiddlewaretoken' || element.name === 'next') { return false; }
            var type = (element.type || '').toLowerCase();
            if (type === 'hidden' || type === 'password' || type === 'file' || type === 'submit' || type === 'button' || type === 'reset') { return false; }
            return !element.closest('[data-rf-panel], template');
        });
    }

    function snapshot(form) {
        var values = {};
        draftFields(form).forEach(function (element) {
            var type = (element.type || '').toLowerCase();
            if (type === 'checkbox') {
                values[element.name] = element.checked ? (element.value || 'on') : '';
            } else if (type === 'radio') {
                if (element.checked) { values[element.name] = element.value; } else if (!(element.name in values)) { values[element.name] = ''; }
            } else if (element.multiple) {
                values[element.name] = Array.prototype.filter.call(element.options, function (option) { return option.selected; }).map(function (option) { return option.value; });
            } else {
                values[element.name] = element.value;
            }
        });
        return values;
    }

    function totalForms(form) {
        var total = form.querySelector('input[name$="-TOTAL_FORMS"]');
        return total ? Number(total.value) || 0 : 0;
    }

    function sameValues(a, b) {
        return JSON.stringify(a) === JSON.stringify(b);
    }

    function applyDraft(form, draft) {
        var grow = form.getAttribute('data-rf-grow');
        var growButton = grow ? doc.querySelector(grow) : null;
        var guard = 0;
        while (growButton && draft.rows && totalForms(form) < draft.rows && guard < 200) {
            growButton.click();
            guard += 1;
        }
        draftFields(form).forEach(function (element) {
            if (!(element.name in draft.values)) { return; }
            var saved = draft.values[element.name];
            var type = (element.type || '').toLowerCase();
            var changed = false;
            if (type === 'checkbox') {
                var want = saved !== '';
                changed = element.checked !== want;
                element.checked = want;
            } else if (type === 'radio') {
                var on = element.value === saved;
                changed = element.checked !== on;
                element.checked = on;
            } else if (element.multiple) {
                Array.prototype.forEach.call(element.options, function (option) {
                    var selected = (saved || []).indexOf(option.value) !== -1;
                    if (option.selected !== selected) { changed = true; }
                    option.selected = selected;
                });
            } else if (element.value !== saved) {
                element.value = saved;
                changed = element.value === saved;
            }
            if (!changed) { return; }
            // A type-ahead that stands in for this select shows the restored choice too.
            if (element._stInput && element.selectedOptions && element.selectedOptions[0]) {
                element._stInput.value = element.value ? element.selectedOptions[0].textContent.trim() : '';
            }
            element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
        });
    }

    $all('form[data-rf-draft]').forEach(function (form) {
        var key = pageKey(DRAFT_PREFIX) + '#' + (form.getAttribute('data-rf-draft') || form.id || 'form');
        var pending = store.get(key);
        var submitting = false;
        var timer = null;
        var baseline = null;

        if (form.hasAttribute('data-rf-bound')) {
            // The server sent the form back with what was posted: that is newer than any draft.
            store.remove(key);
            pending = null;
        }

        var save = function () {
            if (submitting) { return; }
            var values = snapshot(form);
            if (baseline && sameValues(values, baseline)) {
                store.remove(key);
                return;
            }
            store.set(key, { values: values, rows: totalForms(form), t: Date.now() });
        };
        var queueSave = function () {
            window.clearTimeout(timer);
            timer = window.setTimeout(save, 250);
        };

        // Other scripts fill some fields on load; the baseline is taken once they are done.
        window.requestAnimationFrame(function () {
            baseline = snapshot(form);
            if (!pending || !pending.values || Date.now() - (pending.t || 0) > DRAFT_MAX_AGE) {
                if (pending) { store.remove(key); }
                return;
            }
            if (sameValues(pending.values, baseline) && (pending.rows || 0) <= totalForms(form)) {
                store.remove(key);
                return;
            }
            var bar = doc.createElement('div');
            bar.className = 'rf-draft';
            bar.setAttribute('role', 'status');
            var text = doc.createElement('p');
            text.className = 'rf-draft-text';
            text.textContent = 'You left this form before saving. Your entries were kept.';
            var restore = doc.createElement('button');
            restore.type = 'button';
            restore.className = 'btn btn-primary btn-sm';
            restore.textContent = 'Restore what you were typing';
            var discard = doc.createElement('button');
            discard.type = 'button';
            discard.className = 'btn btn-outline-secondary btn-sm';
            discard.textContent = 'Discard';
            bar.appendChild(text);
            bar.appendChild(restore);
            bar.appendChild(discard);
            form.insertBefore(bar, form.firstElementChild ? form.firstElementChild.nextSibling : null);

            restore.addEventListener('click', function () {
                var draft = pending;
                pending = null;
                bar.remove();
                applyDraft(form, draft);
                save();
                announce('Your entries are back.');
                var first = draftFields(form)[0];
                if (first) { first.focus(); }
            });
            discard.addEventListener('click', function () {
                pending = null;
                bar.remove();
                save();
                var first = draftFields(form)[0];
                if (first) { first.focus(); }
            });
        });

        form.addEventListener('input', queueSave);
        form.addEventListener('change', queueSave);
        form.addEventListener('submit', function () {
            // What was posted is either saved or sent back filled in by the server.
            submitting = true;
            window.clearTimeout(timer);
            store.remove(key);
        });
        window.addEventListener('pagehide', function () {
            window.clearTimeout(timer);
            if (!submitting && baseline) { save(); }
        });
    });

    window.ZehnReturnFlow = { openPanel: openPanel, closePanel: closePanel };
})();
