// Sentinel inbox: a list on the left, the selected item on the right, the keyboard to go through it.

// (n) in the tab title and the sidebar badges, refreshed on every swap: the sidebar is rendered once
// at page load while the list is re-fetched every 20 s.
function syncBadge(badgeId, sourceId) {
    const badge = document.getElementById(badgeId);
    const source = document.getElementById(sourceId);
    if (!badge || !source) return null;
    const n = parseInt(source.textContent, 10) || 0;
    badge.textContent = n;
    badge.hidden = n === 0;
    return n;
}
let shown = null;
function updateBadges() {
    const attention = syncBadge('nav-attention', 'attention-count');
    syncBadge('nav-sessions', 'sessions-count');
    syncBadge('nav-cycle', 'cycle-count');
    syncBadge('nav-watch', 'watch-count');
    syncBadge('nav-lookout', 'lookout-count');
    if (attention === null) return;
    document.title = (attention > 0 ? `(${attention}) ` : '') + 'Sentinel';
    const badge = document.getElementById('nav-attention');
    if (shown !== null && attention !== shown && badge) {
        badge.classList.remove('pop'); void badge.offsetWidth; badge.classList.add('pop');
    }
    shown = attention;
}

// the sidebar light: lit only while a source fails, refreshed by every list poll
function renderLight() {
    const light = document.getElementById('light');
    const src = document.getElementById('status-src');
    if (!light || !src) return;
    light.hidden = !src.dataset.text;
    light.querySelector('span').textContent = src.dataset.text;
}

// --- Selection -----------------------------------------------------------------------------
// the rows you can see: a folded group (Sessions' tickets to start, a folded big rock) or a filtered row is skipped
const rows = () => [...document.querySelectorAll('#rows .it')].filter((r) => !r.closest('details:not([open]), .folded') && r.getClientRects().length);
let selected = { key: null, index: 0 };

function select(row, { load = true } = {}) {
    if (!row) return;
    rows().forEach((r) => r.classList.toggle('sel', r === row));
    selected = { key: row.dataset.key, index: rows().indexOf(row) };
    row.scrollIntoView({ block: 'nearest' });
    document.dispatchEvent(new CustomEvent('row-selected', { detail: { key: row.dataset.key } }));
    if (!load || !row.dataset.detail) return;
    const detail = document.getElementById('detail');
    // the pane answers at once; GitHub fills it in a moment
    detail.innerHTML = '<div class="d-loading"><i class="sk sk-kicker"></i><i class="sk sk-title"></i><i class="sk sk-line"></i><i class="sk sk-block"></i></div>';
    htmx.ajax('GET', row.dataset.detail, { target: '#detail', swap: 'innerHTML' });
}

// moving fast through the list: only the answer for the row still selected lands in the pane
document.addEventListener('htmx:beforeSwap', (e) => {
    if (e.detail.target.id !== 'detail' || e.detail.requestConfig.verb !== 'get') return;
    const row = rows().find((r) => r.dataset.key === selected.key);
    if (row && row.dataset.detail !== e.detail.requestConfig.path) e.detail.shouldSwap = false;
});

// after a poll: the same row stays selected; after an action: the row now at its place (the next one)
function reselect({ next = false } = {}) {
    const all = rows();
    if (!all.length) { if (next) clearDetail(); return; }
    const same = all.find((r) => r.dataset.key === selected.key);
    if (same && !next) { select(same, { load: false }); return; }
    if (same || next) select(same || all[Math.min(selected.index, all.length - 1)]);
}

function clearDetail() {
    const detail = document.getElementById('detail');
    if (detail) detail.innerHTML = '<div class="d-empty"><p>All done</p></div>';
}

function move(step) {
    const all = rows();
    if (!all.length) return;
    const at = all.findIndex((r) => r.dataset.key === selected.key);
    select(all[Math.max(0, Math.min(all.length - 1, (at < 0 ? selected.index - (step > 0 ? 1 : 0) : at) + step))]);
}

function openSelected() {
    const link = document.querySelector('#detail [data-open]');
    const row = rows().find((r) => r.dataset.key === selected.key);
    const url = (link && link.href) || (row && row.dataset.url);
    if (url) window.open(url, '_blank', 'noopener');
}

document.addEventListener('click', (e) => {
    if (e.target.closest('[data-unselect]')) {  // a button filling the pane on its own (Sessions' New)
        rows().forEach((r) => r.classList.remove('sel'));
        selected = { key: null, index: selected.index };
        return;
    }
    const row = e.target.closest('#rows .it');
    if (!row) return;
    if (row.dataset.detail || row.dataset.term) select(row);  // a console (Sessions) is shown by its board, not the pane
    else if (row.dataset.url) window.open(row.dataset.url, '_blank', 'noopener');
});

