const CACHE_NAME = "grand-dozen-static-v1";
const STATIC_PREFIX = "/static/";

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith("grand-dozen-static-") && key !== CACHE_NAME)
      .map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || !url.pathname.startsWith(STATIC_PREFIX)) return;

  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      if (response.ok && response.type === "basic") {
        const cache = await caches.open(CACHE_NAME);
        cache.put(request, response.clone());
      }
      return response;
    } catch (error) {
      return await caches.match(request) || Response.error();
    }
  })());
});

// Notifications with the admins' phrases (see bank/push.py).
self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (error) { data = {body: event.data && event.data.text()}; }
  event.waitUntil(self.registration.showNotification(data.title || "Rawasi", {
    body: data.body || "",
    icon: "/static/bank/icons/app-101-192.png",
    badge: "/static/bank/icons/app-101-192.png",
    data: {url: data.url || "/"},
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || "/", self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    const open = windows.find(client => client.url.startsWith(self.location.origin));
    if (open) { await open.focus(); return open.navigate ? open.navigate(url).catch(() => {}) : null; }
    return self.clients.openWindow(url);
  })());
});
