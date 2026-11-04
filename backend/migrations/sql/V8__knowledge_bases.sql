CREATE TABLE knowledge_bases (
  id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  owner_id BIGINT NOT NULL, name VARCHAR(100) NOT NULL, status VARCHAR(16) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 1, config JSON NOT NULL,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL,
  KEY knowledge_base_owner (owner_id,status,updated_at),
  FOREIGN KEY (owner_id) REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE knowledge_members (
  kb_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  user_id BIGINT NOT NULL, role VARCHAR(16) NOT NULL,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (kb_id,user_id), KEY knowledge_member_user (user_id,kb_id),
  FOREIGN KEY (kb_id) REFERENCES knowledge_bases(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY (user_id) REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE knowledge_documents (
  id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
  kb_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  uploaded_by BIGINT NOT NULL, title VARCHAR(255) NOT NULL, status VARCHAR(24) NOT NULL,
  active_version INT UNSIGNED NULL, target_version INT UNSIGNED NOT NULL DEFAULT 1,
  revision BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL,
  KEY knowledge_document_base (kb_id,status,updated_at), KEY knowledge_document_uploader (uploaded_by),
  FOREIGN KEY (kb_id) REFERENCES knowledge_bases(id) ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY (uploaded_by) REFERENCES users(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE knowledge_document_versions (
  document_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  version INT UNSIGNED NOT NULL, chunk_count INT UNSIGNED NOT NULL DEFAULT 0,
  source_kind VARCHAR(24) NOT NULL, source_locator VARCHAR(1024) NULL,
  source_key VARCHAR(512) NOT NULL, parsed_key VARCHAR(512) NULL,
  parser_version VARCHAR(64) NOT NULL, embedding_model VARCHAR(128) NOT NULL,
  es_index VARCHAR(96) NOT NULL, status VARCHAR(24) NOT NULL, error VARCHAR(500) NULL,
  sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  pipeline_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  embedding_dimensions SMALLINT UNSIGNED NOT NULL, chunk_config JSON NOT NULL,
  published_at DATETIME(3) NULL, created_at DATETIME(3) NOT NULL, updated_at DATETIME(3) NOT NULL,
  PRIMARY KEY (document_id,version), KEY knowledge_version_status (status,updated_at),
  KEY knowledge_version_content (document_id,sha256,pipeline_hash),
  FOREIGN KEY (document_id) REFERENCES knowledge_documents(id) ON UPDATE RESTRICT ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
