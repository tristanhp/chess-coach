import json

import httpx

from chess_coach import chesscom
from chess_coach.chesscom import ChessComClient, game_id, month_end_ts
from chess_coach.games import initial_clock, opening_names, summarize
from conftest import FIX


def raw_game(url="https://www.chess.com/game/live/123456789", pgn_file="black_blunder.pgn", **kw):
    g = {
        "url": url, "time_class": "rapid", "rules": "chess", "end_time": 1_760_000_000,
        "pgn": (FIX / pgn_file).read_text(),
        "white": {"username": "Opp", "rating": 500, "result": "win"},
        "black": {"username": "TristanHP", "rating": 520, "result": "checkmated"},
        "accuracies": {"white": 90.0, "black": 40.5},
    }
    g.update(kw)
    return g


def test_summarize_case_insensitive_black():
    i = summarize(raw_game(), "tristanhp")
    assert (i.color, i.result, i.how, i.moves, i.accuracy) == ("black", "loss", "checkmated", 4, 40.5)
    assert i.init_clock == 600 and i.opp == "Opp"


def test_summarize_win_and_skips():
    g = raw_game(white={"username": "tristanhp", "rating": 1, "result": "win"},
                 black={"username": "x", "rating": 2, "result": "resigned"})
    i = summarize(g, "TRISTANHP")
    assert i.result == "win" and i.how == "opp resigned"
    assert summarize(raw_game(rules="chess960"), "tristanhp") is None
    assert summarize(raw_game(), "someoneelse") is None


def test_aborted_game_has_zero_plies():
    g = raw_game(pgn='[White "Opp"]\n[Black "TristanHP"]\n\n*')
    assert summarize(g, "tristanhp").plies == 0


def test_opening_names():
    assert opening_names("https://www.chess.com/openings/Sicilian-Defense-Open-Najdorf-Variation-6.Bg5-e6") == (
        "Sicilian Defense Open Najdorf Variation", "Sicilian Defense")
    assert opening_names(None) == ("Unknown", "Unknown")
    assert initial_clock("600+5") == 600 and initial_clock("1/86400") is None


def test_game_id():
    assert game_id("https://www.chess.com/game/live/185259760818?x=1") == "185259760818"
    assert game_id("nonsense") is None


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url):
        self.calls.append(url)
        body = self.routes.get(url)
        return httpx.Response(404 if body is None else 200, json=body or {}, request=httpx.Request("GET", url))


A = "https://api.chess.com/pub/player/tristanhp/games"
PAST, CUR = f"{A}/2020/01", f"{A}/2026/10"


def make_client(store, now, routes=None):
    routes = routes or {f"{A}/archives": {"archives": [PAST, CUR]},
                        PAST: {"games": [raw_game()]}, CUR: {"games": []}}
    http = FakeHttp(routes)
    return ChessComClient(store, http=http, sleep=lambda s: None, now=lambda: now[0]), http


def test_cache_policy(tmp_store):
    now = [month_end_ts(2026, 10) - 86400 * 5]
    c, http = make_client(tmp_store, now)
    c.month(PAST); c.month(CUR)
    assert len(http.calls) == 2
    now[0] += 300  # inside 10 minutes: nothing refetched
    c.month(PAST); c.month(CUR)
    assert len(http.calls) == 2
    now[0] += 400  # current month stale -> refetch; the (final) past month is not
    c.month(PAST); c.month(CUR)
    assert http.calls.count(CUR) == 2 and http.calls.count(PAST) == 1


def test_month_fetched_before_it_ended_is_refetched(tmp_store):
    now = [month_end_ts(2026, 10) - 100]
    c, http = make_client(tmp_store, now)
    c.month(CUR)
    now[0] = month_end_ts(2026, 10) + 5000  # month over: must re-fetch once, then it is final
    c.month(CUR); c.month(CUR)
    assert http.calls.count(CUR) == 2


def test_find_game_uses_index_and_empty_months(tmp_store):
    now = [month_end_ts(2026, 10) + 10]
    c, http = make_client(tmp_store, now)
    assert c.find_game("https://www.chess.com/game/live/123456789")["url"].endswith("123456789")
    assert list(c.iter_games("tristanhp", "blitz")) == []  # no matching time class
    assert [g["url"] for g in c.iter_games("tristanhp", "rapid")] == [raw_game()["url"]]
    assert c.find_game("https://www.chess.com/game/live/999999999") is None


def test_retry_on_429(tmp_store):
    seq = [httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(503), httpx.Response(200, json={"ok": 1})]
    sleeps = []

    class H:
        def get(self, url):
            r = seq.pop(0)
            r.request = httpx.Request("GET", url)
            return r

    c = ChessComClient(tmp_store, http=H(), sleep=sleeps.append)
    assert c._fetch("https://x") == {"ok": 1} and len(sleeps) == 2


def test_analysis_cache_reuses_deeper(tmp_store):
    tmp_store.put_analysis("u", "me", 14, {"a": 1})
    assert tmp_store.get_analysis("u", "me", 12) == {"a": 1}
    assert tmp_store.get_analysis("u", "me", 16) is None


def test_last_sync(tmp_store):
    from chess_coach.coach import Coach

    co = Coach(store=tmp_store, client=object(), engine=object())
    assert "No coaching" in co.get_last_sync()
    co.mark_synced()
    assert co.get_last_sync().startswith("Last coaching session")
    assert co._parse_since("last_sync") is not None
    assert co._parse_since("2026-01-01") == 1767225600
