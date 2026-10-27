// Sessions' board: up to four consoles side by side — two columns, either one split in two —, and as many boards as you
// need, one shown at a time (their numbers above it, ⌥⇧← ⌥⇧→). A live session dragged from the list lands where the box
// under the pointer says (dropped on a terminal, it splits it while there is room, else takes its place); a terminal's
// head dragged onto another swaps them, back onto the list takes it off, like its ×. A session selected in the list
// takes the active terminal's place — one on another board brings that board —, and so does the pane (a ticket, New, a
// session of another terminal) — the other terminals stay in view. A session just started (New, Pick up, Watch's Claude
// button: `/sessions?open=<console>`) goes beside the active terminal, or onto a board of its own when this one is full:
// nothing leaves for it. A live row dropped on another group of the list goes there (yours, made on the spot by New
// section, a big rock, the run, off cycle), whatever its ticket says. The boards are this browser's (localStorage): the
// sessions run on the host whatever they show. Each terminal is a `Term` (static/console.js) attached from the first
// time its board shows until the page goes; the rows say where theirs is.
(() => {
    const work = document.querySelector('.s-work');
    const board = document.getElementById('board');
    const pagesEl = document.getElementById('boards');
    const list = document.getElementById('list');
    const detail = document.getElementById('detail');
    if (!work || !board || !pagesEl || !list || !detail) return;
    const STORE = 'sentinel.sessions.board';
    // a terminal's place on a 2×2 grid — [column, row, columns, rows]: the whole board, a column or a quarter
    const CELLS = { F: [0, 0, 2, 2], L: [0, 0, 1, 2], R: [1, 0, 1, 2], TL: [0, 0, 1, 1], TR: [1, 0, 1, 1], BL: [0, 1, 1, 1], BR: [1, 1, 1, 1] };
    const LAYOUTS = new Set(['F', 'L R', 'BR L TR', 'BL R TL', 'BL BR TL TR']);
    const READING = { F: 0, L: 0, TL: 0, TR: 1, R: 1, BL: 2, BR: 3 };  // ⌥1–4
    const GAP = 8;
    const ENDED = 'this session has ended';
    const drop = board.querySelector('.b-drop');
    const shown = new Map();  // key → {el, term, head, down}, whichever board it is on
    let boards = [{ slots: [], active: null }];
    let cur = 0;
    // the board shown: `slots` is its array, `active` is written back into it by keep()
    let slots = boards[0].slots;
    let active = null;
    let keyboard = null;  // the terminal a drop opens takes the keys
    let last = null;
    let asked = null;
    let stored = null;
    let opening = null;  // a console just started (New, Pick up, `?open=`), until it is on a board

    const row = (key) => list.querySelector(`#rows .it[data-term][data-key="${CSS.escape(key)}"]`);
    const slotOf = (key) => slots.find((s) => s.key === key);
    const boardOf = (key) => boards.findIndex((b) => b.slots.some((s) => s.key === key));
    const ordered = (of = slots) => [...of].sort((a, b) => READING[a.r] - READING[b.r]);
    const keep = () => { boards[cur].active = active; };

    function turn(i) {
        if (i === cur || !boards[i]) return;
        keep();
        cur = i;
        ({ slots, active } = boards[i]);
    }

    // a board left empty goes — not the one shown
    function tidy() {
        keep();
        const kept = boards.filter((b, i) => i === cur || b.slots.length);
        cur = kept.indexOf(boards[cur]);
        boards = kept;
    }

    function box(r) {
        const [c, w, cols, rows] = CELLS[r];
        const W = board.clientWidth, H = board.clientHeight;
        const cw = (W - GAP) / 2, rh = (H - GAP) / 2;
        return { x: c * (cw + GAP), y: w * (rh + GAP), w: cols === 2 ? W : cw, h: rows === 2 ? H : rh };
    }

    function place(el, r, top = 0) {
        const b = box(r);
        Object.assign(el.style, { left: `${b.x}px`, top: `${top + b.y}px`, width: `${b.w}px`, height: `${b.h}px` });
    }

    // the terminals, or the pane (a ticket, New, a session of another terminal) in the active one's place: the others
    // stay in view, and the board is as it was when a terminal is chosen again; with no terminal, the pane has the room
    function view(terminals) {
        const over = !terminals && slots.length > 0;
        work.classList.toggle('s-board', terminals || over);
        work.classList.toggle('s-pane', over);
        if (over) place(detail, (slotOf(active) || ordered()[0]).r, board.offsetTop);  // under the boards' numbers
        else Object.assign(detail.style, { left: '', top: '', width: '', height: '' });
    }
    const terminals = () => work.classList.contains('s-board') && !work.classList.contains('s-pane');

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

    // where each session is: a 2×2 frame on its row, its cells filled — blue for the terminal that has the keys —, after
    // the number of its board when that one is not shown
    function wheres() {
        list.querySelectorAll('#rows .it[data-term]').forEach((r) => {
            const b = boardOf(r.dataset.key);
            const s = b >= 0 && boards[b].slots.find((x) => x.key === r.dataset.key);
            const on = b === cur && s.key === active;
            r.classList.toggle('on-board', b === cur);
            r.classList.toggle('on-active', on);
            const at = r.querySelector('.where');
            if (!at) return;
            if (!s) { at.innerHTML = ''; return; }
            const [c, w, cols, rows] = CELLS[s.r];
            at.innerHTML = (b === cur ? '' : `<b>${b + 1}</b>`)
                + `<svg class="${on ? 'on' : ''}" viewBox="0 0 16 16" role="img" aria-label="on board ${b + 1}"><rect x="1" y="1" width="14" height="14" rx="3.5"/>`
                + `<rect class="f" x="${3 + c * 5.5}" y="${3 + w * 5.5}" width="${cols * 5.5 - 1}" height="${rows * 5.5 - 1}" rx="1.25"/></svg>`;
        });
    }

    // the boards' numbers, while there is another board or a terminal to make room beside; a dot on a board whose
    // session waits for you
    let drawn = null;
    function pages() {
        pagesEl.hidden = boards.length < 2 && !slots.length;
        const waits = (b) => b.slots.some((s) => row(s.key)?.querySelector('.ic.hot'));
        const html = boards.map((b, i) => `<button type="button" data-board="${i}" class="${i === cur ? 'on' : ''}" title="Board ${i + 1} — ⌥⇧← ⌥⇧→">${i + 1}${i !== cur && waits(b) ? '<i></i>' : ''}</button>`).join('')
            + `<button type="button" data-board="new" title="A new board"${slots.length ? '' : ' disabled'}><svg><use href="#i-plus"/></svg></button>`;
        if (html !== drawn) pagesEl.innerHTML = drawn = html;
    }

    function render() {
        tidy();
        pages();  // first: shown or not, they move the board
        for (const [key, t] of shown) {
            const b = boardOf(key);
            if (b >= 0) { t.el.classList.toggle('away', b !== cur); continue; }  // another board's: hidden, still attached
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
        if (work.classList.contains('s-pane')) view(false);
        heads();
        wheres();
        const now = JSON.stringify({ boards, cur });
        if (now !== stored) {
            stored = now;
            try { localStorage.setItem(STORE, now); } catch (e) { /* private mode */ }
        }
    }

    // a terminal leaves its board: the one above or below takes its column; a column gone gives its room to the other
    function leave(b, key) {
        const of = b.slots;
        const i = of.findIndex((s) => s.key === key);
        if (i < 0) return;
        const r = of[i].r;
        of.splice(i, 1);
        if (r === 'L' || r === 'R') {
            const rest = of.filter((s) => (r === 'L' ? ['R', 'TR', 'BR'] : ['L', 'TL', 'BL']).includes(s.r));
            if (rest.length === 1) rest[0].r = 'F';
            else rest.forEach((s) => { s.r = s.r[0] === 'T' ? 'L' : 'R'; });
        } else if (r !== 'F') {
            const other = of.find((s) => s.r === { TL: 'BL', BL: 'TL', TR: 'BR', BR: 'TR' }[r]);
            if (other) other.r = r[1] === 'L' ? 'L' : 'R';
        }
        if (b.active === key) b.active = of.length ? ordered(of)[0].key : null;
    }

    function remove(key) {
        keep();
        boards.forEach((b) => leave(b, key));
        active = boards[cur].active;
    }

    // beside the active terminal while there is room — the full column when the active one is a quarter —, else onto a
    // board of its own; one on a board already brings that board
    function join(key) {
        const on = boardOf(key);
        if (on >= 0) turn(on);
        else {
            const a = slotOf(active) || ordered()[0];
            const room = slots.length < 4 && (a && /^[FLR]$/.test(a.r) ? a : slots.find((s) => /^[LR]$/.test(s.r)));
            if (!a) slots.push({ key, r: 'F' });
            else if (!room) {
                boards.push({ slots: [{ key, r: 'F' }], active: key });
                turn(boards.length - 1);
            } else {
                const [mine, theirs] = room.r === 'F' ? ['R', 'L'] : [`B${room.r}`, `T${room.r}`];
                room.r = theirs;
                slots.push({ key, r: mine });
            }
        }
        active = key;
        view(true);
    }

    // the list's selection follows the active terminal (app.js `select`, without loading the pane)
    function follow(key) {
        const r = key && row(key);
        if (r && !r.classList.contains('sel')) select(r, { load: false });
    }

    // another board shown, its active terminal with the keys and its row selected in the list
    function go(i) {
        if (!boards[i] || i === cur) return;
        turn(i);
        keyboard = active;  // attached now, or already
        view(true);
        render();
        shown.get(active)?.term.focus();
        follow(active);
    }

    // a terminal chosen by a click or ⌥1–4: sessions.js narrows the list
    const chosen = () => document.dispatchEvent(new CustomEvent('terminal-chosen'));

    function activate(key, keys) {
        const t = shown.get(key);
        if (active !== key) { active = key; render(); }
        if (t && t.down) attach(key, t);
        if (keys && t) t.term.focus();
    }

    // a session selected in the list: on a board, its terminal; else it takes the active one's place — one just started
    // joins. A poll selecting again a session you took off, or one on another board, leaves the board alone; a click on
    // it brings it.
    document.addEventListener('click', (e) => {
        const r = e.target.closest('#rows .it[data-term]');
        if (r) asked = r.dataset.key;
        if (e.target.closest('[data-unselect]')) view(false);
        if (e.target.closest('#rows .it, [data-unselect]')) opening = null;  // you chose something else meanwhile
    });
    document.addEventListener('select-key', (e) => { opening = e.detail.key; }, true);  // before app.js selects it
    document.addEventListener('row-selected', (e) => {
        const key = e.detail.key;
        const again = key === last && key !== asked;
        last = key;
        asked = null;
        if (!row(key)) { view(false); return; }
        if (key === opening) { opening = null; join(key); render(); return; }
        if (!again) opening = null;  // you chose something else meanwhile (the keys, another board)
        const on = boardOf(key);
        if (again && on !== cur) return;
        view(true);
        if (on >= 0) {
            if (on !== cur) { turn(on); render(); }
            activate(key);
            return;
        }
        if (!slots.length) slots.push({ key, r: 'F' });
        else (slotOf(active) || slots[0]).key = key;
        active = key;
        render();
    });

    board.addEventListener('pointerdown', (e) => {
        const slot = e.target.closest('.b-slot');
        if (slot && !slot.classList.contains('leaving') && !e.target.closest('button, a')) {
            view(true);
            activate(slot.dataset.key);
            follow(slot.dataset.key);
            if (e.button === 0 && !e.target.closest('.b-head')) chosen();  // a head waits: it may be a drag
        }
    });
    pagesEl.addEventListener('click', (e) => {
        const p = e.target.closest('[data-board]');
        if (!p) return;
        if (p.dataset.board !== 'new') { go(Number(p.dataset.board)); return; }
        if (!slots.length) return;
        boards.push({ slots: [], active: null });
        go(boards.length - 1);
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
            const on = boardOf(key);
            if (on >= 0 && on !== cur) leave(boards[on], key);  // from another board: it moves here
            if (z.type === 'place') slots.push({ key, r: 'F' });
            else if (z.type === 'swap') { const a = slotOf(key), b = slotOf(z.t); [a.r, b.r] = [b.r, a.r]; }
            else if (z.type === 'replace') slotOf(z.t).key = key;
            else { slotOf(z.t).r = z.other; slots.push({ key, r: z.r }); }
            active = key;
            keyboard = key;
        }
        view(true);
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
        if (!z || z.el) view(was);  // filed in the list: the pane stays what it was
    }

    document.addEventListener('pointerdown', (e) => {
        mute = false;
        if (e.button !== 0 || e.target.closest('button, a')) return;
        const handle = e.target.closest('#rows .it[data-term], #rows .it[data-key^="session:"], .b-head');
        if (!handle || handle.closest('.leaving')) return;
        const key = handle.dataset.key || handle.closest('.b-slot').dataset.key;
        drag = { key, handle, id: e.pointerId, x: e.clientX, y: e.clientY, started: false, z: null,
                 on: !!slotOf(key), head: handle.classList.contains('b-head'), term: !handle.dataset.key?.startsWith('session:'),
                 board: terminals() };
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
            ((slotOf(drag.key) && shown.get(drag.key)?.el) || drag.handle).classList.add('lifted');
            if (drag.term) view(true);
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
            if (head) { head.term.focus(); chosen(); }  // a click on a head: its terminal takes the keys
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
    // ⌥1–4: that terminal, keys included; ⌥⇧← ⌥⇧→: the board before or after — taken before xterm sees them
    document.addEventListener('keydown', (e) => {
        if (!e.altKey || e.metaKey || e.ctrlKey || e.target.closest('#console') || drag?.started) return;  // a drag holds its board
        const side = e.shiftKey && { ArrowLeft: -1, ArrowRight: 1 }[e.code];
        if (!side && (e.shiftKey || !/^Digit[1-4]$/.test(e.code))) return;
        if (e.target.closest('input, textarea, select') && !e.target.closest('.console-term')) return;
        e.preventDefault();
        e.stopPropagation();
        if (side) { go(cur + side); return; }
        const s = ordered()[Number(e.code.slice(5)) - 1];
        if (!s) return;
        view(true);
        activate(s.key, true);
        follow(s.key);
        chosen();
    }, true);

    // after a poll: the terminals whose session ended leave (not on a list that shows the bridge's error: it lists
    // nothing for one answer), the heads and the marks are redrawn
    document.addEventListener('htmx:afterSwap', (e) => {
        if (e.detail.target !== list) return;
        const before = active;
        if (!list.querySelector('.s-error')) boards.flatMap((b) => b.slots).filter((s) => !row(s.key)).forEach((s) => remove(s.key));
        if (opening && row(opening)) { join(opening); opening = null; }
        render();
        if (active !== before && terminals()) follow(active);
    });
    // End on the board failed: the pane has its answer, shown
    document.addEventListener('htmx:afterSwap', (e) => {
        if (e.detail.target.id === 'detail' && e.detail.requestConfig.path === '/api/sessions/end') view(false);
    });

    // the board follows its pane — the columns glide into focus — without gliding itself
    let still = null;
    new ResizeObserver(() => {
        board.classList.add('still');
        slots.forEach((s) => { const t = shown.get(s.key); if (t) place(t.el, s.r); });
        if (work.classList.contains('s-pane')) view(false);
        clearTimeout(still);
        still = setTimeout(() => board.classList.remove('still'), 150);
    }).observe(board);

    // the boards this browser left, without the sessions ended since and the boards they emptied — each read back only
    // as one of LAYOUTS, a session on one board only (one saved before there were several is the first); app.js selects
    // the active terminal's row
    document.addEventListener('DOMContentLoaded', () => {
        let saved = null;
        try { saved = JSON.parse(localStorage.getItem(STORE) || 'null'); } catch (e) { saved = null; }
        const saves = (saved && (Array.isArray(saved.boards) ? saved.boards : [saved])) || [];
        const ended = (key) => !list.querySelector('.s-error') && !row(key);
        const seen = new Set();
        const kept = [];
        let was = null;
        saves.forEach((p, i) => {
            const got = (p && Array.isArray(p.slots) ? p.slots : []).filter((s) => s && CELLS[s.r] && typeof s.key === 'string');
            const keys = got.map((s) => s.key);
            if (!LAYOUTS.has(got.map((s) => s.r).sort().join(' ')) || new Set(keys).size !== keys.length || keys.some((k) => seen.has(k))) return;
            keys.forEach((k) => seen.add(k));
            const b = { slots: got.map((s) => ({ key: s.key, r: s.r })), active: keys.includes(p.active) ? p.active : null };
            keys.filter(ended).forEach((k) => leave(b, k));
            if (!b.slots.length) return;
            if (!b.active) b.active = ordered(b.slots)[0].key;
            if (i === (saved.cur || 0)) was = b;
            kept.push(b);
        });
        if (kept.length) {
            boards = kept;
            cur = Math.max(0, kept.indexOf(was));
            ({ slots, active } = boards[cur]);
        }
        const asked = new URLSearchParams(location.search).get('open');
        if (asked && /^[a-z0-9][a-z0-9-]{0,47}$/.test(asked)) {
            history.replaceState(null, '', location.pathname);  // a reload does not open it again
            if (row(`console:${asked}`)) join(`console:${asked}`);
            else opening = `console:${asked}`;  // just started: the bridge lists it in a moment
        }
        if (active) {
            view(true);
            row(active)?.classList.add('sel');
        }
        render();
    });
})();
