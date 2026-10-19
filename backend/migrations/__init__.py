"""Coordinate existing, idempotent migrations in their original order."""

def initialize_schema(cursor):
    from backend.modules.accounts import service as accounts
    from backend.modules.music import library
    from backend.modules.video import store as video_store
    from backend.modules.music.agent import store as agent_store

    for statement in library.SCHEMA:
        cursor.execute(statement)
    library.migrate_schema(cursor)
    cursor.execute("INSERT IGNORE INTO music_profile (id, nickname) VALUES (1, %s)", ("音乐听众",))
    accounts.migrate(cursor)
    video_store.migrate(cursor)
    agent_store.migrate(cursor)
