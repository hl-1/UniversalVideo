-- Candidate feeds and evidence-grounded profile learning. Preserve the V1-V3 history.
ALTER TABLE music_discovery_profiles
    ADD COLUMN evidence_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    ADD COLUMN control_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    ADD COLUMN agent_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    ADD COLUMN agent_source_revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    ADD COLUMN agent_state JSON NULL,
    ADD COLUMN agent_updated_at DOUBLE NULL,
    ADD COLUMN learning_since DOUBLE NULL,
    ADD COLUMN next_review_at DOUBLE NULL,
    ADD KEY discovery_profile_review (next_review_at, user_id);

CREATE TABLE music_discovery_candidates (
    user_id BIGINT NOT NULL,
    track_key CHAR(64) NOT NULL,
    recording_key CHAR(64) NOT NULL,
    work_key CHAR(64) NULL,
    track_snapshot JSON NOT NULL,
    analysis JSON NOT NULL,
    validation JSON NOT NULL,
    source_run_id CHAR(32) NULL,
    policy_version VARCHAR(40) NOT NULL,
    validated_at DOUBLE NULL,
    validation_expires_at DOUBLE NULL,
    expires_at DOUBLE NOT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, track_key),
    KEY discovery_candidate_recording (user_id, recording_key),
    KEY discovery_candidate_work (user_id, work_key),
    KEY discovery_candidate_expiry (user_id, expires_at),
    KEY discovery_candidate_validation (user_id, validation_expires_at),
    FOREIGN KEY (user_id, track_key)
        REFERENCES music_discovery_identities(user_id, track_key)
        ON DELETE CASCADE,
    FOREIGN KEY (source_run_id)
        REFERENCES music_recommendation_runs(run_id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_discovery_feeds (
    feed_id CHAR(32) NOT NULL,
    user_id BIGINT NOT NULL,
    recommendation_date DATE NOT NULL,
    request_key CHAR(64) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    profile_revision BIGINT UNSIGNED NOT NULL,
    policy_version VARCHAR(40) NOT NULL,
    generation_seed CHAR(64) NOT NULL,
    target_count SMALLINT UNSIGNED NOT NULL,
    actual_count SMALLINT UNSIGNED NOT NULL,
    saved_playlist_id VARCHAR(64) NULL,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (feed_id),
    UNIQUE KEY discovery_feed_request (user_id, request_key),
    KEY discovery_feed_latest (user_id, created_at),
    KEY discovery_feed_date (user_id, recommendation_date),
    KEY discovery_feed_gc (created_at),
    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_discovery_feed_items (
    feed_id CHAR(32) NOT NULL,
    position SMALLINT UNSIGNED NOT NULL,
    track_key CHAR(64) NOT NULL,
    recording_key CHAR(64) NOT NULL,
    work_key CHAR(64) NULL,
    track_snapshot JSON NOT NULL,
    selection JSON NOT NULL,
    validated_at DOUBLE NOT NULL,
    first_exposed_at DOUBLE NULL,
    last_exposed_at DOUBLE NULL,
    exposure_count INT UNSIGNED NOT NULL DEFAULT 0,
    PRIMARY KEY (feed_id, position),
    UNIQUE KEY discovery_feed_item_track (feed_id, track_key),
    UNIQUE KEY discovery_feed_item_recording (feed_id, recording_key),
    KEY discovery_feed_item_recording_lookup (recording_key),
    FOREIGN KEY (feed_id)
        REFERENCES music_discovery_feeds(feed_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_discovery_daily_usage (
    user_id BIGINT NOT NULL,
    usage_date DATE NOT NULL,
    token_limit INT UNSIGNED NOT NULL,
    model_call_limit INT UNSIGNED NOT NULL,
    network_request_limit INT UNSIGNED NOT NULL,
    actual_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    reserved_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    unconfirmed_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0,
    model_calls INT UNSIGNED NOT NULL DEFAULT 0,
    network_requests INT UNSIGNED NOT NULL DEFAULT 0,
    next_refill_at DOUBLE NOT NULL DEFAULT 0,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    PRIMARY KEY (user_id, usage_date),
    KEY discovery_usage_date (usage_date),
    FOREIGN KEY (user_id)
        REFERENCES users(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_discovery_profile_runs (
    run_id CHAR(32) NOT NULL,
    user_id BIGINT NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    trigger_type VARCHAR(24) NOT NULL,

    source_revision BIGINT UNSIGNED NOT NULL,
    base_control_revision BIGINT UNSIGNED NOT NULL,
    base_agent_revision BIGINT UNSIGNED NOT NULL,
    input_hash CHAR(64) NOT NULL,

    policy_version VARCHAR(40) NOT NULL,
    skill_version CHAR(64) NOT NULL,

    input_snapshot JSON NOT NULL,
    proposal JSON NULL,
    context JSON NOT NULL,
    steps JSON NOT NULL,
    applied_version BIGINT UNSIGNED NULL,

    accounting_date DATE NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
    worker_id VARCHAR(64) NULL,
    lease_until DOUBLE NULL,

    error_code VARCHAR(64) NULL,
    error_message VARCHAR(300) NULL,

    created_at DOUBLE NOT NULL,
    started_at DOUBLE NULL,
    finished_at DOUBLE NULL,
    updated_at DOUBLE NOT NULL,

    active_user_id BIGINT GENERATED ALWAYS AS (
        CASE
            WHEN status IN (
                'pending', 'running', 'usage_pending'
            )
            THEN user_id
            ELSE NULL
        END
    ) VIRTUAL,

    PRIMARY KEY (run_id),
    UNIQUE KEY discovery_profile_input (user_id, input_hash),
    UNIQUE KEY discovery_profile_active (active_user_id),
    KEY discovery_profile_queue (
        status, lease_until, created_at
    ),
    KEY discovery_profile_owner (
        user_id, created_at
    ),
    KEY discovery_profile_accounting (
        user_id, accounting_date
    ),

    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE music_discovery_profile_versions (
    user_id BIGINT NOT NULL,
    version BIGINT UNSIGNED NOT NULL,
    base_version BIGINT UNSIGNED NOT NULL,
    source VARCHAR(16) NOT NULL,
    run_id CHAR(32) NULL,

    evidence_revision BIGINT UNSIGNED NOT NULL,
    control_revision BIGINT UNSIGNED NOT NULL,
    policy_version VARCHAR(40) NOT NULL,

    snapshot JSON NOT NULL,
    changes JSON NOT NULL,
    evidence_summary JSON NOT NULL,
    summary VARCHAR(500) NOT NULL,

    created_at DOUBLE NOT NULL,

    PRIMARY KEY (user_id, version),
    KEY discovery_profile_version_time (
        user_id, created_at
    ),
    KEY discovery_profile_version_run (run_id),

    FOREIGN KEY (user_id)
        REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
