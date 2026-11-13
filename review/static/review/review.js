/* ==========================================================================
   COMMENT TOOL (review notes)
   Lets reviewers pin a note on any part of any screen. It sits on top of the
   page: it reads where it is (the URL) and changes nothing in the page itself.
   Dots live in their own layer and are placed again whenever the page redraws
   (the till updates in place) or scrolls.
   Ported from the BBMS prototype's feedback.js.
   ========================================================================== */
(function () {
  'use strict';

  var configEl = document.getElementById('rv-config');
  if (!configEl || window.__zehnReviewTool) return;
  window.__zehnReviewTool = true;

  var CONFIG;
  try { CONFIG = JSON.parse(configEl.textContent) || {}; } catch (e) { return; }
  if (!CONFIG.listUrl) return;

  var POLL_MS = 30000;
  var INTRO_MS = 15000;
  var LOCAL_NOTE_TTL_MS = 120000;
  var NAME_KEY = 'zehnms-review-name-' + (CONFIG.userId || '');
  var INTRO_KEY = 'zehnms-review-intro-seen';

  function clean(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); }
  function clamp(v, min, max) { return Math.max(min, Math.min(v, max)); }

  function storedName() {
    try { return localStorage.getItem(NAME_KEY) || ''; } catch (e) { return ''; }
  }
  function rememberName(name) {
    try { localStorage.setItem(NAME_KEY, name); } catch (e) { /* the default name is used next time */ }
  }
  function make(tag, id, text) {
    var el = document.createElement(tag);
    if (id) el.id = id;
    if (text) el.textContent = text;
    return el;
  }
  /* The server hands over each note link with 0 as the id; put the real one in. */
  function noteUrl(template, id) { return template.replace('/notes/0/', '/notes/' + encodeURIComponent(id) + '/'); }
  function csrf() {
    if (typeof window.getCsrfToken === 'function') return window.getCsrfToken();
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }
  function flash(message, kind) {
    if (window.ZehnShell && typeof window.ZehnShell.flash === 'function') window.ZehnShell.flash(message, kind || 'success');
  }
  function api(url, options) {
    options = options || {};
    var headers = { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' };
    if (options.body) { headers['Content-Type'] = 'application/json'; headers['X-CSRFToken'] = csrf(); }
    return fetch(url, {
      method: options.method || 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error(data.error || data.message || 'Something went wrong. Please try again.');
          err.status = res.status;
          throw err;
        }
        return data;
      });
    });
  }

  /* ---------- where am I ------------------------------------------------- */
  /* A screen is the page path plus its query string (filters). */
  function screenNow() {
    return {
      key: location.pathname + location.search,
      path: location.pathname,
      query: location.search.replace(/^\?/, ''),
      label: clean(document.title)
    };
  }

  /* ---------- pinning a note to an element -------------------------------- */
  var content = null;   // the scrolling page area (#content); set at start

  function selectorFor(el) {
    var parts = [];
    for (var n = el; n && n.nodeType === 1 && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      if (n.id && !/^rv-/.test(n.id)) { parts.unshift('#' + CSS.escape(n.id)); break; }
      var tag = n.tagName.toLowerCase();
      var parent = n.parentElement;
      if (!parent) { parts.unshift(tag); break; }
      var same = Array.prototype.filter.call(parent.children, function (c) { return c.tagName === n.tagName; });
      parts.unshift(same.length > 1 ? tag + ':nth-of-type(' + (same.indexOf(n) + 1) + ')' : tag);
    }
    return parts.join('>');
  }

  function hintFor(el) {
    for (var n = el; n && n !== document.body && n.nodeType === 1; n = n.parentElement) {
      var own = (typeof n.value === 'string' && n.value && n.tagName !== 'BUTTON') ? n.value : n.textContent;
      var text = clean(own) || clean(n.getAttribute('placeholder')) || clean(n.getAttribute('aria-label')) || clean(n.getAttribute('title'));
      if (text) return text.slice(0, 200);
    }
    return '';
  }

  function targetFor(raw) {
    if (!raw || raw === document.documentElement || raw === document.body) return content || document.body;
    return (raw.closest && raw.closest('svg')) || raw;
  }

  function anchorFor(el, x, y) {
    var r = el.getBoundingClientRect();
    var inContent = !!(content && content.contains(el));
    var base = inContent ? content.getBoundingClientRect() : null;
    return {
      selector: selectorFor(el),
      tag: el.tagName.toLowerCase(),
      hint: hintFor(el),
      xFrac: r.width ? clamp((x - r.left) / r.width, 0, 1) : 0.5,
      yFrac: r.height ? clamp((y - r.top) / r.height, 0, 1) : 0.5,
      /* where the click was, in the page's own scroll space, for when the element is gone */
      pageX: inContent ? x - base.left + content.scrollLeft : x,
      pageY: inContent ? y - base.top + content.scrollTop : y,
      scope: inContent ? 'content' : 'viewport'
    };
  }

  /* Rows and cards move as the shop's data changes, so the element's own words
     are checked first and its position in the page second. */
  function locate(anchor) {
    var bySelector = null;
    try { bySelector = anchor.selector ? document.querySelector(anchor.selector) : null; } catch (e) { /* stale selector */ }
    if (bySelector && root.contains(bySelector)) bySelector = null;
    if (bySelector && (!anchor.hint || hintFor(bySelector) === anchor.hint)) return bySelector;
    if (anchor.hint && anchor.tag && /^[a-z][a-z0-9-]*$/.test(anchor.tag)) {
      var twins = Array.prototype.filter.call(document.querySelectorAll(anchor.tag), function (n) {
        return !root.contains(n) && hintFor(n) === anchor.hint;
      });
      if (twins.length === 1) return twins[0];
    }
    return bySelector;
  }

  /* A dot must not float over the head or the sidebar when its row has scrolled away. */
  function insideScrollers(el, x, y) {
    for (var n = el.parentElement; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      var s = window.getComputedStyle(n);
      if (/(auto|scroll|hidden|clip)/.test(s.overflow + s.overflowX + s.overflowY)) {
        var r = n.getBoundingClientRect();
        if (x < r.left - 1 || x > r.right + 1 || y < r.top - 1 || y > r.bottom + 1) return false;
      }
    }
    return true;
  }

  function positionFor(note) {
    var anchor = note.anchor || {};
    var el = locate(anchor);
    if (el) {
      var r = el.getBoundingClientRect();
      if (!r.width && !r.height) return null;
      var x = r.left + r.width * (anchor.xFrac == null ? 0.5 : anchor.xFrac);
      var y = r.top + r.height * (anchor.yFrac == null ? 0.5 : anchor.yFrac);
      if (!insideScrollers(el, x, y)) return null;
      return { x: x, y: y, el: el };
    }
    if (anchor.scope === 'content' && content) {
      var c = content.getBoundingClientRect();
      var cx = c.left + anchor.pageX - content.scrollLeft;
      var cy = c.top + anchor.pageY - content.scrollTop;
      if (cx < c.left || cx > c.right || cy < c.top || cy > c.bottom) return null;
      return { x: cx, y: cy, el: null };
    }
    return { x: anchor.pageX || 0, y: anchor.pageY || 0, el: null };
  }

  /* ---------- build the interface ------------------------------------------ */
  var root = make('div', 'rv-root');
  var layer = make('div', 'rv-layer');
  var tool = make('button', 'rv-tool');
  var toolIcon = make('i');
  toolIcon.className = 'bi bi-chat-square-text';
  toolIcon.setAttribute('aria-hidden', 'true');
  tool.append(toolIcon, document.createTextNode(' Comment'));
  var tip = make('div', 'rv-tip', 'Want something changed? Click here, then click the place on the screen.');
  var banner = make('div', 'rv-banner', 'Click the place you want to comment on. Esc to cancel.');
  var outline = make('div', 'rv-outline');
  var popover = make('div', 'rv-note');
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-label', 'Review note');
  var popMeta = make('div'); popMeta.className = 'rv-meta';
  var popBody = make('div'); popBody.className = 'rv-body';
  var popActions = make('div'); popActions.className = 'rv-pop-actions';
  var resolveBtn = make('button', null, 'Resolve'); resolveBtn.type = 'button'; resolveBtn.className = 'rv-link';
  var popError = make('span'); popError.className = 'rv-error';
  popActions.append(resolveBtn, popError);
  popover.append(popMeta, popBody, popActions);

  var composer = make('form', 'rv-composer');
  composer.setAttribute('aria-label', 'New review note');
  composer.noValidate = true;
  var nameLabel = make('label', null, 'Your name');
  var nameInput = make('input');
  nameInput.maxLength = 80; nameInput.autocomplete = 'name'; nameInput.name = 'rv-author';
  nameLabel.appendChild(nameInput);
  var textLabel = make('label', null, 'Your note');
  var textInput = make('textarea');
  textInput.maxLength = 2000; textInput.placeholder = 'What would you like changed here?'; textInput.name = 'rv-text';
  textLabel.appendChild(textInput);
  var hintLine = make('div', null, 'Ctrl+Enter saves. Esc cancels.'); hintLine.className = 'rv-hint';
  var errorLine = make('div'); errorLine.className = 'rv-error'; errorLine.setAttribute('role', 'alert');
  var actions = make('div'); actions.className = 'rv-actions';
  var cancelBtn = make('button', null, 'Cancel'); cancelBtn.type = 'button';
  var saveBtn = make('button', null, 'Save note'); saveBtn.type = 'submit'; saveBtn.className = 'rv-save';
  actions.append(cancelBtn, saveBtn);
  composer.append(nameLabel, textLabel, hintLine, errorLine, actions);

  tool.type = 'button';
  tool.setAttribute('aria-pressed', 'false');
  tool.title = 'Pin a review note on this screen';
  [banner, outline, popover, composer, tip].forEach(function (el) { el.hidden = true; });
  banner.setAttribute('role', 'status');
  root.append(layer, outline, banner, tip, tool, popover, composer);

  /* ---------- state -------------------------------------------------------- */
  var serverNotes = [];
  var serverKey = '';              // The screen the server's list belongs to.
  var requestedKey = '';           // The screen last asked for (a failed ask is not repeated on every redraw).
  var localNotes = new Map();      // Saved here but not in the last list from the server yet.
  var dots = new Map();
  var placing = false;
  var draft = null;
  var pinnedId = null;
  var shownId = null;
  var shownNote = null;
  var focusedNote = null;          // A note opened from the review page.
  var paintQueued = false;

  function composing() { return !composer.hidden; }

  function allNotes() {
    var key = screenNow().key;
    var known = new Set(serverNotes.map(function (n) { return n.id; }));
    var cutoff = Date.now() - LOCAL_NOTE_TTL_MS;
    localNotes.forEach(function (n, id) {
      if (known.has(id) || n.savedAt < cutoff) localNotes.delete(id);
    });
    var list = serverKey === key ? serverNotes.slice() : [];
    localNotes.forEach(function (n) { if (n.screenKey === key) list.push(n); });
    if (focusedNote && !list.some(function (n) { return n.id === focusedNote.id; })) list.push(focusedNote);
    return list;
  }

  /* ---------- dots --------------------------------------------------------- */
  function paint() {
    paintQueued = false;
    if (screenNow().key !== requestedKey) refresh();
    var keep = new Set();

    allNotes().forEach(function (note) {
      var at = positionFor(note);
      if (!at) return;
      keep.add(note.id);
      var dot = dots.get(note.id);
      if (!dot) {
        dot = make('button');
        dot.type = 'button';
        dot.className = 'rv-dot';
        dot.setAttribute('aria-label', 'Review note from ' + note.author);
        dot.addEventListener('mouseenter', function () { keepNote(); showNote(dot.rvNote, dot); });
        dot.addEventListener('focus', function () { keepNote(); showNote(dot.rvNote, dot); });
        dot.addEventListener('mouseleave', function () { if (pinnedId !== dot.rvNote.id) hideSoon(); });
        dot.addEventListener('blur', function () { if (pinnedId !== dot.rvNote.id && !popover.contains(document.activeElement)) hideNote(); });
        dot.addEventListener('click', function (e) {
          e.stopPropagation();
          pinnedId = pinnedId === dot.rvNote.id ? null : dot.rvNote.id;
          if (pinnedId) showNote(dot.rvNote, dot); else hideNote();
        });
        layer.appendChild(dot);
        dots.set(note.id, dot);
      }
      dot.rvNote = note;
      dot.classList.toggle('is-resolved', !!note.resolved);
      dot.classList.toggle('is-focus', !!(focusedNote && focusedNote.id === note.id));
      dot.style.left = at.x + 'px';
      dot.style.top = at.y + 'px';
    });

    dots.forEach(function (dot, id) {
      if (keep.has(id)) return;
      dot.remove();
      dots.delete(id);
      if (pinnedId === id) pinnedId = null;
      if (shownId === id) hideNote();
    });
    if (shownId != null && dots.get(shownId)) placePopover(dots.get(shownId));
  }

  function schedulePaint() {
    if (paintQueued) return;
    paintQueued = true;
    window.requestAnimationFrame(paint);
  }

  function when(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  }

  function fillPopover(note) {
    popMeta.textContent = note.author + ' · ' + when(note.createdAt) + (note.resolved ? ' · Resolved' : '');
    popBody.textContent = note.text;
    resolveBtn.hidden = !note.canResolve;
    resolveBtn.textContent = note.resolved ? 'Reopen' : 'Resolve';
    resolveBtn.disabled = false;
    popError.textContent = '';
    popover.style.transform = '';
    popover.hidden = false;
    shownId = note.id;
    shownNote = note;
  }
  function showNote(note, dot) { fillPopover(note); placePopover(dot); }
  function hideNote() { keepNote(); popover.hidden = true; shownId = null; shownNote = null; }

  /* A short grace period lets the pointer travel from the dot onto the note (to press Resolve). */
  var hideTimer = null;
  function keepNote() { if (hideTimer) { window.clearTimeout(hideTimer); hideTimer = null; } }
  function hideSoon() {
    keepNote();
    hideTimer = window.setTimeout(function () { hideTimer = null; if (pinnedId == null || pinnedId !== shownId) hideNote(); }, 250);
  }
  popover.addEventListener('mouseenter', keepNote);
  popover.addEventListener('mouseleave', function () { if (pinnedId !== shownId) hideSoon(); });

  function placePopover(dot) {
    var d = dot.getBoundingClientRect();
    var p = popover.getBoundingClientRect();
    var top = d.bottom + 8;
    if (top + p.height > window.innerHeight - 8) top = d.top - p.height - 8;
    var left = d.right + 8 + p.width > window.innerWidth ? d.left - p.width - 8 : d.right + 8;
    popover.style.left = clamp(left, 8, Math.max(8, window.innerWidth - p.width - 8)) + 'px';
    popover.style.top = clamp(top, 8, Math.max(8, window.innerHeight - p.height - 8)) + 'px';
  }

  resolveBtn.addEventListener('click', function (e) {
    e.stopPropagation();
    var note = shownNote;
    if (!note || resolveBtn.disabled) return;
    resolveBtn.disabled = true;
    popError.textContent = '';
    api(noteUrl(CONFIG.resolveUrl, note.id), { method: 'POST', body: { resolved: !note.resolved } })
      .then(function (data) {
        var updated = data.note;
        localNotes.delete(updated.id);
        serverNotes = serverNotes.filter(function (n) { return n.id !== updated.id; });
        if (!updated.resolved) serverNotes.push(updated);
        if (focusedNote && focusedNote.id === updated.id) focusedNote = updated;
        pinnedId = null;
        hideNote();
        schedulePaint();
        flash(updated.resolved ? 'Note resolved' : 'Note reopened');
      })
      .catch(function (err) {
        resolveBtn.disabled = false;
        popError.textContent = err.message === 'Failed to fetch' ? 'No connection. Please try again.' : err.message;
      });
  });

  /* ---------- placing a note ----------------------------------------------- */
  function endIntro() {
    tool.classList.remove('rv-intro');
    tip.hidden = true;
    try { sessionStorage.setItem(INTRO_KEY, '1'); } catch (e) { /* shown again next time */ }
  }

  function setPlacing(on) {
    placing = on;
    document.documentElement.classList.toggle('rv-placing', on);
    tool.setAttribute('aria-pressed', String(on));
    banner.hidden = !on;
    if (!on) outline.hidden = true;
  }

  function openComposer(x, y, target) {
    draft = { anchor: anchorFor(target, x, y), screen: screenNow() };
    setPlacing(false);
    hideNote();
    pinnedId = null;
    nameInput.value = storedName() || CONFIG.author || '';
    textInput.value = '';
    errorLine.textContent = '';
    composer.hidden = false;
    var c = composer.getBoundingClientRect();
    composer.style.left = clamp(x + 12, 8, Math.max(8, window.innerWidth - c.width - 8)) + 'px';
    composer.style.top = clamp(y + 12, 8, Math.max(8, window.innerHeight - c.height - 8)) + 'px';
    (nameInput.value ? textInput : nameInput).focus();
  }

  function closeComposer() {
    composer.hidden = true;
    draft = null;
  }

  function saveDraft() {
    if (!draft || saveBtn.disabled) return;
    var author = clean(nameInput.value);
    var text = textInput.value.trim();
    if (!author) { errorLine.textContent = 'Please enter your name.'; nameInput.focus(); return; }
    if (!text) { errorLine.textContent = 'Please type a note.'; textInput.focus(); return; }

    saveBtn.disabled = true;
    errorLine.textContent = '';
    var screen = draft.screen;
    api(CONFIG.createUrl, {
      method: 'POST',
      body: { author: author, text: text, path: screen.path, query: screen.query, screen: screen.label, anchor: draft.anchor }
    }).then(function (data) {
      if (author !== CONFIG.author) rememberName(author);
      else { try { localStorage.removeItem(NAME_KEY); } catch (e) { /* nothing stored */ } }
      var note = data.note;
      note.savedAt = Date.now();
      note.screenKey = screen.key;
      localNotes.set(note.id, note);
      closeComposer();
      schedulePaint();
      flash('Note saved');
    }).catch(function (err) {
      /* The text stays in the box so nothing typed is lost. */
      errorLine.textContent = err.message === 'Failed to fetch' ? 'No connection. Please try again.' : err.message;
    }).then(function () {
      saveBtn.disabled = false;
    });
  }

  /* ---------- events ------------------------------------------------------- */
  tool.addEventListener('click', function () {
    endIntro();
    closeComposer();
    hideNote();
    pinnedId = null;
    setPlacing(!placing);
    tool.blur();
  });
  cancelBtn.addEventListener('click', closeComposer);
  composer.addEventListener('submit', function (e) { e.preventDefault(); saveDraft(); });

  /* Clicks on the tool's own parts must not reach the page (the till would take the
     focus back to its scan line). */
  root.addEventListener('click', function (e) { e.stopPropagation(); });
  root.addEventListener('submit', function (e) { e.stopPropagation(); });

  /* While placing, clicks belong to the comment tool and must not press the
     page's buttons. Listening on window in the capture phase gets there first. */
  ['pointerdown', 'mousedown', 'mouseup', 'pointerup', 'dblclick', 'touchstart', 'auxclick', 'contextmenu'].forEach(function (type) {
    window.addEventListener(type, function (e) {
      if (!placing || root.contains(e.target)) return;
      if (type !== 'touchstart') e.preventDefault();
      e.stopImmediatePropagation();
    }, { capture: true, passive: false });
  });
  window.addEventListener('click', function (e) {
    if (placing && !root.contains(e.target)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      var raw = document.elementFromPoint(e.clientX, e.clientY) || e.target;
      if (root.contains(raw)) raw = e.target;
      openComposer(e.clientX, e.clientY, targetFor(raw));
      return;
    }
    if (pinnedId != null && !root.contains(e.target)) { pinnedId = null; hideNote(); }
  }, true);

  window.addEventListener('mousemove', function (e) {
    if (!placing) return;
    var raw = document.elementFromPoint(e.clientX, e.clientY);
    if (!raw || root.contains(raw)) { outline.hidden = true; return; }
    var r = targetFor(raw).getBoundingClientRect();
    outline.hidden = false;
    outline.style.left = r.left + 'px'; outline.style.top = r.top + 'px';
    outline.style.width = r.width + 'px'; outline.style.height = r.height + 'px';
  }, true);

  /* ZehnMS is keyboard driven (F-keys, Ctrl+S, Enter at the till, a stray key
     jumps to the scan line). Typing a note must never trigger it. */
  function guardKeys(e) {
    if (!composing() && !placing) return;
    e.stopImmediatePropagation();
    if (e.type !== 'keydown') return;
    var key = e.key || '';
    if (/^F\d{1,2}$/.test(key)) e.preventDefault();
    if ((e.ctrlKey || e.metaKey) && /^[sbkpdfgjhl]$/i.test(key)) e.preventDefault();
    if (e.altKey && /^[0-9]$/.test(key)) e.preventDefault();
    if (key === 'Escape') { e.preventDefault(); setPlacing(false); closeComposer(); return; }
    if (composing() && key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveDraft(); return; }
    /* Enter in the name field moves on instead of submitting half a note. */
    if (composing() && key === 'Enter' && e.target === nameInput) { e.preventDefault(); textInput.focus(); return; }
    /* Outside the composer (placing mode) nothing typed reaches the page. */
    if (!composer.contains(e.target) && key !== 'Tab') e.preventDefault();
  }
  window.addEventListener('keydown', guardKeys, true);
  window.addEventListener('keypress', function (e) { if (composing() || placing) e.stopImmediatePropagation(); }, true);
  window.addEventListener('keyup', function (e) { if (composing() || placing) e.stopImmediatePropagation(); }, true);

  /* Esc closes a pinned note too (without stealing Esc from the page otherwise). */
  window.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && pinnedId != null && !composing() && !placing) { pinnedId = null; hideNote(); }
  });

  window.addEventListener('resize', schedulePaint);
  window.addEventListener('scroll', schedulePaint, true);
  window.addEventListener('popstate', schedulePaint);
  window.addEventListener('beforeprint', function () { setPlacing(false); hideNote(); });

  /* ---------- keeping up with the server ----------------------------------- */
  var refreshing = false;
  function refresh() {
    if (refreshing) return;
    var screen = screenNow();
    refreshing = true;
    requestedKey = screen.key;
    var url = CONFIG.listUrl + '?path=' + encodeURIComponent(screen.path) + '&query=' + encodeURIComponent(screen.query);
    api(url).then(function (data) {
      serverNotes = Array.isArray(data.notes) ? data.notes : [];
      serverKey = screen.key;
      schedulePaint();
    }).catch(function () { /* the next refresh will try again */ })
      .then(function () { refreshing = false; });
  }
  window.setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });

  /* ---------- opening a note from the review page --------------------------- */
  function showLinkedFallback(note) {
    fillPopover(note);
    var where = clean(note.screen) || note.url || '';
    var hint = (note.anchor && note.anchor.hint) || '';
    popMeta.textContent += ' · Was on: ' + where + (hint ? ' (' + hint + ')' : '');
    popover.style.left = '50%';
    popover.style.top = '90px';
    popover.style.transform = 'translateX(-50%)';
  }

  function openLinkedNote() {
    var match = location.hash.match(/^#fbnote=(\d+)$/);
    if (!match) return;
    history.replaceState(null, '', location.pathname + location.search);
    api(noteUrl(CONFIG.noteUrl, match[1])).then(function (data) {
      var note = data.note;
      if (!note || !note.anchor) return;
      focusedNote = note;
      var el = locate(note.anchor);
      if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center', inline: 'nearest' });
      paint();
      var dot = dots.get(note.id);
      if (dot) {
        pinnedId = note.id;
        showNote(note, dot);
        dot.focus({ preventScroll: true });
        return;
      }
      /* The spot is not on this screen any more (or the record has changed): show the note anyway. */
      pinnedId = note.id;
      showLinkedFallback(note);
    }).catch(function (err) {
      flash(err.status === 404 ? 'That review note no longer exists.' : 'The review note could not be opened.', 'warning');
    });
  }

  /* ---------- start -------------------------------------------------------- */
  function start() {
    content = document.getElementById('content');
    document.body.appendChild(root);
    var seen = false;
    try { seen = sessionStorage.getItem(INTRO_KEY) === '1'; } catch (e) { /* show it */ }
    if (!seen) {
      tool.classList.add('rv-intro');
      tip.hidden = false;
      window.setTimeout(endIntro, INTRO_MS);
    }

    if (window.MutationObserver) {
      new MutationObserver(function (records) {
        for (var i = 0; i < records.length; i++) {
          if (!root.contains(records[i].target)) { schedulePaint(); return; }
        }
      }).observe(document.body, { childList: true, subtree: true, characterData: true });
    }

    refresh();
    openLinkedNote();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
