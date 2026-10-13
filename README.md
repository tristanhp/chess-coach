# chess-coach

Local [MCP](https://modelcontextprotocol.io) server (stdio, FastMCP) that lets Claude coach you from your
chess.com games. Uses only the public Published-Data API (no login), Stockfish for analysis, and a SQLite
cache so nothing is fetched or analysed twice. Outputs are compact text (numbers, not JSON dumps).

## Install

```bash
git clone https://github.com/tristanhp/chess-coach && cd chess-coach
uv sync
sudo apt install stockfish          # or see below if you have no sudo
uv run pytest -m "not live"         # offline tests; plain `uv run pytest` also hits chess.com
```

No sudo? Download the package and drop the binary in `bin/` (picked up automatically):

```bash
apt download stockfish && dpkg -x stockfish_*.deb /tmp/sf && install -D /tmp/sf/usr/games/stockfish bin/stockfish
```

Stockfish lookup order: `$CHESS_COACH_STOCKFISH`, `stockfish` on `PATH`, `/usr/games/stockfish`, `bin/stockfish`.
The default username is set in `src/chess_coach/config.py` (`DEFAULT_USER`). Cache lives in `data/cache.db`
(override the directory with `CHESS_COACH_DATA_DIR`); it is git-ignored.

## Tools

| Tool | What it does |
|---|---|
| `get_profile_stats(username)` | rapid/blitz/bullet rating, best, W/L/D |
| `list_games(username, time_class="rapid", since=None, limit=20)` | one line per game, newest first: date, colour, opponent, result + how it ended, opening, moves, accuracy, URL. `since`: ISO date or `"last_sync"` |
| `get_game_pgn(game_url)` | full PGN |
| `analyze_game(game_url, depth=14)` | your mistakes/blunders only (win-% drop, Lichess-style), missed mates/material, phase, clock, FEN, decisive moment |
| `analyze_recent(username, time_class, n=20, depth=12, time_budget_s=45)` | aggregate coaching report: errors per game by phase, hung pieces, missed tactics, openings by colour, how losses end, time trouble, rating trend |
| `mark_synced()` / `get_last_sync()` | store / read the "last coaching session" time |

Notes:
- Classification uses win% = 50 + 50·(2/(1+e^(−0.00368208·cp)) − 1) from your side; drops of ≥5 / ≥10 / ≥15 points are
  inaccuracy / mistake / blunder. Phase: opening ≤ move 12, endgame when ≤ 20 points of non-pawn material remain.
- Each position gets a depth cap plus a 0.5 s time cap, with one reused Stockfish process.
- `analyze_recent` stops starting new games after `time_budget_s` (Claude Desktop times tool calls out at ~60 s).
  If games remain it says so; just call it again, finished games are cached. Use `time_budget_s=0` for no limit.
- Variants/daily games are skipped; games with no moves are hidden; your name matches in either colour, any case.
- stdout carries only MCP protocol; progress and logs go to stderr.

## Claude Desktop (Windows, server runs in WSL)

Add to `%APPDATA%\Claude\claude_desktop_config.json` (merge into any existing `mcpServers`), with your real paths:

```json
{
  "mcpServers": {
    "chess-coach": {
      "command": "wsl.exe",
      "args": ["-e", "bash", "-lc",
               "cd /home/<you>/dev/chess-coach && /home/<you>/.local/bin/uv run chess-coach-mcp"]
    }
  }
}
```

Fully quit Claude Desktop from the system tray, then start it again. To test the launch from Windows:
`wsl.exe -e bash -lc "cd /home/<you>/dev/chess-coach && /home/<you>/.local/bin/uv run chess-coach-mcp"`
(it should sit waiting for input silently). Or inspect it with `npx @modelcontextprotocol/inspector uv run chess-coach-mcp`.
