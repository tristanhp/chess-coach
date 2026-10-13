"""MCP stdio server. stdout carries protocol only; everything else goes to stderr."""
import asyncio
import logging
import sys

from mcp.server.fastmcp import FastMCP

from .config import DEFAULT_USER

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

mcp = FastMCP("chess-coach")
_coach = None


def coach():
    global _coach
    if _coach is None:
        from .coach import Coach

        _coach = Coach()
    return _coach


async def _run(fn, *a, **kw):
    try:
        return await asyncio.to_thread(fn, *a, **kw)
    except Exception as e:  # report to the model instead of crashing the call
        logging.getLogger("chess_coach").exception("tool failed")
        return f"Error: {type(e).__name__}: {e}"


@mcp.tool()
async def get_profile_stats(username: str = DEFAULT_USER) -> str:
    """Current rapid/blitz/bullet ratings, best rating and W/L/D record."""
    return await _run(lambda: coach().profile_stats(username))


@mcp.tool()
async def list_games(username: str = DEFAULT_USER, time_class: str = "rapid",
                     since: str | None = None, limit: int = 20) -> str:
    """One line per game, newest first. `since`: ISO date or "last_sync". time_class: rapid|blitz|bullet|all."""
    return await _run(lambda: coach().list_games(username, time_class, since, limit))


@mcp.tool()
async def get_game_pgn(game_url: str) -> str:
    """Full PGN of one game."""
    return await _run(lambda: coach().get_game_pgn(game_url))


@mcp.tool()
async def analyze_game(game_url: str, depth: int = 14) -> str:
    """Stockfish analysis of one game from the user's side: mistakes, blunders, missed tactics, decisive moment."""
    return await _run(lambda: coach().analyze_game(game_url, depth))


@mcp.tool()
async def analyze_recent(username: str = DEFAULT_USER, time_class: str = "rapid",
                         n: int = 20, depth: int = 12, time_budget_s: int = 40) -> str:
    """Aggregate coaching report over the last n games. Analyses at most ~time_budget_s seconds per
    call (0 = no limit); if games remain it says so: call again, finished games are cached."""
    return await _run(lambda: coach().analyze_recent(username, time_class, n, depth, time_budget_s))


@mcp.tool()
async def position_at(game_url: str, move_number: int, side: str) -> str:
    """FEN before a move of a game (+ move played, clock, ASCII board). side: white|black. game_url may be "latest"."""
    return await _run(lambda: coach().position_at(game_url, move_number, side))


@mcp.tool()
async def eval_position(fen: str, depth: int = 18, multipv: int = 3, max_seconds: int = 15) -> str:
    """Stockfish eval of any FEN (side-to-move and White views) with top lines in SAN."""
    return await _run(lambda: coach().eval_position(fen, depth, multipv, max_seconds))


@mcp.tool()
async def evaluate_move(fen: str, move: str, depth: int = 18, max_seconds: int = 15) -> str:
    """Judge one move (SAN or UCI) in a position: eval before/after, cp loss, class, best line, and any piece left en prise."""
    return await _run(lambda: coach().evaluate_move(fen, move, depth, max_seconds))


@mcp.tool()
async def explore_line(fen: str, moves: list[str], depth: int = 16, max_seconds: int = 20) -> str:
    """Play SAN moves from a FEN ("what if..."): eval after each ply, final FEN/board, best continuation. Stops at the first illegal move."""
    return await _run(lambda: coach().explore_line(fen, moves, depth, max_seconds))


@mcp.tool()
async def review_moves(game_url: str, side: str = "me", depth: int = 14) -> str:
    """One line per move of a game (eval after, best move, cp loss), not just flagged ones. side: me|opponent|white|black|both. Resumable: call again if it says so."""
    return await _run(lambda: coach().review_moves(game_url, side, depth))


@mcp.tool()
async def get_puzzle(tag: str | None = None, hint: bool = False) -> str:
    """Next "find the best move" puzzle built from the user's own mistakes (spaced repetition). tag: e.g. missed_mate, ignored_threat, endgame."""
    return await _run(lambda: coach().get_puzzle(tag, hint))


@mcp.tool()
async def submit_answer(puzzle_id: int, move: str) -> str:
    """Check a puzzle answer (SAN or UCI); reveals the solution when wrong and schedules the puzzle."""
    return await _run(lambda: coach().submit_answer(puzzle_id, move))


@mcp.tool()
async def save_note(text: str, topic: str = "focus") -> str:
    """Remember something between coaching sessions (topic "focus" is shown by get_last_sync/mark_synced)."""
    return await _run(lambda: coach().save_note(text, topic))


@mcp.tool()
async def get_notes(topic: str | None = None, limit: int = 10) -> str:
    """Saved coaching notes, newest first."""
    return await _run(lambda: coach().get_notes(topic, limit))


@mcp.tool()
async def delete_note(note_id: int) -> str:
    """Delete a saved note."""
    return await _run(lambda: coach().delete_note(note_id))


@mcp.tool()
async def mark_synced() -> str:
    """Record now as the 'last coaching session' (for list_games since="last_sync") and save a progress snapshot (blunders/game etc., compared with the previous one)."""
    return await _run(lambda: coach().mark_synced())


@mcp.tool()
async def get_last_sync() -> str:
    """When the last coaching session was marked, plus the current focus notes."""
    return await _run(lambda: coach().get_last_sync())


def main():
    try:
        mcp.run(transport="stdio")
    finally:
        # python-chess's engine thread is non-daemon: without an explicit close the process never exits
        if _coach is not None:
            _coach.engine.close()


if __name__ == "__main__":
    main()
