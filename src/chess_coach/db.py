import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache(url TEXT PRIMARY KEY, body TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS game_index(game_id TEXT PRIMARY KEY, archive_url TEXT);
CREATE TABLE IF NOT EXISTS analyses(
  game_url TEXT, username TEXT, depth INT, result TEXT, created_at REAL,
  PRIMARY KEY(game_url, username, depth));
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    """SQLite cache: HTTP bodies, game-id -> archive index, engine analyses, kv."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path, timeout=30)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        finally:
            con.close()

    def get_http(self, url):
        with self._conn() as c:
            return c.execute(
                "SELECT body, fetched_at FROM http_cache WHERE url=?", (url,)
            ).fetchone()

    def put_http(self, url, body, now=None):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO http_cache VALUES(?,?,?)",
                (url, body, now or time.time()),
            )

    def index_games(self, game_ids, archive_url):
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO game_index VALUES(?,?)",
                [(i, archive_url) for i in game_ids],
            )

    def archive_for_game(self, game_id):
        with self._conn() as c:
            r = c.execute(
                "SELECT archive_url FROM game_index WHERE game_id=?", (game_id,)
            ).fetchone()
        return r[0] if r else None

    def get_analysis(self, game_url, username, depth):
        """Cached analysis at >= the requested depth (shallowest sufficient one)."""
        with self._conn() as c:
            r = c.execute(
                "SELECT result FROM analyses WHERE game_url=? AND username=? AND depth>=?"
                " ORDER BY depth LIMIT 1",
                (game_url, username, depth),
            ).fetchone()
        return json.loads(r[0]) if r else None

    def put_analysis(self, game_url, username, depth, result):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO analyses VALUES(?,?,?,?,?)",
                (game_url, username, depth, json.dumps(result), time.time()),
            )

    def get_kv(self, key):
        with self._conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_kv(self, key, value):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, str(value)))
