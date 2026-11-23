const CACHE_PREFIX = "nanobot-static-";  // kept: installed phones already cache under it
// v3: navigations are cached per shell (see shellKey); v2 could hold the
// companion page under "/" or the console under the companion's URL.
const CACHE_NAME = `${CACHE_PREFIX}v3`;
const ICON_CACHE_PREFIX = "nanobot-icons-";
const ICON_CACHE_NAME = `${ICON_CACHE_PREFIX}v1`;
const ICON_CACHE_LIMIT = 128;
const ASSET_MANIFEST_PATH = "/asset-manifest.json";
const PRECACHE = ["/companion.html", "/companion-manifest.json", ASSET_MANIFEST_PATH];
// The companion page is a separate HTML entry from the console's index.html.
const COMPANION_SHELL = "/companion.html";

/** Cache key of the HTML shell that serves a navigation to `path`. */
function shellKey(path) {
  return path === COMPANION_SHELL ? COMPANION_SHELL : "/";
}
const NETWORK_FIRST_STATIC_PATHS = new Set([
  "/companion-manifest.json",
  ASSET_MANIFEST_PATH,
  "/brand/companion_apple_touch.png",
  "/brand/companion_favicon_64.png",
  "/brand/companion_icon_192.png",
  "/brand/companion_icon_512.png",
  "/brand/companion_icon_maskable.png",
  "/brand/companion_mark.svg",
  // The built-in companion pet (webui/src/companion/Pet.tsx, BUILTIN_PETS).
  "/pets/orange-cat/spritesheet.webp",
  "/pets/orange-cat/actions.webp",
]);

function responseMayBeCached(response) {
  const cacheControl = response.headers?.get("Cache-Control") ?? "";
  return response.ok && !/(?:^|,)\s*(?:private|no-cache|no-store)\b/i.test(cacheControl);
}

function isPublicIconRequest(request, url) {
  if (request.destination !== "image" || url.protocol !== "https:") return false;
  const host = url.hostname.toLowerCase();
  const path = url.pathname.toLowerCase();
  if (path.endsWith("/favicon.ico")) return true;
  if (host === "favicon.im") return true;
  if (host === "icons.duckduckgo.com") return path.startsWith("/ip3/");
  return (host === "google.com" || host === "www.google.com")
    && path === "/s2/favicons";
}

function publicIconResponseMayBeCached(response) {
  // Cross-origin <img> requests produce opaque responses, so their status and
  // cache headers cannot be inspected. Only the public icon URLs above reach
  // this path; regular remote images continue to use the browser directly.
  return response.type === "opaque" || responseMayBeCached(response);
}

async function trimIconCache(cache) {
  const keys = await cache.keys();
  const overflow = keys.length - ICON_CACHE_LIMIT;
  if (overflow <= 0) return;
  await Promise.all(keys.slice(0, overflow).map((request) => cache.delete(request)));
}

async function cacheFirstPublicIcon(request) {
  let cache;
  try {
    cache = await caches.open(ICON_CACHE_NAME);
    const cached = await cache.match(request);
    if (cached) return cached;
  } catch {
    return fetch(request);
  }

  const response = await fetch(request);
  if (!publicIconResponseMayBeCached(response)) return response;
  try {
    await cache.put(request, response.clone());
    await trimIconCache(cache);
  } catch {
    // Cache storage is optional; a successful network response still wins.
  }
  return response;
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      await cache.addAll(PRECACHE);
      await warmCompanionChunks(cache);
    })
  );
  self.skipWaiting();
});

const DEFERRED_HEAVY_CHUNK_RE = /(?:^|\/)assets\/(?:katex-|markdown-|streamdown-|syntax-highlight-|ViewportCodeRows-)/;

