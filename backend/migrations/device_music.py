"""Classify legacy music data after Flyway creates the schema; no DDL or file deletion."""
import hashlib
import json
import mimetypes
import time



def migrate(cursor):
    from backend.core import storage
    from backend.modules.music import library
    cursor.execute("""SELECT d.* FROM music_downloads d LEFT JOIN music_server_assets a
        ON a.user_id=d.user_id AND a.filename=d.filename WHERE a.id IS NULL""")
    rows = cursor.fetchall()
    # Record inventory before classification; rerunning only processes absent assets.
    if rows:
        backup = library.backup_library(cursor)
        backup.with_suffix('.inventory.json').write_text(json.dumps(rows, ensure_ascii=False, default=str), encoding='utf-8')
    for row in rows:
        path = storage.owned_path('file', row['filename'], uid=row['user_id'])
        exists = path.is_file()
        kind = 'site_upload' if row['imported'] else 'legacy_download'
        identity = row['id']
        cursor.execute("""INSERT IGNORE INTO music_server_assets
            (id,user_id,track_id,filename,original_filename,asset_kind,status,mime_type,file_size,sha256,created_at,updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (identity,row['user_id'],row['track_id'],row['filename'],storage.download_filename(row['filename']),kind,
             'ready' if exists else 'missing',mimetypes.guess_type(row['filename'])[0] or 'application/octet-stream',
             row['file_size'],row['file_hash'],row['created_at'],time.time()))
        if kind == 'site_upload':
            prefix = '' if row['user_id'] == 1 else str(row['user_id']) + ':'
            alias = hashlib.sha256(f'{prefix}site:{identity}'.encode()).hexdigest()
            cursor.execute("INSERT IGNORE INTO music_track_aliases(alias_id,track_id) VALUES (%s,%s)", (alias,row['track_id']))
            cursor.execute("UPDATE music_tracks SET provider='site',identifier=%s,webpage_url=%s WHERE id=%s AND provider='local'", (identity,f'/api/music/site/{identity}/content',row['track_id']))
    # Unindexed file resources have no reliable upload provenance. Quarantine them
    # on the old resource screen instead of inventing a song or device record.
