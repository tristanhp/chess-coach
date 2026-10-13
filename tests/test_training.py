import json

import chess
import pytest

from chess_coach import training
from chess_coach.analysis import Ev, analyze_pgn
from chess_coach.coach import Coach
from chess_coach.games import increment, summarize
from chess_coach.report import coaching_report, compute_metrics, metrics_line
from test_games_and_cache import raw_game
from test_positions import StubClient

# Black rook on d5 is attacked by the c4 bishop; Black ignores it with Ke7.
THREAT_FEN = "4k3/8/8/3r4/2B5/8/P7/4K3 b - - 0 1"
THREAT_PGN = f'[SetUp "1"]\n[FEN "{THREAT_FEN}"]\n\n1... Ke7 {{[%clk 0:09:50.0]}} 2. Bxd5 *'


def scripted_threat(board, me):
    return Ev(300, ["d5d8"]) if board.epd() == chess.Board(THREAT_FEN).epd() else Ev(-300, [])


def test_ignored_threat_and_think_time():
    res = analyze_pgn(THREAT_PGN, chess.BLACK, scripted_threat, init_clock=600, increment=5)
    e = res["errors"][0]
    assert e["cls"] == "blunder" and "ignored_threat" in e["flags"]
    assert e["think"] == 15.0 and res["thinks"] == [15.0]  # 600 + 5 - 590
    assert res["v"] == 3


def test_increment_parse():
    assert increment("600+5") == 5 and increment("600") == 0 and increment(None) == 0


def test_puzzles_from_result_filters():
    res = analyze_pgn(THREAT_PGN, chess.BLACK, scripted_threat)
    (p,) = training.puzzles_from_result("u", "me", res)
    assert p["best"] == "Rd8" and p["best_uci"] == "d5d8" and "ignored_threat" in json.loads(p["tags"])
    res["errors"][0]["before"] = -400  # already lost: not a useful puzzle
    assert training.puzzles_from_result("u", "me", res) == []
    res["errors"][0].update(before=100, best=None)
    assert training.puzzles_from_result("u", "me", res) == []


def test_leitner_flow(tmp_store):
    res = analyze_pgn(THREAT_PGN, chess.BLACK, scripted_threat)
    assert training.add_from_result(tmp_store, "u", "me", res) == 1
    assert training.add_from_result(tmp_store, "u", "me", res) == 0  # no duplicates
    out = training.get_puzzle(tmp_store, "me")
    assert out.startswith("Puzzle #1 | Black to move") and "Rd8" not in out  # answer hidden
    assert "ignored_threat" in training.get_puzzle(tmp_store, "me", hint=True)
    assert training.submit_answer(tmp_store, None, 1, "Rd8").startswith("Correct")  # best move: no engine needed
    p = tmp_store.get_puzzle(1)
    assert p["box"] == 1 and p["attempts"] == 1 and p["correct"] == 1 and p["due"] > p["last_seen"] + 86000
    assert "none due" in training.get_puzzle(tmp_store, "me")
    assert "Illegal" in training.submit_answer(tmp_store, None, 1, "Qh9")
    assert training.submit_answer(tmp_store, None, 99, "e4") == "No puzzle #99."
    assert "No puzzles tagged" in training.get_puzzle(tmp_store, "me", tag="zzz")


def test_backfill_from_cached_analyses(tmp_store):
    res = analyze_pgn(THREAT_PGN, chess.BLACK, scripted_threat)
    tmp_store.put_analysis("u", "me", 12, res)
    assert "Puzzle #1" in training.get_puzzle(tmp_store, "me")


def test_notes_and_focus_in_last_sync(tmp_store):
    co = Coach(store=tmp_store, client=object(), engine=object())
    assert co.get_notes() == "No notes yet."
    assert co.save_note("check threats").startswith("Saved note #1")
    assert "[focus] check threats" in co.get_notes()
    assert "Focus: #1 check threats" in co.get_last_sync()
    assert "Focus: check threats" in co.mark_synced()
    assert co.delete_note(1) == "Deleted note #1." and co.delete_note(1) == "No note #1."


def test_report_has_causes_and_think_lines():
    res = analyze_pgn(THREAT_PGN, chess.BLACK, scripted_threat, init_clock=600)
    info = summarize(raw_game(), "tristanhp")
    out = coaching_report("me", "rapid", [(info, res)], [info], 12, 0, 0)
    assert "Causes of 1 mistakes/blunders: ignored opponent's threat 1" in out
    assert "Think time:" in out
    m = compute_metrics([(info, res)], [info])
    assert m["blunders"] == 1 and m["ignored"] == 1
    assert "blunders/game 3.0→1.0" in metrics_line(m, {**m, "blunders": 3.0})


@pytest.mark.engine
def test_end_to_end_puzzle_from_game(tmp_store, engine):
    co = Coach(store=tmp_store, client=StubClient(), engine=engine)
    assert "Blunders 1" in co.analyze_game("https://www.chess.com/game/live/123456789", depth=10)
    out = co.get_puzzle()
    assert "Black to move" in out and "3...Nf6" not in out
    assert co.submit_answer(1, "Nf6").startswith("Incorrect")  # the move from the game
    best = tmp_store.get_puzzle(1)["best"]
    assert co.submit_answer(1, best).startswith("Correct")
    first = co.mark_synced()
    assert "first snapshot" in first and "blunders/game 1.0" in first
    assert "1.0→1.0" in co.mark_synced()
