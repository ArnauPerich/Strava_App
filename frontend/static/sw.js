/* Pulse – Service Worker
 *
 * Objetivo: que la web sea instalable como PWA y arranque rápido cacheando el
 * "shell" estático (HTML, iconos, manifest). NO toca SocketIO ni las rutas de
 * datos/API: esas siempre van a la red para no servir información obsoleta.
 */
const CACHE = "pulse-shell-v1";
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/apple-touch-icon.png",
];

// Instala y precachea el shell.
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

// Limpia versiones antiguas del cache al activar.
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Solo GET. POST/PUT y demás van directos a la red.
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Ignora otros orígenes (fuentes de Google, CDN de socket.io, etc.).
  if (url.origin !== self.location.origin) return;

  // NO interceptar tiempo real ni APIs: Socket.IO, webhooks y endpoints de datos.
  // Se dejan pasar a la red sin tocar el cache.
  if (
    url.pathname.startsWith("/socket.io") ||
    url.pathname.startsWith("/api") ||
    url.pathname.startsWith("/webhook") ||
    url.pathname.startsWith("/callback") ||
    url.pathname.startsWith("/auth")
  ) {
    return;
  }

  // Navegaciones (el documento HTML): network-first con fallback al cache.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put("/", copy));
          return res;
        })
        .catch(() => caches.match("/"))
    );
    return;
  }

  // Resto de estáticos del mismo origen: cache-first, y refresca en segundo plano.
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