document.addEventListener('keydown', (e) => {
    if (e.target.closest('#console, .console-term')) return;  // every key, Escape included, belongs to Claude there
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.closest('input, textarea, select')) {
        if (e.key === 'Escape') e.target.blur();
        return;
    }
    if (e.key === 'Enter' && e.target.closest('button, a, summary')) return;  // a focused control keeps its Enter
    const press = (k) => { const b = document.querySelector(`#detail [data-key="${k}"]`); if (b) b.click(); };
    const keys = { j: () => move(1), ArrowDown: () => move(1), k: () => move(-1), ArrowUp: () => move(-1),
                   Enter: openSelected, e: () => press('e'), l: () => press('l') };
    if (!keys[e.key] || !document.getElementById('rows')) return;
    e.preventDefault();
    keys[e.key]();
});

// --- After an action (Done, Later, a review sent): a short note, then the next row ----------
let toastTimer = null;
function toast(text, bad) {
    const el = document.getElementById('toast');
    if (!el || !text) return;
    el.textContent = text;
    el.className = bad ? 'bad' : '';
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 8000 : 3500);
}
// a session just started: its row shows on the list's next poll, selected
document.addEventListener('select-key', (e) => {
    selected = { key: e.detail.key, index: selected.index };
    htmx.trigger('#list', 'refresh');
});
document.addEventListener('acted', (e) => {
    toast(e.detail.error || e.detail.notice || (rows().length ? '' : 'Inbox zero'), !!e.detail.error);
    reselect({ next: true });
});

// a Done, a Later or a review sent from the pane: the row slides out while the request runs
document.addEventListener('htmx:beforeRequest', (e) => {
    const cfg = e.detail.requestConfig;
    if (cfg.verb !== 'post' || !e.detail.elt.closest('#detail') || /^\/api\/(agent|sessions)/.test(cfg.path)) return;
    const row = rows().find((r) => r.dataset.key === selected.key);
    if (row) row.classList.add('leaving');
});
document.addEventListener('htmx:afterSwap', (e) => {
    // an error comes back in the pane: the row stays
    if (e.detail.target.id === 'detail') rows().forEach((r) => r.classList.remove('leaving'));
});

// --- Polling without jumps -------------------------------------------------------------------
// #list, #cycle, #watch, #lookout and #watchers are re-fetched every 20 s - 5 min: skip the swap when the HTML is the
// same, otherwise keep the list scroll, the open <details id> and the selection.
const POLLED = new Set(['list', 'cycle', 'watch', 'lookout', 'watchers']);
const lastHtml = {};
let kept = null;

document.addEventListener('htmx:beforeSwap', (e) => {
    const id = e.detail.target.id;
    if (!POLLED.has(id) || e.detail.xhr.status !== 200) return;
    if (lastHtml[id] === e.detail.serverResponse) { e.detail.shouldSwap = false; return; }
    lastHtml[id] = e.detail.serverResponse;
    const box = e.detail.target.querySelector('.scroll');
    kept = { id, top: box ? box.scrollTop : 0, keys: new Set(rows().map((r) => r.dataset.key)),
             open: [...e.detail.target.querySelectorAll('details[open][id]')].map((d) => d.id) };
});

document.addEventListener('htmx:afterSettle', (e) => {
    updateBadges();
    if (!kept || kept.id !== e.detail.target.id) return;
    const st = kept; kept = null;
    for (const id of st.open) { const d = document.getElementById(id); if (d) d.open = true; }
    const box = e.detail.target.querySelector('.scroll');
    if (box) box.scrollTop = st.top;
    if (st.id !== 'list') return;
    renderLight();
    // what arrived since the last look lights up once
    if (st.keys.size) rows().filter((r) => !st.keys.has(r.dataset.key)).forEach((r) => {
        r.classList.add('fresh');
        setTimeout(() => r.classList.remove('fresh'), 1900);
    });
    reselect();
});

document.addEventListener('DOMContentLoaded', () => {
    updateBadges();
    renderLight();
    const all = rows();
    // a row the page marked already (the board's active terminal), else the first one that opens
    const first = all.find((r) => r.classList.contains('sel')) || all.find((r) => r.dataset.detail || r.dataset.term);
    if (first) select(first);
});

