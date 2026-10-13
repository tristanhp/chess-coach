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
| `analyze_recent(username, time_class, n=20, depth=12, time_budget_s=40)` | aggregate coaching report: errors per game by phase, hung pieces, missed tactics, openings by colour, how losses end, time trouble, rating trend |
| `position_at(game_url, move_number, side)` | FEN before that move, the move played (SAN), clock, ASCII board. `game_url` may be `"latest"` |
| `eval_position(fen, depth=18, multipv=3, max_seconds=15)` | eval from the side to move's and White's view (`+3.2`, `M4`, `-M2`), top lines in SAN, board. Rejects illegal FENs |
| `evaluate_move(fen, move, depth=18, max_seconds=15)` | SAN or UCI move: eval before/after, cp loss, win% drop, best/good/inaccuracy/mistake/blunder, best line, material change, and any piece left **en prise** (even when still winning) |
| `explore_line(fen, moves, depth=16, max_seconds=20)` | play SAN moves out ("what if..."): eval after each ply, final FEN + board, best continuation; stops at the first illegal move |
| `review_moves(game_url, side="me", depth=14)` | one line per move (eval after, best move, cp loss, newly hanging pieces). `side`: me/opponent/white/black/both. Resumable |
| `mark_synced()` / `get_last_sync()` | store / read the "last coaching session" time |

Notes:
- Classification uses win% = 50 + 50·(2/(1+e^(−0.00368208·cp)) − 1) from your side; drops of ≥5 / ≥10 / ≥15 points are
  inaccuracy / mistake / blunder. Phase: opening ≤ move 12, endgame when ≤ 20 points of non-pawn material remain.
- Each position gets a depth cap plus a 0.5 s time cap, with one reused Stockfish process.
- `analyze_recent` stops starting new games after `time_budget_s` (Claude Desktop times tool calls out at ~60 s).
  If games remain it says so; just call it again, finished games are cached. Use `time_budget_s=0` for no limit.
- Every tool call is kept under ~40 s (the bridge times out at 60 s): per-position time caps, `max_seconds` clamped to 30,
  and `analyze_game`/`analyze_recent`/`review_moves` stop at their budget. Positions are cached (by FEN and depth), so
  "call again to continue" resumes where it stopped.
- Per-move cp loss (`evaluate_move`, `review_moves`) is clamped at ±30 pawns, not ±10 like `analyze_game`'s ACPL, so losses stay
  visible when far ahead. `analyze_game` adds a "large advantage" hint when you were above +5, because win% barely moves there.
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

Fully quit Claude Desktop from the system tray, then start it again (needed whenever tools are added so they load). To test the launch from Windows:
`wsl.exe -e bash -lc "cd /home/<you>/dev/chess-coach && /home/<you>/.local/bin/uv run chess-coach-mcp"`
(it should sit waiting for input silently). Or inspect it with `npx @modelcontextprotocol/inspector uv run chess-coach-mcp`.
