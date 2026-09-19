(function () {
    'use strict';

    function browserTimeZone() {
        try {
            return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
        } catch (error) {
            return '';
        }
    }

    function run() {
        var zone = browserTimeZone();

        document.querySelectorAll('[data-browser-time-zone-input]').forEach(function (input) {
            input.value = zone;
        });

        if (!zone) {
            return;
        }

        document.querySelectorAll('[data-google-sign-in-link]').forEach(function (link) {
            var target = new URL(link.href, window.location.origin);
            target.searchParams.set('browser_time_zone', zone);
            link.href = target.toString();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', run);
    } else {
        run();
    }

    /* help behind the info icon. These pages load without the app shell, so this is a small copy of its tooltip:
       hover or keyboard focus shows it, a click or tap pins it, Esc or a click elsewhere closes it, one at a time. */
    var pop = null;
    var owner = null;
    var pinned = false;
    var timer = 0;

    function layer() {
        if (!pop) {
            pop = document.createElement('div');
            pop.className = 'k-tip-pop';
            pop.hidden = true;
            pop.setAttribute('aria-hidden', 'true');
            document.body.appendChild(pop);
        }
        return pop;
    }

    function place() {
        if (!owner || !pop || pop.hidden) { return; }
        var box = owner.getBoundingClientRect();
        var width = pop.offsetWidth;
        var height = pop.offsetHeight;
        var top = box.top - height - 8;
        if (top < 8) { top = Math.min(box.bottom + 8, window.innerHeight - height - 8); }
        var left = Math.max(8, Math.min(box.left + box.width / 2 - width / 2, window.innerWidth - width - 8));
        pop.style.top = Math.max(8, top) + 'px';
        pop.style.left = left + 'px';
    }

    function show(button, pin) {
        window.clearTimeout(timer);
        var source = document.getElementById(button.getAttribute('aria-describedby') || '');
        if (!source || !(source.textContent || '').trim()) { return; }
        var layerEl = layer();
        if (owner && owner !== button) { owner.setAttribute('aria-expanded', 'false'); }
        layerEl.innerHTML = source.innerHTML;
        layerEl.querySelectorAll('[id]').forEach(function (el) { el.removeAttribute('id'); });
        owner = button;
        pinned = !!pin;
        button.setAttribute('aria-expanded', 'true');
        layerEl.classList.remove('is-open');
        layerEl.hidden = false;
        place();
        void layerEl.offsetWidth;
        layerEl.classList.add('is-open');
    }

    function hide() {
        window.clearTimeout(timer);
        if (owner) { owner.setAttribute('aria-expanded', 'false'); }
        if (pop) { pop.classList.remove('is-open'); pop.hidden = true; }
        owner = null;
        pinned = false;
    }

    function tipButton(target) {
        return target && target.closest ? target.closest('.k-tip-btn') : null;
    }

    document.addEventListener('pointerover', function (event) {
        if (event.pointerType !== 'mouse' || pinned) { return; }
        var button = tipButton(event.target);
        if (!button || button === owner) { return; }
        window.clearTimeout(timer);
        timer = window.setTimeout(function () { show(button, false); }, 60);
    });

    document.addEventListener('pointerout', function (event) {
        if (event.pointerType !== 'mouse') { return; }
        var button = tipButton(event.target);
        if (!button) { return; }
        window.clearTimeout(timer);
        if (button === owner && !pinned) { timer = window.setTimeout(hide, 120); }
    });

    document.addEventListener('focusin', function (event) {
        var button = tipButton(event.target);
        if (button && button === event.target) { show(button, false); }
    });

    document.addEventListener('focusout', function (event) {
        if (event.target === owner && !pinned) { hide(); }
    });

    document.addEventListener('click', function (event) {
        var button = tipButton(event.target);
        if (button) {
            event.preventDefault();
            if (button === owner && pinned) { hide(); } else { show(button, true); }
            return;
        }
        if (owner) { hide(); }
    }, true);

    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && owner) { hide(); }
    });

    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
})();
