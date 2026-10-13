"""Serial, cached client for the chess.com public Published-Data API."""
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import httpx

from .config import API, CURRENT_MONTH_TTL, DEFAULT_USER, USER_AGENT

log = logging.getLogger("chess_coach.api")


class ChessComError(RuntimeError):
    pass


def month_end_ts(year: int, month: int) -> float:
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return datetime(ny, nm, 1, tzinfo=timezone.utc).timestamp()


def game_id(url: str) -> str | None:
    m = re.search(r"(\d{6,})", url.split("?")[0])
    return m.group(1) if m else None


class ChessComClient:
    def __init__(self, store, http=None, sleep=time.sleep, now=time.time):
        self.store = store
        self.http = http or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
            follow_redirects=True,
        )
        self._sleep = sleep
        self._now = now
        self._lock = threading.Lock()  # one request at a time (parallel -> 429)

    def _fetch(self, url):
        """GET json with retry/backoff on 429/5xx. None on 404."""
        with self._lock:
            delay = 1.0
            for attempt in range(5):
                try:
                    r = self.http.get(url)
                except httpx.TransportError as e:
                    log.warning("transport error %s (%s)", url, e)
                    r = None
                if r is not None:
                    if r.status_code == 404:
                        return None
                    if r.status_code != 429 and r.status_code < 500:
                        r.raise_for_status()
                        return r.json()
                    ra = r.headers.get("Retry-After", "")
                    wait = float(ra) if ra.isdigit() else delay
                    log.warning("HTTP %s on %s, retrying in %.0fs", r.status_code, url, wait)
                else:
                    wait = delay
                if attempt < 4:
                    self._sleep(min(wait, 30))
                    delay *= 2
            raise ChessComError(f"giving up on {url}")

    def _cached(self, url, is_fresh):
        """Return (data|None, fetched_now)."""
        row = self.store.get_http(url)
        if row and is_fresh(row[1]):
            return json.loads(row[0]), False
        try:
            data = self._fetch(url)
        except ChessComError:
            if row:
                log.warning("using stale cache for %s", url)
                return json.loads(row[0]), False
            raise
        if data is None:
            return None, False
        self.store.put_http(url, json.dumps(data), self._now())
        return data, True

    def _ttl(self, ttl):
        return lambda fetched: self._now() - fetched < ttl

    def profile(self, username):
        data, _ = self._cached(f"{API}/player/{username.lower()}", self._ttl(3600))
        return data

    def stats(self, username):
        data, _ = self._cached(f"{API}/player/{username.lower()}/stats", self._ttl(CURRENT_MONTH_TTL))
        return data

    def archives(self, username):
        data, _ = self._cached(
            f"{API}/player/{username.lower()}/games/archives", self._ttl(CURRENT_MONTH_TTL)
        )
        return (data or {}).get("archives", [])

    def month(self, archive_url):
        """Monthly archive. Final once fetched after the month ended; else 10-minute TTL."""
        m = re.search(r"/(\d{4})/(\d{2})$", archive_url)
        end = month_end_ts(int(m[1]), int(m[2])) if m else float("inf")
        data, fresh = self._cached(
            archive_url, lambda fetched: fetched >= end or self._now() - fetched < CURRENT_MONTH_TTL
        )
        data = data or {}
        if fresh:
            ids = [game_id(g.get("url", "")) for g in data.get("games", [])]
            self.store.index_games([i for i in ids if i], archive_url)
        return data

    def iter_games(self, username, time_class=None, since_ts=None):
        """Standard-chess games, newest first."""
        for url in reversed(self.archives(username)):
            m = re.search(r"/(\d{4})/(\d{2})$", url)
            if since_ts and m and month_end_ts(int(m[1]), int(m[2])) <= since_ts:
                break
            games = self.month(url).get("games", [])
            for g in sorted(games, key=lambda g: g.get("end_time", 0), reverse=True):
                if g.get("rules", "chess") != "chess":
                    continue
                if time_class and time_class != "all" and g.get("time_class") != time_class:
                    continue
                if since_ts and g.get("end_time", 0) < since_ts:
                    continue
                yield g

    def find_game(self, url, username=DEFAULT_USER):
        gid = game_id(url)
        if not gid:
            return None

        def search(archive_url):
            for g in self.month(archive_url).get("games", []):
                if game_id(g.get("url", "")) == gid:
                    return g

        archive = self.store.archive_for_game(gid)
        if archive and (g := search(archive)):
            return g
        for a in reversed(self.archives(username)):  # not indexed yet: scan newest first
            if a != archive and (g := search(a)):
                return g
        return None
