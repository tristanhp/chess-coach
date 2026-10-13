"""Puzzles from your own mistakes (Leitner spaced repetition)."""
import json
import time

import chess

from . import positions
from .analysis import fmt_eval

INTERVALS = [0, 1, 3, 7, 21]  # days until a puzzle is due again, by box
WRONG_RETRY_S = 600
ALSO_GOOD_LOSS = 60  # cp: another move this close to the best also solves the puzzle


def puzzles_from_result(game_url, username, res, date=None, opp=None):
    """Mistakes/blunders and missed tactics that have a clear best move and were not already lost."""
    out = []
    for e in res.get("errors", []):
        serious = e["cls"] in ("mistake", "blunder") or {"missed_mate", "missed_material"} & set(e["flags"])
        if not serious or not e.get("best") or e["drop"] < 10 or e["before"] <= -300:
            continue
        try:
            best_uci = chess.Board(e["fen"]).parse_san(e["best"]).uci()
        except ValueError:
            continue
        out.append({"game_url": game_url, "username": username, "fen": e["fen"], "best": e["best"],
                    "best_uci": best_uci, "played": e["n"], "before_cp": e["before"], "after_cp": e["after"],
                    "phase": e["phase"], "tags": json.dumps([e["cls"], *e["flags"]]), "game_date": date, "opp": opp})
    return out


def add_from_result(store, game_url, username, res, date=None, opp=None) -> int:
    return sum(store.add_puzzle(p) for p in puzzles_from_result(game_url, username, res, date, opp))


def backfill(store, username) -> int:
    n = 0
    for row in store.iter_analyses():
        if row["username"] == username:
            n += add_from_result(store, row["game_url"], username, json.loads(row["result"]))
    return n


def get_puzzle(store, username, tag=None, hint=False) -> str:
    if not store.puzzle_counts(username)["total"]:
        backfill(store, username)
    p, due = store.next_puzzle(username, tag)
    c = store.puzzle_counts(username)
    if not p:
        return ("No puzzles yet: run analyze_recent / analyze_game first, puzzles are built from your "
                "mistakes and blunders." if not tag else f"No puzzles tagged '{tag}'.")
    board = chess.Board(p["fen"])
    side = "White" if board.turn else "Black"
    tags = json.loads(p["tags"])
    head = (f"Puzzle #{p['id']} | {side} to move, find the best move | box {p['box']}/4 | "
            f"{c['due']} due, {c['total']} total, solved {c['cor']}/{c['att']} attempts")
    if not due:
        head += " | none due: extra practice"
    lines = [head, f"From your game {p['game_date'] or ''} {p['opp'] and 'vs ' + p['opp'] or ''} ({p['phase']}); "
             "in the game you did not find it. Answer with submit_answer(puzzle_id, move).",
             f"FEN: {p['fen']}", positions.ascii_board(board)]
    if hint:
        lines.append("Hint: " + (", ".join(t for t in tags if t not in ("mistake", "blunder")) or "none"))
    return "\n".join(lines)


def submit_answer(store, engine, puzzle_id, move, depth=14) -> str:
    p = store.get_puzzle(puzzle_id)
    if not p:
        return f"No puzzle #{puzzle_id}."
    board = chess.Board(p["fen"])
    mv, err = positions.parse_move(board, move)
    if err:
        return err + "\n(Not counted as an attempt.)"
    j = None
    if mv.uci() == p["best_uci"]:
        ok, why = True, "That is the best move."
    else:
        j = positions.judge_move(engine, board, mv, depth, 8)
        both_mate = j["before"].cp >= 9000 and j["after"].cp >= 9000
        ok = j["loss"] <= ALSO_GOOD_LOSS or both_mate
        why = (f"Not the engine's first choice ({p['best']}), but it holds the evaluation ({j['loss']}cp)."
               if ok else "")
    now = time.time()
    box = min(4, p["box"] + 1) if ok else 0
    store.update_puzzle(puzzle_id, attempts=p["attempts"] + 1, correct=p["correct"] + ok, box=box,
                        due=now + (INTERVALS[box] * 86400 if ok else WRONG_RETRY_S), last_seen=now)
    nxt = f"back in {INTERVALS[box]}d" if ok and INTERVALS[box] else "back soon"
    if ok:
        return f"Correct! {why} ({nxt})\nIn the game you played {p['played']}. Best: {p['best']} (eval {fmt_eval(p['before_cp'])})."
    return (f"Incorrect. Best was {p['best']}; in the game you played {p['played']}. ({nxt})\n"
            + positions.render_move(board, mv, j))
