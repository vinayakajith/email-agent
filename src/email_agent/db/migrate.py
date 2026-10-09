from pathlib import Path

import psycopg

from email_agent.config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def migrate() -> list[str]:
    """Apply pending numbered .sql migrations in order. Returns the names applied."""
    applied: list[str] = []
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied
