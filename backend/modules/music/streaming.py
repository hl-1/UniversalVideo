"""Short lived, authenticated byte relay. Audio is not staged on server disk."""
import json
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from backend.infrastructure import video_network, platform_access
from backend.modules.accounts import service as accounts
from backend.modules.video.cache import media_ttl


def key(uid, token):
    return accounts.redis_prefix() + f'music:source:{uid}:{token}'


def prepare(track):
    from backend.modules.music import sources
    from backend.modules.video import parsing
    if not track.duration:
        raise RuntimeError('时长未知，需校验完整音源。')
    def validate(url, options, source):
        video_network.validate(url)
        with video_network.get(url, referer=track.webpage_url,
                headers=options.get('http_headers', {}) | {'Range': 'bytes=0-0', 'Accept-Encoding': 'identity'}, stream=True, timeout=(4, 5)) as response:
            if response.status_code not in {200, 206} or not response.headers.get('Content-Type', '').lower().startswith(('audio/', 'video/mp4', 'application/octet-stream', 'application/ogg')):
                raise RuntimeError('音源不能渐进播放。')
    url, options, source = sources.resolve(track, parsing._ydl_base_options(track.webpage_url), validate_source=validate)
    ttl = media_ttl([url], ceiling=600)
    if ttl <= 0:
        raise RuntimeError('音源即将过期。')
    token = secrets.token_urlsafe(24)
    user = accounts.current()
    value = {'version': 1, 'uid': user.id, 'sid': user.session_id, 'track_key': f'{track.provider}:{track.identifier}',
             'provider': track.provider, 'mode': 'relay', 'source_url': track.webpage_url, 'media_url': url,
             'headers': options.get('http_headers', {}), 'expected_duration': track.duration,
             'verification': 'pending', 'expires_at': time.time() + ttl}
    accounts.redis_client().set(key(user.id, token), json.dumps(value), ex=ttl)
    accounts.register_resource('playback', token, {'source_url': track.webpage_url})
    return {'playback_url': f'/api/music/playback/{token}/content', 'expires_in': ttl, 'source': source,
            'duration': track.duration, 'full': False, 'verification': 'pending', 'mode': 'relay'}


def content(token, request: Request):
    user = accounts.current()
    raw = accounts.redis_client().get(key(user.id, token))
    if not raw:
        return None
    value = json.loads(raw)
    if value['uid'] != user.id or value['sid'] != user.session_id:
        raise HTTPException(404, '播放来源不存在。')
    platform_access.require(value['source_url'])
    headers = value['headers'] | {'Accept-Encoding': 'identity'}
    range_header = request.headers.get('range')
    if range_header:
        from backend.modules.music.sources import RANGE_PATTERN
        if not RANGE_PATTERN.fullmatch(range_header):
            raise HTTPException(416, '仅支持单段音频范围请求。')
        headers['Range'] = range_header
        if request.headers.get('if-range'):
            headers['If-Range'] = request.headers['if-range']
    response = video_network.get(value['media_url'], referer=value['source_url'], headers=headers, stream=True, timeout=(5,20))
    if response.status_code not in {200,206,416} or response.headers.get('Content-Type','').lower().startswith(('text/', 'application/json')):
        response.close()
        raise HTTPException(502, '音源暂不可用，请重试。')
    outgoing = {'Cache-Control': 'private, no-store', 'X-Accel-Buffering': 'no'}
    outgoing.update({k: response.headers[k] for k in ('Content-Type','Content-Length','Content-Range','Accept-Ranges','ETag','Last-Modified') if k in response.headers})
    if request.method == 'HEAD':
        response.close()
        return Response(status_code=response.status_code, headers=outgoing)
    def chunks():
        checked = 0
        received = 0
        try:
            for chunk in response.iter_content(64*1024):
                if time.monotonic() - checked > 1:
                    if not accounts.redis_client().exists(accounts.session_key(value['sid'])):
                        raise RuntimeError('登录失效。')
                    accounts.active_worker(user.id, source={'url': value['source_url']})
                    checked = time.monotonic()
                received += len(chunk)
                yield chunk
            expected = response.headers.get('Content-Length')
            if expected and received != int(expected):
                raise RuntimeError('音频传输中断。')
        finally:
            response.close()
    return StreamingResponse(chunks(), status_code=response.status_code, headers=outgoing)
