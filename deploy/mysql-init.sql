-- Template for a NEW database on the existing code-generator MySQL instance.
-- Log in interactively with the existing MySQL root account, then paste these
-- statements after replacing the password. Do not save real credentials here.
-- CREATE intentionally fails if these objects already exist; inspect them first.

CREATE DATABASE videodream
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE USER 'videodream_app'@'%'
  IDENTIFIED BY 'REPLACE_WITH_DATABASE_PASSWORD';

GRANT ALL PRIVILEGES ON videodream.* TO 'videodream_app'@'%';

SHOW GRANTS FOR 'videodream_app'@'%';
