// (n) badge in the tab title, refreshed on every HTMX swap
function updateTitleBadge() {
    const el = document.getElementById('pending-count');
    if (!el) return;
    const n = parseInt(el.textContent, 10) || 0;
    document.title = (n > 0 ? `(${n}) ` : '') + 'Sentinel';
}
document.addEventListener('htmx:afterSwap', updateTitleBadge);
document.addEventListener('DOMContentLoaded', updateTitleBadge);

// --- Polling without jumps -------------------------------------------------------
// The lists (#items, #my-prs, #watchers) are re-fetched every 15-60s. Two things made the page
// jump: htmx moves hx-preserve'd panels out and back in (which resets their inner scroll boxes),
// and a focused textarea inside the panel gets re-focused with a scroll-into-view.
// 1) skip the swap entirely when the server sent exactly the same HTML as last time;
// 2) otherwise save window scroll, focus/caret and inner scroll positions, and restore them after settle.
const POLLED = new Set(['items', 'my-prs', 'watchers']);
const lastHtml = {};
let scrollState = null;

document.addEventListener('htmx:beforeSwap', (e) => {
    const id = e.detail.target.id;
    if (!POLLED.has(id) || e.detail.xhr.status !== 200) return;
    const isPoll = !(e.detail.requestConfig.triggeringEvent && e.detail.requestConfig.triggeringEvent.type === 'click');
    if (isPoll && lastHtml[id] === e.detail.serverResponse) { e.detail.shouldSwap = false; return; }
    lastHtml[id] = e.detail.serverResponse;
    const active = document.activeElement;
    scrollState = {
        x: window.scrollX, y: window.scrollY,
        focus: e.detail.target.contains(active) ? active : null,
        caret: active && active.selectionStart != null ? [active.selectionStart, active.selectionEnd] : null,
        boxes: [...e.detail.target.querySelectorAll('.pr-diff, .md-body, pre')].map((b) => [b, b.scrollTop]).filter((x) => x[1]),
    };
});

document.addEventListener('htmx:afterSettle', (e) => {
    if (!POLLED.has(e.detail.target.id) || !scrollState) return;
    const st = scrollState; scrollState = null;
    for (const [b, top] of st.boxes) if (b.isConnected) b.scrollTop = top;
    if (st.focus && st.focus.isConnected && document.activeElement !== st.focus) {
        st.focus.focus({ preventScroll: true });
        if (st.caret) { try { st.focus.setSelectionRange(st.caret[0], st.caret[1]); } catch (_) { /* not a text field */ } }
    }
    window.scrollTo(st.x, st.y);
});

// Snooze menus (<details>): close when clicking anywhere else
document.addEventListener('click', (e) => {
    document.querySelectorAll('details.menu[open]').forEach((d) => { if (!d.contains(e.target)) d.removeAttribute('open'); });
});

// PR review panel: open/close toggle on the Review button
function togglePrDetails(itemId) {
    const panel = document.getElementById(`pr-details-${itemId}`);
    if (!panel) return;
    if (panel.innerHTML.trim()) {
        const n = (pending[itemId] || []).length;
        if (n && !confirm(`Discard ${n} queued line comment${n > 1 ? 's' : ''}?`)) return;
        panel.innerHTML = ''; delete pending[itemId]; anchors.delete(itemId); return;
    }
    htmx.ajax('GET', `/api/items/${itemId}/pr`, { target: panel, swap: 'innerHTML' });
}

// --- Line comments, GitHub style: click a line number to comment it, shift-click for a range.
// Queued in memory per item (the panel survives the 15s poll thanks to hx-preserve) and sent
// as JSON with Approve / Comment / Request changes.
const pending = {};      // itemId → [{path, side, line, start_line, start_side, body}]
const anchors = new Map(); // itemId → {path, side, line, el} — first click of a shift-click range

function pendingJson(itemId) { return JSON.stringify(pending[itemId] || []); }

function updatePendingCount(itemId) {
    const el = document.getElementById(`pending-count-${itemId}`);
    if (!el) return;
    const n = (pending[itemId] || []).length;
    el.hidden = n === 0;
    el.textContent = n ? `${n} line comment${n > 1 ? 's' : ''} queued` : '';
}

function lineInfo(dl) {
    return { path: dl.closest('.pr-file').dataset.path, side: dl.dataset.side, line: parseInt(dl.dataset.line, 10), el: dl };
}

function clearRangeMarks(panel) {
    panel.querySelectorAll('.dl.is-anchor, .dl.in-range').forEach((d) => d.classList.remove('is-anchor', 'in-range'));
}

