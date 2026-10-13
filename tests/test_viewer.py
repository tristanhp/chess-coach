import http.client
import json
import time
import urllib.error
import urllib.request

import chess
import pytest

from chess_coach.analysis import Ev
from chess_coach.coach import Coach
from chess_coach.viewer import Viewer, accuracy, classify_ply
from test_positions import StubClient


def test_classify_ply():
    # White moves, eval (White view) 30 -> -300 while the best move was something else: blunder
    assert classify_ply(30, -300, True, "f2f3", "e2e4")["cls"] == "blunder"
    assert classify_ply(30, 25, True, "e2e4", "e2e4")["cls"] == "best"
    assert classify_ply(30, 20, True, "d2d4", "e2e4")["cls"] == "excellent"
    assert classify_ply(30, -10, True, "d2d4", "e2e4")["cls"] == "good"
    # Black moves: its view is the negation, so a rising White eval is a loss for Black
    assert classify_ply(0, 300, False, "a7a6", "e7e5")["cls"] == "blunder"
    # far ahead: win% barely moves but 8 pawns are gone
    r = classify_ply(2000, 1200, True, "a2a3", "e2e4")
    assert r["cls"] == "inaccuracy" and r["loss"] == 800
    assert classify_ply(9990, 100, True, "a2a3", "e2e4")["cls"] == "miss"


def test_accuracy():
    assert accuracy([]) is None
    assert accuracy([0, 0]) == pytest.approx(100, abs=0.1)
    assert accuracy([30]) < accuracy([5]) < 100


class StubEngine:
    def evaluator(self, depth, cap, pv_len=4):
        def evaluate(board, me):
            if board.is_game_over():
                return Ev(10000 if board.is_checkmate() and board.turn != me else 0)
            mv = next(iter(board.legal_moves))
            return Ev(30 if me else -30, [mv.uci()])
        return evaluate


@pytest.fixture
def viewer(tmp_store):
    co = Coach(store=tmp_store, client=StubClient(), engine=StubEngine())
    v = Viewer(co, port=0)
    v.start()
    yield v
    v.server.shutdown()


def get(v, path, host=None):
    c = http.client.HTTPConnection("127.0.0.1", v.port, timeout=10)
    c.request("GET", path, headers={"Host": host} if host else {})
    r = c.getresponse()
    return r.status, r.read().decode()


def test_viewer_http(viewer):
    st, body = get(viewer, "/game/123456789")
    assert st == 200 and "<title>chess-coach analysis" in body
    st, body = get(viewer, "/api/game/123456789")
    g = json.loads(body)
    assert st == 200 and len(g["fens"]) == 8 and g["plies"][0]["san"] == "e4" and g["mine"] == "black"
    assert g["plies"][5]["clock"] == 40
    for _ in range(50):
        p = json.loads(get(viewer, "/api/evals/123456789")[1])
        if p["done"]:
            break
        time.sleep(0.1)
    assert p["done"] and p["error"] is None and p["n_done"] == p["total"] == 8
    assert p["moves"][0] is None and all(m["cls"] for m in p["moves"][1:])
    assert set(p["summary"]) == {"white", "black"}
    st, body = get(viewer, "/api/games")
    assert st == 200 and json.loads(body)[0]["id"] == "123456789"
    assert get(viewer, "/")[0] == 200


def test_viewer_rejects_foreign_host_and_unknown(viewer):
    assert get(viewer, "/api/games", host="evil.example")[0] == 403
    assert get(viewer, "/nope")[0] == 404
    st, body = get(viewer, "/api/game/999999999")
    assert st == 404 and "error" in json.loads(body)


def test_open_game_viewer_tool(tmp_store):
    co = Coach(store=tmp_store, client=StubClient(), engine=StubEngine())
    out = co.open_game_viewer("latest", open_browser=False, ply=4)
    try:
        assert "/game/123456789#4" in out and "Open that URL" in out
        assert "http://127.0.0.1:" in co.open_game_viewer("games", open_browser=False)
    finally:
        co.viewer.server.shutdown()
