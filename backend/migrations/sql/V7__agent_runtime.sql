CREATE TABLE agent_tasks (
  id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  parent_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
  root_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NULL,
  user_id BIGINT NOT NULL,
  kind VARCHAR(32) NOT NULL, engine VARCHAR(24) NOT NULL,
  graph_version VARCHAR(64) NOT NULL, skill_version VARCHAR(64) NOT NULL,
  business_ref VARCHAR(96) NULL, status VARCHAR(32) NOT NULL, worker_id VARCHAR(64) NULL,
  idempotency_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 0, next_event_seq BIGINT UNSIGNED NOT NULL DEFAULT 0,
  attempts SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  available_at DATETIME(3) NOT NULL, lease_until DATETIME(3) NULL,
  started_at DATETIME(3) NULL, finished_at DATETIME(3) NULL,
  request JSON NOT NULL, context JSON NOT NULL, limits JSON NOT NULL, `usage` JSON NOT NULL, result JSON NULL,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY agent_task_idempotency (user_id,idempotency_key),
  KEY agent_task_schedule (status,available_at,lease_until),
  KEY agent_task_user (user_id,kind,created_at), KEY agent_task_parent (parent_id,status),
  KEY agent_task_root (root_id),
  FOREIGN KEY (user_id) REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY (parent_id) REFERENCES agent_tasks(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY (root_id) REFERENCES agent_tasks(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE agent_actions (
  id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  task_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  step_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  observation_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  kind VARCHAR(24) NOT NULL, tool VARCHAR(80) NOT NULL, status VARCHAR(24) NOT NULL,
  approval_state VARCHAR(16) NOT NULL, external_ref VARCHAR(128) NULL, error_code VARCHAR(64) NULL,
  approved_by BIGINT NULL, attempts SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  arguments JSON NOT NULL, result JSON NULL, approval_expires_at DATETIME(3) NULL,
  started_at DATETIME(3) NULL, finished_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY agent_action_step (task_id,step_key), KEY agent_action_approval (approval_state,approval_expires_at),
  KEY agent_action_approver (approved_by),
  FOREIGN KEY (task_id) REFERENCES agent_tasks(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY (approved_by) REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE agent_model_calls (
  id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  task_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  root_task_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  user_id BIGINT NOT NULL, accounting_day DATE NOT NULL,
  phase VARCHAR(24) NOT NULL, provider VARCHAR(80) NOT NULL, model VARCHAR(128) NOT NULL,
  state VARCHAR(16) NOT NULL, provider_request_id VARCHAR(128) NULL,
  response_ref VARCHAR(512) NULL, error_code VARCHAR(64) NULL,
  request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  attempt SMALLINT UNSIGNED NOT NULL DEFAULT 0, dispatched BOOLEAN NOT NULL DEFAULT FALSE,
  reserved_tokens BIGINT UNSIGNED NOT NULL DEFAULT 0, actual_tokens BIGINT UNSIGNED NULL,
  `usage` JSON NOT NULL, pricing JSON NULL, response JSON NULL,
  started_at DATETIME(3) NULL, settled_at DATETIME(3) NULL,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL,
  UNIQUE KEY agent_call_replay (task_id,request_hash,attempt),
  KEY agent_call_daily (user_id,accounting_day,state), KEY agent_call_root (root_task_id,state),
  FOREIGN KEY (task_id) REFERENCES agent_tasks(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY (root_task_id) REFERENCES agent_tasks(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY (user_id) REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE agent_checkpoints (
  task_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  namespace VARCHAR(255) NOT NULL, checkpoint_id VARCHAR(64) NOT NULL,
  parent_checkpoint_id VARCHAR(64) NULL, payload_format VARCHAR(32) NOT NULL,
  payload LONGBLOB NOT NULL, metadata JSON NOT NULL, pending_writes JSON NOT NULL,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (task_id,namespace,checkpoint_id), KEY agent_checkpoint_time (task_id,namespace,created_at),
  FOREIGN KEY (task_id) REFERENCES agent_tasks(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE agent_events (
  task_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  sequence_no BIGINT UNSIGNED NOT NULL, event_type VARCHAR(40) NOT NULL, payload JSON NOT NULL,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (task_id,sequence_no), KEY agent_event_retention (created_at),
  FOREIGN KEY (task_id) REFERENCES agent_tasks(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
