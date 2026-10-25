-- Stop the application and run scripts/database.py migrate. Its offline data
-- stage rekeys existing users, their relational/JSON ownership and user folders.
CREATE TABLE user_id_generator_state (
  worker_id SMALLINT UNSIGNED NOT NULL PRIMARY KEY,
  last_timestamp_ms BIGINT UNSIGNED NOT NULL,
  sequence SMALLINT UNSIGNED NOT NULL,
  CONSTRAINT user_id_worker_range CHECK (worker_id <= 1023),
  CONSTRAINT user_id_sequence_range CHECK (sequence <= 4095)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE user_id_migration_map (
  old_id BIGINT NOT NULL PRIMARY KEY,
  new_id BIGINT NOT NULL UNIQUE,
  created_at DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

SET @USER_ID_OLD_FK_CHECKS = @@SESSION.FOREIGN_KEY_CHECKS;
SET FOREIGN_KEY_CHECKS=0;
ALTER TABLE users MODIFY COLUMN id BIGINT NOT NULL;
SET FOREIGN_KEY_CHECKS=@USER_ID_OLD_FK_CHECKS;
ALTER TABLE music_profile MODIFY COLUMN id BIGINT NOT NULL;
ALTER TABLE music_theme MODIFY COLUMN id BIGINT NOT NULL;
ALTER TABLE music_profile ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE music_tracks ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE music_playlists ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE music_favorites ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE music_history ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE music_downloads ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE music_lyrics ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE music_theme ALTER COLUMN user_id DROP DEFAULT;
