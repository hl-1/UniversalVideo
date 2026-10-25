-- v1 basic data. No usable administrator password is stored in SQL.
INSERT INTO users (id, username, password_hash, role, must_change_password, created_at)
VALUES (1, 'admin', '!uninitialized', 'super_admin', TRUE, UNIX_TIMESTAMP());
INSERT INTO music_profile (id, nickname, user_id) VALUES (1, '音乐听众', 1);
