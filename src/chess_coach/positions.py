"""Position-level tools: FEN/move parsing, ASCII boards, en-prise detection, eval/explore logic."""
import re
import time

import chess

from .tactics import en_prise, en_prise_after  # noqa: F401  (re-exported)
from .analysis import (MATE, VALUES, WIDE, classify, fmt_eval, line_gain, win_pct)

MAX_SECONDS = 30  # hard clamp: the bridge to the caller times out at 60 s
MAX_PLIES = 20
UCI_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")


def clamp_seconds(s) -> float:
    return max(1.0, min(float(s), MAX_SECONDS))


def ascii_board(board: chess.Board) -> str:
    rows = []
    for rank in range(7, -1, -1):
        cells = [(board.piece_at(chess.square(f, rank)) or None) for f in range(8)]
        rows.append(f"{rank + 1} " + " ".join(p.symbol() if p else "." for p in cells))
    rows.append("  a b c d e f g h")
    return "\n".join(rows)


def parse_fen(fen: str):
    """(board, None) or (None, error message)."""
    fen = (fen or "").strip()
    try:
        board = chess.Board(fen)
    except ValueError as e:
        return None, f"Illegal FEN: {e}"
    st = board.status()
    if st != chess.STATUS_VALID:
        reasons = ", ".join(f.name.lower().replace("_", " ") for f in chess.Status if f in st and f != chess.STATUS_VALID)
        return None, f"Illegal position: {reasons}"
    return board, None


def clean_token(text: str) -> str:
    t = re.sub(r"^\d+\.+\s*", "", text.strip())  # "23...Ng4" / "24.fxg4"
    return re.sub(r"[!?]+$", "", t)


def parse_move(board: chess.Board, text: str):
    """(move, None) or (None, error). Accepts SAN or UCI."""
    t = clean_token(text)
    try:
        if UCI_RE.match(t):
            m = chess.Move.from_uci(t)
            if m in board.legal_moves:
                return m, None
            # a SAN like "e4e5" never exists, but "Nb1c3" style isn't matched here: fall through
        return board.parse_san(t), None
    except ValueError as e:
        kind = "ambiguous" if "ambiguous" in str(e) else "illegal"
        legal = " ".join(board.san(m) for m in board.legal_moves)
        return None, f"{kind.capitalize()} move '{text}' ({'white' if board.turn else 'black'} to move). Legal: {legal}"


def eval_both(cp: int, turn: bool) -> str:
    """'+3.2 (Black to move) | -3.2 (White)' style."""
    mover, other = ("White", "Black") if turn else ("Black", "White")
    w = cp if turn else -cp
    return f"{fmt_eval(cp)} ({mover} to move) | {fmt_eval(-cp)} ({other}) | White view {fmt_eval(w)}"


def _score_cp(info, pov):
    return info["score"].pov(pov).score(mate_score=MATE)


def terminal_text(board: chess.Board) -> str | None:
    if board.is_checkmate():
        return f"Checkmate: {'Black' if board.turn else 'White'} wins."
    if board.is_game_over():
        return "Game over: draw (stalemate/insufficient material/repetition rule)."
    return None


def eval_position(engine, fen, depth=18, multipv=3, max_seconds=15) -> str:
    board, err = parse_fen(fen)
    if err:
        return err
    if t := terminal_text(board):
        return f"{t}\n{ascii_board(board)}"
    depth, multipv = max(1, min(int(depth), 40)), max(1, min(int(multipv), 5))
    infos = engine.analyse(board, depth, clamp_seconds(max_seconds), multipv=multipv)
    infos = infos if isinstance(infos, list) else [infos]
    cp = _score_cp(infos[0], board.turn)
    lines = [f"Eval: {eval_both(cp, board.turn)} | depth {infos[0].get('depth', '?')} | win% {win_pct(cp):.0f} for mover",
             f"FEN: {board.fen()}"]
    for i, info in enumerate(infos, 1):
        pv = info.get("pv", [])[:8]
        lines.append(f"{i}) {fmt_eval(_score_cp(info, board.turn))} {board.variation_san(pv) if pv else '-'}")
    lines.append(ascii_board(board))
    return "\n".join(lines)


def judge_move(engine, board, mv, depth, seconds):
    """Evaluate `mv` in `board` from the mover's side. Returns a dict (also used by the puzzle checker)."""
    me = board.turn
    ev = engine.evaluator(depth, seconds / 2, pv_len=8)
    before = ev(board, me)
    after_board = board.copy(stack=False)
    after_board.push(mv)
    after = ev(after_board, me)
    best_uci = before.pv[0] if before.pv else None
    played_best = mv.uci() == best_uci
    clamp = lambda c: max(-WIDE, min(WIDE, c))  # noqa: E731
    loss = 0 if played_best else max(0, clamp(before.cp) - clamp(after.cp))
    drop = 0.0 if played_best else max(0.0, win_pct(before.cp) - win_pct(after.cp))
    cls = classify(drop) or ("best" if played_best or loss <= 5 else "good")
    return {"me": me, "before": before, "after": after, "after_board": after_board, "best_uci": best_uci,
            "played_best": played_best, "loss": loss, "drop": drop, "cls": cls}


