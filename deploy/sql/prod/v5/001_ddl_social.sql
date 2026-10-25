-- Social spaces; additive migration. Back up before applying.
CREATE TABLE social_user_settings (
  user_id BIGINT PRIMARY KEY,
  discoverable BOOLEAN NOT NULL DEFAULT TRUE,
  online_visibility VARCHAR(16) NOT NULL DEFAULT 'friends',
  listening_visibility VARCHAR(16) NOT NULL DEFAULT 'friends',
  dm_policy VARCHAR(16) NOT NULL DEFAULT 'friends',
  invite_policy VARCHAR(16) NOT NULL DEFAULT 'friends',
  avatar_asset_id CHAR(32) NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_relations (
  user_id BIGINT NOT NULL,
  target_user_id BIGINT NOT NULL,
  kind VARCHAR(12) NOT NULL,
  status VARCHAR(16) NOT NULL,
  remark VARCHAR(40) NOT NULL DEFAULT '',
  request_message VARCHAR(200) NOT NULL DEFAULT '',
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  PRIMARY KEY (user_id, target_user_id, kind),
  KEY social_relation_target (target_user_id, kind, status, created_at),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY (target_user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_conversations (
  id CHAR(32) PRIMARY KEY,
  kind VARCHAR(12) NOT NULL,
  direct_key CHAR(64) NULL UNIQUE,
  owner_id BIGINT NULL,
  title VARCHAR(80) NOT NULL DEFAULT '',
  settings JSON NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  last_seq BIGINT UNSIGNED NOT NULL DEFAULT 0,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY social_conversation_list (kind, status, updated_at),
  FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_conversation_members (
  conversation_id CHAR(32) NOT NULL,
  user_id BIGINT NOT NULL,
  role VARCHAR(16) NOT NULL DEFAULT 'member',
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  joined_seq BIGINT UNSIGNED NOT NULL DEFAULT 0,
  left_seq BIGINT UNSIGNED NULL,
  last_delivered_seq BIGINT UNSIGNED NOT NULL DEFAULT 0,
  last_read_seq BIGINT UNSIGNED NOT NULL DEFAULT 0,
  hidden_before_seq BIGINT UNSIGNED NOT NULL DEFAULT 0,
  muted_until DOUBLE NULL,
  pinned BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  PRIMARY KEY (conversation_id, user_id),
  KEY social_member_user (user_id, status, pinned, updated_at),
  FOREIGN KEY (conversation_id) REFERENCES social_conversations(id) ON DELETE CASCADE,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_messages (
  conversation_id CHAR(32) NOT NULL,
  seq BIGINT UNSIGNED NOT NULL,
  sender_id BIGINT NULL,
  client_msg_id CHAR(32) NOT NULL,
  msg_type VARCHAR(16) NOT NULL,
  body JSON NOT NULL,
  reply_to_seq BIGINT UNSIGNED NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'sent',
  created_at DOUBLE NOT NULL,
  recalled_at DOUBLE NULL,
  PRIMARY KEY (conversation_id, seq),
  UNIQUE KEY social_message_client (conversation_id, client_msg_id),
  KEY social_message_sender (sender_id, created_at),
  FOREIGN KEY (conversation_id) REFERENCES social_conversations(id) ON DELETE CASCADE,
  FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE social_assets (
  id CHAR(32) PRIMARY KEY,
  owner_id BIGINT NULL,
  kind VARCHAR(20) NOT NULL,
  storage_key VARCHAR(512) NOT NULL UNIQUE,
  mime VARCHAR(100) NOT NULL,
  size_bytes BIGINT UNSIGNED NOT NULL,
  duration_ms BIGINT UNSIGNED NULL,
  sha256 CHAR(64) NOT NULL,
  metadata JSON NOT NULL,
  state VARCHAR(16) NOT NULL DEFAULT 'uploading',
  expires_at DOUBLE NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY social_asset_owner (owner_id, state, created_at),
  KEY social_asset_cleanup (state, expires_at),
  FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_message_assets (
  conversation_id CHAR(32) NOT NULL,
  seq BIGINT UNSIGNED NOT NULL,
  asset_id CHAR(32) NOT NULL,
  purpose VARCHAR(16) NOT NULL DEFAULT 'attachment',
  PRIMARY KEY (conversation_id, seq, asset_id),
  FOREIGN KEY (conversation_id, seq) REFERENCES social_messages(conversation_id, seq) ON DELETE CASCADE,
  FOREIGN KEY (asset_id) REFERENCES social_assets(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_shares (
  id CHAR(32) PRIMARY KEY,
  owner_id BIGINT NULL,
  target_kind VARCHAR(16) NOT NULL,
  source_key VARCHAR(64) NOT NULL,
  snapshot JSON NOT NULL,
  scope VARCHAR(16) NOT NULL,
  conversation_id CHAR(32) NULL,
  token_hash CHAR(64) NULL UNIQUE,
  expires_at DOUBLE NULL,
  revoked_at DOUBLE NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY social_share_owner (owner_id, created_at),
  KEY social_share_scope (scope, expires_at),
  FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (conversation_id) REFERENCES social_conversations(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_share_assets (
  share_id CHAR(32) NOT NULL,
  asset_id CHAR(32) NOT NULL,
  purpose VARCHAR(16) NOT NULL,
  item_no INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (share_id, asset_id, purpose, item_no),
  FOREIGN KEY (share_id) REFERENCES social_shares(id) ON DELETE CASCADE,
  FOREIGN KEY (asset_id) REFERENCES social_assets(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_notifications (
  id CHAR(32) PRIMARY KEY,
  user_id BIGINT NOT NULL,
  event_type VARCHAR(32) NOT NULL,
  object_key VARCHAR(96) NOT NULL,
  payload JSON NOT NULL,
  dedupe_key CHAR(64) NOT NULL,
  read_at DOUBLE NULL,
  created_at DOUBLE NOT NULL,
  UNIQUE KEY social_notification_dedupe (user_id, dedupe_key),
  KEY social_notification_list (user_id, read_at, created_at),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_outbox (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  topic VARCHAR(64) NOT NULL,
  object_key VARCHAR(96) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
  dedupe_key CHAR(64) NOT NULL UNIQUE,
  payload JSON NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  available_at DOUBLE NOT NULL,
  worker_id VARCHAR(64) NULL,
  lease_until DOUBLE NULL,
  created_at DOUBLE NOT NULL,
  delivered_at DOUBLE NULL,
  KEY social_outbox_ready (status, available_at),
  KEY social_outbox_lease (status, lease_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE social_rooms (
  id CHAR(32) PRIMARY KEY,
  conversation_id CHAR(32) NOT NULL UNIQUE,
  owner_id BIGINT NULL,
  kind VARCHAR(16) NOT NULL,
  title VARCHAR(80) NOT NULL DEFAULT '',
  visibility VARCHAR(16) NOT NULL DEFAULT 'private',
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  config JSON NOT NULL,
  playback JSON NOT NULL,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
  last_active_at DOUBLE NOT NULL,
  closed_at DOUBLE NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY social_room_list (status, visibility, kind, updated_at),
  FOREIGN KEY (conversation_id) REFERENCES social_conversations(id) ON DELETE CASCADE,
  FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_room_members (
  room_id CHAR(32) NOT NULL,
  user_id BIGINT NOT NULL,
  role VARCHAR(16) NOT NULL DEFAULT 'audience',
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  permissions JSON NOT NULL,
  joined_at DOUBLE NOT NULL,
  left_at DOUBLE NULL,
  ban_until DOUBLE NULL,
  updated_at DOUBLE NOT NULL,
  PRIMARY KEY (room_id, user_id),
  KEY social_room_member_user (user_id, status),
  FOREIGN KEY (room_id) REFERENCES social_rooms(id) ON DELETE CASCADE,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_room_invites (
  id CHAR(32) PRIMARY KEY,
  room_id CHAR(32) NOT NULL,
  inviter_id BIGINT NULL,
  invitee_id BIGINT NULL,
  token_hash CHAR(64) NOT NULL UNIQUE,
  max_uses INT UNSIGNED NOT NULL DEFAULT 1,
  used_count INT UNSIGNED NOT NULL DEFAULT 0,
  expires_at DOUBLE NOT NULL,
  revoked_at DOUBLE NULL,
  created_at DOUBLE NOT NULL,
  KEY social_room_invite_list (room_id, expires_at),
  FOREIGN KEY (room_id) REFERENCES social_rooms(id) ON DELETE CASCADE,
  FOREIGN KEY (inviter_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (invitee_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_room_queue (
  id CHAR(32) PRIMARY KEY,
  room_id CHAR(32) NOT NULL,
  requester_id BIGINT NULL,
  partner_id BIGINT NULL,
  queue_kind VARCHAR(12) NOT NULL,
  track_snapshot JSON NOT NULL,
  karaoke_song_id CHAR(32) NULL,
  order_no BIGINT UNSIGNED NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'queued',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  UNIQUE KEY social_room_queue_order (room_id, order_no),
  KEY social_room_queue_status (room_id, status, order_no),
  FOREIGN KEY (room_id) REFERENCES social_rooms(id) ON DELETE CASCADE,
  FOREIGN KEY (requester_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (partner_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE social_karaoke_songs (
  id CHAR(32) PRIMARY KEY,
  creator_id BIGINT NULL,
  track_snapshot JSON NOT NULL,
  backing_asset_id CHAR(32) NOT NULL,
  guide_asset_id CHAR(32) NULL,
  lyrics JSON NOT NULL,
  duration_ms BIGINT UNSIGNED NOT NULL,
  version INT UNSIGNED NOT NULL DEFAULT 1,
  content_hash CHAR(64) NOT NULL,
  rights_note VARCHAR(300) NOT NULL DEFAULT '',
  visibility VARCHAR(16) NOT NULL DEFAULT 'private',
  state VARCHAR(16) NOT NULL DEFAULT 'draft',
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY social_karaoke_hash (content_hash),
  KEY social_karaoke_list (state, visibility, created_at),
  FOREIGN KEY (creator_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (backing_asset_id) REFERENCES social_assets(id) ON DELETE RESTRICT,
  FOREIGN KEY (guide_asset_id) REFERENCES social_assets(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_performances (
  id CHAR(32) PRIMARY KEY,
  room_id CHAR(32) NULL,
  queue_id CHAR(32) NULL UNIQUE,
  karaoke_song_id CHAR(32) NOT NULL,
  owner_id BIGINT NULL,
  kind VARCHAR(16) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'draft',
  mix_asset_id CHAR(32) NULL,
  visibility VARCHAR(16) NOT NULL DEFAULT 'private',
  revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
  mix_input_hash CHAR(64) NULL,
  worker_id VARCHAR(64) NULL,
  lease_until DOUBLE NULL,
  attempts INT UNSIGNED NOT NULL DEFAULT 0,
  last_error VARCHAR(500) NULL,
  started_at DOUBLE NULL,
  finished_at DOUBLE NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY social_performance_owner (owner_id, status, created_at),
  KEY social_performance_worker (status, lease_until),
  FOREIGN KEY (room_id) REFERENCES social_rooms(id) ON DELETE SET NULL,
  FOREIGN KEY (queue_id) REFERENCES social_room_queue(id) ON DELETE SET NULL,
  FOREIGN KEY (karaoke_song_id) REFERENCES social_karaoke_songs(id) ON DELETE RESTRICT,
  FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (mix_asset_id) REFERENCES social_assets(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_performance_parts (
  performance_id CHAR(32) NOT NULL,
  slot TINYINT UNSIGNED NOT NULL,
  user_id BIGINT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'empty',
  raw_asset_id CHAR(32) NULL,
  offset_ms INT NOT NULL DEFAULT 0,
  gain_db DECIMAL(5,2) NOT NULL DEFAULT 0,
  record_consent_at DOUBLE NULL,
  publish_consent_hash CHAR(64) NULL,
  reserved_until DOUBLE NULL,
  metadata JSON NOT NULL,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  PRIMARY KEY (performance_id, slot),
  UNIQUE KEY social_performance_part_user (performance_id, user_id),
  FOREIGN KEY (performance_id) REFERENCES social_performances(id) ON DELETE CASCADE,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (raw_asset_id) REFERENCES social_assets(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_duet_invites (
  id CHAR(32) PRIMARY KEY,
  performance_id CHAR(32) NOT NULL,
  slot TINYINT UNSIGNED NOT NULL,
  inviter_id BIGINT NULL,
  invitee_id BIGINT NULL,
  claimed_by BIGINT NULL,
  token_hash CHAR(64) NOT NULL UNIQUE,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  expires_at DOUBLE NOT NULL,
  claimed_at DOUBLE NULL,
  created_at DOUBLE NOT NULL,
  KEY social_duet_invite_slot (performance_id, slot, status),
  FOREIGN KEY (performance_id) REFERENCES social_performances(id) ON DELETE CASCADE,
  FOREIGN KEY (inviter_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (invitee_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (claimed_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE social_rules (
  id CHAR(32) PRIMARY KEY,
  author_id BIGINT NULL,
  title VARCHAR(80) NOT NULL,
  summary VARCHAR(500) NOT NULL DEFAULT '',
  tags JSON NOT NULL,
  scene VARCHAR(80) NOT NULL DEFAULT '',
  status VARCHAR(16) NOT NULL DEFAULT 'draft',
  published_version_id CHAR(32) NULL,
  origin_version_id CHAR(32) NULL,
  license VARCHAR(40) NOT NULL DEFAULT 'personal',
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  KEY social_rule_list (status, scene, updated_at),
  KEY social_rule_author (author_id, status, updated_at),
  FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_rule_versions (
  id CHAR(32) PRIMARY KEY,
  rule_id CHAR(32) NOT NULL,
  version INT UNSIGNED NOT NULL,
  schema_version VARCHAR(20) NOT NULL,
  engine_version VARCHAR(40) NOT NULL,
  config JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  change_note VARCHAR(500) NOT NULL DEFAULT '',
  created_at DOUBLE NOT NULL,
  UNIQUE KEY social_rule_version (rule_id, version),
  KEY social_rule_content (content_hash),
  FOREIGN KEY (rule_id) REFERENCES social_rules(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_rule_library (
  user_id BIGINT NOT NULL,
  rule_id CHAR(32) NOT NULL,
  version_id CHAR(32) NOT NULL,
  saved BOOLEAN NOT NULL DEFAULT TRUE,
  liked BOOLEAN NOT NULL DEFAULT FALSE,
  follow_updates BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  PRIMARY KEY (user_id, rule_id),
  KEY social_rule_saved (rule_id, saved),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY (rule_id) REFERENCES social_rules(id) ON DELETE CASCADE,
  FOREIGN KEY (version_id) REFERENCES social_rule_versions(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_rule_run_bindings (
  run_id CHAR(32) PRIMARY KEY,
  version_id CHAR(32) NOT NULL,
  effective_rule JSON NOT NULL,
  content_hash CHAR(64) NOT NULL,
  created_at DOUBLE NOT NULL,
  FOREIGN KEY (version_id) REFERENCES social_rule_versions(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE social_reports (
  id CHAR(32) PRIMARY KEY,
  reporter_id BIGINT NULL,
  target_kind VARCHAR(20) NOT NULL,
  target_key VARCHAR(96) NOT NULL,
  reason VARCHAR(500) NOT NULL,
  evidence JSON NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  handler_id BIGINT NULL,
  resolution VARCHAR(500) NULL,
  created_at DOUBLE NOT NULL,
  resolved_at DOUBLE NULL,
  KEY social_report_status (status, created_at),
  KEY social_report_reporter (reporter_id, created_at),
  FOREIGN KEY (reporter_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (handler_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE social_rules
  ADD CONSTRAINT social_rule_published_fk FOREIGN KEY (published_version_id) REFERENCES social_rule_versions(id) ON DELETE SET NULL,
  ADD CONSTRAINT social_rule_origin_fk FOREIGN KEY (origin_version_id) REFERENCES social_rule_versions(id) ON DELETE SET NULL;
ALTER TABLE social_user_settings
  ADD CONSTRAINT social_avatar_fk FOREIGN KEY (avatar_asset_id) REFERENCES social_assets(id) ON DELETE SET NULL;
ALTER TABLE social_room_queue
  ADD CONSTRAINT social_queue_song_fk FOREIGN KEY (karaoke_song_id) REFERENCES social_karaoke_songs(id) ON DELETE SET NULL;

CREATE TABLE social_playlists (
  id CHAR(32) PRIMARY KEY,
  owner_id BIGINT NULL,
  title VARCHAR(80) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at DOUBLE NOT NULL,
  updated_at DOUBLE NOT NULL,
  FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE social_playlist_members (
  playlist_id CHAR(32) NOT NULL,
  user_id BIGINT NOT NULL,
  role VARCHAR(16) NOT NULL DEFAULT 'editor',
  created_at DOUBLE NOT NULL,
  PRIMARY KEY (playlist_id,user_id),
  KEY social_playlist_user (user_id),
  FOREIGN KEY (playlist_id) REFERENCES social_playlists(id) ON DELETE CASCADE,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE social_playlist_items (
  id CHAR(32) PRIMARY KEY,
  playlist_id CHAR(32) NOT NULL,
  contributor_id BIGINT NULL,
  track_key CHAR(64) NOT NULL,
  track_snapshot JSON NOT NULL,
  order_no BIGINT UNSIGNED NOT NULL,
  created_at DOUBLE NOT NULL,
  UNIQUE KEY social_playlist_track (playlist_id,track_key),
  UNIQUE KEY social_playlist_order (playlist_id,order_no),
  FOREIGN KEY (playlist_id) REFERENCES social_playlists(id) ON DELETE CASCADE,
  FOREIGN KEY (contributor_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
