"""Tool logic (kept separate from the MCP wiring so it can be tested)."""
import logging
import sys
import threading
import time
from datetime import datetime, timezone

import chess

from .analysis import Engine, analyze_pgn
from .chesscom import ChessComClient
from .config import DB_PATH, DEFAULT_USER, MOVE_TIME_CAP
from .db import Store
from .games import GameInfo, summarize
from .report import coaching_report, game_analysis

log = logging.getLogger("chess_coach")
MIN_PLIES = 6  # shorter games (aborts, early resigns) have nothing to coach


def _ts(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class Coach:
    def __init__(self, store=None, client=None, engine=None):
        self.store = store or Store(DB_PATH)
        self.client = client or ChessComClient(self.store)
        self.engine = engine or Engine()
        self._analysis_lock = threading.Lock()

    # -- profile ---------------------------------------------------------
    def profile_stats(self, username=DEFAULT_USER):
        stats = self.client.stats(username)
        if stats is None:
            return f"No chess.com player '{username}'."
        prof = self.client.profile(username) or {}
        joined = _ts(prof["joined"])[:10] if prof.get("joined") else "?"
        lines = [f"{username.lower()} (joined {joined}, {prof.get('status', '?')})"]
        for tc in ("rapid", "blitz", "bullet"):
            s = stats.get(f"chess_{tc}")
            if not s:
                lines.append(f"{tc}: no games")
                continue
            r, b = s.get("record", {}), s.get("best", {})
            w, l, d = r.get("win", 0), r.get("loss", 0), r.get("draw", 0)
            best = f", best {b['rating']} ({_ts(b['date'])[:10]})" if b.get("rating") else ""
            lines.append(f"{tc}: {s.get('last', {}).get('rating', '?')}{best} | "
                         f"W{w} L{l} D{d} ({100 * w / max(1, w + l + d):.0f}% wins)")
        return "\n".join(lines)

    # -- games -----------------------------------------------------------
    def _parse_since(self, since):
        if not since:
            return None
        if since == "last_sync":
            v = self.store.get_kv("last_sync")
            return float(v) if v else None
        dt = datetime.fromisoformat(since)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()

    def _infos(self, username, time_class, since_ts, limit):
        out = []
        for g in self.client.iter_games(username, time_class, since_ts):
            info = summarize(g, username)
            if info and info.plies > 0:  # skip variants and aborted games with no moves
                out.append(info)
                if len(out) >= limit:
                    break
        return out

    def list_games(self, username=DEFAULT_USER, time_class="rapid", since=None, limit=20):
        since_ts = self._parse_since(since)
        note = ""
        if since == "last_sync" and since_ts is None:
            note = "(no last sync recorded; showing latest games)\n"
        infos = self._infos(username, time_class, since_ts, limit)
        if not infos:
            return note + f"No {time_class} games" + (f" since {_ts(since_ts)}" if since_ts else "") + "."
        lines = [
            f"{i.date} {i.color[0].upper()} vs {i.opp} ({i.opp_rating}) {i.result[0].upper()} {i.how} | "
            f"{i.opening} | {i.moves}mv"
            + (f" | acc {i.accuracy:.0f}" if i.accuracy else "") + f" | {i.url}"
            for i in infos
        ]
        w = sum(i.result == "win" for i in infos)
        l = sum(i.result == "loss" for i in infos)
        head = (f"{len(infos)} {time_class} games for {username.lower()}"
                + (f" since {_ts(since_ts)}" if since_ts else "") + f": {w}W {l}L {len(infos) - w - l}D")
        return note + "\n".join([head] + lines)

    def get_game_pgn(self, game_url, username=DEFAULT_USER):
        g = self.client.find_game(game_url, username)
        return g["pgn"] if g else f"Game not found: {game_url}"

    # -- analysis --------------------------------------------------------
    def _analysis(self, info: GameInfo, username, depth):
        cached = self.store.get_analysis(info.url, username.lower(), depth)
        if cached:
            return cached, True
        with self._analysis_lock:
            t0 = time.time()
            res = analyze_pgn(info.pgn, info.color == "white",
                              self.engine.evaluator(depth, MOVE_TIME_CAP), info.init_clock)
            print(f"  analysed {info.url} ({info.plies} plies) in {time.time() - t0:.0f}s",
                  file=sys.stderr, flush=True)
        self.store.put_analysis(info.url, username.lower(), depth, res)
        return res, False

    def analyze_game(self, game_url, depth=14, username=DEFAULT_USER):
        g = self.client.find_game(game_url, username)
        if not g:
            return f"Game not found: {game_url}"
        info = summarize(g, username)
        if not info:
            return "Not a standard-chess game played by " + username
        if info.plies < 2:
            return "Game has no moves to analyse."
        res, _ = self._analysis(info, username, depth)
        return game_analysis(info, res, depth)

    def analyze_recent(self, username=DEFAULT_USER, time_class="rapid", n=20, depth=12,
                       time_budget_s=45):
        infos = self._infos(username, time_class, None, n)
        if not infos:
            return f"No {time_class} games found."
        deadline = time.time() + time_budget_s if time_budget_s else None
        pairs, pending, short = [], 0, 0
        for k, info in enumerate(infos, 1):
            if info.plies < MIN_PLIES:
                short += 1
                continue
            cached = self.store.get_analysis(info.url, username.lower(), depth)
            if not cached and deadline and time.time() > deadline:
                pending += 1
                continue
            print(f"[{k}/{len(infos)}] {'cached' if cached else 'analysing'} {info.url}",
                  file=sys.stderr, flush=True)
            res, _ = (cached, True) if cached else self._analysis(info, username, depth)
            pairs.append((info, res))
        return coaching_report(username.lower(), time_class, pairs, infos, depth, pending, short)

    # -- sync marker -----------------------------------------------------
    def mark_synced(self):
        prev = self.store.get_kv("last_sync")
        now = time.time()
        self.store.set_kv("last_sync", now)
        return f"Marked synced at {_ts(now)}" + (f" (previous: {_ts(float(prev))})" if prev else "")

    def get_last_sync(self):
        v = self.store.get_kv("last_sync")
        return f"Last coaching session: {_ts(float(v))}" if v else "No coaching session recorded yet."
