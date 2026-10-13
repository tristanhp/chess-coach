"""Turn a chess.com game object into a compact GameInfo from the user's side."""
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import chess.pgn

DRAW_CODES = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}
FAMILY_END = {"Defense", "Game", "Opening", "Gambit", "Attack", "System", "Countergambit"}


@dataclass
class GameInfo:
    url: str
    end_time: int
    time_class: str
    color: str  # "white" | "black"
    opp: str
    my_rating: int | None
    opp_rating: int | None
    result: str  # win | loss | draw
    my_code: str
    opp_code: str
    opening: str
    family: str
    plies: int
    accuracy: float | None
    init_clock: float | None
    pgn: str

    @property
    def date(self):
        return datetime.fromtimestamp(self.end_time, timezone.utc).strftime("%Y-%m-%d")

    @property
    def moves(self):
        return (self.plies + 1) // 2

    @property
    def how(self):
        return f"opp {self.opp_code}" if self.result == "win" else self.my_code

    @property
    def loss_kind(self):
        return self.my_code if self.my_code in ("checkmated", "resigned", "timeout", "abandoned") else "other"


def opening_names(eco_url: str | None) -> tuple[str, str]:
    """(full name, family) from a chess.com ECOUrl slug."""
    if not eco_url:
        return "Unknown", "Unknown"
    words = []
    for tok in eco_url.rstrip("/").rsplit("/", 1)[-1].split("-"):
        if re.match(r"^\d+\.", tok):
            break
        words.append(tok)
    if not words:
        return "Unknown", "Unknown"
    fam = words[:3]
    for i, w in enumerate(words):
        if w in FAMILY_END:
            fam = words[: i + 1]
            break
    return " ".join(words), " ".join(fam)


def initial_clock(time_control: str | None) -> float | None:
    m = re.match(r"^(\d+)", time_control or "")
    return float(m[1]) if m and "/" not in (time_control or "") else None


def summarize(g: dict, username: str) -> GameInfo | None:
    """None if the game isn't standard chess or `username` isn't a player."""
    if g.get("rules", "chess") != "chess" or not g.get("pgn"):
        return None
    me = username.lower()
    if g["white"]["username"].lower() == me:
        color, mine, theirs = "white", g["white"], g["black"]
    elif g["black"]["username"].lower() == me:
        color, mine, theirs = "black", g["black"], g["white"]
    else:
        return None
    pg = chess.pgn.read_game(io.StringIO(g["pgn"]))
    if pg is None or pg.headers.get("Variant", "Chess") not in ("Chess", "Standard"):
        return None
    plies = sum(1 for _ in pg.mainline_moves())
    name, fam = opening_names(pg.headers.get("ECOUrl"))
    code = mine["result"]
    result = "win" if code == "win" else "draw" if code in DRAW_CODES else "loss"
    acc = (g.get("accuracies") or {}).get(color)
    return GameInfo(
        url=g["url"], end_time=g.get("end_time", 0), time_class=g.get("time_class", "?"),
        color=color, opp=theirs["username"], my_rating=mine.get("rating"),
        opp_rating=theirs.get("rating"), result=result, my_code=code, opp_code=theirs["result"],
        opening=name, family=fam, plies=plies, accuracy=acc,
        init_clock=initial_clock(pg.headers.get("TimeControl")), pgn=g["pgn"],
    )
