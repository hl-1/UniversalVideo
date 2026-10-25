-- Reverse of 002_dml_seed_data.sql. Changed rows and initialized accounts are preserved.
-- A full restoration after user activity requires the pre-migration backup.
DELETE FROM music_profile WHERE id=1 AND user_id=1 AND nickname='音乐听众'
  AND bio='' AND avatar IS NULL
  AND EXISTS (SELECT 1 FROM users WHERE id=1 AND username='admin' AND password_hash='!uninitialized');
DELETE FROM users WHERE id=1 AND username='admin' AND password_hash='!uninitialized'
  AND role='super_admin' AND status='active' AND must_change_password=TRUE AND auth_version=0
  AND NOT EXISTS (SELECT 1 FROM music_profile WHERE user_id=1)
  AND NOT EXISTS (SELECT 1 FROM music_tracks WHERE user_id=1)
  AND NOT EXISTS (SELECT 1 FROM resources WHERE user_id=1)
  AND NOT EXISTS (SELECT 1 FROM music_recommendation_runs WHERE user_id=1)
  AND NOT EXISTS (SELECT 1 FROM music_track_features WHERE user_id=1);