// Pre-warm Companion's code-split screen/sheet chunks, default locales, and web
// workers in the background so switching tabs or opening Settings works offline
// right after the first visit without inflating the entry bundle.
async function warmCompanionChunks(cache) {
  const response = await cache.match(ASSET_MANIFEST_PATH);
  if (!response) return;
  let manifest;
  try {
    manifest = await response.json();
  } catch {
    return;
  }
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) return;

  const visited = new Set();
  const urls = new Set();
  const visit = (key) => {
    if (!key || visited.has(key)) return;
    visited.add(key);
    const entry = manifest[key];
    if (!entry || typeof entry !== "object") return;
    for (const file of [entry.file, ...(entry.css ?? [])]) {
      if (typeof file !== "string" || DEFERRED_HEAVY_CHUNK_RE.test(file)) continue;
      const url = new URL(file, self.location.origin);
      if (url.origin === self.location.origin) urls.add(url.pathname + url.search);
    }
    for (const dep of entry.imports ?? []) {
      visit(dep);
    }
  };

  for (const key of Object.keys(manifest)) {
    if (
      key === "companion.html"
      || key.startsWith("src/companion/")
      || key.startsWith("_worker_")
      || key === "src/i18n/locales/zh-CN/common.json"
      || key === "src/i18n/locales/en/common.json"
    ) {
      visit(key);
    }
  }

  await Promise.allSettled(
    Array.from(urls).map(async (assetPath) => {
      const existing = await cache.match(assetPath);
      if (existing) return;
      const res = await fetch(assetPath);
      if (responseMayBeCached(res)) {
        await cache.put(assetPath, res);
      }
    })
  );
}

// Collect same-origin paths referenced by the given HTML document.
function referencedAssetPaths(html) {
  const refs = new Set();
  const re = /(?:src|href)="(\/[^"]*)"/g;
  let match;
  while ((match = re.exec(html))) {
    const url = new URL(match[1], self.location.origin);
    if (url.origin === self.location.origin) refs.add(url.pathname + url.search);
  }
  return refs;
}

// Vite's build manifest contains every emitted entry, static dependency, and
// lazy chunk. The HTML alone only references the entry chunk, so pruning from
// its tags can delete a current build's not-yet-requested dynamic imports.
async function manifestedAssetPaths(cache) {
  const response = await cache.match(ASSET_MANIFEST_PATH);
  if (!response) return new Set();
  try {
    const manifest = await response.json();
    const refs = new Set();
    for (const entry of Object.values(manifest)) {
      if (!entry || typeof entry !== "object") continue;
      for (const file of [entry.file, ...(entry.css ?? []), ...(entry.assets ?? [])]) {
        if (typeof file !== "string") continue;
        const url = new URL(file, self.location.origin);
        if (url.origin === self.location.origin) refs.add(url.pathname + url.search);
      }
    }
    return refs;
  } catch {
    return new Set();
  }
}

async function refreshAssetManifest(cache) {
  const response = await fetch(ASSET_MANIFEST_PATH, { cache: "no-store" });
  if (!response.ok) return false;
  try {
    const manifest = await response.clone().json();
    if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) return false;
  } catch {
    return false;
  }
  await cache.put(ASSET_MANIFEST_PATH, response);
  return true;
}

