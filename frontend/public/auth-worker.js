// Media requests authenticate with an HttpOnly Cookie. Claim old clients so
// cached workers stop requiring page tokens for media and downloads.
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()))
