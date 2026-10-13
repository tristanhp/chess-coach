"""Live smoke test against chess.com. Skip offline with: pytest -m 'not live'."""
import httpx
import pytest

from chess_coach.chesscom import ChessComClient
from chess_coach.coach import Coach

pytestmark = pytest.mark.live


def test_list_games_live(tmp_store):
    try:
        httpx.get("https://api.chess.com/pub/player/tristanhp", timeout=10,
                  headers={"User-Agent": "chess-coach-mcp/0.1 (tests)"}).raise_for_status()
    except httpx.HTTPError as e:
        pytest.skip(f"chess.com unreachable: {e}")
    coach = Coach(store=tmp_store, client=ChessComClient(tmp_store), engine=object())
    out = coach.list_games("tristanhp", "rapid", limit=5)
    print(out)
    lines = out.splitlines()
    assert "rapid games for tristanhp" in lines[0]
    assert len(lines) >= 2 and "https://www.chess.com/game/" in lines[1]
    assert "rapid:" in coach.profile_stats("tristanhp")