// --- `path:line` refs in Claude's review: jump to that line in the diff below ------
// md.link_file_refs() turned them into <a class="fileref" data-path data-line [data-end]>.
// The file may be missing from the rendered diff (50 files / 600 lines limits, or a line outside
// a hunk): then open the blob on GitHub instead.
function findDiffFile(panel, path) {
    const files = [...panel.querySelectorAll('.pr-file')];
    const base = path.split('/').pop();
    return files.find((f) => f.dataset.path === path)
        || files.find((f) => f.dataset.path.endsWith('/' + path))
        || files.find((f) => f.dataset.path.split('/').pop() === base);
}

function revealDiffLine(file, from, to) {
    const pick = (sel) => [...file.querySelectorAll(sel)]
        .filter((d) => { const n = parseInt(d.dataset.line, 10); return n >= from && n <= to; });
    const marks = pick('.dl[data-side="RIGHT"]').length ? pick('.dl[data-side="RIGHT"]') : pick('.dl[data-line]');
    if (!marks.length) return false;
    const box = marks[0].closest('.pr-diff');
    file.scrollIntoView({ block: 'center', behavior: 'smooth' });
    box.scrollTop += marks[0].getBoundingClientRect().top - box.getBoundingClientRect().top - box.clientHeight / 3;
    marks.forEach((d) => d.classList.add('is-target'));
    setTimeout(() => marks.forEach((d) => d.classList.remove('is-target')), 3000);
    return true;
}

document.addEventListener('click', (e) => {
    const a = e.target.closest('a.fileref');
    if (!a) return;
    e.preventDefault();
    const panel = a.closest('#detail')?.querySelector('.d-body');
    if (!panel) return;
    const path = a.dataset.path;
    const from = parseInt(a.dataset.line, 10);
    const to = parseInt(a.dataset.end || a.dataset.line, 10);
    const file = findDiffFile(panel, path);
    if (file && revealDiffLine(file, from, to)) return;
    if (panel.dataset.blob) {
        window.open(`${panel.dataset.blob}/${file ? file.dataset.path : path}#L${from}`, '_blank', 'noopener');
    }
});

// --- Web Push -----------------------------------------------------------------
function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

function pushStatus(msg) {
    const el = document.getElementById('push-status');
    if (el) el.textContent = msg;
}

async function subscribePush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        pushStatus('Web Push is not supported by this browser.');
        return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') { pushStatus('Permission denied.'); return; }
    const reg = await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    const { key } = await (await fetch('/api/push/key')).json();
    if (!key) { pushStatus('VAPID key missing on the server.'); return; }
    const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
    });
    await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub.toJSON()),
    });
    pushStatus('Subscribed ✔ — try the "Send test notification" button.');
}

async function browserSubscription() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
    const reg = await navigator.serviceWorker.getRegistration('/sw.js');
    return reg ? reg.pushManager.getSubscription() : null;
}

// Re-registers this browser's current subscription (idempotent): the one stored server-side may be
// stale after a permission reset or a push-service key rotation, while FCM keeps returning 201.
async function ensureRegistered(sub) {
    await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub.toJSON()),
    });
}

async function syncPushState() {
    const sub = await browserSubscription();
    if (!sub) {
        pushStatus(Notification.permission === 'denied'
            ? 'This browser is not subscribed — notifications are blocked in the browser site settings.'
            : 'This browser is not subscribed.');
        return;
    }
    await ensureRegistered(sub);
    pushStatus('This browser is subscribed ✔');
}

async function unsubscribePush() {
    const sub = await browserSubscription();
    if (!sub) { pushStatus('No subscription on this browser.'); return; }
    await fetch('/api/push/subscribe', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: sub.endpoint }),
    });
    await sub.unsubscribe();
    pushStatus('Unsubscribed.');
}

async function testPush() {
    const sub = await browserSubscription();
    if (!sub) { pushStatus('This browser is not subscribed — click "Subscribe this browser" first.'); return; }
    await ensureRegistered(sub);
    const res = await (await fetch('/api/push/test', { method: 'POST' })).json();
    pushStatus(`Push sent to ${res.sent} subscription(s), this browser included. Nothing shown? `
        + 'Check the OS notification settings for your browser (macOS: System Settings → Notifications → Google Chrome → Allow).');
}

document.addEventListener('DOMContentLoaded', () => {
    const sub = document.getElementById('btn-subscribe');
    const unsub = document.getElementById('btn-unsubscribe');
    const test = document.getElementById('btn-test');
    if (sub) sub.addEventListener('click', () => subscribePush().catch((e) => pushStatus('Error: ' + e)));
    if (unsub) unsub.addEventListener('click', () => unsubscribePush().catch((e) => pushStatus('Error: ' + e)));
    if (test) test.addEventListener('click', () => testPush().catch((e) => pushStatus('Error: ' + e)));
    if (sub) syncPushState().catch((e) => pushStatus('Error: ' + e));
});
