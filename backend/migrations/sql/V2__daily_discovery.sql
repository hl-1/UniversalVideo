-- Daily discovery; additive migration. Existing business data is unchanged.
CREATE TABLE music_discovery_profiles (
    user_id BIGINT NOT NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    built_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    profile_version VARCHAR(40) NOT NULL,
    settings JSON NOT NULL,
    long_term JSON NOT NULL,
    short_term JSON NOT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_identities (
    user_id BIGINT NOT NULL,
    track_key CHAR(64) NOT NULL,
    recording_key CHAR(64) NOT NULL,
    work_key CHAR(64) NULL,
    canonical JSON NOT NULL,
    evidence JSON NOT NULL,
    identity_version VARCHAR(40) NOT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, track_key),
    KEY discovery_identity_recording (user_id, recording_key),
    KEY discovery_identity_work (user_id, work_key),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_batches (
    batch_id CHAR(32) NOT NULL,
    user_id BIGINT NOT NULL,
    run_id CHAR(32) NOT NULL,
    recommendation_date DATE NOT NULL,
    batch_no TINYINT UNSIGNED NOT NULL,
    target_count SMALLINT UNSIGNED NOT NULL,
    request_key CHAR(64) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    profile_revision BIGINT UNSIGNED NOT NULL,
    policy_version VARCHAR(40) NOT NULL,
    generation_seed CHAR(64) NOT NULL,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (batch_id),
    UNIQUE KEY discovery_batch_run (run_id),
    UNIQUE KEY discovery_batch_request (user_id, request_key),
    UNIQUE KEY discovery_batch_number
        (user_id, recommendation_date, batch_no),
    KEY discovery_batch_created (created_at),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id)
        REFERENCES music_recommendation_runs(run_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_items (
    batch_id CHAR(32) NOT NULL,
    position SMALLINT UNSIGNED NOT NULL,
    track_key CHAR(64) NOT NULL,
    recording_key CHAR(64) NOT NULL,
    work_key CHAR(64) NULL,
    track_snapshot JSON NOT NULL,
    selection JSON NOT NULL,
    validated_at DOUBLE NOT NULL,
    published_at DOUBLE NOT NULL,
    first_exposed_at DOUBLE NULL,
    last_exposed_at DOUBLE NULL,
    exposure_count INT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (batch_id, position),
    UNIQUE KEY discovery_item_track (batch_id, track_key),
    UNIQUE KEY discovery_item_recording (batch_id, recording_key),
    KEY discovery_item_recording_lookup (recording_key),
    KEY discovery_item_work_lookup (work_key),
    KEY discovery_item_published (published_at),
    FOREIGN KEY (batch_id)
        REFERENCES music_discovery_batches(batch_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_events (
    user_id BIGINT NOT NULL,
    event_id CHAR(36) NOT NULL,
    track_key CHAR(64) NOT NULL,
    batch_id CHAR(32) NULL,
    position SMALLINT UNSIGNED NULL,
    playback_id CHAR(36) NULL,
    seq INT UNSIGNED NULL,
    event_type VARCHAR(24) NOT NULL,
    origin VARCHAR(16) NOT NULL,
    listened_ms INT UNSIGNED NOT NULL DEFAULT 0,
    covered_ms INT UNSIGNED NOT NULL DEFAULT 0,
    duration_ms INT UNSIGNED NULL,
    terminal_reason VARCHAR(24) NOT NULL DEFAULT '',
    payload JSON NOT NULL,
    occurred_at DOUBLE NOT NULL,
    received_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, event_id),
    UNIQUE KEY discovery_event_sequence
        (user_id, playback_id, seq),
    KEY discovery_event_owner_time (user_id, received_at),
    KEY discovery_event_track_time
        (user_id, track_key, received_at),
    KEY discovery_event_item (batch_id, position),
    KEY discovery_event_gc (received_at),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, track_key)
        REFERENCES music_discovery_identities(user_id, track_key)
        ON DELETE CASCADE,
    FOREIGN KEY (batch_id, position)
        REFERENCES music_discovery_items(batch_id, position)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE music_discovery_feedback (
    user_id BIGINT NOT NULL,
    scope VARCHAR(16) NOT NULL,
    target_key CHAR(64) NOT NULL,
    reason VARCHAR(16) NOT NULL,
    note VARCHAR(300) NOT NULL DEFAULT '',
    expires_at DOUBLE NULL,
    revoked_at DOUBLE NULL,
    source_event_id CHAR(36) NOT NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, scope, target_key, reason),
    KEY discovery_feedback_expiry (user_id, expires_at),
    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
