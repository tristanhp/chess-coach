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
CREATE TABLE IF NOT EXISTS pos_cache(
  epd TEXT, depth INT, cp_white INT, pv TEXT, PRIMARY KEY(epd, depth));
CREATE TABLE IF NOT EXISTS puzzles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, game_url TEXT, username TEXT, fen TEXT, best TEXT, best_uci TEXT,
  played TEXT, before_cp INT, after_cp INT, phase TEXT, tags TEXT, game_date TEXT, opp TEXT,
  box INT DEFAULT 0, due REAL DEFAULT 0, attempts INT DEFAULT 0, correct INT DEFAULT 0, last_seen REAL,
  UNIQUE(game_url, fen));
CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, topic TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS snapshots(ts REAL, username TEXT, time_class TEXT, data TEXT);
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

    def get_pos(self, epd, depth):
        """(cp from White's view, pv as UCI list) cached at >= depth, else None."""
        with self._conn() as c:
            r = c.execute(
                "SELECT cp_white, pv FROM pos_cache WHERE epd=? AND depth>=? ORDER BY depth LIMIT 1",
                (epd, depth),
            ).fetchone()
        return (r[0], json.loads(r[1])) if r else None

    def put_pos(self, epd, depth, cp_white, pv):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO pos_cache VALUES(?,?,?,?)",
                      (epd, depth, cp_white, json.dumps(pv)))

    # -- puzzles ---------------------------------------------------------
    def add_puzzle(self, p: dict) -> bool:
        cols = ("game_url", "username", "fen", "best", "best_uci", "played", "before_cp", "after_cp",
                "phase", "tags", "game_date", "opp")
        with self._conn() as c:
            cur = c.execute(f"INSERT OR IGNORE INTO puzzles({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            [p.get(k) for k in cols])
            return cur.rowcount > 0

    def _rows(self, sql, args=()):
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def get_puzzle(self, pid):
        r = self._rows("SELECT * FROM puzzles WHERE id=?", (pid,))
        return r[0] if r else None

    def next_puzzle(self, username, tag=None):
        """Most overdue puzzle (lowest box first); falls back to the soonest-due one."""
        like = f"%{tag}%" if tag else "%"
        base = "SELECT * FROM puzzles WHERE username=? AND tags LIKE ? "
        r = self._rows(base + "AND due<=? ORDER BY box, due LIMIT 1", (username, like, time.time()))
        return (r[0], True) if r else ((self._rows(base + "ORDER BY due LIMIT 1", (username, like)) or [None])[0], False)

    def update_puzzle(self, pid, **kw):
        with self._conn() as c:
            c.execute(f"UPDATE puzzles SET {','.join(k + '=?' for k in kw)} WHERE id=?", [*kw.values(), pid])

    def puzzle_counts(self, username):
        r = self._rows("SELECT COUNT(*) total, SUM(due<=?) due, SUM(attempts) att, SUM(correct) cor, "
                       "SUM(box>=3) learned FROM puzzles WHERE username=?", (time.time(), username))[0]
        return {k: (v or 0) for k, v in r.items()}

    def iter_analyses(self):
        return self._rows("SELECT game_url, username, result FROM analyses")

    # -- notes / snapshots -----------------------------------------------
    def add_note(self, text, topic="focus"):
        with self._conn() as c:
            return c.execute("INSERT INTO notes(ts, topic, text) VALUES(?,?,?)", (time.time(), topic, text)).lastrowid

    def get_notes(self, topic=None, limit=10):
        return self._rows("SELECT * FROM notes WHERE (? IS NULL OR topic=?) ORDER BY ts DESC LIMIT ?",
                          (topic, topic, limit))

    def delete_note(self, note_id):
        with self._conn() as c:
            return c.execute("DELETE FROM notes WHERE id=?", (note_id,)).rowcount > 0

    def add_snapshot(self, username, time_class, data):
        with self._conn() as c:
            c.execute("INSERT INTO snapshots VALUES(?,?,?,?)", (time.time(), username, time_class, json.dumps(data)))

    def last_snapshot(self, username, time_class):
        r = self._rows("SELECT ts, data FROM snapshots WHERE username=? AND time_class=? ORDER BY ts DESC LIMIT 1",
                       (username, time_class))
        return (r[0]["ts"], json.loads(r[0]["data"])) if r else (None, None)

    def get_kv(self, key):
        with self._conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_kv(self, key, value):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, str(value)))
