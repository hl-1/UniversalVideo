-- Reverse of 001_ddl_schema.sql ONLY. Stop the application and back up before execution.
-- Drops exactly the 23 tables created by 001, in reverse dependency order.
-- Does not modify flyway_schema_history. For Flyway databases use a reviewed compensation version
-- or restore a matching database backup; do not leave history and schema inconsistent.

DROP TABLE IF EXISTS `music_server_assets`;
DROP TABLE IF EXISTS `music_track_features`;
DROP TABLE IF EXISTS `music_recommendation_runs`;
DROP TABLE IF EXISTS `video_view_states`;
DROP TABLE IF EXISTS `video_mind_maps`;
DROP TABLE IF EXISTS `video_summaries`;
DROP TABLE IF EXISTS `video_parse_records`;
DROP TABLE IF EXISTS `video_recent_links`;
DROP TABLE IF EXISTS `account_migrations`;
DROP TABLE IF EXISTS `resources`;
DROP TABLE IF EXISTS `audit_logs`;
DROP TABLE IF EXISTS `system_settings`;
DROP TABLE IF EXISTS `users`;
DROP TABLE IF EXISTS `music_theme`;
DROP TABLE IF EXISTS `music_lyrics`;
DROP TABLE IF EXISTS `music_track_aliases`;
DROP TABLE IF EXISTS `music_downloads`;
DROP TABLE IF EXISTS `music_playlist_tracks`;
DROP TABLE IF EXISTS `music_playlists`;
DROP TABLE IF EXISTS `music_history`;
DROP TABLE IF EXISTS `music_favorites`;
DROP TABLE IF EXISTS `music_tracks`;
DROP TABLE IF EXISTS `music_profile`;
