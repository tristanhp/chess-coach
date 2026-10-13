import chess
import pytest

from chess_coach import positions as P
from chess_coach.analysis import Ev, OutOfTime, analyze_pgn, count_moves, iter_review
from chess_coach.coach import Coach
from conftest import FIX
from test_games_and_cache import FakeHttp, raw_game

# Black to move; Ng4 leaves the knight attacked by the f3 pawn, defended only by the queen (and Bc4 hangs to b3xc4).
FEN = "r5k1/pb3rpp/1p2pnq1/8/P1b5/1PB2P2/6PP/2Q2RK1 b - - 0 23"


def test_fen_validation():
    assert P.parse_fen(FEN)[1] is None
    assert "Illegal FEN" in P.parse_fen("garbage")[1]
    assert "no white king" in P.parse_fen("8/8/8/8/8/8/8/k7 w - - 0 1")[1]
    # side NOT to move is in check
    assert "opposite check" in P.parse_fen("4k3/8/8/8/8/8/4R3/4K3 w - - 0 1")[1]
    assert P.parse_fen("")[1]


def test_san_and_uci_parsing():
    b = chess.Board(FEN)
    assert P.parse_move(b, "Ng4")[0] == chess.Move.from_uci("f6g4")
    assert P.parse_move(b, "f6g4")[0] == chess.Move.from_uci("f6g4")
    assert P.parse_move(b, "23...Ng4?!")[0] == chess.Move.from_uci("f6g4")  # numbering/annotations tolerated
    assert P.parse_move(b, "O-O")[0] is None  # king already castled: not legal
    mv, err = P.parse_move(b, "Nf3")
    assert mv is None and "Legal:" in err
    assert "Ambiguous" in P.parse_move(chess.Board("4k3/8/8/8/8/2N3N1/8/4K3 w - - 0 1"), "Ne4")[1]


def test_ascii_board():
    lines = P.ascii_board(chess.Board()).splitlines()
    assert len(lines) == 9 and lines[0] == "8 r n b q k b n r" and lines[-1] == "  a b c d e f g h"


def test_en_prise_detects_hanging_knight_in_winning_position():
    b = chess.Board(FEN)
    hang = P.en_prise_after(b, b.parse_san("Ng4"))
    knight = next(h for h in hang if h["name"] == "Ng4")
    assert "pawn f3" in knight["text"] and "loses N for P after fxg4" in knight["text"] and knight["net"] == 2
    assert not knight["was"]
    bishop = next(h for h in hang if h["name"] == "Bc4")
    assert bishop["was"]  # already hanging before the move
    # a protected, lower-attacked piece is not reported
    safe = chess.Board()
    assert P.en_prise_after(safe, safe.parse_san("Nf3")) == []


def test_explore_line_stops_at_illegal_move_without_engine():
    out = P.explore_line(object(), FEN, ["Ng4", "fxg5", "Rxf1+"])
    assert out.startswith("Stopped at ply 2") and "fxg5" in out and "23...Ng4" in out and "Legal:" in out
    assert "Illegal FEN" in P.explore_line(object(), "nope", ["e4"])
    assert "Too many" in P.explore_line(object(), FEN, ["Ng4"] * 30)


def test_time_clamp():
    assert P.clamp_seconds(500) == P.MAX_SECONDS < 40 and P.clamp_seconds(0) == 1


@pytest.mark.engine
def test_evaluate_move_flags_hanging_piece(engine):
    out = P.evaluate_move(engine, FEN, "Ng4", depth=12, max_seconds=6)
    assert "EN PRISE" in out and "pawn f3" in out and "loses N for P" in out
    assert "Best:" in out and "(Black)" in out
    assert "Illegal" in P.evaluate_move(engine, FEN, "Qa1", depth=8, max_seconds=2)


@pytest.mark.engine
def test_explore_and_eval_position_run(engine):
    out = P.explore_line(engine, FEN, ["Ng4", "fxg4", "Rxf1+"], depth=10, max_seconds=6)
    assert "24.fxg4" in out and "Final FEN:" in out and "Best continuation" in out
    out = P.eval_position(engine, FEN, depth=10, multipv=2, max_seconds=4)
    assert out.count("\n") >= 10 and "1)" in out and "2)" in out and "White view" in out
    assert P.eval_position(engine, "8/8/8/8/8/8/8/8 w - - 0 1").startswith("Illegal position")


def test_iter_review_every_move_and_resume_on_out_of_time():
    pgn = (FIX / "scholars.pgn").read_text()
    calls = []

    def evaluate(board, me):
        calls.append(1)
        if len(calls) > 5:
            raise OutOfTime
        return Ev(30, [next(iter(board.legal_moves)).uci()])

    rows, done = [], True
    try:
        for r in iter_review(pgn, [chess.WHITE], evaluate):
            rows.append(r)
    except OutOfTime:
        done = False
    assert not done and 0 < len(rows) < count_moves(pgn, [chess.WHITE])
    assert count_moves(pgn, [chess.WHITE]) == 5 and count_moves(pgn, [chess.BLACK]) == 4
    full = list(iter_review(pgn, [chess.WHITE, chess.BLACK], lambda b, me: Ev(0, [])))
    assert len(full) == 9 and full[0]["n"] == "1.e4" and full[1]["n"] == "1...e5"


def test_far_ahead_loss_is_visible():
    # +20 -> +12: win% does not move, but the cp loss and the large-advantage counters must
    start, after = chess.Board(), chess.Board()
    after.push_san("f3")
    table = {start.epd(): Ev(2000, ["e2e4"]), after.epd(): Ev(1200, [])}
    rows = list(iter_review("1. f3 e5 *", [chess.WHITE], lambda b, me: table[b.epd()]))
    assert rows[0]["loss"] == 800 and rows[0]["cls"] is None and rows[0]["best"] == "e4"
    res = analyze_pgn("1. f3 e5 *", chess.WHITE, lambda b, me: table[b.epd()])
    assert res["adv_moves"] == 1 and res["adv_unflagged"] == 1 and res["errors"] == []
    from chess_coach.report import game_analysis
    from chess_coach.games import summarize
    info = summarize(raw_game(), "tristanhp")
    assert "use review_moves" in game_analysis(info, res, 14)


def test_pos_cache_roundtrip(tmp_store):
    tmp_store.put_pos("epd", 12, -35, ["e2e4"])
    assert tmp_store.get_pos("epd", 10) == (-35, ["e2e4"]) and tmp_store.get_pos("epd", 14) is None


class StubClient:
    def __init__(self):
        self.g = raw_game()

    def find_game(self, url, username, deadline=None):
        return self.g if url.endswith("123456789") else None

    def iter_games(self, username, time_class=None, since_ts=None):
        yield self.g


def test_position_at(tmp_store):
    co = Coach(store=tmp_store, client=StubClient(), engine=object())
    out = co.position_at("https://www.chess.com/game/live/123456789", 3, "black")
    assert "Before 3...Nf6 (black to move)" in out and "clock 0:40 after the move" in out
    assert "FEN: r1bqkbnr/pppp1ppp/2n5/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 3 3" in out
    assert out.count("\n") >= 11
    assert "Before 3...Nf6" in co.position_at("latest", 3, "black")
    assert "only" in co.position_at("latest", 30, "black") or "No such move" in co.position_at("latest", 30, "black")
    assert "side must be" in co.position_at("latest", 3, "x")
    assert "not found" in co.position_at("https://www.chess.com/game/live/999999999", 1, "white")
