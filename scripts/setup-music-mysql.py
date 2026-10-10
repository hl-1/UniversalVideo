"""Provision an isolated music database using an existing local MySQL container."""
import argparse
import json
import secrets
import subprocess
from pathlib import Path

import pymysql


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--container")
    parser.add_argument("--local-config", type=Path)
    args = parser.parse_args()
    config_path = Path(__file__).resolve().parent.parent / "config" / "database.json"
    if args.local_config:
        administrator = json.loads(args.local_config.read_text(encoding="utf-8"))
        username = f"videodream_{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(32)
        with pymysql.connect(**administrator, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE DATABASE IF NOT EXISTS videodream CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                cursor.execute("CREATE USER %s@'localhost' IDENTIFIED BY %s", (username, password))
                cursor.execute("GRANT ALL PRIVILEGES ON videodream.* TO %s@'localhost'", (username,))
            connection.commit()
        config = {**administrator, "database": "videodream", "user": username, "password": password}
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    elif config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        if not args.container:
            parser.error("Provide --container or --local-config")
        username = f"videodream_{secrets.token_hex(4)}"
        password = secrets.token_urlsafe(32)
        sql = (
            "CREATE DATABASE IF NOT EXISTS videodream CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
            f"CREATE USER '{username}'@'%' IDENTIFIED BY '{password}';\n"
            f"GRANT ALL PRIVILEGES ON videodream.* TO '{username}'@'%';\n"
        )
        result = subprocess.run(
            ["docker", "exec", "-i", args.container, "sh", "-c",
             'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot'],
            input=sql, text=True, capture_output=True, timeout=30,
        )
        if result.returncode:
            raise SystemExit("MySQL provisioning failed. Check the container's local administrator access.")
        config = {"host": "127.0.0.1", "port": 3306, "database": "videodream", "user": username, "password": password}
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    with pymysql.connect(**config, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE(), VERSION()")
            database, version = cursor.fetchone()
    print(f"MySQL ready: {database}, server {version}. Credentials saved locally in config/database.json.")


if __name__ == "__main__":
    main()
