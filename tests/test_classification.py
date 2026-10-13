import chess
import pytest

from chess_coach.analysis import (Ev, analyze_pgn, classify, fmt_eval, game_phase, line_gain, win_pct)
from conftest import FIX


def test_win_pct_lichess_formula():
    assert win_pct(0) == pytest.approx(50)
    assert win_pct(100) == pytest.approx(59.1, abs=0.1)
    assert win_pct(-300) == pytest.approx(100 - win_pct(300))
    assert win_pct(10_000) == win_pct(1000) > 97


def test_classify_thresholds():
    assert classify(4.9) is None
    assert classify(5) == "inaccuracy"
    assert classify(10) == "mistake"
    assert classify(15) == "blunder"


def test_fmt_eval():
    assert fmt_eval(35) == "+0.3"
    assert fmt_eval(9997) == "M3"
    assert fmt_eval(-9998) == "-M2"
    assert fmt_eval(10000) == "mate"


def test_line_gain_counts_captures():
    b = chess.Board("4k3/8/8/3q4/8/8/8/3QK3 w - - 0 1")
    assert line_gain(b, ["d1d5"], chess.WHITE) == 9
    assert line_gain(b, ["d1d5", "e8d8"], chess.WHITE) == 9
    assert line_gain(b, ["zzzz"], chess.WHITE) == 0  # bad uci is ignored


def test_phase():
    assert game_phase(chess.Board()) == "opening"
    late = chess.Board("4r1k1/5ppp/8/8/8/8/5PPP/4R1K1 w - - 0 30")
    assert game_phase(late) == "endgame"
    mid = chess.Board("r1bq1rk1/pp2bppp/2n1pn2/2pp4/3P4/2PBPN2/PP1N1PPP/R1BQ1RK1 w - - 0 13")
    assert game_phase(mid) == "middlegame"


def scripted(table):
    """Fake evaluator: table maps epd -> (cp, pv)."""
    def evaluate(board, me):
        cp, pv = table[board.epd()]
        return Ev(cp, pv)
    return evaluate


def test_blunder_with_fake_engine():
    # White (me) plays 1.f3?? instead of best 1.e4; eval 0.3 -> -3.0
    start, after = chess.Board(), chess.Board()
    after.push_san("f3")
    pgn = '1. f3 {[%clk 0:09:00]} 1... e5 *'
    ev = scripted({start.epd(): (30, ["e2e4"]), after.epd(): (-300, ["e7e5"])})
    # only my moves are evaluated, so the black reply position is never needed
    res = analyze_pgn(pgn, chess.WHITE, ev, init_clock=600)
    assert res["counts"]["blunder"] == 1
    e = res["errors"][0]
    assert (e["n"], e["best"], e["cls"], e["clock"]) == ("1.f3", "e4", "blunder", 540)
    assert e["fen"] == chess.STARTING_FEN
    assert res["decisive"] == e


def test_playing_best_move_is_never_an_error():
    start, after = chess.Board(), chess.Board()
    after.push_san("e4")
    ev = scripted({start.epd(): (30, ["e2e4"]), after.epd(): (-200, [])})  # noisy lower after-eval
    res = analyze_pgn("1. e4 e5 *", chess.WHITE, ev)
    assert res["errors"] == [] and res["acpl"] == 0


@pytest.mark.engine
def test_blunder_into_mate_black(engine):
    res = analyze_pgn((FIX / "black_blunder.pgn").read_text(), chess.BLACK, engine.evaluator(10, 0.3), 600)
    assert res["color"] == "black"
    e = res["decisive"]
    assert e["n"] == "3...Nf6" and e["cls"] == "blunder" and e["phase"] == "opening"
    assert e["after"] <= -9000 and e["clock"] == 40
    assert e["clock"] < 0.1 * res["init_clock"]


@pytest.mark.engine
def test_missed_mate_and_hung_queen_white(engine):
    res = analyze_pgn((FIX / "scholars.pgn").read_text(), chess.WHITE, engine.evaluator(10, 0.3), 600)
    e = next(x for x in res["errors"] if x["n"] == "4.Nf3")
    assert e["best"] == "Qxf7#" and "missed_mate" in e["flags"] and "hung_piece" in e["flags"]
    assert res["missed_mate"] == 1 and res["hung"] == 1
