// Sessions' board: up to four consoles side by side — two columns, either one split in two. A live session dragged
// from the list lands where the box under the pointer says (dropped on a terminal, it splits it while there is room,
// else takes its place); a terminal's head dragged onto another swaps them, back onto the list takes it off, like its ×.
// A session selected in the list takes the active terminal's place; a live row dropped on another group of the list
// goes there (yours, made on the spot by New section, a big rock, the run, off cycle), whatever its ticket says. The board is this browser's (localStorage): the
// sessions run on the host whatever it shows. Each terminal is a `Term` (static/console.js) attached while the page
// lives, the board shown or not; the rows say where theirs is. `/sessions?open=<console>` (Watch's Claude button)
// puts that console beside the active terminal.
(() => {
    const work = document.querySelector('.s-work');
    const board = document.getElementById('board');
    const list = document.getElementById('list');
    if (!work || !board || !list) return;
    const STORE = 'sentinel.sessions.board';
    // a terminal's place on a 2×2 grid — [column, row, columns, rows]: the whole board, a column or a quarter
    const CELLS = { F: [0, 0, 2, 2], L: [0, 0, 1, 2], R: [1, 0, 1, 2], TL: [0, 0, 1, 1], TR: [1, 0, 1, 1], BL: [0, 1, 1, 1], BR: [1, 1, 1, 1] };
    const LAYOUTS = new Set(['F', 'L R', 'BR L TR', 'BL R TL', 'BL BR TL TR']);
    const READING = { F: 0, L: 0, TL: 0, TR: 1, R: 1, BL: 2, BR: 3 };  // ⌥1–4
    const GAP = 8;
    const ENDED = 'this session has ended';
    const drop = board.querySelector('.b-drop');
    const shown = new Map();  // key → {el, term, head, down}
    let slots = [];
    let active = null;
    let keyboard = null;  // the terminal a drop opens takes the keys
    let last = null;
    let asked = null;
    let stored = null;
    let opening = null;  // the console `?open=` names, until the list shows it

    const row = (key) => list.querySelector(`#rows .it[data-term][data-key="${CSS.escape(key)}"]`);
    const slotOf = (key) => slots.find((s) => s.key === key);
    const ordered = () => [...slots].sort((a, b) => READING[a.r] - READING[b.r]);

    function box(r) {
        const [c, w, cols, rows] = CELLS[r];
        const W = board.clientWidth, H = board.clientHeight;
        const cw = (W - GAP) / 2, rh = (H - GAP) / 2;
        return { x: c * (cw + GAP), y: w * (rh + GAP), w: cols === 2 ? W : cw, h: rows === 2 ? H : rh };
    }

    function place(el, r) {
        const b = box(r);
        Object.assign(el.style, { left: `${b.x}px`, top: `${b.y}px`, width: `${b.w}px`, height: `${b.h}px` });
    }

    function attach(key, t) {
        t.term.attach(`/api/console/${key.slice('console:'.length)}/attach`)
            .catch((err) => t.say(err.status === 404 ? ENDED : err.message, true));
    }

    function make(key) {
        const el = document.createElement('div');
        el.className = 'b-slot entering';
        el.dataset.key = key;
        el.innerHTML = '<header class="b-head"><span class="b-info"></span><span class="console-state b-state"></span><span class="spacer"></span><span class="b-acts"></span>'
            + '<button type="button" class="icon-btn b-off" title="Off the board — the session goes on"><svg><use href="#i-close"/></svg></button></header>'
            + '<div class="console-term b-term"></div>';
        // its name until the list brings its head (none while the bridge is away)
        const name = Object.assign(document.createElement('span'), { className: 'b-title', textContent: key.slice('console:'.length) });
        el.querySelector('.b-info').appendChild(name);
        const state = el.querySelector('.b-state');
        const t = { el, head: '', down: false };
        t.say = (text, bad) => {
            t.down = !!bad && text !== ENDED;  // a click on it tries again; an ended session leaves on the next poll
            state.textContent = state.title = text ? `· ${text}${t.down ? ' — click to reconnect' : ''}` : '';
            state.classList.toggle('bad', !!bad);
        };
        t.term = Term(el.querySelector('.b-term'), '--pane', t.say, keyboard === key);
        attach(key, t);
        return t;
    }

    // the head of each terminal, as the last poll of the list wrote it
    function heads() {
        for (const [key, t] of shown) {
            const tpl = list.querySelector(`#heads template[data-key="${CSS.escape(key)}"]`);
            if (!tpl || tpl.innerHTML === t.head) continue;
            t.head = tpl.innerHTML;
            for (const part of ['.b-info', '.b-acts']) t.el.querySelector(part).replaceWith(tpl.content.querySelector(part).cloneNode(true));
        }
    }

    // where each session is: a 2×2 frame on its row, its cells filled — blue for the terminal that has the keys
    function wheres() {
        list.querySelectorAll('#rows .it[data-term]').forEach((r) => {
            const s = slotOf(r.dataset.key);
            r.classList.toggle('on-board', !!s);
            r.classList.toggle('on-active', !!s && s.key === active);
            const at = r.querySelector('.where');
            if (!at) return;
            if (!s) { at.innerHTML = ''; return; }
            const [c, w, cols, rows] = CELLS[s.r];
            at.innerHTML = `<svg class="${s.key === active ? 'on' : ''}" viewBox="0 0 16 16" role="img" aria-label="on the board"><rect x="1" y="1" width="14" height="14" rx="3.5"/>`
                + `<rect class="f" x="${3 + c * 5.5}" y="${3 + w * 5.5}" width="${cols * 5.5 - 1}" height="${rows * 5.5 - 1}" rx="1.25"/></svg>`;
        });
    }

    function render() {
        for (const [key, t] of shown) {
            if (slotOf(key)) continue;
            shown.delete(key);
            t.term.dispose();
            t.el.classList.add('leaving');
            setTimeout(() => t.el.remove(), 220);
        }
        for (const s of slots) {
            let t = shown.get(s.key);
            if (!t) {
                t = make(s.key);
                shown.set(s.key, t);
                place(t.el, s.r);
                board.appendChild(t.el);
                requestAnimationFrame(() => requestAnimationFrame(() => t.el.classList.remove('entering')));
            } else place(t.el, s.r);
            t.el.classList.toggle('on', s.key === active);
        }
        keyboard = null;
        board.classList.toggle('empty', !slots.length);
        heads();
        wheres();
        const now = JSON.stringify({ slots, active });
        if (now !== stored) {
            stored = now;
            try { localStorage.setItem(STORE, now); } catch (e) { /* private mode */ }
        }
    }

    // a terminal leaves: the one above or below takes its column; a column gone gives its room to the other
    function remove(key) {
        const i = slots.findIndex((s) => s.key === key);
        if (i < 0) return;
        const r = slots[i].r;
        slots.splice(i, 1);
        if (r === 'L' || r === 'R') {
            const rest = slots.filter((s) => (r === 'L' ? ['R', 'TR', 'BR'] : ['L', 'TL', 'BL']).includes(s.r));
            if (rest.length === 1) rest[0].r = 'F';
            else rest.forEach((s) => { s.r = s.r[0] === 'T' ? 'L' : 'R'; });
        } else if (r !== 'F') {
            const other = slots.find((s) => s.r === { TL: 'BL', BL: 'TL', TR: 'BR', BR: 'TR' }[r]);
            if (other) other.r = r[1] === 'L' ? 'L' : 'R';
        }
        if (active === key) active = slots.length ? ordered()[0].key : null;
    }

    // beside the active terminal while there is room — the full column when the active one is a quarter —, else in its place
    function join(key) {
        if (!slotOf(key)) {
            const a = slotOf(active) || ordered()[0];
            const room = slots.length < 4 && (a && /^[FLR]$/.test(a.r) ? a : slots.find((s) => /^[LR]$/.test(s.r)));
            if (!a) slots.push({ key, r: 'F' });
            else if (!room) a.key = key;
            else {
                const [mine, theirs] = room.r === 'F' ? ['R', 'L'] : [`B${room.r}`, `T${room.r}`];
                room.r = theirs;
                slots.push({ key, r: mine });
            }
        }
        active = key;
        work.classList.add('s-board');
    }

    // the list's selection follows the active terminal (app.js `select`, without loading the pane)
    function follow(key) {
        const r = key && row(key);
        if (r && !r.classList.contains('sel')) select(r, { load: false });
    }

    function activate(key, keys) {
        const t = shown.get(key);
        if (active !== key) { active = key; render(); }
        if (t && t.down) attach(key, t);
        if (keys && t) t.term.focus();
    }

    // a session selected in the list: on the board, its terminal; else it takes the active one's place. A poll selecting
    // again a session you took off leaves the board alone; a click on it brings it back.
    document.addEventListener('click', (e) => {
        const r = e.target.closest('#rows .it[data-term]');
        if (r) asked = r.dataset.key;
        if (e.target.closest('[data-unselect]')) work.classList.remove('s-board');
        if (e.target.closest('#rows .it, [data-unselect]')) opening = null;  // you chose something else meanwhile
    });
    document.addEventListener('row-selected', (e) => {
        const key = e.detail.key;
        const again = key === last && key !== asked;
        last = key;
        asked = null;
        if (!row(key)) { work.classList.remove('s-board'); return; }
        if (again && !slotOf(key)) return;
        work.classList.add('s-board');
        if (slotOf(key)) { activate(key); return; }
        if (!slots.length) slots.push({ key, r: 'F' });
        else (slotOf(active) || slots[0]).key = key;
        active = key;
        render();
    });

    board.addEventListener('pointerdown', (e) => {
        const slot = e.target.closest('.b-slot');
        if (slot && !slot.classList.contains('leaving') && !e.target.closest('button, a')) {
            activate(slot.dataset.key);
            follow(slot.dataset.key);
        }
    });
    board.addEventListener('click', (e) => {
        const slot = e.target.closest('.b-slot');
        const t = slot && shown.get(slot.dataset.key);
        if (!t) return;
        if (e.target.closest('.b-off')) {
            remove(slot.dataset.key);
            render();
            follow(active);
            return;
        }
        const typed = e.target.closest('[data-term-type]');
        if (typed) t.term.type(typed.dataset.termType);
    });

    // --- Drag and drop ---------------------------------------------------------------------
    const chip = document.createElement('div');
    chip.className = 'b-chip';
    chip.hidden = true;
    chip.innerHTML = '<svg class="ic"><use href=""/></svg><span></span>';
    document.body.appendChild(chip);
    let drag = null;
    // the click that ends a drag — dropped or called off — is not a click on the row it started from
    let mute = false;
    window.addEventListener('click', (e) => { if (mute) { mute = false; e.stopPropagation(); e.preventDefault(); } }, true);

    const inside = (r, x, y) => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
    const titled = (key) => {
        const text = (shown.get(key)?.el.querySelector('.b-title')?.textContent || 'this one').trim();
        return text.length > 28 ? `${text.slice(0, 27)}…` : text;
    };

    // what a drop at (x, y) does, and the box it fills
    function zone(x, y) {
        if (!drag.head) {  // a row over the list: another group, or a new one
            const over = document.elementFromPoint(x, y)?.closest('#rows .proj[data-group], #rows [data-section-add]');
            if (over?.matches('[data-section-add]')) return { type: 'section', el: over };
            if (over) return over === drag.handle.closest('.proj') ? null : { type: 'file', group: over.dataset.group, el: over };
            if (!drag.term) return null;  // a session of another terminal never goes on the board
        }
        if (drag.head && inside(list.getBoundingClientRect(), x, y)) return { type: 'off' };
        const B = board.getBoundingClientRect();
        if (!inside(B, x, y)) return null;
        if (!slots.length) return { type: 'place', r: 'F', label: 'Here' };
        const lx = x - B.left, ly = y - B.top;
        const t = slots.find((s) => { const b = box(s.r); return lx >= b.x && lx <= b.x + b.w && ly >= b.y && ly <= b.y + b.h; });
        if (!t || t.key === drag.key) return null;
        if (drag.on) return { type: 'swap', t: t.key, r: t.r, label: `Swap with ${titled(t.key)}` };
        const b = box(t.r);
        if (t.r === 'F') return lx - b.x < b.w / 2 ? { type: 'split', t: t.key, r: 'L', other: 'R', label: 'Left half' } : { type: 'split', t: t.key, r: 'R', other: 'L', label: 'Right half' };
        if (t.r === 'L' || t.r === 'R') {
            const top = ly - b.y < b.h / 2;
            return { type: 'split', t: t.key, r: `${top ? 'T' : 'B'}${t.r}`, other: `${top ? 'B' : 'T'}${t.r}`, label: top ? 'Top half' : 'Bottom half' };
        }
        return { type: 'replace', t: t.key, r: t.r, label: `In place of ${titled(t.key)}` };
    }

    function show(z) {
        list.classList.toggle('b-off-target', !!z && z.type === 'off');
        list.querySelectorAll('.drop-on').forEach((el) => el.classList.toggle('drop-on', el === z?.el));
        if (z?.el) z.el.classList.add('drop-on');
        if (!z || z.type === 'off' || z.el) { drop.hidden = true; return; }
        drop.classList.toggle('fresh', drop.hidden);  // the first box appears in place, the next ones glide
        place(drop, z.r);
        drop.firstElementChild.textContent = z.label;
        drop.hidden = false;
    }

    // a row dropped on a group of the list: filed there (or in a new section, named next), the list drawn again
    async function file(z, key) {
        const r = await fetch(z.type === 'file' ? '/api/sessions/file' : '/api/sessions/sections', {
            method: 'POST', body: new URLSearchParams(z.type === 'file' ? { key, group: z.group } : { key, title: 'New section' }) });
        const made = r.ok && z.type === 'section' ? (await r.json()).key : null;
        if (!r.ok) toast(await r.text() || `Sentinel answered ${r.status}`, true);
        await htmx.ajax('GET', '/partials/sessions', { target: list, swap: 'innerHTML' });
        if (made) document.dispatchEvent(new CustomEvent('section-made', { detail: { key: made } }));
    }

    function land(z, key) {
        if (z.el) { file(z, key); return; }
        if (z.type === 'off') remove(key);
        else {
            if (z.type === 'place') slots.push({ key, r: 'F' });
            else if (z.type === 'swap') { const a = slotOf(key), b = slotOf(z.t); [a.r, b.r] = [b.r, a.r]; }
            else if (z.type === 'replace') slotOf(z.t).key = key;
            else { slotOf(z.t).r = z.other; slots.push({ key, r: z.r }); }
            active = key;
            keyboard = key;
        }
        work.classList.add('s-board');
        render();
        follow(active);
    }

    function end(z) {
        const { key, board: was } = drag;
        drag = null;
        mute = true;
        chip.hidden = true;
        drop.hidden = true;
        list.classList.remove('b-off-target');
        list.querySelectorAll('.drop-on').forEach((el) => el.classList.remove('drop-on'));
        document.body.classList.remove('s-dragging');
        document.querySelectorAll('.lifted').forEach((el) => el.classList.remove('lifted'));
        if (z) land(z, key);
        if (!z || z.el) work.classList.toggle('s-board', was);  // filed in the list: the pane stays what it was
    }

    document.addEventListener('pointerdown', (e) => {
        mute = false;
        if (e.button !== 0 || e.target.closest('button, a')) return;
        const handle = e.target.closest('#rows .it[data-term], #rows .it[data-key^="session:"], .b-head');
        if (!handle || handle.closest('.leaving')) return;
        const key = handle.dataset.key || handle.closest('.b-slot').dataset.key;
        drag = { key, handle, id: e.pointerId, x: e.clientX, y: e.clientY, started: false, z: null,
                 on: !!slotOf(key), head: handle.classList.contains('b-head'), term: !handle.dataset.key?.startsWith('session:'),
                 board: work.classList.contains('s-board') };
    });

    document.addEventListener('pointermove', (e) => {
        if (!drag || e.pointerId !== drag.id) return;
        if (!drag.started) {
            if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 5) return;
            drag.started = true;
            drag.handle.setPointerCapture(e.pointerId);  // the terminals under the pointer must not see it
            const icon = drag.handle.querySelector('.ic');
            chip.querySelector('svg').setAttribute('class', icon ? icon.getAttribute('class') : 'ic');
            chip.querySelector('use').setAttribute('href', icon ? icon.querySelector('use').getAttribute('href') : '');
            chip.querySelector('span').textContent = (drag.handle.querySelector('.t, .b-title')?.textContent || '').trim();
            chip.hidden = false;
            document.body.classList.add('s-dragging');
            (shown.get(drag.key)?.el || drag.handle).classList.add('lifted');
            if (drag.term) work.classList.add('s-board');
        }
        chip.style.transform = `translate(${e.clientX + 14}px, ${e.clientY + 12}px)`;
        drag.z = zone(e.clientX, e.clientY);
        show(drag.z);
    });

    document.addEventListener('pointerup', (e) => {
        if (!drag || e.pointerId !== drag.id) return;
        if (!drag.started) {
            const head = drag.head && shown.get(drag.key);
            drag = null;
            if (head) head.term.focus();  // a click on a head: its terminal takes the keys
            return;
        }
        end(drag.z);
    });
    document.addEventListener('pointercancel', () => { if (drag && drag.started) end(null); else drag = null; });

    // the list is not polled under a drag: its rows are what the pointer holds
    document.addEventListener('htmx:beforeRequest', (e) => { if (drag && drag.started && e.detail.elt === list) e.preventDefault(); });

    // Esc calls a drag off — on window, before sessions.js leaves focus with it
    window.addEventListener('keydown', (e) => {
        if (drag && drag.started && e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); end(null); }
    }, true);
    // ⌥1–4: that terminal, keys included — taken before xterm sees them
    document.addEventListener('keydown', (e) => {
        if (!e.altKey || e.metaKey || e.ctrlKey || !/^Digit[1-4]$/.test(e.code) || e.target.closest('#console')) return;
        if (e.target.closest('input, textarea, select') && !e.target.closest('.console-term')) return;
        e.preventDefault();
        e.stopPropagation();
        const s = ordered()[Number(e.code.slice(5)) - 1];
        if (!s) return;
        work.classList.add('s-board');
        activate(s.key, true);
        follow(s.key);
    }, true);

    // after a poll: the terminals whose session ended leave (not on a list that shows the bridge's error: it lists
    // nothing for one answer), the heads and the marks are redrawn
    document.addEventListener('htmx:afterSwap', (e) => {
        if (e.detail.target !== list) return;
        const before = active;
        if (!list.querySelector('.s-error')) slots.filter((s) => !row(s.key)).forEach((s) => remove(s.key));
        if (opening && row(opening)) { join(opening); opening = null; }
        render();
        if (active !== before && work.classList.contains('s-board')) follow(active);
    });
    // End on the board failed: the pane has its answer, shown
    document.addEventListener('htmx:afterSwap', (e) => {
        if (e.detail.target.id === 'detail' && e.detail.requestConfig.path === '/api/sessions/end') work.classList.remove('s-board');
    });

    // the board follows its pane — the columns glide into focus — without gliding itself
    let still = null;
    new ResizeObserver(() => {
        board.classList.add('still');
        slots.forEach((s) => { const t = shown.get(s.key); if (t) place(t.el, s.r); });
        clearTimeout(still);
        still = setTimeout(() => board.classList.remove('still'), 150);
    }).observe(board);

    // the board this browser left, without the sessions ended since; app.js selects its active terminal's row
    document.addEventListener('DOMContentLoaded', () => {
        let saved = null;
        try { saved = JSON.parse(localStorage.getItem(STORE) || 'null'); } catch (e) { saved = null; }
        const kept = (saved && Array.isArray(saved.slots) ? saved.slots : []).filter((s) => s && CELLS[s.r] && typeof s.key === 'string');
        if (LAYOUTS.has(kept.map((s) => s.r).sort().join(' ')) && new Set(kept.map((s) => s.key)).size === kept.length) {
            slots = kept.map((s) => ({ key: s.key, r: s.r }));
            active = slotOf(saved.active) ? saved.active : null;
            stored = JSON.stringify({ slots, active });
            if (!list.querySelector('.s-error')) slots.filter((s) => !row(s.key)).forEach((s) => remove(s.key));
        }
        if (!slotOf(active)) active = slots.length ? ordered()[0].key : null;
        const asked = new URLSearchParams(location.search).get('open');
        if (asked && /^[a-z0-9][a-z0-9-]{0,47}$/.test(asked)) {
            history.replaceState(null, '', location.pathname);  // a reload does not open it again
            if (row(`console:${asked}`)) join(`console:${asked}`);
            else opening = `console:${asked}`;  // just started: the bridge lists it in a moment
        }
        if (active) {
            work.classList.add('s-board');
            row(active)?.classList.add('sel');
        }
        render();
    });
})();
