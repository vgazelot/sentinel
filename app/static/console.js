// The console: a live Claude Code session in a window over the page — the real CLI in xterm.js, run by the
// bridge on the host. A button `data-console="<url>"` asks Sentinel to start (or reopen) the session; the
// answer is a WebSocket address with a one-time ticket. Closing the window detaches: the session goes on,
// and the window comes back after a page change until you close it.
(() => {
    const box = document.getElementById('console');
    if (!box) return;
    const XTERM = [
        ['link', 'https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/css/xterm.css', 'sha384-n2n7twoohnW+d3myBKaUgl7DSiwidw6MkQy9oesGzkPpMjejKRR3XlnD+5yCdtBD'],
        ['script', 'https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0/lib/xterm.js', 'sha384-f/1U6Z9wM4D71a5eRXEZnyOTMOvjqxr2XLwh+Go1OvIl3L3tOcvUrzudnhbECwl4'],
        ['script', 'https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0/lib/addon-fit.js', 'sha384-txoiwu4RR2GD3qySbaj+BbzibkLbSJRcfqGYMu6z1EqHil4A2dyBiBW5dlacG6OR'],
    ];
    const KEY = 'sentinel.console';
    const titleEl = box.querySelector('.console-title');
    const stateEl = box.querySelector('.console-state');
    const encoder = new TextEncoder();
    let loading = null, term = null, fit = null, ws = null, current = null;

    const remember = (c) => { try { c ? sessionStorage.setItem(KEY, JSON.stringify(c)) : sessionStorage.removeItem(KEY); } catch (e) { /* private mode */ } };
    const remembered = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) { return null; } };
    const state = (text, bad) => { stateEl.textContent = text ? `· ${text}` : ''; stateEl.classList.toggle('bad', !!bad); };

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

    function terminal() {
        if (term) return term;
        const css = getComputedStyle(document.documentElement);
        const v = (name) => css.getPropertyValue(name).trim();
        term = new Terminal({
            fontFamily: v('--mono'), fontSize: 12.5, lineHeight: 1.15, cursorBlink: true,
            macOptionClickForcesSelection: true,  // ⌥-drag selects text even while tmux takes the mouse
            theme: { background: v('--pane-2'), foreground: v('--text'), cursor: v('--claude'), selectionBackground: 'rgba(87, 193, 255, 0.25)' },
        });
        fit = new FitAddon.FitAddon();
        term.loadAddon(fit);
        term.open(box.querySelector('.console-term'));
        term.onData((d) => { if (ws && ws.readyState === 1) ws.send(encoder.encode(d)); });
        term.onResize(({ cols, rows }) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ cols, rows })); });
        new ResizeObserver(() => { if (!box.hidden) fit.fit(); }).observe(box.querySelector('.console-term'));
        return term;
    }

    async function open(url, title) {
        box.hidden = false;
        titleEl.textContent = title || titleEl.textContent;
        state('starting');
        try {
            const r = await fetch(url, { method: 'POST' });
            const answer = r.headers.get('Content-Type')?.includes('json') ? await r.json() : {};
            if (r.status === 404) throw new Error(url.includes('/attach') ? 'this session has ended' : 'this problem is no longer open');
            if (!r.ok) throw new Error(answer.error || `Sentinel answered ${r.status}`);
            await load();
            connect(answer.name, title || answer.title, answer.ws);
        } catch (e) {
            state(e.message, true);
            if (url.includes('/attach')) remember(null);
        }
    }

    function connect(name, title, address) {
        if (ws) { ws.onclose = null; ws.close(); }
        current = { name, title };
        remember(current);
        titleEl.textContent = title;
        terminal().reset();
        const socket = ws = new WebSocket(address);
        socket.binaryType = 'arraybuffer';
        socket.onopen = () => {
            state('');
            fit.fit();
            socket.send(JSON.stringify({ cols: term.cols, rows: term.rows }));
            term.focus();
        };
        socket.onmessage = (e) => term.write(new Uint8Array(e.data));
        socket.onclose = () => { if (ws === socket) state('disconnected — the button on the alert reopens it'); };
    }

    function close() {
        if (ws) { ws.onclose = null; ws.close(); ws = null; }
        box.hidden = true;
        remember(null);
    }

    async function end() {
        if (!current || !confirm(`End the Claude session ${current.title}?`)) return;
        await fetch(`/api/console/${current.name}/end`, { method: 'POST' });
        close();
    }

    document.addEventListener('click', (e) => {
        const start = e.target.closest('[data-console]');
        if (start) { e.preventDefault(); open(start.dataset.console); return; }
        const act = e.target.closest('[data-console-do]');
        if (!act) return;
        if (act.dataset.consoleDo === 'close') close();
        else if (act.dataset.consoleDo === 'end') end();
        else act.textContent = box.classList.toggle('big') ? 'Shrink' : 'Expand';
    });

    const last = remembered();
    if (last) open(`/api/console/${last.name}/attach`, last.title);
})();
