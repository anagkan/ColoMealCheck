/* Service worker for the door screen.
 *
 * The offline queue in kiosk.js already protects a meal from a network blip,
 * but it only works while the page is *already open*. The hole it leaves is the
 * one the README names: if the server is away and the kiosk laptop reboots, the
 * browser has nothing to load and the door screen cannot start at all. This
 * closes that — the shell is served from the cache, the queue takes the scans,
 * and everything syncs when the server comes back.
 *
 * Two rules govern everything below.
 *
 * 1. NETWORK FIRST, ALWAYS. The cache is a fallback for an outage, never the
 *    normal path. A kiosk quietly running last month's kiosk.js — with last
 *    month's rules on the screen in front of a member — is a worse failure than
 *    the one this file exists to prevent, and it is a silent one. Every
 *    successful fetch refreshes the cache behind it, so the copy on disk is
 *    whatever the server last served rather than whatever was there on install.
 *
 * 2. TOUCH NOTHING BUT THE SHELL. Scans, guest meals, alumni meals, member
 *    search, photos, the admin screens and every POST go straight to the
 *    network with no interception. The queue in kiosk.js owns what happens to a
 *    request the server cannot take; a service worker that also had opinions
 *    about them would be a second, invisible answer to the same question.
 */

const CACHE = "colomeal-kiosk-v1";

/* The door screen and everything it paints with. Deliberately short: this is a
 * boot path, not an offline copy of the app. /enroll is absent because it can
 * do nothing without the server — it searches members, uploads a photo and
 * writes a credential — and a page that loads only to fail every action is
 * worse than one that plainly does not load. */
const SHELL = [
  "/",
  "/static/kiosk.css",
  "/static/kiosk.js",
  "/static/crest.png",
  "/manifest.webmanifest",
];

/* How long to wait for the server before falling back to the cached copy.
 *
 * A refused connection rejects immediately and never reaches this. The case it
 * is here for is the host being unreachable rather than down — an unplugged
 * switch, the wrong SSID — where the TCP connect sits there for the better part
 * of a minute. Without a bound on it, the kiosk boots to a white screen for
 * that whole time, which looks exactly like a broken kiosk to whoever is
 * standing at the door. */
const NETWORK_TIMEOUT_MS = 3000;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      // A shell that cannot be pre-cached must not block activation: the worker
      // is still useful, and the first successful fetch of each file fills the
      // cache anyway.
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names.filter((name) => name !== CACHE).map((name) => caches.delete(name))
        )
      )
      // Take over the page that registered us rather than waiting for the next
      // reload. A kiosk is reloaded roughly never.
      .then(() => self.clients.claim())
  );
});

function isShellRequest(request, url) {
  if (request.method !== "GET" || url.origin !== self.location.origin) return false;
  // A navigation to "/" with a query string on it is still the door screen.
  if (request.mode === "navigate") return url.pathname === "/";
  return SHELL.includes(url.pathname);
}

/* The cache key for a request, so "/" and "/?anything" are one entry. */
function cacheKey(request, url) {
  return request.mode === "navigate" ? "/" : url.pathname;
}

/* Tell the page it is looking at a cached copy of itself.
 *
 * The kiosk template seeds the meal banner server-side, so the cached HTML
 * carries whatever meal was being served when it was cached. Replayed at
 * dinner, a shell cached at breakfast would announce "Now serving Breakfast"
 * with a live countdown under it — confidently, and wrongly. The flag is how
 * kiosk.js knows to throw that seed away and say it does not know until
 * /api/status answers.
 *
 * Injected into the HTML rather than sent as a header or a postMessage because
 * this is the one channel that is certain to arrive *before* the page's own
 * scripts run: a document cannot read its own response headers, and a message
 * races the very code it is meant to inform.
 */
async function markAsCached(response) {
  const html = await response.text();
  const marker = "<script>window.KIOSK_FROM_CACHE = true;</script>";
  const anchor = html.includes("</head>") ? "</head>" : "</body>";
  if (!html.includes(anchor)) return response;
  return new Response(html.replace(anchor, marker + anchor), {
    status: 200,
    statusText: "OK",
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

async function fromCache(request, url) {
  const cached = await caches.match(cacheKey(request, url));
  if (!cached) return null;
  return request.mode === "navigate" ? markAsCached(cached) : cached;
}

async function networkFirst(request, url) {
  const key = cacheKey(request, url);

  // Kept out of the race below so a response that arrives after the timeout
  // still refreshes the cache — the next boot then has the newer copy even
  // though this one fell back.
  const network = fetch(request).then(async (response) => {
    if (response && response.ok) {
      const cache = await caches.open(CACHE);
      await cache.put(key, response.clone());
    }
    return response;
  });

  // The race below abandons this promise when the timeout wins. Without a
  // handler on it, a rejection arriving after that point is an unhandled one,
  // which in a service worker means a console error on every offline load.
  network.catch(() => undefined);

  const timeout = new Promise((resolve) =>
    setTimeout(() => resolve(null), NETWORK_TIMEOUT_MS)
  );

  try {
    const response = await Promise.race([network, timeout]);
    if (response) return response;
  } catch (err) {
    /* unreachable or refused — fall through to the cache */
  }
  return (await fromCache(request, url)) || network;
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (!isShellRequest(event.request, url)) return; // not ours; browser handles it
  event.respondWith(networkFirst(event.request, url));
});
