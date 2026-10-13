"""Tool logic (kept separate from the MCP wiring so it can be tested)."""
import io
import logging
import sys
import threading
import time
from datetime import datetime, timezone

import chess
import chess.pgn

from . import positions, training
from .analysis import Ev, Engine, OutOfTime, analyze_pgn, count_moves, iter_review
from .chesscom import ChessComClient
from .config import DB_PATH, DEFAULT_USER, MOVE_TIME_CAP
from .db import Store
from .games import GameInfo, summarize
from .report import (coaching_report, compute_metrics, game_analysis, metrics_line, mmss,
                     review_table)

log = logging.getLogger("chess_coach")
MIN_PLIES = 6  # shorter games (aborts, early resigns) have nothing to coach
BUDGET_S = 35  # per tool call; the bridge to the caller times out at 60 s


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
    def _resolve(self, game_url, username, deadline=None):
        """(GameInfo, None) or (None, message). game_url may be "latest"."""
        if (game_url or "").strip().lower() == "latest":
            infos = self._infos(username, "all", None, 1)
            return (infos[0], None) if infos else (None, "No games found.")
        g = self.client.find_game(game_url, username, deadline)
        if not g:
            return None, f"Game not found in the recent archives: {game_url}"
        info = summarize(g, username)
        if not info:
            return None, "Not a standard-chess game played by " + username
        return info, None

    def _game_evaluator(self, depth, deadline=None):
        """Engine evaluator backed by the per-position cache; raises OutOfTime past `deadline`."""
        raw = self.engine.evaluator(depth, MOVE_TIME_CAP)

        def evaluate(board, me):
            if board.is_game_over():
                return raw(board, me)
            key = board.epd()
            hit = self.store.get_pos(key, depth)
            if hit:
                return Ev(hit[0] if me else -hit[0], hit[1])
            if deadline and time.time() > deadline:
                raise OutOfTime
            ev = raw(board, me)
            self.store.put_pos(key, depth, ev.cp if me else -ev.cp, ev.pv)
            return ev
        return evaluate

    def _cached_analysis(self, info, username, depth):
        c = self.store.get_analysis(info.url, username.lower(), depth)
        return c if c and c.get("v", 1) >= 3 else None  # older results lack think times / cause tags

    def _analysis(self, info: GameInfo, username, depth, deadline=None):
        """(result, cached) or (None, False) if the time budget ran out (progress stays cached)."""
        cached = self._cached_analysis(info, username, depth)
        if cached:
            return cached, True
        with self._analysis_lock:
            t0 = time.time()
            try:
                res = analyze_pgn(info.pgn, info.color == "white",
                                  self._game_evaluator(depth, deadline), info.init_clock, info.increment)
            except OutOfTime:
                print(f"  out of time on {info.url}", file=sys.stderr, flush=True)
                return None, False
            print(f"  analysed {info.url} ({info.plies} plies) in {time.time() - t0:.0f}s",
                  file=sys.stderr, flush=True)
        self.store.put_analysis(info.url, username.lower(), depth, res)
        training.add_from_result(self.store, info.url, username.lower(), res, info.date, info.opp)
        return res, False

    def analyze_game(self, game_url, depth=14, username=DEFAULT_USER):
        deadline = time.time() + BUDGET_S
        info, err = self._resolve(game_url, username, deadline)
        if err:
            return err
        if info.plies < 2:
            return "Game has no moves to analyse."
        res, _ = self._analysis(info, username, depth, deadline)
        if res is None:
            return "Analysis not finished within the time budget; call again to continue (progress is cached)."
        return game_analysis(info, res, depth)

    def analyze_recent(self, username=DEFAULT_USER, time_class="rapid", n=20, depth=12,
                       time_budget_s=40):
        deadline = time.time() + time_budget_s if time_budget_s else None
        infos = self._infos(username, time_class, None, n)
        if not infos:
            return f"No {time_class} games found."
        pairs, pending, short = [], 0, 0
        for k, info in enumerate(infos, 1):
            if info.plies < MIN_PLIES:
                short += 1
                continue
            cached = self._cached_analysis(info, username, depth)
            if not cached and deadline and time.time() > deadline:
                pending += 1
                continue
            print(f"[{k}/{len(infos)}] {'cached' if cached else 'analysing'} {info.url}",
                  file=sys.stderr, flush=True)
            res = cached or self._analysis(info, username, depth, deadline)[0]
            if res is None:
                pending += 1
                continue
            pairs.append((info, res))
        return coaching_report(username.lower(), time_class, pairs, infos, depth, pending, short)

    # -- position-level tools -------------------------------------------
    def position_at(self, game_url, move_number, side, username=DEFAULT_USER):
        side = (side or "").lower()
        if side not in ("white", "black"):
            return "side must be 'white' or 'black'."
        info, err = self._resolve(game_url, username, time.time() + 25)
        if err:
            return err
        game = chess.pgn.read_game(io.StringIO(info.pgn))
        nodes = list(game.mainline())
        ply = 2 * (int(move_number) - 1) + (side == "black")
        if ply < 0 or ply >= len(nodes):
            return f"No such move: game has {len(nodes)} plies ({info.moves} moves)."
        node = nodes[ply]
        board = node.parent.board()
        prev = nodes[ply - 2].clock() if ply >= 2 else info.init_clock
        label = f"{board.fullmove_number}{'.' if board.turn else '...'}{board.san(node.move)}"
        clk = f"clock {mmss(node.clock())} after the move" + (f" ({mmss(prev)} before)" if prev is not None else "")
        head = (f"{info.date} {info.color[0].upper()} vs {info.opp} ({info.opp_rating}) "
                f"{info.result[0].upper()} {info.how} | {info.url}")
        return "\n".join([head, f"Before {label} ({side} to move) | {clk}",
                          f"FEN: {board.fen()}", f"Played: {board.san(node.move)}", positions.ascii_board(board)])

    def eval_position(self, fen, depth=18, multipv=3, max_seconds=15):
        return positions.eval_position(self.engine, fen, depth, multipv, max_seconds)

    def evaluate_move(self, fen, move, depth=18, max_seconds=15):
        return positions.evaluate_move(self.engine, fen, move, depth, max_seconds)

    def explore_line(self, fen, moves, depth=16, max_seconds=20):
        return positions.explore_line(self.engine, fen, moves, depth, max_seconds)

    def review_moves(self, game_url, side="me", depth=14, username=DEFAULT_USER):
        deadline = time.time() + BUDGET_S
        info, err = self._resolve(game_url, username, deadline - 10)
        if err:
            return err
        side = (side or "me").lower()
        mine = chess.WHITE if info.color == "white" else chess.BLACK
        colors = {"me": [mine], "opponent": [not mine], "white": [chess.WHITE],
                  "black": [chess.BLACK], "both": [chess.WHITE, chess.BLACK]}.get(side)
        if colors is None:
            return "side must be me, opponent, white, black or both."
        total = count_moves(info.pgn, colors)
        rows, done = [], True
        try:
            for r in iter_review(info.pgn, colors, self._game_evaluator(depth, deadline)):
                # pieces this move newly leaves en prise (win% often hides it when far ahead)
                b = chess.Board(r["fen"])
                r["hangs"] = [h for h in positions.en_prise_after(b, chess.Move.from_uci(r["uci"]))
                              if not h["was"] and h["net"] >= 2]
                rows.append(r)
        except OutOfTime:
            done = False
        return review_table(info, rows, total, depth, side, done)

    # -- training --------------------------------------------------------
    def get_puzzle(self, tag=None, hint=False, username=DEFAULT_USER):
        return training.get_puzzle(self.store, username.lower(), tag, hint)

    def submit_answer(self, puzzle_id, move):
        return training.submit_answer(self.store, self.engine, int(puzzle_id), move)

    # -- notes / progress ------------------------------------------------
    def save_note(self, text, topic="focus"):
        nid = self.store.add_note(text.strip(), topic or "focus")
        return f"Saved note #{nid} [{topic}]."

    def get_notes(self, topic=None, limit=10):
        rows = self.store.get_notes(topic, limit)
        if not rows:
            return "No notes yet."
        return "\n".join(f"#{r['id']} {_ts(r['ts'])[:10]} [{r['topic']}] {r['text']}" for r in rows)

    def delete_note(self, note_id):
        return f"Deleted note #{note_id}." if self.store.delete_note(int(note_id)) else f"No note #{note_id}."

    def _snapshot(self, username, time_class="rapid", n=20):
        infos = self._infos(username, time_class, None, n)
        pairs = []
        for i in infos:
            if i.plies < MIN_PLIES:
                continue
            res = self._cached_analysis(i, username, 1)
            if res:
                pairs.append((i, res))
        return compute_metrics(pairs, infos)

    # -- sync marker -----------------------------------------------------
    def mark_synced(self, username=DEFAULT_USER):
        prev = self.store.get_kv("last_sync")
        now = time.time()
        self.store.set_kv("last_sync", now)
        out = [f"Marked synced at {_ts(now)}" + (f" (previous: {_ts(float(prev))})" if prev else "")]
        try:
            snap = self._snapshot(username.lower())
        except Exception as e:  # offline etc.: the marker is still saved
            snap, out = None, out + [f"(no progress snapshot: {type(e).__name__})"]
        if snap:
            pts, before = self.store.last_snapshot(username.lower(), "rapid")
            self.store.add_snapshot(username.lower(), "rapid", snap)
            out.append(f"Progress, last {snap['n']} analysed rapid games"
                       + (f" vs snapshot of {_ts(pts)[:10]} ({before['n']} games)" if before else " (first snapshot)")
                       + ": " + metrics_line(snap, before))
        elif snap is None and len(out) == 1:
            out.append("No analysed games yet, so no progress snapshot (run analyze_recent first).")
        notes = self.store.get_notes("focus", 3)
        if notes:
            out.append("Focus: " + " / ".join(r["text"] for r in notes))
        return "\n".join(out)

    def get_last_sync(self):
        v = self.store.get_kv("last_sync")
        out = f"Last coaching session: {_ts(float(v))}" if v else "No coaching session recorded yet."
        notes = self.store.get_notes("focus", 3)
        if notes:
            out += "\nFocus: " + " / ".join(f"#{r['id']} {r['text']}" for r in notes)
        return out
