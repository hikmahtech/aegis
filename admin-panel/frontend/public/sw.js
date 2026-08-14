// Service worker for AEGIS — deliberately does NOTHING but exist.
//
// Chrome requires a registered service worker before it will offer "Install
// app" on Android. It does NOT require a fetch handler, so this file has none.
//
// ⚠️ DO NOT ADD A `fetch` HANDLER THAT CACHES NAVIGATION REQUESTS.
// AEGIS is served behind an authenticating proxy (Cloudflare Access), and in
// that setup a caching SW turns an expired session into a bricked app: the SW
// answers the top-level navigation from cache, the shell boots, its API calls
// hit the proxy's cross-origin redirect to the login page, that redirect fails
// silently inside fetch(), and the user is stuck on a dead screen with no way
// to re-authenticate short of uninstalling the app. Letting navigations go to
// the network means the browser follows the redirect normally, the user logs
// in, and comes back.
//
// If offline shell caching is ever genuinely wanted, it MUST pass navigations
// straight through, e.g.:
//   if (event.request.mode === 'navigate') return;  // never cache navigations
//
// ponytail: no precache, no versioning, no Workbox — none of it is needed for
// installability. Add them only when there is a real offline requirement.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
