"""Conservative identity: uncertain works remain unknown, never title-only merges."""
import hashlib
import json
import math
import re
import time
import unicodedata
from collections import defaultdict
from backend.modules.music import library
from backend.modules.music.agent.store import encode
from .policy import VERSION


def text(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


def artists(value):
    # Sort credits, preserving the artists themselves and all title/version text.
    return tuple(sorted(set(filter(None, (text(part) for part in re.split(
        r'[/、,，;；]|\s+(?:feat\.?|ft\.?|featuring|&)\s+', text(value)))))))


def artist_members(value):
    """Group individual credits for recommendation policy, never recording identity."""
    members=set()
    for name in artists(value):
        # Decorative symbols are not proof of a different artist. Keep punctuation
        # and mathematical symbols in names such as P!nk and C++; never merge IDs.
        name=name.strip()
        while name and unicodedata.category(name[-1]) in {'So','Sk','Mn','Me'}:
            name=name[:-1].rstrip()
        while name and unicodedata.category(name[0]) in {'So','Sk'}:
            name=name[1:].lstrip()
        if name: members.add(name)
    return frozenset(members)


def metadata(track):
    title, credits, duration = text(track.get('title')), artists(track.get('artist')), track.get('duration')
    if not title or not credits or not isinstance(duration, (float, int)) or not math.isfinite(duration) or duration <= 0:
        return None
    return title, credits, duration


def same_recording(left, right):
    a, b = metadata(left), metadata(right)
    return bool(a and b and a[:2] == b[:2] and abs(a[2] - b[2]) <= 1)


class Catalog:
    """Read-time aliases preserve historical keys, feedback and event references."""
    def __init__(self, rows):
        parent = {row['recording_key']: row['recording_key'] for row in rows}
        def root(key):
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key
        groups = defaultdict(list)
        for row in rows:
            track = json.loads(row['canonical']) if isinstance(row['canonical'], str) else row['canonical']
            values = metadata(track)
            if values:
                groups[values[:2]].append((values[2], row['recording_key']))
        for values in groups.values():
            anchor = None
            for duration, key in sorted(values):
                # Do not chain duration tolerances into increasingly different versions.
                if anchor is None or duration - anchor[0] > 1:
                    anchor = duration, key
                a, b = root(anchor[1]), root(key)
                parent[max(a, b)] = min(a, b)
        aliases = defaultdict(set)
        for key in parent:
            aliases[root(key)].add(key)
        self._groups = {key: tuple(sorted(aliases[root(key)])) for key in parent}

    def aliases(self, recording):
        return self._groups.get(recording, (recording,))

    def recording(self, recording):
        return self.aliases(recording)[0]


def catalog(cursor, uid):
    cursor.execute('SELECT recording_key,canonical FROM music_discovery_identities WHERE user_id=%s', (uid,))
    return Catalog(cursor.fetchall())


def ensure(cursor, uid, track):
    payload = library.TrackInput.model_validate(track)
    key = library.resolve_track_id(cursor, library.track_id(payload))
    cursor.execute('SELECT * FROM music_discovery_identities WHERE user_id=%s AND track_key=%s', (uid, key))
    found = cursor.fetchone()
    if found:
        return found
    # Stable provider IDs prove a recording; metadata alone cannot prove a work.
    recording = hashlib.sha256(('recording:'+str(uid)+':'+key).encode()).hexdigest()
    basis = 'provider_identity'
    # Match across provider IDs, including different editions on the same provider.
    if payload.title and payload.artist and payload.duration:
        cursor.execute("SELECT recording_key,canonical FROM music_discovery_identities WHERE user_id=%s", (uid,))
        for row in cursor.fetchall():
            other=json.loads(row['canonical']) if isinstance(row['canonical'],str) else row['canonical']
            if same_recording(other, payload.model_dump()):
                recording=row['recording_key']
                basis = 'title_credits_duration'
                break
    now = time.time()
    cursor.execute('''INSERT INTO music_discovery_identities
        (user_id,track_key,recording_key,work_key,canonical,evidence,identity_version,created_at,updated_at)
        VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s)''',
        (uid,key,recording,encode(payload.model_dump()),encode({'basis':basis,'work_known':False}),VERSION,now,now))
    return dict(user_id=uid,track_key=key,recording_key=recording,work_key=None)
