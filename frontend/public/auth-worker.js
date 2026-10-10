self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()))

async function credential(clientId, force) {
  const client = await self.clients.get(clientId)
  if (!client) return ''
  return new Promise(resolve => {
    const channel = new MessageChannel()
    const timeout = setTimeout(() => { channel.port1.close(); resolve('') }, 10000)
    channel.port1.onmessage = event => { clearTimeout(timeout); channel.port1.close(); resolve(event.data.token || '') }
    client.postMessage({ type: 'videodream-token', force }, [channel.port2])
  })
}

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url)
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/api/') || url.pathname.startsWith('/api/auth/') || url.pathname === '/api/health') return
  event.respondWith((async () => {
    if (event.request.headers.has('Authorization')) return fetch(event.request)
    const saved = event.request.clone()
    const headers = new Headers(event.request.headers)
    let token = await credential(event.clientId, false)
    if (!token) return new Response('Authentication required', { status: 401 })
    headers.set('Authorization', `Bearer ${token}`)
    // Media elements use no-cors, which strips the Authorization header.
    let response = await fetch(new Request(event.request, { headers, mode: 'same-origin', cache: 'no-store' }))
    if (response.status === 401) {
      token = await credential(event.clientId, true)
      if (token) {
        headers.set('Authorization', `Bearer ${token}`)
        response = await fetch(new Request(saved, { headers, mode: 'same-origin', cache: 'no-store' }))
      }
    }
    return response
  })())
})
