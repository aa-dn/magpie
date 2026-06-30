import os
import psycopg2
import psycopg2.pool
from datetime import datetime, timezone

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            1, 10,
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "5432")),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            dbname=os.environ.get("DB_NAME", "postgres"),
            sslmode="require",
        )
    return _pool


def _conn():
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        pool = _get_pool()
        conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    return _ctx()


def init_db() -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS uploads (
                    id            TEXT PRIMARY KEY,
                    created_at    TIMESTAMPTZ NOT NULL,
                    source_label  TEXT,
                    source_type   TEXT,
                    engines_used  TEXT,
                    total_results INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS selected_results (
                    id          SERIAL PRIMARY KEY,
                    upload_id   TEXT NOT NULL,
                    recorded_at TIMESTAMPTZ NOT NULL,
                    action      TEXT NOT NULL,
                    url         TEXT,
                    title       TEXT,
                    source      TEXT,
                    engine      TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sel_upload
                    ON selected_results(upload_id);
            """)
            cur.execute("""
                ALTER TABLE uploads ADD COLUMN IF NOT EXISTS thumbnail_url TEXT;
            """)


def record_upload(search_id: str, source_label: str, source_type: str,
                  engines: str, total_results: int, thumbnail_url: str = None) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO uploads
                   (id, created_at, source_label, source_type, engines_used, total_results, thumbnail_url)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO NOTHING""",
                (search_id, _now(), source_label, source_type, engines, total_results, thumbnail_url),
            )


def record_selections(upload_id: str, results: list, action: str = "export") -> None:
    now = _now()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO selected_results
                   (upload_id, recorded_at, action, url, title, source, engine)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                [
                    (upload_id, now, action,
                     r.get("url", ""), r.get("title", ""),
                     r.get("source", ""), r.get("engine", ""))
                    for r in results
                ],
            )


def delete_upload(upload_id: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM selected_results WHERE upload_id = %s", (upload_id,))
            cur.execute("DELETE FROM uploads WHERE id = %s", (upload_id,))


def get_stats() -> dict:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM uploads")
            total_uploads = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM selected_results")
            total_selected = cur.fetchone()[0]

            cur.execute("""
                SELECT engine, COUNT(*) AS cnt
                FROM selected_results
                GROUP BY engine
                ORDER BY cnt DESC
            """)
            engine_counts_raw = cur.fetchall()

            cur.execute("""
                SELECT u.id, u.source_label, u.created_at, u.source_type,
                       u.engines_used, u.total_results, u.thumbnail_url,
                       COUNT(s.id) AS selected_count
                FROM uploads u
                LEFT JOIN selected_results s ON s.upload_id = u.id
                GROUP BY u.id
                ORDER BY u.created_at DESC
                LIMIT 500
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    # Flatten multi-engine labels like "Google Lens · Bing" into individual counts
    engine_totals = {}
    for engine_label, cnt in engine_counts_raw:
        for part in (engine_label or "").split("·"):
            key = part.strip()
            if key:
                engine_totals[key] = engine_totals.get(key, 0) + cnt

    return {
        "total_uploads": total_uploads,
        "total_selected": total_selected,
        "engine_selection_counts": engine_totals,
        "rows": rows,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)