function openInlineForm(itemId, target, start) {
    const panel = target.el.closest('.pr-panel');
    panel.querySelectorAll('.inline-form').forEach((f) => f.remove());
    clearRangeMarks(panel);
    if (start) {
        // mark the range in the gutter
        let mark = false;
        for (const d of target.el.parentElement.querySelectorAll('.dl')) {
            if (d === start.el || d === target.el) { mark = !mark; d.classList.add('in-range'); if (!mark) break; continue; }
            if (mark) d.classList.add('in-range');
        }
    }
    target.el.classList.add('is-anchor');
    const form = document.createElement('div');
    form.className = 'inline-form';
    const range = start ? `<div class="range">${target.path} · lines ${start.line}–${target.line}</div>` : `<div class="range">${target.path} · line ${target.line}</div>`;
    form.innerHTML = `${range}<textarea class="field" rows="3" placeholder="Comment on this ${start ? 'range' : 'line'} — Ctrl+Enter to add"></textarea>
        <div class="pr-actions"><button type="button" class="btn btn-primary" data-act="add">Add to review</button><button type="button" class="btn btn-quiet" data-act="cancel">Cancel</button></div>`;
    target.el.insertAdjacentElement('afterend', form);
    const ta = form.querySelector('textarea');
    ta.focus();
    const close = () => { form.remove(); clearRangeMarks(panel); anchors.delete(itemId); };
    const add = () => {
        const body = ta.value.trim();
        if (!body) return;
        const c = { path: target.path, side: target.side, line: target.line, body };
        if (start && start.line !== target.line) { c.start_line = start.line; c.start_side = start.side; }
        (pending[itemId] = pending[itemId] || []).push(c);
        renderPending(itemId, target.el, c);
        close();
        updatePendingCount(itemId);
    };
    form.querySelector('[data-act="add"]').addEventListener('click', add);
    form.querySelector('[data-act="cancel"]').addEventListener('click', close);
    ta.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') add(); if (e.key === 'Escape') close(); });
}

function renderPending(itemId, lineEl, c) {
    const box = document.createElement('div');
    box.className = 'dl-threads';
    const where = c.start_line ? `lines ${c.start_line}–${c.line}` : `line ${c.line}`;
    box.innerHTML = `<div class="thread pending"><div class="comment"><div class="comment-head"><span class="comment-user">Pending</span> <span class="row-meta">${where} · sent with your review</span></div>
        <div class="md-body comment-body"></div><div class="comment-actions"><button type="button" class="btn btn-quiet" data-act="remove">Remove</button></div></div></div>`;
    box.querySelector('.comment-body').textContent = c.body;
    box.querySelector('[data-act="remove"]').addEventListener('click', () => {
        pending[itemId] = (pending[itemId] || []).filter((x) => x !== c);
        box.remove();
        updatePendingCount(itemId);
    });
    // after the line and any existing threads on it
    let after = lineEl;
    while (after.nextElementSibling && after.nextElementSibling.classList.contains('dl-threads')) after = after.nextElementSibling;
    after.insertAdjacentElement('afterend', box);
}

document.addEventListener('click', (e) => {
    const btn = e.target.closest('.dl-add');
    if (!btn || btn.tagName !== 'BUTTON') return;
    const dl = btn.closest('.dl');
    const itemId = parseInt(dl.closest('.pr-panel').dataset.item, 10);
    const info = lineInfo(dl);
    const anchor = anchors.get(itemId);
    if (e.shiftKey && anchor && anchor.path === info.path && anchor.side === info.side && anchor.line !== info.line) {
        const [start, end] = anchor.line < info.line ? [anchor, info] : [info, anchor];
        openInlineForm(itemId, end, start);
        anchors.delete(itemId);
        return;
    }
    anchors.set(itemId, info);
    openInlineForm(itemId, info, null);
});

// A submitted review answers with an HX-Trigger: drop the queue. An error re-renders the panel
// without it, so the queue is put back on the fresh DOM (afterSwap) instead of being lost.
document.addEventListener('reviewSubmitted', (e) => { delete pending[e.detail.id]; anchors.delete(e.detail.id); });

document.addEventListener('htmx:afterSwap', (e) => {
    const m = (e.detail.target.id || '').match(/^pr-details-(\d+)$/);
    if (!m) return;
    const itemId = parseInt(m[1], 10);
    for (const c of pending[itemId] || []) {
        const dl = [...e.detail.target.querySelectorAll('.pr-file')]
            .filter((f) => f.dataset.path === c.path)
            .flatMap((f) => [...f.querySelectorAll(`.dl[data-side="${c.side}"][data-line="${c.line}"]`)])[0];
        if (dl) renderPending(itemId, dl, c);
    }
    updatePendingCount(itemId);
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
    const panel = a.closest('.pr-panel');
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
