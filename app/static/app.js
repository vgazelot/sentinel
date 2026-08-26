// (n) badge in the tab title, refreshed on every HTMX swap
function updateTitleBadge() {
    const el = document.getElementById('pending-count');
    if (!el) return;
    const n = parseInt(el.textContent, 10) || 0;
    document.title = (n > 0 ? `(${n}) ` : '') + 'Sentinel';
}
document.addEventListener('htmx:afterSwap', updateTitleBadge);
document.addEventListener('DOMContentLoaded', updateTitleBadge);

// PR review panel: open/close toggle on the Review button
function togglePrDetails(itemId) {
    const panel = document.getElementById(`pr-details-${itemId}`);
    if (!panel) return;
    if (panel.innerHTML.trim()) { panel.innerHTML = ''; return; }
    htmx.ajax('GET', `/api/items/${itemId}/pr`, { target: panel, swap: 'innerHTML' });
}

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
