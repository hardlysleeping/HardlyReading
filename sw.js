/* HardlyReading service worker.
 *
 * Its job is narrow: make the app installable and let the shell open without a
 * network. It deliberately does not try to make downloads work offline — the
 * whole app is a proxy for archive.org, so with no network there is nothing to
 * queue.
 *
 * Caching rules:
 *   /api/*      never cached — those are live queue and file responses, and a
 *               stale one would show a queue that is not running.
 *   /vendor/*   cache first. Those files are pinned versions and never change.
 *   everything  network first, cache as a fallback, so an update to
 *   else        index.html is picked up on the next load.
 */

const CACHE = "hardlyreading-v1";

const SHELL = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      // A missing optional file must not fail the whole install.
      .then((c) => Promise.allSettled(SHELL.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  // Only this origin: archive.org covers and thumbnails are not ours to cache.
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith("/api/")) return;   // always live

  if (url.pathname.startsWith("/vendor/")) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return res;
      }))
    );
    return;
  }

  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      // Offline: serve the cached shell so the app opens and says so, rather
      // than showing the browser's network error page.
      .catch(() => caches.match(req).then((hit) => hit || caches.match("/")))
  );
});
