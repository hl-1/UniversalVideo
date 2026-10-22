"""Additive migration. Legacy files are classified, never removed or assigned to a device."""
import hashlib
import json
import mimetypes
import time

SCHEMA = """CREATE TABLE IF NOT EXISTS music_server_assets (
 id CHAR(24) NOT NULL PRIMARY KEY, user_id BIGINT NOT NULL, track_id CHAR(64) NOT NULL,
 filename VARCHAR(255) NOT NULL, original_filename VARCHAR(255) NOT NULL,
 asset_kind VARCHAR(24) NOT NULL, status VARCHAR(16) NOT NULL, mime_type VARCHAR(100) NOT NULL,
 file_size BIGINT NULL, sha256 CHAR(64) NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
 UNIQUE KEY asset_owner_file (user_id,filename),
 KEY asset_owner_kind_status (user_id,asset_kind,status,created_at),
 KEY asset_track (track_id), KEY asset_hash (sha256),
 CONSTRAINT asset_track_fk FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def migrate(cursor):
    from backend.core import storage
    from backend.modules.music import library
    cursor.execute("SHOW COLUMNS FROM resources LIKE 'device_id'")
    if not cursor.fetchone():
        backup = library.backup_library(cursor)
        cursor.execute("SELECT * FROM resources")
        backup.with_suffix('.resources.json').write_text(json.dumps(cursor.fetchall(), ensure_ascii=False, default=str), encoding='utf-8')
        cursor.execute("ALTER TABLE resources ADD COLUMN device_id CHAR(36) NULL")
    cursor.execute("SHOW INDEX FROM resources WHERE Key_name='resource_device'")
    if not cursor.fetchone():
        cursor.execute("CREATE INDEX resource_device ON resources(user_id,kind,device_id,created_at)")
    cursor.execute(SCHEMA)
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
