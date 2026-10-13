import os
from pathlib import Path

DEFAULT_USER = "tristanhp"
USER_AGENT = "chess-coach-mcp/0.1 (personal use; username tristanhp)"
API = "https://api.chess.com/pub"

PROJECT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("CHESS_COACH_DATA_DIR") or PROJECT_DIR / "data")
DB_PATH = DATA_DIR / "cache.db"

CURRENT_MONTH_TTL = 600  # seconds between re-fetches of the current month
MOVE_TIME_CAP = 0.5  # seconds of engine time per position (on top of the depth cap)


def stockfish_path() -> str:
    import shutil

    for cand in (
        os.environ.get("CHESS_COACH_STOCKFISH"),
        shutil.which("stockfish"),
        "/usr/games/stockfish",
        str(PROJECT_DIR / "bin" / "stockfish"),
    ):
        if cand and os.access(cand, os.X_OK):
            return cand
    raise FileNotFoundError(
        "Stockfish not found: apt install stockfish, or set CHESS_COACH_STOCKFISH"
    )
