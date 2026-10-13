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
                         n: int = 20, depth: int = 12, time_budget_s: int = 45) -> str:
    """Aggregate coaching report over the last n games. Analyses at most ~time_budget_s seconds per
    call (0 = no limit); if games remain it says so: call again, finished games are cached."""
    return await _run(lambda: coach().analyze_recent(username, time_class, n, depth, time_budget_s))


@mcp.tool()
async def mark_synced() -> str:
    """Record now as the 'last coaching session' (for list_games since="last_sync")."""
    return await _run(lambda: coach().mark_synced())


@mcp.tool()
async def get_last_sync() -> str:
    """When the last coaching session was marked."""
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
