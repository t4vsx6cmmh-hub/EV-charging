/* Service worker: gør appen installerbar og viser senest hentede data, når
   der ikke er forbindelse til appen (fx uden for hjemmenetværket). */
const VERSION = "v2";
const SHELL = `shell-${VERSION}`, DATA = `data-${VERSION}`;
const PRECACHE = ["/", "/vendor/chart.umd.js", "/vendor/inter-var.woff2", "/manifest.webmanifest",
                  "/icons/icon-192.png", "/icons/apple-touch-icon.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== SHELL && k !== DATA).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function networkFirst(req, cacheName, markOffline) {
  try {
    const res = await fetch(req);
    if (res.ok) (await caches.open(cacheName)).put(req, res.clone());
    return res;
  } catch (err) {
    const hit = await caches.match(req, {ignoreSearch: req.mode === "navigate"});
    if (!hit) throw err;
    if (!markOffline) return hit;
    const headers = new Headers(hit.headers);
    headers.set("X-Fra-Cache", "1");  // dashboardet viser "offline"
    return new Response(await hit.blob(), {status: hit.status, headers});
  }
}

async function cacheFirst(req) {
  const hit = await caches.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) (await caches.open(SHELL)).put(req, res.clone());
  return res;
}

self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/")) {
    if (url.pathname.endsWith(".csv")) return;  // eksport hentes altid frisk
    e.respondWith(networkFirst(req, DATA, true));
  } else if (url.pathname.startsWith("/vendor/") || url.pathname.startsWith("/icons/")) {
    e.respondWith(cacheFirst(req));
  } else {
    e.respondWith(networkFirst(req, SHELL, false));
  }
});
