"""Shared, identity-checked lyrics lookup and line classification."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import threading
import time

import requests
from fastapi import HTTPException

from backend.modules.accounts import service as accounts
from backend.modules.music import library
from backend.modules.music import sources as music
from backend.infrastructure import network


MAX_BYTES = 512000
_remote_lock = threading.Lock()
_next_remote_at = 0.0
_CREDITS = re.compile(r'^(?:作\s*词|作\s*曲|词曲|编\s*曲|制作人?|制作|监制|混音|母带|录音|录制|和声(?:编写)?|配唱(?:制作人)?|吉他|贝斯|鼓|键盘|出品(?:人|公司)?|发行|版权|词|曲|编|翻译|译|上传(?:者)?|歌手|专辑|歌曲|词作者|曲作者|lyrics?(?:\s+by)?|music\s+by|compos(?:er|ed\s+by)|written\s+by|arrang(?:er|ed\s+by)|produc(?:er|ed\s+by)|mix(?:ed\s+by|ing)?|master(?:ed\s+by|ing)?|copyright|vocal(?:s)?\s+by|작사|작곡|편곡|作詞|作曲|編曲)\s*[:：]', re.I)
_DECLARATION = re.compile(r'^(?:纯音乐[，,。\s]*(?:请欣赏)?|純音樂[，,。\s]*(?:請欣賞)?|instrumental(?:\s+music)?)[.!。\s]*$', re.I)


def classify_lines(text):
    result = {"credits": [], "body": [], "declarations": []}
    for raw in text.splitlines():
        line = re.sub(r'\[[^\]]*\]', '', raw).strip()
        if not line:
            continue
        if _CREDITS.match(line):
            result['credits'].append(line)
        elif _DECLARATION.fullmatch(line):
            result['declarations'].append(line)
        else:
            result['body'].append(line)
    return result


def lrclib(title, artist, duration, album=''):
    """Shared transport: errors never become negative-cache entries."""
    global _next_remote_at
    if not artist.strip() or not duration or not 1 <= duration <= 3600:
        return {"text": "", "source": "", "instrumental": None, "document_complete": False}
    with _remote_lock:
        delay = _next_remote_at - time.monotonic()
        if delay > 1:
            raise HTTPException(503, '在线歌词服务暂时繁忙，请稍后重试。')
        if delay > 0:
            time.sleep(delay)
        network.charge_request()
        response = requests.get('https://lrclib.net/api/get', params={
            'track_name': title, 'artist_name': artist, 'album_name': album, 'duration': round(duration)},
            headers={'User-Agent': 'VideoDream/1.0 (https://github.com/hl-1/UniversalVideo)'}, timeout=(4, 10))
        _next_remote_at = time.monotonic() + .5
        if response.status_code in (429, 503):
            try:
                delay = min(3600, max(1, float(response.headers.get('Retry-After', '60'))))
            except ValueError:
                delay = 60
            _next_remote_at = time.monotonic() + delay
            raise HTTPException(503, '在线歌词服务暂时繁忙。')
        if response.status_code == 404:
            return {"text": "", "source": "LRCLIB", "instrumental": None, "document_complete": False}
        response.raise_for_status()
        data = response.json()
    normalize = lambda value: re.sub(r'[\W_]+', '', str(value or '').casefold())
    if not isinstance(data, dict) or normalize(data.get('trackName')) != normalize(title) or normalize(data.get('artistName')) != normalize(artist) or abs(float(data.get('duration') or 0)-duration) > max(3, duration*.03):
        raise ValueError('歌词歌曲身份不匹配')
    text = data.get('syncedLyrics') or data.get('plainLyrics') or ''
    if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
        raise ValueError('歌词内容格式无效或过大')
    return {'text': text, 'source': 'LRCLIB 身份匹配', 'instrumental': data.get('instrumental') is True,
            'document_complete': True}


def platform_lyrics(track):
    provider, identifier = track.get('provider'), track.get('identifier')
    if provider == 'netease':
        data = music._json('https://music.163.com/api/song/lyric', params={'id': identifier, 'lv': -1, 'kv': -1, 'tv': -1}, referer='https://music.163.com/')
        if data.get('code') not in (None, 200):
            raise ValueError('平台歌词接口未成功返回')
        text = (data.get('lrc') or {}).get('lyric') or ''
        if text or data.get('nolyric') is True:
            return {'text': text, 'source': '网易云歌曲身份歌词', 'instrumental': data.get('nolyric') is True,
                    'document_complete': True}
    elif provider == 'qq':
        data = music._json('https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg', params={'songmid': identifier, 'format': 'json', 'nobase64': 1}, referer='https://y.qq.com/')
        if data.get('code') != 0:
            raise ValueError('平台歌词接口未成功返回')
        text = data.get('lyric') or ''
        if text:
            if not text.startswith('[') and '&#' not in text:
                try:
                    text = base64.b64decode(text, validate=True).decode('utf-8')
                except (ValueError, UnicodeError):
                    pass
            return {'text': html.unescape(text), 'source': 'QQ 音乐歌曲身份歌词', 'instrumental': None, 'document_complete': True}
    return None


def remote(track):
    error = None
    try:
        value = platform_lyrics(track)
        if value is not None:
            return value
    except (ValueError, requests.RequestException) as exc:
        network.raise_if_guarded(exc)
        error = exc
    fallback = lrclib(track['title'], track.get('artist') or '', track.get('duration'), track.get('album') or '')
    if error is not None and not fallback['text'] and not fallback.get('instrumental'):
        raise ValueError('平台歌词服务失败，备用来源未匹配到歌词') from error
    return fallback


def cached_value(row):
    try:
        metadata = json.loads(row['signature'])
        if not isinstance(metadata, dict):
            metadata = {}
    except (ValueError, TypeError):
        metadata = {}
    return {'text': row['text'], 'source': row['source'], 'instrumental': metadata.get('instrumental'),
            'document_complete': True, 'checked_at': row['checked_at'], 'signature': row['signature']}


def lookup(track, allow_network=True, check=None):
    payload = library.TrackInput.model_validate(track)
    uid = accounts.owner_id()
    identity = hashlib.sha256(json.dumps([payload.provider, payload.identifier, payload.title, payload.artist, payload.duration], ensure_ascii=False).encode()).hexdigest()
    with library.database() as cursor:
        key = library.resolve_track_id(cursor, library.track_id(payload))
        cursor.execute('SELECT * FROM music_lyrics WHERE track_id=%s AND user_id=%s', (key, uid))
        cached = cursor.fetchone()
    if cached:
        value = cached_value(cached)
        try:
            same = json.loads(cached['signature']).get('identity') == identity
        except (ValueError, TypeError, AttributeError):
            same = False
        classified = classify_lines(value['text'])
        ttl = 604800 if classified['body'] or classified['declarations'] or value['instrumental'] else 86400
        if cached['manual'] or not allow_network or same and time.time()-cached['checked_at'] < ttl:
            return value
    if not allow_network:
        return {'text': '', 'source': '', 'instrumental': None, 'document_complete': False}
    if check:
        check()
    value = remote(track)
    text = value['text']
    if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
        raise ValueError('歌词内容格式无效或过大')
    if check:
        check()
    with library.database() as cursor:
        key = library.save_track(cursor, payload)
        library.write_lyrics(cursor, key, {'text': text, 'source': value['source'],
            'signature': json.dumps({'identity': identity, 'instrumental': value.get('instrumental')}, ensure_ascii=False),
            'checked_at': time.time()})
        cursor.execute('SELECT * FROM music_lyrics WHERE track_id=%s AND user_id=%s', (key, uid))
        stored = cursor.fetchone()
    return cached_value(stored)