def render_move(board, mv, j) -> str:
    me, before, after, cls, loss, drop = j["me"], j["before"], j["after"], j["cls"], j["loss"], j["drop"]
    after_board = j["after_board"]
    label = f"{board.fullmove_number}{'.' if me else '...'}{board.san(mv)}"
    note = (f" (win% barely moves because you are far ahead, but it costs {loss}cp)"
            if cls in ("good", "inaccuracy") and loss >= 150 else "")
    out = [f"{label} ({'White' if me else 'Black'}): eval {fmt_eval(before.cp)} → {fmt_eval(after.cp)} (mover view) | "
           f"loss {loss}cp | win% {win_pct(before.cp):.0f}→{win_pct(after.cp):.0f} (-{drop:.0f}) | {cls.upper()}{note}"]
    if j["best_uci"] and not j["played_best"]:
        out.append(f"Best: {board.variation_san([chess.Move.from_uci(u) for u in before.pv])} ({fmt_eval(before.cp)})")
    elif j["played_best"]:
        out.append("Played the engine's top move.")
    if after.pv:
        out.append(f"Then: {after_board.variation_san([chess.Move.from_uci(u) for u in after.pv])}")
    hang = en_prise_after(board, mv)
    cap = board.piece_at(mv.to_square)
    gain = VALUES.get(cap.piece_type, 0) if cap else 0
    out.append(f"Material: move {'captures ' + chess.piece_symbol(cap.piece_type).upper() + f' (+{gain})' if cap else 'no capture'}; "
               f"over engine line (4 plies) {line_gain(board, [mv.uci()] + after.pv[:3], me):+d}")
    if hang:
        out.append("EN PRISE after this move" + (" (even though the position may stay winning)" if cls in ("best", "good") or after.cp > 300 else "") + ":")
        out += [f"  {h['full']}" for h in hang[:3]]
    return "\n".join(out)


def evaluate_move(engine, fen, move, depth=18, max_seconds=15) -> str:
    board, err = parse_fen(fen)
    if err:
        return err
    mv, err = parse_move(board, move)
    if err:
        return err
    j = judge_move(engine, board, mv, max(1, min(int(depth), 40)), clamp_seconds(max_seconds))
    return render_move(board, mv, j)


def explore_line(engine, fen, moves, depth=16, max_seconds=20) -> str:
    board, err = parse_fen(fen)
    if err:
        return err
    if isinstance(moves, str):
        moves = [t for t in re.split(r"[\s,]+", moves) if t and not re.fullmatch(r"\d+\.+", t)]
    if not moves:
        return "No moves given."
    if len(moves) > MAX_PLIES:
        return f"Too many moves ({len(moves)}); max {MAX_PLIES} plies."
    me, boards, labels = board.turn, [board.copy(stack=False)], []
    cur = board.copy(stack=False)
    for i, text in enumerate(moves, 1):  # validate everything first: cheap, and no engine time wasted
        mv, err = parse_move(cur, text)
        if err:
            done = " ".join(labels) or "(none)"
            return f"Stopped at ply {i}: {err}\nPlayed OK before that: {done}\nFEN at that point: {cur.fen()}"
        labels.append(f"{cur.fullmove_number}{'.' if cur.turn else '...'}{cur.san(mv)}")
        cur.push(mv)
        boards.append(cur.copy(stack=False))
    depth, budget = max(1, min(int(depth), 40)), clamp_seconds(max_seconds)
    t0 = time.time()
    per = max(0.2, min(2.0, budget * 0.6 / len(boards)))
    ev = engine.evaluator(depth, per, pv_len=1)
    rows = [f"Start ({'White' if me else 'Black'} to move): {fmt_eval(ev(boards[0], me).cp)}"]
    for i in range(1, len(boards) - 1):
        rows.append(f"{labels[i - 1]}: {fmt_eval(ev(boards[i], me).cp)}")
    final = boards[-1]
    if t := terminal_text(final):
        rows.append(f"{labels[-1]}: {t}")
        best = ""
    else:
        left = max(1.0, budget - (time.time() - t0))
        info = engine.analyse(final, depth, left)
        rows.append(f"{labels[-1]}: {fmt_eval(_score_cp(info, me))}")
        pv = info.get("pv", [])[:8]
        best = f"Best continuation ({'White' if final.turn else 'Black'} to move): {final.variation_san(pv) if pv else '-'}"
    head = f"Evals from {'White' if me else 'Black'}'s view (the side that moves first)"
    out = [head] + rows + [f"Final FEN: {final.fen()}", ascii_board(final)]
    if best:
        out.append(best)
    return "\n".join(out)
