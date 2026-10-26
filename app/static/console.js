// Consoles: live Claude Code sessions — the real CLI in xterm.js, run by the bridge on the host — in two places.
// The window over any page: a button `data-console="<url>"` asks Sentinel to start (or reopen) the session; closing
// the window detaches, the session goes on, and the window comes back after a page change, where you dragged it,
// until you close it. Sessions' board (static/board.js) makes one `Term` per terminal it shows. Either way the answer
// to a POST on the url is a WebSocket address with a one-time ticket.
(() => {
    const XTERM = [
        ['link', 'https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/css/xterm.css', 'sha384-n2n7twoohnW+d3myBKaUgl7DSiwidw6MkQy9oesGzkPpMjejKRR3XlnD+5yCdtBD'],
        ['script', 'https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/lib/xterm.js', 'sha384-f/1U6Z9wM4D71a5eRXEZnyOTMOvjqxr2XLwh+Go1OvIl3L3tOcvUrzudnhbECwl4'],
        ['script', 'https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0/lib/addon-fit.js', 'sha384-txoiwu4RR2GD3qySbaj+BbzibkLbSJRcfqGYMu6z1EqHil4A2dyBiBW5dlacG6OR'],
        ['script', 'https://cdn.jsdelivr.net/npm/@xterm/addon-web-links@0.12.0/lib/addon-web-links.js', 'sha384-0IIwkXq0LAkIxEVKxlOxWbje2g/VT+5CzVTZiEukyiZ02pOl/O+M0fkpEKAYqto9'],
    ];
    const encoder = new TextEncoder();
    let loading = null;

    function load() {
        loading = loading || Promise.all(XTERM.map(([tag, url, sri]) => new Promise((ok, ko) => {
            const el = document.createElement(tag);
            if (tag === 'link') { el.rel = 'stylesheet'; el.href = url; } else { el.src = url; }
            el.integrity = sri;
            el.crossOrigin = 'anonymous';
            el.onload = ok;
            el.onerror = () => { loading = null; ko(new Error('the terminal could not load')); };
            document.head.appendChild(el);
        })));
        return loading;
    }

    // one terminal in `host`, on the background `bg`; `report(text, bad)` hears its state until it is disposed. `focus`:
    // the keyboard goes to the session once it attaches — not for a board brought back, whose list keeps the keys
    function Term(host, bg, report, focus) {
        let term = null, fit = null, ws = null, sizer = null, gone = false;
        const say = (text, bad) => { if (!gone) report(text, bad); };

        function terminal() {
            if (term) return term;
            const css = getComputedStyle(document.documentElement);
            const v = (name) => css.getPropertyValue(name).trim();
            // a left click opens a link in a new tab, http(s) only: Claude's own (OSC 8 — its text is not its address,
            // the address shows on hover, in place of xterm's confirm box) and the bare addresses in the output
            const visit = (e, uri) => { if (e.button === 0) window.open(uri, '_blank', 'noopener'); };
            term = new Terminal({
                fontFamily: v('--mono'), fontSize: 12.5, lineHeight: 1.15, cursorBlink: true,
                macOptionClickForcesSelection: true,  // ⌥-drag selects text even while tmux takes the mouse
                theme: { background: v(bg), foreground: v('--text'), cursor: v('--claude'), selectionBackground: 'rgba(87, 193, 255, 0.25)' },
                linkHandler: { activate: visit, hover: (e, uri) => { host.title = uri; }, leave: () => host.removeAttribute('title') },
            });
            fit = new FitAddon.FitAddon();
            term.loadAddon(fit);
            term.loadAddon(new WebLinksAddon.WebLinksAddon(visit));
            term.open(host);
            term.onData((d) => { if (ws && ws.readyState === 1) ws.send(encoder.encode(d)); });
            term.onResize(({ cols, rows }) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ cols, rows })); });
            // once the size has settled: the Sessions columns glide, and every step would redraw Claude Code
            let resized = null;
            sizer = new ResizeObserver(() => { clearTimeout(resized); resized = setTimeout(() => { if (term && host.offsetParent) fit.fit(); }, 120); });
            sizer.observe(host);
            return term;
        }

        function connect(address) {
            detach();
            terminal().reset();
            const socket = ws = new WebSocket(address);
            socket.binaryType = 'arraybuffer';
            socket.onopen = () => {
                say('');
                fit.fit();
                socket.send(JSON.stringify({ cols: term.cols, rows: term.rows }));
                if (focus) term.focus();
            };
            socket.onmessage = (e) => term.write(new Uint8Array(e.data));
            socket.onclose = () => { if (ws === socket) say('disconnected', true); };
        }

        // POST `url`: Sentinel starts or reopens the session and answers {name, title, ws}
        async function attach(url) {
            say('starting');
            const r = await fetch(url, { method: 'POST' });
            const answer = r.headers.get('Content-Type')?.includes('json') ? await r.json() : {};
            if (!r.ok) throw Object.assign(new Error(answer.error || `Sentinel answered ${r.status}`), { status: r.status });
            await load();
            if (!gone) connect(answer.ws);  // the pane may have moved on meanwhile
            return answer;
        }

        function detach() {
            if (ws) { ws.onclose = null; ws.close(); ws = null; }
        }

        function dispose() {
            gone = true;
            detach();
            if (sizer) sizer.disconnect();
            if (term) term.dispose();
            term = null;
        }

        // text into the session's prompt, as if typed — Enter stays yours
        function type(text) {
            if (!ws || ws.readyState !== 1) return;
            ws.send(encoder.encode(text));
            term.focus();
        }

        return { attach, detach, dispose, type, focus: () => { if (term) term.focus(); } };
    }
    window.Term = Term;

    // --- The window over the page ------------------------------------------------------------
    const box = document.getElementById('console');
    if (box) {
        const KEY = 'sentinel.console';
        const titleEl = box.querySelector('.console-title');
        const stateEl = box.querySelector('.console-state');
        const remember = (c) => { try { c ? sessionStorage.setItem(KEY, JSON.stringify(c)) : sessionStorage.removeItem(KEY); } catch (e) { /* private mode */ } };
        const remembered = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) { return null; } };
        const state = (text, bad) => {
            const said = text === 'disconnected' ? 'disconnected — the button on the alert reopens it' : text;
            stateEl.textContent = said ? `· ${said}` : '';
            stateEl.classList.toggle('bad', !!bad);
        };
        const win = Term(box.querySelector('.console-term'), '--pane-2', state, true);
        let current = null;

        const open = async (url, title) => {
            box.hidden = false;
            inside();
            titleEl.textContent = title || titleEl.textContent;
            try {
                const answer = await win.attach(url);
                current = { name: answer.name, title: title || answer.title };
                remember(current);
                titleEl.textContent = current.title;
            } catch (e) {
                state(e.status === 404 ? (url.includes('/attach') ? 'this session has ended' : 'this problem is no longer open') : e.message, true);
                if (url.includes('/attach')) remember(null);
            }
        };

        const close = () => {
            win.detach();
            box.hidden = true;
            remember(null);
        };

        const end = async () => {
            if (!current || !confirm(`End the Claude session ${current.title}?`)) return;
            await fetch(`/api/console/${current.name}/end`, { method: 'POST' });
            close();
        };

        // its head drags it anywhere in the page, kept inside; a page change finds it there again
        const AT = 'sentinel.console.at';
        const head = box.querySelector('.console-head');
        const inside = () => {
            if (box.hidden || !box.style.left) return;
            const r = box.getBoundingClientRect();
            box.style.left = `${Math.max(8, Math.min(r.left, innerWidth - r.width - 8))}px`;
            box.style.top = `${Math.max(8, Math.min(r.top, innerHeight - r.height - 8))}px`;
        };
        const moveTo = (x, y) => {
            Object.assign(box.style, { left: `${x}px`, top: `${y}px`, right: 'auto', bottom: 'auto' });
            inside();
        };
        head.addEventListener('pointerdown', (e) => {
            if (e.button !== 0 || e.target.closest('button')) return;
            const r = box.getBoundingClientRect();
            const follow = (ev) => moveTo(ev.clientX - (e.clientX - r.left), ev.clientY - (e.clientY - r.top));
            head.setPointerCapture(e.pointerId);
            head.addEventListener('pointermove', follow);
            head.addEventListener('lostpointercapture', () => {
                head.removeEventListener('pointermove', follow);
                try { sessionStorage.setItem(AT, JSON.stringify([parseFloat(box.style.left), parseFloat(box.style.top)])); } catch (err) { /* private mode */ }
            }, { once: true });
        });
        window.addEventListener('resize', inside);
        try { const at = JSON.parse(sessionStorage.getItem(AT) || 'null'); if (at) moveTo(...at); } catch (e) { /* private mode */ }

        document.addEventListener('click', (e) => {
            const start = e.target.closest('[data-console]');
            if (start) { e.preventDefault(); open(start.dataset.console); return; }
            const act = e.target.closest('[data-console-do]');
            if (!act) return;
            if (act.dataset.consoleDo === 'close') close();
            else if (act.dataset.consoleDo === 'end') end();
            else { act.textContent = box.classList.toggle('big') ? 'Shrink' : 'Expand'; inside(); }
        });

        const last = remembered();
        if (last) open(`/api/console/${last.name}/attach`, last.title);
    }
})();
