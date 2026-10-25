"""Files explicitly uploaded to the site are online sources, never device files."""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from backend.core import storage, blobs
from backend.infrastructure.database import database
from backend.modules.accounts import service as accounts
from backend.modules.music import library

router = APIRouter(prefix="/api/music/site", tags=["site-music"])


def asset(identity: str):
    if not re.fullmatch(r"[a-f0-9]{24}", identity):
        raise HTTPException(404, "本站音源不存在。")
    with database() as cursor:
        cursor.execute("SELECT * FROM music_server_assets WHERE id=%s AND user_id=%s AND asset_kind='site_upload' AND status='ready'", (identity, accounts.owner_id()))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(404, "本站音源不存在。")
    accounts.resource('file', row['filename'])
    return row


def serialize(row):
    track = library.serialize_track(dict(row, liked=row.get('liked', False)))
    track.update(provider='site', identifier=row['asset_id'], webpage_url=f"/api/music/site/{row['asset_id']}/content",
                 server_url=f"/api/music/site/{row['asset_id']}/content", local_url=None, device_file=False,
                 list_time=row.get('uploaded_at'))
    return track


@router.get('/tracks')
def tracks():
    with database() as cursor:
        cursor.execute("""SELECT t.*,a.id AS asset_id,a.filename,a.created_at AS uploaded_at FROM music_server_assets a
            JOIN music_tracks t ON t.id=a.track_id JOIN resources r ON r.kind='file' AND r.resource_key=a.filename
            AND r.user_id=a.user_id AND r.deleted=FALSE
            WHERE a.user_id=%s AND a.asset_kind='site_upload' AND a.status='ready' ORDER BY a.created_at DESC LIMIT 1000""", (accounts.owner_id(),))
        rows = cursor.fetchall()
    return {'tracks': [serialize(row) for row in rows if storage.owned_path('file', row['filename'], uid=accounts.owner_id()).is_file()]}


@router.post('/uploads')
def upload(file: UploadFile):
    original = Path(file.filename or 'audio').name
    if Path(original).suffix.lower() not in library.AUDIO_EXTENSIONS:
        raise HTTPException(422, '请选择有效音频文件。')
    identity = secrets.token_hex(12)
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', original)[:160]
    destination = storage.path('file', f'import-{secrets.token_hex(6)}.{safe_name}')
    try:
        total = 0
        with destination.open('xb') as output:
            while chunk := file.file.read(256 * 1024):
                total += len(chunk)
                if total > 512 * 1024 * 1024:
                    raise HTTPException(413, '单个音频不能超过 512 MB。')
                output.write(chunk)
        metadata = library.inspect_audio(destination)
        fingerprint = library.hash_file(destination)
        blobs.publish(destination)
        accounts.register_resource('file', destination.name, {'filename': destination.name, 'size': total, 'asset_kind': 'site_upload'})
        with database() as cursor:
            # Preserve the same canonical indexing and lyric/cover ACL rules as
            # older uploads, while exposing the source through its site asset.
            key = library.save_track(cursor, library.TrackInput.model_validate(metadata))
            now = time.time()
            cursor.execute("""INSERT INTO music_server_assets
                (id,user_id,track_id,filename,original_filename,asset_kind,status,mime_type,file_size,sha256,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,'site_upload','ready',%s,%s,%s,%s,%s)""",
                (identity,accounts.owner_id(),key,destination.name,original,mimetypes.guess_type(original)[0] or 'application/octet-stream',total,fingerprint,now,now))
            site_track = library.TrackInput.model_validate(metadata | {'provider': 'site', 'identifier': identity, 'webpage_url': f'/api/music/site/{identity}/content'})
            cursor.execute('INSERT INTO music_track_aliases(alias_id,track_id) VALUES (%s,%s)', (library.track_id(site_track),key))
            # Change the stored source; old local identity remains an alias.
            cursor.execute("UPDATE music_tracks SET provider='site',identifier=%s,webpage_url=%s WHERE id=%s", (identity,site_track.webpage_url,key))
        return {'filename': destination.name, 'track': metadata | site_track.model_dump() | {'id': key, 'server_url': site_track.webpage_url, 'local_url': None}}
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(422, '音频无法读取或上传未完成。') from exc
    finally:
        file.file.close()


@router.api_route('/{identity}/content', methods=['GET', 'HEAD'])
def content(identity: str):
    row = asset(identity)
    return storage.media_response(storage.owned_path('file', row['filename'], uid=row['user_id']))
