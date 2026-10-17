const CACHE = "horseprediction-mvp-v57";
const ASSETS = [
  "/",
  "/styles.css",
  "/vendor/nes.min.css",
  "/app.js",
  "/manifest.webmanifest",
  "/fonts/DotGothic16-Regular.ttf",
  "/mascots/turf-scout.svg",
  "/icons/more-horizontal.svg",
  "/icons/search.svg",
  "/icons/download.svg",
  "/icons/upload.svg",
  "/icons/pixel-calendar.svg",
  "/icons/pixel-map.svg",
  "/icons/pixel-target.svg",
  "/icons/pixel-flag.svg",
  "/icons/pixel-sparkles.svg",
  "/icons/pixel-clipboard.svg",
  "/icons/pixel-clock.svg",
  "/icons/pixel-star.svg",
  "/icons/pixel-refresh.svg",
  "/icons/pixel-user.svg",
  "/icons/pixel-trophy.svg",
  "/icons/hackernoon-analytics.svg",
  "/icons/hackernoon-calendar-alt.svg",
  "/icons/hackernoon-chart-line.svg",
  "/icons/hackernoon-clipboard.svg",
  "/icons/hackernoon-clock.svg",
  "/icons/hackernoon-flag.svg",
  "/icons/hackernoon-lightbulb.svg",
  "/icons/hackernoon-trophy.svg",
  "/icons/hackernoon-user.svg"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
