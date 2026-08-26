self.addEventListener('push', (event) => {
    let data = {};
    try { data = event.data ? event.data.json() : {}; } catch (e) { /* payload non-JSON */ }
    event.waitUntil(self.registration.showNotification(data.title || 'Sentinel', {
        body: data.body || '',
        tag: data.tag || undefined,
        data: { url: data.url || '/' },
    }));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const url = (event.notification.data && event.notification.data.url) || '/';
    event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
        for (const w of windows) {
            if (w.url.startsWith(self.location.origin)) { w.navigate(url); return w.focus(); }
        }
        return clients.openWindow(url);
    }));
});

// browser rotated the subscription (key/endpoint change): resubscribe and re-register server-side
self.addEventListener('pushsubscriptionchange', (event) => {
    const key = event.oldSubscription && event.oldSubscription.options.applicationServerKey;
    event.waitUntil(self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key })
        .then((sub) => fetch('/api/push/subscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(sub.toJSON()),
        })));
});
