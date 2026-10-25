-- Immutable after execution. Sources: deploy/sql/prod/v1/001, 002 and 003 (in that order).

-- v1 full schema. Empty database only; existing databases must pass preflight before baseline.

CREATE TABLE music_profile (
        id INT PRIMARY KEY, nickname VARCHAR(40) NOT NULL, bio VARCHAR(200) NOT NULL DEFAULT '', avatar MEDIUMTEXT,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_profile_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_tracks (
        id CHAR(64) PRIMARY KEY, provider VARCHAR(12) NOT NULL, identifier VARCHAR(255) NOT NULL,
        title VARCHAR(255) NOT NULL, artist VARCHAR(255) NOT NULL, webpage_url TEXT NOT NULL,
        thumbnail TEXT, duration DOUBLE, access VARCHAR(100) NOT NULL,
    custom_thumbnail TEXT NULL,
    cover_updated_at DOUBLE NOT NULL DEFAULT 0,
    cover_checked_at DOUBLE NOT NULL DEFAULT 0,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_tracks_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_favorites (
        track_id CHAR(64) PRIMARY KEY, created_at DOUBLE NOT NULL,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_favorites_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_history (
        track_id CHAR(64) PRIMARY KEY, played_at DOUBLE NOT NULL, play_count INT NOT NULL DEFAULT 1,
        preview BOOLEAN NOT NULL DEFAULT FALSE,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_history_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_playlists (
        id CHAR(24) PRIMARY KEY, name VARCHAR(80) NOT NULL, description VARCHAR(300) NOT NULL,
        created_at DOUBLE NOT NULL,
    thumbnail TEXT NULL,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_playlists_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_playlist_tracks (
        playlist_id CHAR(24) NOT NULL, track_id CHAR(64) NOT NULL, added_at DOUBLE NOT NULL,
        PRIMARY KEY (playlist_id, track_id),
        FOREIGN KEY (playlist_id) REFERENCES music_playlists(id) ON DELETE CASCADE,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    position INT UNSIGNED NULL DEFAULT NULL,
    KEY playlist_position (playlist_id,position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_downloads (
        id CHAR(24) PRIMARY KEY, track_id CHAR(64) NOT NULL, filename VARCHAR(255) NOT NULL UNIQUE,
        created_at DOUBLE NOT NULL, imported BOOLEAN NOT NULL DEFAULT FALSE,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    file_hash CHAR(64) NULL,
    file_size BIGINT NULL,
    file_mtime BIGINT NULL,
    file_available BOOLEAN NOT NULL DEFAULT TRUE,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_downloads_owner (user_id),
    KEY file_hash_index (file_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_track_aliases (
        alias_id CHAR(64) PRIMARY KEY, track_id CHAR(64) NOT NULL,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_lyrics (
        track_id CHAR(64) PRIMARY KEY, text MEDIUMTEXT NOT NULL, source VARCHAR(80) NOT NULL,
        format VARCHAR(8) NOT NULL, signature TEXT NOT NULL, checked_at DOUBLE NOT NULL,
        `manual` BOOLEAN NOT NULL DEFAULT FALSE, updated_at DOUBLE NOT NULL,
        FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_lyrics_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_theme (
        id INT PRIMARY KEY, config TEXT NOT NULL,
    user_id BIGINT NOT NULL DEFAULT 1,
    KEY music_theme_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE users (id BIGINT PRIMARY KEY AUTO_INCREMENT, username VARCHAR(40) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL, role VARCHAR(16) NOT NULL DEFAULT 'user', status VARCHAR(16) NOT NULL DEFAULT 'active',
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE, auth_version INT NOT NULL DEFAULT 0,
    created_at DOUBLE NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE system_settings (id INT PRIMARY KEY, config MEDIUMTEXT NOT NULL)
    ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE audit_logs (id BIGINT PRIMARY KEY AUTO_INCREMENT, actor_id BIGINT,
    action VARCHAR(80) NOT NULL,target VARCHAR(255) NOT NULL,result VARCHAR(40) NOT NULL,ip VARCHAR(64) NOT NULL,
    created_at DOUBLE NOT NULL, INDEX audit_actor(actor_id,created_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE resources (kind VARCHAR(24) NOT NULL, resource_key VARCHAR(255) NOT NULL,
    user_id BIGINT NOT NULL, payload MEDIUMTEXT NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY(kind,resource_key), INDEX resource_owner(user_id,kind),
    device_id CHAR(36) NULL,
    KEY resource_device (user_id,kind,device_id,created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE account_migrations (id VARCHAR(40) PRIMARY KEY, completed_at DOUBLE NOT NULL,
    backup_path TEXT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_recent_links (
        user_id BIGINT NOT NULL, url_hash CHAR(64) NOT NULL, link_id VARCHAR(64) NOT NULL,
        url TEXT NOT NULL, submitted_at DOUBLE NOT NULL,
        PRIMARY KEY(user_id,url_hash), INDEX video_recent_time(user_id,submitted_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_parse_records (
        record_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL,
        url TEXT NOT NULL, webpage_url TEXT, title TEXT, platform VARCHAR(100) NOT NULL,
        status VARCHAR(24) NOT NULL, worker_id VARCHAR(255), snapshot JSON NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        INDEX video_parse_owner_time(user_id,deleted,created_at,record_id),
        INDEX video_parse_platform(user_id,deleted,platform,created_at),
        INDEX video_parse_status(user_id,deleted,status,created_at), INDEX video_parse_worker(deleted,status,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_summaries (
        task_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL, parse_record_id VARCHAR(64),
        webpage_url TEXT, status VARCHAR(24) NOT NULL, worker_id VARCHAR(255), content JSON NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        INDEX video_summary_record(user_id,parse_record_id,deleted,created_at),
        INDEX video_summary_worker(deleted,status,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_mind_maps (
        task_id VARCHAR(64) PRIMARY KEY, user_id BIGINT NOT NULL, parse_record_id VARCHAR(64),
        summary_task_id VARCHAR(64), version INT UNSIGNED NOT NULL, status VARCHAR(24) NOT NULL,
        worker_id VARCHAR(255), content JSON NOT NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
        deleted BOOLEAN NOT NULL DEFAULT FALSE, UNIQUE KEY video_map_version(summary_task_id,version),
        INDEX video_map_record(user_id,parse_record_id,deleted,created_at),
        INDEX video_map_summary(user_id,summary_task_id,deleted,status,created_at),
        INDEX video_map_worker(deleted,status,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE video_view_states (
        user_id BIGINT NOT NULL, record_id VARCHAR(64) NOT NULL, view_key VARCHAR(255) NOT NULL,
        selected_tab VARCHAR(24) NOT NULL, playback_time DOUBLE NOT NULL, view_state JSON NOT NULL,
        created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL, deleted BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY(user_id,record_id), UNIQUE KEY video_view_key(view_key),
        INDEX video_view_recent(user_id,deleted,updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_recommendation_runs (
    run_id CHAR(32) NOT NULL PRIMARY KEY, user_id BIGINT NOT NULL, parent_run_id CHAR(32) NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending', request JSON NOT NULL, context JSON NOT NULL,
    steps JSON NOT NULL, result JSON NOT NULL, policy_version VARCHAR(40) NOT NULL,
    skill_version VARCHAR(64) NOT NULL, idempotency_key CHAR(64) NOT NULL,
    revision INT UNSIGNED NOT NULL DEFAULT 0, result_version INT UNSIGNED NOT NULL DEFAULT 0,
    saved_playlist_id CHAR(24) NULL, saved_at DOUBLE NULL, worker_id VARCHAR(64) NULL,
    lease_until DOUBLE NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE KEY rec_request(user_id,idempotency_key), KEY rec_owner(user_id,deleted,created_at),
    KEY rec_worker(deleted,status,lease_until),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_run_id) REFERENCES music_recommendation_runs(run_id) ON DELETE SET NULL,
    FOREIGN KEY(saved_playlist_id) REFERENCES music_playlists(id) ON DELETE SET NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_track_features (
    user_id BIGINT NOT NULL, track_key CHAR(64) NOT NULL, track_snapshot JSON NOT NULL,
    features JSON NOT NULL, evidence JSON NOT NULL, content_signature CHAR(64) NOT NULL,
    analyzer_version VARCHAR(40) NOT NULL, checked_at DOUBLE NOT NULL, expires_at DOUBLE NOT NULL,
    PRIMARY KEY(user_id,track_key), KEY feature_expiry(expires_at),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_server_assets (
 id CHAR(24) NOT NULL PRIMARY KEY, user_id BIGINT NOT NULL, track_id CHAR(64) NOT NULL,
 filename VARCHAR(255) NOT NULL, original_filename VARCHAR(255) NOT NULL,
 asset_kind VARCHAR(24) NOT NULL, status VARCHAR(16) NOT NULL, mime_type VARCHAR(100) NOT NULL,
 file_size BIGINT NULL, sha256 CHAR(64) NULL, created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL,
 UNIQUE KEY asset_owner_file (user_id,filename),
 KEY asset_owner_kind_status (user_id,asset_kind,status,created_at),
 KEY asset_track (track_id), KEY asset_hash (sha256),
 CONSTRAINT asset_track_fk FOREIGN KEY (track_id) REFERENCES music_tracks(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- v1 basic data. No usable administrator password is stored in SQL.
INSERT INTO users (id, username, password_hash, role, must_change_password, created_at)
VALUES (1, 'admin', '!uninitialized', 'super_admin', TRUE, UNIX_TIMESTAMP());
INSERT INTO music_profile (id, nickname, user_id) VALUES (1, '音乐听众', 1);

-- v1 default settings. Existing settings are never overwritten during adoption.
INSERT INTO system_settings (id, config) VALUES (1, '{"registration_enabled":false,"music_enabled":true,"video_enabled":true,"summary_enabled":true,"watermark_enabled":true,"ai_base_url":"https://api.openai.com/v1","ai_model":"gpt-4.1-mini","ai_api_key":"","ai_vision_base_url":"","ai_vision_model":"","ai_vision_api_key":"","ai_asr_base_url":"","ai_asr_model":"","ai_asr_api_key":"","music_agent_enabled":false,"music_agent_network_enabled":true,"music_agent_sources":["netease","qq","kugou"],"music_agent_decisions":6,"music_agent_network_requests":300,"music_agent_search_rounds":5,"music_agent_candidates":100,"music_agent_seconds":600,"music_agent_tokens":150000,"music_agent_model_calls":40,"music_agent_concurrency":2,"music_agent_api":"auto"}');
