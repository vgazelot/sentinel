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