// Drop cached entries that the current index.html no longer references.
// CACHE_NAME is stable across deployments, so without this, hashed assets from
// previous builds would pile up in the same cache forever. The cached
// index.html is the latest one this client saw (navigation is network-first
// and overwrites it on every successful visit), so pruning against it keeps
// the offline shell consistent with the last loaded build.
async function pruneStaleEntries() {
  const cache = await caches.open(CACHE_NAME);
  const [cachedIndex, cachedCompanion] = await Promise.all([
    cache.match("/"),
    cache.match(COMPANION_SHELL),
  ]);
  if (!cachedIndex && !cachedCompanion) return;
  const refs = new Set();
  if (cachedIndex) {
    for (const path of referencedAssetPaths(await cachedIndex.text())) refs.add(path);
  }
  if (cachedCompanion) {
    for (const path of referencedAssetPaths(await cachedCompanion.text())) refs.add(path);
  }
  for (const path of await manifestedAssetPaths(cache)) refs.add(path);
  const keys = await cache.keys();
  await Promise.all(
    keys.map(async (request) => {
      const url = new URL(request.url);
      if (
        url.pathname === "/"
        || url.pathname === COMPANION_SHELL
        || url.pathname === "/manifest.json"
        || url.pathname === "/companion-manifest.json"
        || url.pathname === ASSET_MANIFEST_PATH
        || NETWORK_FIRST_STATIC_PATHS.has(url.pathname)
      ) return;
      if (refs.has(url.pathname + url.search)) return;
      await cache.delete(request);
    })
  );
}

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) =>
              (k.startsWith(CACHE_PREFIX) && k !== CACHE_NAME)
              || (k.startsWith(ICON_CACHE_PREFIX) && k !== ICON_CACHE_NAME)
            )
            .map((k) => caches.delete(k))
        )
      )
      .then(() => pruneStaleEntries())
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Requests are handed to fetch() as-is (never reconstructed), so their
  // credentials mode is preserved and gateway auth cookies flow through on
  // every path we touch. WebSocket upgrades are never dispatched to a service
  // worker's fetch handler, so the WS endpoint cannot be cached.
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    if (isPublicIconRequest(request, url)) {
      event.respondWith(cacheFirstPublicIcon(request));
    }
    return;
  }

  const path = url.pathname;

  // Static assets: cache-first. Only files under /assets/ carry content hashes
  // (the gateway serves them immutable); brand icons, the favicon and other
  // un-hashed files can change between releases and stay on the network-first
  // path below so updates reach installed clients.
  if (path.startsWith("/assets/")) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request).then((response) => {
          if (responseMayBeCached(response)) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((c) => c.put(request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // Cache only explicit public files and browser navigations. Dynamic routes
  // are deliberately passed through because token_issue_path and extension
  // endpoints are configurable and cannot be safely identified by prefixes.
  const isNavigation = request.mode === "navigate";
  if (!isNavigation && !NETWORK_FIRST_STATIC_PATHS.has(path)) return;

  // App shell and public files: network-first with an offline fallback.
  const networkResponse = fetch(request);
  event.waitUntil(
    networkResponse
      .then(async (response) => {
        if (!responseMayBeCached(response)) return;
        // Clone before the first await. The original response is also handed
        // to respondWith(), which may lock its body as soon as this callback
        // yields to the event loop.
        const cachedResponse = response.clone();
        const cache = await caches.open(CACHE_NAME);
        await cache.put(isNavigation ? shellKey(path) : request, cachedResponse);
        // Refresh the complete build graph before pruning. A deployment can
        // change index.html without changing sw.js, so this cannot rely only
        // on the manifest cached when the worker was installed.
        if (isNavigation || path === "/" || path === COMPANION_SHELL) {
          if (await refreshAssetManifest(cache)) {
            await warmCompanionChunks(cache);
            await pruneStaleEntries();
          }
        }
      })
      .catch(() => undefined)
  );
  event.respondWith(
    networkResponse
      .catch(() => {
        // Offline: serve the app shell for navigations (deep links resolve
        // client-side), the last cached copy for everything else.
        if (request.mode === "navigate") return caches.match(shellKey(path));
        return caches.match(request);
      })
  );
});

// ---------------------------------------------------------------------------
// Companion mode: Web Push. The gateway pushes when the companion has news the
// user did not just ask for (scheduled tasks, triggers, approvals waiting).
// ---------------------------------------------------------------------------
const COMPANION_URL = "/companion.html";

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "伙伴", {
      body: data.body || "",
      tag: data.tag || undefined,
      icon: "/brand/companion_icon_192.png",
      badge: "/brand/companion_icon_192.png",
      data: { url: data.url || COMPANION_URL },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  let target;
  try {
    const candidate = new URL(event.notification.data?.url || COMPANION_URL, self.location.origin);
    target = candidate.origin === self.location.origin ? candidate : new URL(COMPANION_URL, self.location.origin);
  } catch {
    target = new URL(COMPANION_URL, self.location.origin);
  }
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((windows) => {
        // Prefer an open companion window over opening another one.
        const existing = windows.find((client) => {
          try {
            const clientUrl = new URL(client.url);
            return clientUrl.origin === target.origin && clientUrl.pathname === target.pathname;
          } catch {
            return false;
          }
        });
        if (existing) return existing.focus();
        return self.clients.openWindow(target.href);
      })
  );
});
