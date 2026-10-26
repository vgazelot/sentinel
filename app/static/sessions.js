// Sessions: every ticket by big rock to choose, live sessions to work in — on the board (static/board.js). Clicking a
// session (or ⏎ on it) is focus: the sidebar folds into a rail, the list keeps the live sessions or folds into a column
// of icons (⌥0); Esc or the grid button goes back. The layout is classes on .app and #list; the list, polled, is
// re-dressed after each swap.
(() => {
    const app = document.querySelector('.app');
    const list = document.getElementById('list');
    if (!app || !list) return;
    const load = (key, fallback) => {
        try { const v = localStorage.getItem(`sentinel.sessions.${key}`); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
    };
    const save = (key, value) => {
        try { localStorage.setItem(`sentinel.sessions.${key}`, JSON.stringify(value)); } catch (e) { /* private mode */ }
    };
    const folded = new Set(load('folded', []));
    const live = (key) => /^(console|session):/.test(key || '');
    let selectedKey = null;
    let wantFocus = null;  // the key a click (or a session just started) asks to work in
    let moving = false;
    let settled = null;

    function dress() {
        app.classList.toggle('s-dock', !!load('dock', false));
        list.classList.toggle('s-live', load('filter', 'all') === 'live');
        list.querySelectorAll('.proj').forEach((p) => {
            p.classList.toggle('folded', folded.has(p.id.slice(5)));
            p.querySelector('.proj-head').setAttribute('aria-expanded', !p.classList.contains('folded'));
        });
    }

    // `el` over `target`, in `box`'s scrolled coordinates; fresh (just drawn, or hidden until now) means no glide
    function glide(el, target, box, animate) {
        if (moving || !target || target.closest('.folded, details:not([open])') || !target.getClientRects().length) {
            el.style.opacity = 0;
            delete el.dataset.at;
            return false;
        }
        const r = target.getBoundingClientRect();
        const b = box.getBoundingClientRect();
        const still = !animate || !el.dataset.at;
        if (still) el.style.transition = 'none';
        el.style.transform = `translate(${r.left - b.left + box.scrollLeft}px, ${r.top - b.top + box.scrollTop}px)`;
        el.style.width = `${r.width}px`;
        el.style.height = `${r.height}px`;
        el.style.opacity = 1;
        if (still) { void el.offsetWidth; el.style.transition = ''; }
        el.dataset.at = '1';
        return true;
    }

    function mark(animate = true) {
        const rows = document.getElementById('rows');
        const hl = rows && rows.querySelector('.s-hl');
        const key = selectedKey && CSS.escape(selectedKey);
        if (hl) rows.classList.toggle('s-glide', glide(hl, key && rows.querySelector(`.it[data-key="${key}"]`), rows, animate));
    }

    // the columns glide for a third of a second: the mark waits for them to land
    function relayout() {
        moving = true;
        mark();
        clearTimeout(settled);
        settled = setTimeout(() => { moving = false; mark(false); }, 380);
    }

    function focus(on) {
        if (app.classList.contains('s-focus') === on) return;
        app.classList.toggle('s-focus', on);
        relayout();
    }

    function toggleDock() {
        if (!app.classList.contains('s-focus')) return;
        save('dock', !load('dock', false));
        dress();
        relayout();
    }

    // app.js selected a row: a ticket is the overview, the session a click asked for is focus
    document.addEventListener('row-selected', (e) => {
        selectedKey = e.detail.key;
        if (!live(selectedKey)) focus(false);
        else if (wantFocus === selectedKey) focus(true);
        if (wantFocus === selectedKey) wantFocus = null;
        mark();
    });
    document.addEventListener('select-key', (e) => { wantFocus = e.detail.key; });

    document.addEventListener('click', (e) => {
        const t = e.target;
        const row = t.closest('#rows .it');
        if (row && live(row.dataset.key)) { wantFocus = row.dataset.key; return; }
        const fold = t.closest('[data-fold]');
        if (fold) {
            const id = fold.dataset.fold;
            if (!folded.delete(id)) folded.add(id);
            save('folded', [...folded]);
            dress();
            relayout();  // the rows below move while it folds
            return;
        }
        const filter = t.closest('[data-filter]');
        if (filter) { save('filter', filter.dataset.filter); dress(); mark(false); return; }
        if (t.closest('[data-unselect]')) selectedKey = null;  // the pane is New's now: a × on the old one must not take it
        if (t.closest('[data-overview], [data-unselect]')) { focus(false); return; }
        if (t.closest('[data-dock]')) toggleDock();
    });

    // a session's × or its End on the board, before its row sees the click: a session of another terminal shown in the
    // pane ends there, like its End button; a console or another session without a word — the list, fetched again,
    // drops it from the list and the board, or, if it failed, gives it back
    document.addEventListener('click', (e) => {
        const x = e.target.closest('[data-close]');
        if (!x) return;
        e.stopPropagation();
        const key = x.dataset.close;
        if (x.hasAttribute('data-ask') && !confirm(`End this Claude session (${x.dataset.word})? Its transcript stays.`)) return;
        const closing = (on) => document.querySelectorAll(`:is(.it, .b-slot)[data-key="${CSS.escape(key)}"]`).forEach((el) => el.classList.toggle('closing', on));
        closing(true);
        const here = key === selectedKey && !key.startsWith('console:');
        // from the × itself: htmx.ajax wants a source or a target, and no other request queues this one
        htmx.ajax('POST', '/api/sessions/end', here ? { source: x, values: { key }, target: '#detail', swap: 'innerHTML' } : { source: x, values: { key, quiet: 1 }, swap: 'none' })
            .then(() => htmx.ajax('GET', '/partials/sessions', { source: x, target: list, swap: 'innerHTML' }))
            .finally(() => closing(false));
    }, true);

    // ⌥0 works in a terminal too: taken before xterm sees it (⌥1–4 are the board's)
    document.addEventListener('keydown', (e) => {
        if (e.target.closest('#console')) return;
        const typing = e.target.closest('input, textarea, select') && !e.target.closest('.console-term');
        if (e.altKey && !e.metaKey && !e.ctrlKey && !typing && e.code === 'Digit0') {
            e.preventDefault();
            e.stopPropagation();
            toggleDock();
            return;
        }
        if (typing || e.target.closest('.console-term') || e.metaKey || e.ctrlKey || e.altKey) return;
        if (e.key === 'Escape' && app.classList.contains('s-focus')) { focus(false); return; }
        // ⏎ on a session works in it (on a ticket, app.js opens the ticket)
        if (e.key === 'Enter' && !e.target.closest('button, a, summary') && live(selectedKey) && !app.classList.contains('s-focus')) {
            e.preventDefault();
            e.stopPropagation();
            focus(true);
        }
    }, true);

    // re-dressed before the next paint
    document.addEventListener('htmx:afterSwap', (e) => { if (e.detail.target === list) { dress(); mark(false); } });
    document.addEventListener('htmx:afterSettle', (e) => {
        if (e.detail.target !== list) return;
        // the session worked in has ended — not a bridge that failed once, which lists nothing for a poll
        const gone = !list.querySelector(`#rows .it[data-key="${CSS.escape(selectedKey || '')}"]`);
        if (app.classList.contains('s-focus') && gone && !list.querySelector('.s-error')) focus(false);
    });
    window.addEventListener('resize', () => mark(false));
    dress();
})();
