(function () {
    'use strict';

    var tabStorageKey = 'settingsActiveTab';
    var ERROR_SELECTOR = '.k-error, .invalid-feedback, .text-danger';
    var tabButtons = Array.prototype.slice.call(document.querySelectorAll('[data-settings-tab]'));

    function readSavedTab() {
        try {
            return window.localStorage.getItem(tabStorageKey) || '';
        } catch (error) {
            return '';
        }
    }

    function saveTab(tabName) {
        try {
            window.localStorage.setItem(tabStorageKey, tabName);
        } catch (error) {
            /* private mode: the tab is simply not remembered */
        }
    }

    function findTabButtonByName(tabName) {
        return tabButtons.find(function (button) {
            return button.dataset.settingsTab === tabName;
        });
    }

    function showSettingsTab(button) {
        if (!button || typeof bootstrap === 'undefined') {
            return;
        }
        bootstrap.Tab.getOrCreateInstance(button).show();
    }

    function hashTarget() {
        var hash = window.location.hash;
        if (!hash || hash.length < 2) {
            return null;
        }
        try {
            return document.querySelector(hash);
        } catch (error) {
            return null;
        }
    }

    function tabNameFromHash() {
        var hash = window.location.hash;
        if (!hash) {
            return '';
        }
        if (hash === '#audit-trail') {
            return 'audit';
        }
        if (findTabButtonByName(hash.slice(1))) {
            return hash.slice(1);
        }
        var target = hashTarget();
        var pane = target ? target.closest('.tab-pane') : null;
        if (!pane) {
            return '';
        }
        var button = tabButtons.find(function (candidate) {
            return candidate.getAttribute('data-bs-target') === '#' + pane.id;
        });
        return button ? button.dataset.settingsTab : '';
    }

    function syncAddress(tabName) {
        var currentUrl = new URL(window.location.href);
        currentUrl.searchParams.delete('tab');
        currentUrl.hash = tabName === 'business' ? '' : tabName;
        window.history.replaceState({}, '', currentUrl);
    }

    function revealInIndex(button) {
        var strip = button.closest('.set-index');
        if (!strip || strip.scrollWidth <= strip.clientWidth) {
            return;
        }
        var item = button.parentElement || button;
        strip.scrollLeft = Math.max(0, item.offsetLeft - (strip.clientWidth - item.offsetWidth) / 2);
    }

    var settled = false;

    tabButtons.forEach(function (button) {
        button.addEventListener('shown.bs.tab', function (event) {
            var tabName = event.target.dataset.settingsTab || 'business';
            saveTab(tabName);
            revealInIndex(event.target);

            if (!settled) {
                /* first paint: keep an anchor such as #audit-trail and bring it into view */
                var target = hashTarget();
                if (target && !findTabButtonByName(window.location.hash.slice(1))) {
                    window.requestAnimationFrame(function () {
                        target.scrollIntoView({ block: 'start' });
                    });
                }
                return;
            }

            syncAddress(tabName);
            var content = document.getElementById('content');
            if (content) {
                content.scrollTop = 0;
            }
        });
    });

    /* a form sent back with a mistake opens on the section that holds it */
    function tabWithError() {
        var error = document.querySelector('.tab-pane ' + ERROR_SELECTOR);
        var pane = error ? error.closest('.tab-pane') : null;
        if (!pane) {
            return '';
        }
        var button = tabButtons.find(function (candidate) {
            return candidate.getAttribute('data-bs-target') === '#' + pane.id;
        });
        return button ? button.dataset.settingsTab : '';
    }

    function openInitialTab() {
        var requestedTab = new URLSearchParams(window.location.search).get('tab');
        var initialTab = tabNameFromHash() || tabWithError() || requestedTab || readSavedTab() || 'business';
        var button = findTabButtonByName(initialTab) || findTabButtonByName('business');
        if (button && button.classList.contains('active')) {
            saveTab(button.dataset.settingsTab);
            revealInIndex(button);
        } else {
            showSettingsTab(button);
        }
        settled = true;
    }

    window.addEventListener('hashchange', function () {
        var tabName = tabNameFromHash();
        if (tabName) {
            showSettingsTab(findTabButtonByName(tabName));
        }
    });

    function shouldResetSettingsScroll() {
        if (window.location.hash) {
            return false;
        }
        return !document.querySelector(ERROR_SELECTOR + ', .alert-warning, .alert-danger');
    }

    function resetSettingsScroll() {
        if (!shouldResetSettingsScroll()) {
            return;
        }
        var content = document.getElementById('content');
        if (content) {
            content.scrollTop = 0;
        }
    }

    if ('scrollRestoration' in window.history) {
        window.history.scrollRestoration = 'manual';
    }

    function markDirtyForms() {
        document.querySelectorAll('form[data-set-form]').forEach(function (form) {
            function markDirty() {
                form.classList.add('is-dirty');
            }
            form.addEventListener('input', markDirty);
            form.addEventListener('change', markDirty);
        });
    }

    function start() {
        openInitialTab();
        markDirtyForms();
        window.requestAnimationFrame(resetSettingsScroll);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }

    window.addEventListener('pageshow', function () {
        window.requestAnimationFrame(resetSettingsScroll);
    });
})();
