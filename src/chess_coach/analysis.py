"""Stockfish analysis from one player's perspective + move classification."""
import io
import logging
import math
import os
import signal
import threading
from dataclasses import dataclass, field

import chess
import chess.engine
import chess.pgn

from .config import MOVE_TIME_CAP, stockfish_path
from .tactics import VALUES, en_prise_after

log = logging.getLogger("chess_coach.analysis")

MATE = 10000
MATE_THRESHOLD = 9000
# Lichess judgement thresholds (0.1/0.2/0.3 on the [-1,1] winning-chances scale) in win% points
THRESHOLDS = [("blunder", 15.0), ("mistake", 10.0), ("inaccuracy", 5.0)]
MATERIAL_SWING = 3  # a minor piece
WIDE = 3000  # clamp for per-move cp loss (ACPL keeps +-1000); keeps losses visible when far ahead


class OutOfTime(Exception):
    """Raised by evaluators when the tool's time budget is spent (progress stays cached)."""


@dataclass
class Ev:
    """Evaluation from the analysed player's point of view."""
    cp: int
    pv: list = field(default_factory=list)  # UCI moves from this position


def win_pct(cp: float) -> float:
    cp = max(-1000, min(1000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def classify(drop: float) -> str | None:
    for name, limit in THRESHOLDS:
        if drop >= limit:
            return name
    return None


def fmt_eval(cp: int) -> str:
    if cp >= MATE_THRESHOLD:
        d = MATE - cp
        return "mate" if d <= 0 else f"M{d}"
    if cp <= -MATE_THRESHOLD:
        d = MATE + cp
        return "mated" if d <= 0 else f"-M{d}"
    return f"{cp / 100:+.1f}"


def material(board: chess.Board, color: bool) -> int:
    return sum(VALUES[p.piece_type] for p in board.piece_map().values()
               if p.color == color and p.piece_type in VALUES)


def line_gain(board: chess.Board, ucis: list, me: bool) -> int:
    """Material balance change for `me` after playing the line from `board`."""
    b = board.copy()
    start = material(b, me) - material(b, not me)
    for u in ucis:
        try:
            b.push_uci(u)
        except ValueError:
            break
    return material(b, me) - material(b, not me) - start


def game_phase(board: chess.Board) -> str:
    if board.fullmove_number <= 12:
        return "opening"
    npm = sum(VALUES[p.piece_type] for p in board.piece_map().values()
              if p.piece_type not in (chess.PAWN, chess.KING))
    return "endgame" if npm <= 20 else "middlegame"


class Engine:
    """One long-lived Stockfish process, reused across all analyses."""

    def __init__(self, path=None):
        self.path = path
        self._eng = None
        self._lock = threading.Lock()

    def _start(self):
        path = self.path or stockfish_path()
        log.info("starting %s", path)
        self._eng = chess.engine.SimpleEngine.popen_uci(path)
        self._eng.configure({"Threads": max(1, min(4, os.cpu_count() or 1)), "Hash": 128})

    def analyse(self, board, depth, time_cap=MOVE_TIME_CAP, multipv=None):
        limit = chess.engine.Limit(depth=depth, time=time_cap)
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._eng is None:
                        self._start()
                    return self._eng.analyse(
                        board, limit, multipv=multipv,
                        info=chess.engine.INFO_SCORE | chess.engine.INFO_PV,
                    )
                except (chess.engine.EngineTerminatedError, BrokenPipeError):
                    self._eng = None
                    if attempt:
                        raise

    def evaluator(self, depth, time_cap=MOVE_TIME_CAP, pv_len=4):
        def evaluate(board: chess.Board, me: bool) -> Ev:
            if board.is_checkmate():
                return Ev(MATE if board.turn != me else -MATE)
            if board.is_game_over():
                return Ev(0)
            info = self.analyse(board, depth, time_cap)
            score = info["score"].pov(me)
            return Ev(score.score(mate_score=MATE), [m.uci() for m in info.get("pv", [])[:pv_len]])
        return evaluate

    def close(self):
        """Stop Stockfish. Killing the process directly: SimpleEngine.quit() can hang at interpreter exit."""
        with self._lock:
            eng, self._eng = self._eng, None
        if eng is not None:
            try:
                os.kill(eng.transport.get_pid(), signal.SIGTERM)
            except Exception:
                pass


def analyze_pgn(pgn_text: str, me: bool, evaluate, init_clock=None, increment=0.0) -> dict:
    """Classify `me`'s moves. `evaluate(board, me) -> Ev` (from me's point of view)."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("no game in PGN")
    memo = {}

    def ev(board):
        key = board.epd()
        if key not in memo:
            memo[key] = evaluate(board, me)
        return memo[key]

    counts = {"inaccuracy": 0, "mistake": 0, "blunder": 0}
    by_phase = {p: dict(counts) for p in ("opening", "middlegame", "endgame")}
    errors, losses = [], []
    n_missed_mate = n_missed_mat = n_hung = my_moves = adv_moves = adv_unflagged = 0
    clock_at_20 = None
    thinks, prev_clock = [], init_clock
    board = game.board()
    plies = sum(1 for _ in game.mainline_moves())

    for node in game.mainline():
        before_board, move = board.copy(), node.move
        board.push(move)
        if before_board.turn != me:
            continue
        my_moves += 1
        if before_board.fullmove_number == 20:
            clock_at_20 = node.clock()
        before, after = ev(before_board), ev(board)
        best_uci = before.pv[0] if before.pv else None
        played_best = move.uci() == best_uci
        drop = 0.0 if played_best else max(0.0, win_pct(before.cp) - win_pct(after.cp))
        if not played_best:
            clamp = lambda c: max(-1000, min(1000, c))  # noqa: E731
            losses.append(max(0, clamp(before.cp) - clamp(after.cp)))
        else:
            losses.append(0)
        cls = classify(drop)
        if before.cp > 500:  # winning by more than +5: material-losing moves can slip under the win% threshold
            adv_moves += 1
            wide_loss = 0 if played_best else max(0, min(before.cp, WIDE) - min(after.cp, WIDE))
            if wide_loss >= 150 and cls in (None, "inaccuracy"):
                adv_unflagged += 1
        flags = []
        if not played_best and before.cp >= MATE_THRESHOLD and after.cp < MATE_THRESHOLD:
            flags.append("missed_mate")
            n_missed_mate += 1
        elif cls and best_uci:
            best_gain = line_gain(before_board, before.pv[:4], me)
            actual = line_gain(before_board, [move.uci()] + after.pv[:3], me)
            if best_gain >= MATERIAL_SWING and best_gain - actual >= MATERIAL_SWING:
                flags.append("missed_material")
                n_missed_mat += 1
        if cls in ("mistake", "blunder"):
            if line_gain(before_board, [move.uci()] + after.pv[:2], me) <= -MATERIAL_SWING:
                flags.append("hung_piece")
                n_hung += 1
        cl = node.clock()
        think = None
        if cl is not None and prev_clock is not None:
            think = round(max(0.0, prev_clock + increment - cl), 1)
        thinks.append(think)
        prev_clock = cl if cl is not None else prev_clock
        if cls in ("mistake", "blunder"):  # why: static tactics check on the position before/after the move
            for h in en_prise_after(before_board, move):
                if h["net"] >= 2:
                    flag = "ignored_threat" if h["was"] else "left_hanging"
                    if flag not in flags:
                        flags.append(flag)
            if after.cp <= -MATE_THRESHOLD < before.cp:
                flags.append("allowed_mate")
        if not cls and not flags:
            continue
        if cls:
            counts[cls] += 1
            by_phase[game_phase(before_board)][cls] += 1
        san = before_board.san(move)
        best_san = before_board.san(chess.Move.from_uci(best_uci)) if best_uci else None
        n = before_board.fullmove_number
        errors.append({
            "n": f"{n}.{san}" if me == chess.WHITE else f"{n}...{san}",
            "best": best_san, "cls": cls, "flags": flags, "drop": round(drop, 1),
            "before": before.cp, "after": after.cp, "phase": game_phase(before_board),
            "clock": node.clock(), "think": think, "fen": before_board.fen(),
        })

    serious = [e for e in errors if e["cls"] in ("mistake", "blunder")]
    decisive = max(serious, key=lambda e: e["drop"], default=None)
    return {
        "v": 3, "thinks": thinks, "color": "white" if me else "black", "plies": plies, "my_moves": my_moves,
        "init_clock": init_clock, "counts": counts, "by_phase": by_phase,
        "adv_moves": adv_moves, "adv_unflagged": adv_unflagged,
        "missed_mate": n_missed_mate, "missed_material": n_missed_mat, "hung": n_hung,
        "acpl": round(sum(losses) / len(losses)) if losses else 0,
        "clock_at_20": clock_at_20, "errors": errors, "decisive": decisive,
    }


def iter_review(pgn_text: str, colors, evaluate):
    """Yield one row per move by `colors` (list of chess colors), eval from the mover's view.
    `evaluate` may raise OutOfTime; rows yielded so far stay valid."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("no game in PGN")
    board = game.board()
    for node in game.mainline():
        before_board, move = board.copy(), node.move
        board.push(move)
        me = before_board.turn
        if me not in colors:
            continue
        before, after = evaluate(before_board, me), evaluate(board, me)
        best_uci = before.pv[0] if before.pv else None
        played_best = move.uci() == best_uci
        clamp = lambda c: max(-WIDE, min(WIDE, c))  # noqa: E731
        loss = 0 if played_best else max(0, clamp(before.cp) - clamp(after.cp))
        drop = 0.0 if played_best else max(0.0, win_pct(before.cp) - win_pct(after.cp))
        n = before_board.fullmove_number
        san = before_board.san(move)
        yield {
            "n": f"{n}.{san}" if me == chess.WHITE else f"{n}...{san}",
            "after": after.cp, "loss": loss, "fen": before_board.fen(), "uci": move.uci(), "cls": classify(drop), "clock": node.clock(),
            "best": None if played_best or not best_uci
            else before_board.san(chess.Move.from_uci(best_uci)),
        }


def count_moves(pgn_text: str, colors) -> int:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        return 0
    return sum(1 for i, _ in enumerate(game.mainline_moves())
               if (chess.WHITE if i % 2 == 0 else chess.BLACK) in colors)
